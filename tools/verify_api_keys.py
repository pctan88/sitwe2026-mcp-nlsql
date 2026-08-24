"""Verify API key presence and (optionally) validity via lightweight requests."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOTENV = ROOT / ".env"

_SSL_CONTEXT: ssl.SSLContext | None = None


def _build_ssl_context(insecure: bool) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def _request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: bytes | None = None,
    timeout_s: int = 20,
) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    with urllib.request.urlopen(req, timeout=timeout_s, context=_SSL_CONTEXT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body


def _summarize_error(body: str) -> str:
    body = body.strip()
    if not body:
        return "empty response"
    try:
        payload = json.loads(body)
        for key in ("error", "message"):
            if key in payload:
                return str(payload[key])
        return "json response"
    except Exception:
        return body[:160]


def _check_anthropic(live: bool) -> tuple[bool, str]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return False, "missing ANTHROPIC_API_KEY"
    if not live:
        return True, "present"
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    try:
        status, body = _request("https://api.anthropic.com/v1/models", headers=headers)
        return (status == 200), f"status {status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}: {_summarize_error(exc.read().decode())}"
    except Exception as exc:
        return False, f"error: {exc}"


def _check_openai(live: bool) -> tuple[bool, str]:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return False, "missing OPENAI_API_KEY"
    if not live:
        return True, "present"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        status, _ = _request("https://api.openai.com/v1/models", headers=headers)
        return (status == 200), f"status {status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}: {_summarize_error(exc.read().decode())}"
    except Exception as exc:
        return False, f"error: {exc}"


def _check_xai(live: bool) -> tuple[bool, str]:
    key = os.getenv("XAI_API_KEY", "")
    if not key:
        return False, "missing XAI_API_KEY"
    if not live:
        return True, "present"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        status, _ = _request("https://api.x.ai/v1/models", headers=headers)
        return (status == 200), f"status {status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}: {_summarize_error(exc.read().decode())}"
    except Exception as exc:
        return False, f"error: {exc}"


def _check_gemini(live: bool) -> tuple[bool, str]:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    if not key:
        return False, "missing GEMINI_API_KEY/GOOGLE_API_KEY"
    if not live:
        return True, "present"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    try:
        status, _ = _request(url)
        return (status == 200), f"status {status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}: {_summarize_error(exc.read().decode())}"
    except Exception as exc:
        return False, f"error: {exc}"


def _check_napkin(live: bool) -> tuple[bool, str]:
    key = os.getenv("NAPKIN_API_KEY", "")
    url = os.getenv("NAPKIN_API_URL", "")
    if not key:
        return False, "missing NAPKIN_API_KEY"
    if not url:
        return False, "missing NAPKIN_API_URL"
    if not live:
        return True, "present"

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = json.dumps({"prompt": "ping", "dry_run": True}).encode()
    try:
        status, body = _request(url, headers=headers, method="POST", data=payload)
        if status in {200, 201, 202}:
            return True, f"status {status}"
        return False, f"status {status}: {_summarize_error(body)}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}: {_summarize_error(exc.read().decode())}"
    except Exception as exc:
        return False, f"error: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify API keys.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Perform live API checks instead of env-only presence checks.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (last resort for local cert issues).",
    )
    args = parser.parse_args()

    _load_env(DOTENV)

    global _SSL_CONTEXT
    _SSL_CONTEXT = _build_ssl_context(args.insecure)

    checks = {
        "anthropic": _check_anthropic,
        "openai": _check_openai,
        "xai": _check_xai,
        "gemini": _check_gemini,
        "napkin": _check_napkin,
    }

    failed = 0
    for name, fn in checks.items():
        ok, detail = fn(args.live)
        status = "ok" if ok else "fail"
        print(f"{name}: {status} ({detail})")
        failed += int(not ok)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
