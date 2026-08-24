"""LLM client wrapper for the pilot.

Calls Claude Haiku via the official ``anthropic`` SDK when ``ANTHROPIC_API_KEY``
is set; falls back to a deterministic mock that mimics realistic stale-schema
behaviour (an LLM that knows only the old schema will keep referencing the
renamed table) when no key is available.

Why a mock? The pilot must be reproducible and run end-to-end without an API
key during artefact verification. The mock is deliberately *pessimistic* on
the baseline configuration — it always uses the table/column names from the
schema text it sees — so the recovery-rate numbers it produces are a
conservative lower bound on what the live API would deliver.
"""

from __future__ import annotations

import os
import json
import re
import time
import urllib.request
import ssl
from dataclasses import dataclass
from typing import Optional, Protocol

# Default Haiku model ID for the pilot. Confirmed in the SITWE 2026 cost plan.
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Gemini model used when GOOGLE_API_KEY (or GEMINI_API_KEY) is set
# (testing / cost alternative — exploration only, not part of the locked
# SITWE methodology). Selected per session 2026-05-14 user decision.
GEMINI_MODEL = "gemini-2.5-flash"

GPT4O_MINI_MODEL = "gpt-4o-mini"
GPT4O_MODEL = "gpt-4o"
LLAMA31_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
# Grok (xAI) removed 2026-07-03 — excluded from all current and future runs
# (never part of the SITWE paper; May 2026 attempts hit 429 and were skipped).

# Local models served through an OpenAI-compatible endpoint (Ollama /
# LM Studio / vLLM on the M5 Max). Tags are env-overridable because local
# registries name quantised builds differently across installs.
QWEN_CODER_MODEL = os.getenv("QWEN_CODER_MODEL", "qwen3-coder:30b")   # Qwen3 Coder 30B-A3B, Q4/Q5
QWEN_MODEL       = os.getenv("QWEN_MODEL", "qwen3.6:27b")              # Qwen 3.6 27B, Q4
QWEN_SMALL_MODEL = os.getenv("QWEN_SMALL_MODEL", "qwen3:8b")           # fast-iteration subject

_LOCAL_MODELS = {QWEN_CODER_MODEL, QWEN_MODEL, QWEN_SMALL_MODEL}

# Qwen3 / Qwen 3.6 are hybrid thinking models: served locally at T=0 their
# reasoning phase can consume the entire max_tokens budget before any SQL is
# emitted (observed 2026-07-11: 78% empty completions on qwen3.6:27b, 28% on
# qwen3:8b at max_tokens=512). Qwen's documented soft switch "/no_think" in
# the prompt disables the thinking phase. Applied only to local thinking
# models — qwen3-coder is a non-thinking instruct model and is unaffected.
_LOCAL_THINKING_MODELS = {QWEN_MODEL, QWEN_SMALL_MODEL}
_NO_THINK_TAG = "/no_think"


# --------------------------------------------------------------------------- #
# Prompts                                                                     #
# --------------------------------------------------------------------------- #

SQL_SYSTEM_PROMPT = (
    "You are an NL-to-SQL assistant. Given a database schema and a natural-"
    "language question, output a single SQLite SQL query that answers the "
    "question. Output ONLY the SQL with no commentary or markdown."
)

RELINK_SYSTEM_PROMPT = (
    "You are an SQL repair assistant. You are given (a) a SQL query written "
    "against an older schema, (b) a diff describing how the schema has "
    "evolved, and (c) the current schema. Rewrite the query so it executes "
    "correctly against the current schema. Output ONLY the rewritten SQL."
)

ERROR_FEEDBACK_SYSTEM_PROMPT = (
    "You are an NL-to-SQL repair assistant. You are given the current database "
    "schema, the original natural-language question, a SQL query that failed "
    "to execute, and the exact execution error. Produce exactly one corrected "
    "SQLite SQL query. Output ONLY the SQL with no commentary or markdown."
)


def build_sql_prompt(schema_text: str, question: str) -> str:
    return f"-- Schema --\n{schema_text}\n\n-- Question --\n{question}\n\n-- SQL --\n"


def build_relink_prompt(
    stale_sql: str,
    diff_text: str,
    schema_text: str,
    question: str = "",
    guidance: str = "",
) -> str:
    """Relink prompt. ``question`` and ``guidance`` are optional enrichments
    added for the COLUMN_MERGE / TABLE_SPLIT fallback paths (2026-07): the
    original NL question disambiguates intent, and the per-operator guidance
    (from ``mcp_server.relink.build_llm_guidance``) spells out the mechanical
    rewrite rules the raw diff JSON leaves implicit. Both default to empty,
    preserving the original prompt byte-for-byte for existing callers."""
    parts = [f"-- Stale SQL --\n{stale_sql}\n\n"]
    if question:
        parts.append(f"-- Original question --\n{question}\n\n")
    parts.append(f"-- Schema diff --\n{diff_text}\n\n")
    if guidance:
        parts.append(f"-- Rewrite rules --\n{guidance}\n\n")
    parts.append(f"-- Current schema --\n{schema_text}\n\n")
    parts.append("-- Rewritten SQL --\n")
    return "".join(parts)


def build_error_feedback_prompt(
    schema_text: str,
    question: str,
    failed_sql: str,
    execution_error: str,
) -> str:
    return (
        f"-- Current schema --\n{schema_text}\n\n"
        f"-- Question --\n{question}\n\n"
        f"-- Failed SQL --\n{failed_sql}\n\n"
        f"-- Execution error --\n{execution_error}\n\n"
        f"-- Corrected SQL --\n"
    )


# --------------------------------------------------------------------------- #
# Result type                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class LLMResponse:
    text: str
    backend: str         # backend identifier, e.g. "anthropic" or "mock"
    model: str           # full model id
    latency_s: float
    input_tokens: Optional[int]
    output_tokens: Optional[int]


# --------------------------------------------------------------------------- #
# Backends                                                                    #
# --------------------------------------------------------------------------- #

class LLMBackend(Protocol):
    """Backend interface shared by all model providers."""

    name: str
    backend_id: str
    model_id: str

    def complete(self, system: str, user: str) -> LLMResponse:
        """Return a completion for the given system/user prompt pair."""
        ...


@dataclass(frozen=True)
class ModelSpec:
    name: str
    backend: str
    model_id: str


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "haiku": ModelSpec("haiku", "anthropic", HAIKU_MODEL),
    "gemini": ModelSpec("gemini", "gemini", GEMINI_MODEL),
    "gpt4o-mini": ModelSpec("gpt4o-mini", "openai_compat", GPT4O_MINI_MODEL),
    "gpt-4o": ModelSpec("gpt-4o", "openai_compat", GPT4O_MODEL),
    "llama31": ModelSpec("llama31", "openai_compat", LLAMA31_MODEL),
    "qwen-coder": ModelSpec("qwen-coder", "openai_compat", QWEN_CODER_MODEL),
    "qwen": ModelSpec("qwen", "openai_compat", QWEN_MODEL),
    "qwen-small": ModelSpec("qwen-small", "openai_compat", QWEN_SMALL_MODEL),
    "mock": ModelSpec("mock", "mock", "mock"),
}

def _strip_fences(text: str) -> str:
    text = text.strip()
    # Qwen3-style thinking models can prefix the answer with a (possibly
    # empty) <think>...</think> block even when /no_think is applied.
    text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"^```sql\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip().rstrip(";").strip()


def _transient_gemini_errors() -> tuple:
    try:
        from google.genai import errors  # type: ignore
        import httpx  # type: ignore
    except Exception:
        return ()
    candidates = [
        getattr(errors, "ServerError", None),
        getattr(errors, "ClientError", None),
        getattr(httpx, "ConnectError", None),
        getattr(httpx, "ReadTimeout", None),
        getattr(httpx, "ConnectTimeout", None),
        getattr(httpx, "RemoteProtocolError", None),
    ]
    return tuple(cls for cls in candidates if cls is not None)


def _is_transient_gemini_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {429, 500, 502, 503, 504}:
        return True
    text = str(exc).lower()
    transient_markers = (
        "resource_exhausted",
        "unavailable",
        "deadline_exceeded",
        "rate limit",
        "high demand",
        "temporarily",
        "timeout",
        "timed out",
        "connection",
    )
    return any(marker in text for marker in transient_markers)


def _call_gemini(
    system: str,
    user: str,
    model: str = GEMINI_MODEL,
    *,
    max_retries: int = 5,
    base_delay_s: float = 1.0,
) -> LLMResponse:
    from google import genai
    from google.genai import types
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    transient = _transient_gemini_errors()

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            t0 = time.perf_counter()
            response = client.models.generate_content(
                model=model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.0,
                    max_output_tokens=512,
                ),
            )
            dt = time.perf_counter() - t0
            text = response.text or ""
            usage = response.usage_metadata
            return LLMResponse(
                text=_strip_fences(text),
                backend="gemini",
                model=model,
                latency_s=dt,
                input_tokens=usage.prompt_token_count if usage else None,
                output_tokens=usage.candidates_token_count if usage else None,
            )
        except transient as exc:  # type: ignore[misc]
            if not _is_transient_gemini_error(exc):
                raise
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = base_delay_s * (2 ** attempt)
            print(
                f"  [retry] gemini {type(exc).__name__} on attempt "
                f"{attempt + 1}/{max_retries}, sleeping {delay:.1f}s..."
            )
            time.sleep(delay)
        except Exception:
            raise
    assert last_exc is not None
    raise last_exc


# Transient API errors that should be retried with exponential backoff.
# Names are resolved lazily so the module imports without ``anthropic`` present.
def _transient_anthropic_errors() -> tuple:
    try:
        import anthropic  # type: ignore
    except Exception:
        return ()
    candidates = (
        "OverloadedError",
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "APIStatusError",
    )
    out = []
    for name in candidates:
        cls = getattr(anthropic, name, None)
        if cls is not None:
            out.append(cls)
    return tuple(out)


def _call_anthropic(
    system: str,
    user: str,
    model: str,
    *,
    max_retries: int = 5,
    base_delay_s: float = 1.0,
) -> LLMResponse:
    """Call Claude with exponential backoff on transient errors.

    Retries on OverloadedError / RateLimitError / connection failures with
    delays of base_delay * 2**i. Raises the underlying exception if all
    retries are exhausted — callers decide whether to surface or absorb it.
    """
    import anthropic  # type: ignore
    client = anthropic.Anthropic()
    transient = _transient_anthropic_errors()

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            t0 = time.perf_counter()
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                temperature=0.0,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            dt = time.perf_counter() - t0
            text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
            return LLMResponse(
                text=_strip_fences(text),
                backend="anthropic",
                model=model,
                latency_s=dt,
                input_tokens=msg.usage.input_tokens if msg.usage else None,
                output_tokens=msg.usage.output_tokens if msg.usage else None,
            )
        except transient as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = base_delay_s * (2 ** attempt)
            print(
                f"  [retry] anthropic {type(exc).__name__} on attempt "
                f"{attempt + 1}/{max_retries}, sleeping {delay:.1f}s..."
            )
            time.sleep(delay)
        except Exception as exc:
            # Non-transient — propagate immediately so callers don't silently
            # mask malformed prompts, auth failures, etc.
            raise
    assert last_exc is not None
    raise last_exc


def _openai_compat_base_url(model: str) -> str:
    if model in {GPT4O_MINI_MODEL, GPT4O_MODEL}:
        return os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if model in _LOCAL_MODELS:
        # Local inference on the M5 Max. Ollama's OpenAI-compatible endpoint
        # is the default; LM Studio (localhost:1234/v1) or vLLM users set
        # the env var instead.
        return (
            os.getenv("OLLAMA_BASE_URL")
            or os.getenv("LMSTUDIO_BASE_URL")
            or os.getenv("OPENAI_COMPAT_BASE_URL")
            or "http://localhost:11434/v1"
        )
    return (
        os.getenv("OPENAI_COMPAT_BASE_URL")
        or os.getenv("VLLM_BASE_URL")
        or os.getenv("OLLAMA_BASE_URL")
        or "http://localhost:8000/v1"
    )


def _openai_compat_api_key(model: str) -> str:
    if model in {GPT4O_MINI_MODEL, GPT4O_MODEL}:
        return os.getenv("OPENAI_API_KEY", "")
    if model in _LOCAL_MODELS:
        # Local servers need no auth; an env override is honoured for
        # vLLM setups that enforce a token.
        return os.getenv("OPENAI_COMPAT_API_KEY", "")
    return (
        os.getenv("OPENAI_COMPAT_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or "EMPTY"
    )


# Transient HTTP statuses worth retrying on OpenAI-compatible endpoints.
_OPENAI_TRANSIENT_HTTP = {429, 500, 502, 503, 504}


def _call_openai_compat(
    system: str,
    user: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    max_retries: int = 5,
    base_delay_s: float = 2.0,
) -> LLMResponse:
    """Call an OpenAI-compatible chat-completions endpoint.

    Retries 429/5xx and connection/timeout failures with exponential
    backoff, mirroring ``_call_anthropic`` — a single rate-limit blip must
    not kill a multi-hour strict-mode sweep (observed 2026-08-08: gpt-4o
    seeds run aborted on one unretried 429).
    """
    endpoint = f"{(base_url or _openai_compat_base_url(model)).rstrip('/')}/chat/completions"
    if model in _LOCAL_THINKING_MODELS and _NO_THINK_TAG not in system:
        system = f"{system} {_NO_THINK_TAG}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Pilot-Harness/1.0",
    }
    key = api_key if api_key is not None else _openai_compat_api_key(model)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    # Local servers need headroom for cold model loads (a 17 GB dense model
    # can take >120 s to page in after a model swap); cloud endpoints keep
    # the tighter budget.
    timeout_s = (
        float(os.getenv("LOCAL_HTTP_TIMEOUT_S", "300"))
        if model in _LOCAL_MODELS else 120
    )
    t0 = time.perf_counter()
    last_exc: Exception | None = None
    body = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(
                req, timeout=timeout_s, context=_ssl_context()
            ) as resp:
                body = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in _OPENAI_TRANSIENT_HTTP:
                raise
            last_exc = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
        if attempt == max_retries - 1:
            break
        delay = base_delay_s * (2 ** attempt)
        # Sustained TPM rate limits need far more headroom than transient
        # 5xx blips (observed 2026-08-08: a 429 outlasted a 30s backoff
        # budget and killed a strict-mode sweep).
        if isinstance(last_exc, urllib.error.HTTPError) and last_exc.code == 429:
            delay = max(delay, 30.0 * (2 ** attempt))
        print(
            f"  [retry] openai_compat {type(last_exc).__name__} on attempt "
            f"{attempt + 1}/{max_retries}, sleeping {delay:.1f}s..."
        )
        time.sleep(delay)
        # Request objects are single-use once sent; rebuild for the retry.
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
    if body is None:
        assert last_exc is not None
        raise last_exc
    dt = time.perf_counter() - t0
    text = body["choices"][0]["message"]["content"]
    usage = body.get("usage") or {}
    return LLMResponse(
        text=_strip_fences(text),
        backend="openai_compat",
        model=model,
        latency_s=dt,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
    )


@dataclass
class AnthropicBackend:
    model_id: str = HAIKU_MODEL
    name: str = "haiku"
    backend_id: str = "anthropic"

    def complete(self, system: str, user: str) -> LLMResponse:
        return _call_anthropic(system, user, self.model_id)


@dataclass
class GeminiBackend:
    model_id: str = GEMINI_MODEL
    name: str = "gemini"
    backend_id: str = "gemini"

    def complete(self, system: str, user: str) -> LLMResponse:
        return _call_gemini(system, user, self.model_id)


@dataclass
class OpenAICompatBackend:
    model_id: str
    name: str
    backend_id: str = "openai_compat"
    base_url: str | None = None
    api_key: str | None = None

    def complete(self, system: str, user: str) -> LLMResponse:
        return _call_openai_compat(
            system,
            user,
            self.model_id,
            base_url=self.base_url,
            api_key=self.api_key,
        )


@dataclass
class MockBackend:
    model_id: str = "mock"
    name: str = "mock"
    backend_id: str = "mock"

    def complete(self, system: str, user: str) -> LLMResponse:
        if system == RELINK_SYSTEM_PROMPT:
            return _mock_relink_response(system, user, self.model_id)
        if system == ERROR_FEEDBACK_SYSTEM_PROMPT:
            return _mock_error_feedback_response(system, user, self.model_id)
        return _mock_response(system, user, self.model_id)


# ---- Deterministic mock that emulates a "stale-schema-bound" LLM. -----------

def _extract_first_table(schema_text: str) -> dict[str, list[str]]:
    """Parse the 'TABLE x ( a TYPE, b TYPE )' format produced by fingerprint.to_prompt_block."""
    out: dict[str, list[str]] = {}
    for line in schema_text.splitlines():
        m = re.match(r"\s*TABLE\s+(\w+)\s*\(\s*(.*?)\s*\)\s*$", line)
        if not m:
            continue
        tbl = m.group(1)
        cols = [c.strip().split()[0] for c in m.group(2).split(",") if c.strip()]
        out[tbl] = cols
    return out


def _mock_response(system: str, user: str, model: str) -> LLMResponse:
    """Generate a plausible SQL answer using the schema text in the prompt.

    The mock is deliberately *literal*: it picks table/column names from the
    schema it's given. That means in the baseline configuration (stale schema
    text), the mock produces a query against the *renamed* table — emulating
    a real LLM that wasn't told about the schema change.
    """
    t0 = time.perf_counter()
    schema_match = re.search(r"-- Schema --(.*?)-- Question --", user, re.DOTALL)
    question_match = re.search(r"-- Question --(.*?)-- SQL --", user, re.DOTALL)
    schema_text = (schema_match.group(1) if schema_match else "").strip()
    question = (question_match.group(1) if question_match else user).strip().lower()
    tables = _extract_first_table(schema_text)
    answer = _mock_sql_for(question, tables)
    return LLMResponse(
        text=answer,
        backend="mock",
        model=("mock" if model == "mock" else f"{model}-mock"),
        latency_s=time.perf_counter() - t0 + 0.01,
        input_tokens=len(user) // 4,
        output_tokens=len(answer) // 4,
    )


def _mock_relink_response(system: str, user: str, model: str) -> LLMResponse:
    """Mock LLM-assisted relink: produce SQL by reading the current schema block."""
    t0 = time.perf_counter()
    schema_match = re.search(
        r"-- Current schema --(.*?)-- Rewritten SQL --", user, re.DOTALL
    )
    stale_match = re.search(r"-- Stale SQL --(.*?)-- Schema diff --", user, re.DOTALL)
    schema_text = (schema_match.group(1) if schema_match else "").strip()
    stale_sql = (stale_match.group(1) if stale_match else "").strip()
    tables = _extract_first_table(schema_text)
    # Heuristic: keep the stale SQL but swap any 'singer' tokens for 'artist'
    # when the schema diff mentions the rename. The deterministic AST path in
    # mcp_server.relink should already do this; we just preserve the stale SQL.
    return LLMResponse(
        text=stale_sql,
        backend="mock",
        model=("mock" if model == "mock" else f"{model}-mock-relink"),
        latency_s=time.perf_counter() - t0 + 0.01,
        input_tokens=len(user) // 4,
        output_tokens=len(stale_sql) // 4,
    )


def _replace_identifier(sql: str, old: str, new: str) -> str:
    """Replace an unquoted SQL identifier while preserving surrounding text."""
    return re.sub(rf"\b{re.escape(old)}\b", new, sql, flags=re.IGNORECASE)


def _schema_column_names(tables: dict[str, list[str]]) -> list[str]:
    seen: dict[str, str] = {}
    for cols in tables.values():
        for col in cols:
            seen.setdefault(col.lower(), col)
    return list(seen.values())


def _choose_replacement_table(
    missing: str,
    question: str,
    tables: dict[str, list[str]],
) -> str | None:
    if missing.lower() in {t.lower() for t in tables}:
        return None
    q = question.lower()
    if missing.lower() == "singer" and "artist" in tables:
        return "artist"
    if ("singer" in q or "song" in q or "artist" in q) and "artist" in tables:
        return "artist"
    unused = [t for t in tables if t.lower() not in q and t.lower() != missing.lower()]
    if len(tables) == 1:
        return next(iter(tables))
    if len(unused) == 1:
        return unused[0]
    return None


def _column_tokens(name: str) -> set[str]:
    parts = set(re.findall(r"[a-z0-9]+", name.lower()))
    expanded = set(parts)
    if "name" in parts:
        expanded.add("title")
    if "title" in parts:
        expanded.add("name")
    if "release" in parts:
        expanded.add("debut")
    if "debut" in parts:
        expanded.add("release")
    return expanded


def _choose_replacement_column(
    missing: str,
    tables: dict[str, list[str]],
) -> str | None:
    columns = _schema_column_names(tables)
    if missing.lower() in {c.lower() for c in columns}:
        return None

    known = {
        "song_name": "song_title",
        "song_release_year": "debut_year",
    }
    mapped = known.get(missing.lower())
    if mapped:
        for col in columns:
            if col.lower() == mapped:
                return col

    old_tokens = _column_tokens(missing)
    scored = [
        (len(old_tokens & _column_tokens(col)), col)
        for col in columns
    ]
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return None


def _mock_error_feedback_response(system: str, user: str, model: str) -> LLMResponse:
    """Repair a failed SQL query from a missing-table/column SQLite error."""
    t0 = time.perf_counter()
    schema_match = re.search(
        r"-- Current schema --(.*?)-- Question --", user, re.DOTALL
    )
    question_match = re.search(
        r"-- Question --(.*?)-- Failed SQL --", user, re.DOTALL
    )
    sql_match = re.search(
        r"-- Failed SQL --(.*?)-- Execution error --", user, re.DOTALL
    )
    error_match = re.search(
        r"-- Execution error --(.*?)-- Corrected SQL --", user, re.DOTALL
    )
    schema_text = (schema_match.group(1) if schema_match else "").strip()
    question = (question_match.group(1) if question_match else "").strip()
    repaired = (sql_match.group(1) if sql_match else "").strip()
    error = (error_match.group(1) if error_match else "").strip()
    tables = _extract_first_table(schema_text)

    table_error = re.search(r"no such table:\s*([A-Za-z_][\w]*)", error, re.I)
    if table_error:
        missing = table_error.group(1)
        replacement = _choose_replacement_table(missing, question, tables)
        if replacement:
            repaired = _replace_identifier(repaired, missing, replacement)

    column_error = re.search(
        r"no such column:\s*(?:[A-Za-z_][\w]*\.)?([A-Za-z_][\w]*)",
        error,
        re.I,
    )
    if column_error:
        missing = column_error.group(1)
        replacement = _choose_replacement_column(missing, tables)
        if replacement:
            repaired = _replace_identifier(repaired, missing, replacement)

    return LLMResponse(
        text=_strip_fences(repaired),
        backend="mock",
        model=("mock" if model == "mock" else f"{model}-mock-error-feedback"),
        latency_s=time.perf_counter() - t0 + 0.01,
        input_tokens=len(user) // 4,
        output_tokens=len(repaired) // 4,
    )


# Lightweight rule-based NL-to-SQL for the mock. Covers the 20 pilot questions
# by pattern-matching keywords to the schema's table/column names.
def _mock_sql_for(question: str, tables: dict[str, list[str]]) -> str:
    if not tables:
        # No schema given; return a permissive SELECT that will fail noisily
        # rather than crash the harness.
        return "SELECT 1"
    # Prefer the table the question seems to refer to.
    q = question.lower()
    name_hits: dict[str, int] = {}
    for tbl in tables:
        if tbl.lower() in q:
            name_hits[tbl] = name_hits.get(tbl, 0) + 1
        # Singer-like questions usually mention singer/song/artist.
    if "singer" in q or "song" in q or "artist" in q:
        for tbl in tables:
            if tbl.lower() in ("singer", "artist"):
                name_hits[tbl] = name_hits.get(tbl, 0) + 2
    if "stadium" in q or "capacity" in q:
        if "stadium" in tables:
            name_hits["stadium"] = name_hits.get("stadium", 0) + 2
    if "concert" in q:
        if "concert" in tables:
            name_hits["concert"] = name_hits.get("concert", 0) + 2
    target = (
        max(name_hits, key=lambda t: name_hits[t])
        if name_hits
        else next(iter(tables))
    )
    cols = tables.get(target, [])

    def pick(*candidates: str) -> str:
        for c in candidates:
            for col in cols:
                if col.lower() == c.lower():
                    return col
        return cols[0] if cols else "*"

    # Detect aggregate intent.
    is_count = any(k in q for k in ("how many", "count", "number of"))
    is_avg   = "average" in q
    is_max   = "oldest" in q or "largest" in q or "highest" in q or "max" in q
    is_min   = "youngest" in q or "smallest" in q or "lowest" in q or "min" in q

    if is_count:
        if "male" in q:
            return f"SELECT COUNT(*) FROM {target} WHERE Is_male = 'T'"
        if "songs released after 2010" in q or "after 2010" in q:
            yr = pick("Song_release_year", "debut_year")
            return f"SELECT COUNT(*) FROM {target} WHERE {yr} > 2010"
        return f"SELECT COUNT(*) FROM {target}"

    if is_avg and "song" in q:
        yr = pick("Song_release_year", "debut_year")
        return f"SELECT Country, AVG({yr}) FROM {target} GROUP BY Country"

    if "average" in q and "min" in q and "max" in q:
        return f"SELECT AVG(Age), MIN(Age), MAX(Age) FROM {target}"

    if is_max and "stadium" in q and "capacity" in q:
        return "SELECT Name FROM stadium ORDER BY Capacity DESC LIMIT 1"

    if "oldest" in q:
        return f"SELECT Name FROM {target} ORDER BY Age DESC LIMIT 1"
    if "youngest" in q and "song" in q:
        col = pick("Song_Name", "song_title")
        return f"SELECT {col} FROM {target} ORDER BY Age ASC LIMIT 1"
    if "youngest" in q:
        return f"SELECT Name FROM {target} ORDER BY Age ASC LIMIT 1"

    if "country and the number" in q or ("country" in q and "number" in q):
        return f"SELECT Country, COUNT(*) FROM {target} GROUP BY Country"

    if "ordered by age" in q or "oldest to youngest" in q:
        return f"SELECT Name FROM {target} ORDER BY Age DESC"

    if "song" in q and "ordered" in q and "release" in q:
        a = pick("Song_Name", "song_title")
        y = pick("Song_release_year", "debut_year")
        return f"SELECT {a} FROM {target} ORDER BY {y} ASC"

    if "from france" in q or "french" in q:
        if "song" in q and "year" in q:
            a = pick("Song_Name", "song_title")
            y = pick("Song_release_year", "debut_year")
            return f"SELECT {a}, {y} FROM {target} WHERE Country = 'France'"
        return f"SELECT Name FROM {target} WHERE Country = 'France'"

    if "name and country" in q:
        return f"SELECT Name, Country FROM {target}"

    if "name and their song" in q or ("name" in q and "song" in q and "and" in q):
        a = pick("Song_Name", "song_title")
        return f"SELECT Name, {a} FROM {target}"

    if "did not perform" in q:
        return (
            f"SELECT Name FROM {target} "
            f"WHERE Singer_ID NOT IN (SELECT Singer_ID FROM singer_in_concert)"
        )

    if "wide awake" in q:
        return (
            f"SELECT T2.Name FROM singer_in_concert AS T1 "
            f"JOIN {target} AS T2 ON T1.Singer_ID = T2.Singer_ID "
            f"JOIN concert AS T3 ON T1.Concert_ID = T3.Concert_ID "
            f"WHERE T3.Theme = 'Wide Awake'"
        )

    if "names of singers and the names of concerts" in q or "concerts they performed" in q:
        return (
            f"SELECT T2.Name, T3.concert_Name FROM singer_in_concert AS T1 "
            f"JOIN {target} AS T2 ON T1.Singer_ID = T2.Singer_ID "
            f"JOIN concert AS T3 ON T1.Concert_ID = T3.Concert_ID"
        )

    if "auditions" in q:
        a = pick("Song_Name", "song_title")
        return (
            f"SELECT T2.{a} FROM singer_in_concert AS T1 "
            f"JOIN {target} AS T2 ON T1.Singer_ID = T2.Singer_ID "
            f"JOIN concert AS T3 ON T1.Concert_ID = T3.Concert_ID "
            f"WHERE T3.concert_Name = 'Auditions'"
        )

    if "how many singers performed in each concert" in q or "singers performed in each concert" in q:
        return (
            "SELECT T2.concert_Name, COUNT(*) FROM singer_in_concert AS T1 "
            "JOIN concert AS T2 ON T1.Concert_ID = T2.Concert_ID "
            "GROUP BY T2.concert_Name"
        )

    # Default: SELECT * from the chosen table.
    return f"SELECT * FROM {target}"


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #

def resolve_model(
    model_name: str = "auto",
    *,
    force_mock: bool = False,
    force_backend: str = "",
) -> ModelSpec:
    """Resolve a short model name to a backend/model specification."""
    if force_mock:
        return MODEL_REGISTRY["mock"]

    legacy = (force_backend or "").strip().lower()
    if legacy and legacy != "auto":
        legacy_map = {
            "anthropic": "haiku",
            "haiku": "haiku",
            "gemini": "gemini",
            "openai": "gpt4o-mini",
            "openai_compat": "gpt4o-mini",
            "openai-compatible": "gpt4o-mini",
            "mock": "mock",
        }
        model_name = legacy_map.get(legacy, model_name)

    name = (model_name or "auto").strip()
    if name in MODEL_REGISTRY:
        return MODEL_REGISTRY[name]
    if name == "auto":
        if os.getenv("ANTHROPIC_API_KEY"):
            return MODEL_REGISTRY["haiku"]
        if os.getenv("OPENAI_API_KEY"):
            return MODEL_REGISTRY["gpt4o-mini"]
        if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
            return MODEL_REGISTRY["gemini"]
        return MODEL_REGISTRY["mock"]

    # Allow full model identifiers for compatibility with the old call sites.
    full_id_map = {
        HAIKU_MODEL: MODEL_REGISTRY["haiku"],
        GEMINI_MODEL: MODEL_REGISTRY["gemini"],
        GPT4O_MINI_MODEL: MODEL_REGISTRY["gpt4o-mini"],
        GPT4O_MODEL: MODEL_REGISTRY["gpt-4o"],
        LLAMA31_MODEL: MODEL_REGISTRY["llama31"],
        QWEN_CODER_MODEL: MODEL_REGISTRY["qwen-coder"],
        QWEN_MODEL: MODEL_REGISTRY["qwen"],
        QWEN_SMALL_MODEL: MODEL_REGISTRY["qwen-small"],
    }
    if name in full_id_map:
        return full_id_map[name]
    raise ValueError(
        f"Unknown model {model_name!r}; expected one of "
        f"{', '.join(['auto', *MODEL_REGISTRY])}."
    )


def resolve_backend(
    model_name: str = "auto",
    *,
    force_mock: bool = False,
    force_backend: str = "",
) -> LLMBackend:
    """Instantiate the backend selected by ``resolve_model``."""
    spec = resolve_model(
        model_name,
        force_mock=force_mock,
        force_backend=force_backend,
    )
    if spec.backend == "anthropic":
        return AnthropicBackend(model_id=spec.model_id, name=spec.name)
    if spec.backend == "gemini":
        return GeminiBackend(model_id=spec.model_id, name=spec.name)
    if spec.backend == "openai_compat":
        return OpenAICompatBackend(model_id=spec.model_id, name=spec.name)
    if spec.backend == "mock":
        return MockBackend(model_id=spec.model_id, name=spec.name)
    raise ValueError(f"Unsupported backend kind {spec.backend!r}")


# Per-run fallback counters. Reset by reset_fallback_stats() at harness start;
# inspected by run_pilot.py to decide whether to fail-fast under --strict mode.
FALLBACK_STATS: dict[str, int] = {
    "anthropic_fallback": 0,
    "gemini_fallback": 0,
    "openai_compat_fallback": 0,
}


def reset_fallback_stats() -> None:
    for k in FALLBACK_STATS:
        FALLBACK_STATS[k] = 0


def fallback_count() -> int:
    return sum(FALLBACK_STATS.values())


def _fallback_key(backend: LLMBackend) -> str | None:
    key = f"{backend.backend_id}_fallback"
    return key if key in FALLBACK_STATS else None


def _complete_with_fallback(
    backend: LLMBackend,
    system: str,
    user: str,
    *,
    strict: bool = False,
) -> LLMResponse:
    try:
        return backend.complete(system, user)
    except Exception as exc:
        if strict or backend.backend_id == "mock":
            raise
        key = _fallback_key(backend)
        if key is not None:
            FALLBACK_STATS[key] += 1
        mock = MockBackend(model_id=backend.model_id)
        fallback = mock.complete(system, user)
        return LLMResponse(
            text=fallback.text,
            backend=f"mock-after-{type(exc).__name__}",
            model=f"{backend.model_id}-mock",
            latency_s=0.0,
            input_tokens=None,
            output_tokens=None,
        )


def generate_sql(
    schema_text: str,
    question: str,
    *,
    model: str = "auto",
    force_mock: bool = False,
    force_backend: str = "",
    strict: bool = False,
) -> LLMResponse:
    """Generate SQL for ``question`` against ``schema_text``.

    When ``strict=True`` any API failure (after the internal retry budget is
    exhausted) is re-raised instead of being silently masked by the mock.
    Otherwise the mock fallback is invoked and ``FALLBACK_STATS`` is updated
    so the harness can detect and refuse contamination.
    """
    user = build_sql_prompt(schema_text, question)
    backend = resolve_backend(
        model,
        force_mock=force_mock,
        force_backend=force_backend,
    )
    return _complete_with_fallback(
        backend,
        SQL_SYSTEM_PROMPT,
        user,
        strict=strict,
    )


def generate_sql_with_error(
    schema_text: str,
    question: str,
    failed_sql: str,
    execution_error: str,
    *,
    model: str = "auto",
    force_mock: bool = False,
    force_backend: str = "",
    strict: bool = False,
) -> LLMResponse:
    """Generate one corrected SQL query after an execution failure."""
    user = build_error_feedback_prompt(
        schema_text, question, failed_sql, execution_error
    )
    backend = resolve_backend(
        model,
        force_mock=force_mock,
        force_backend=force_backend,
    )
    return _complete_with_fallback(
        backend,
        ERROR_FEEDBACK_SYSTEM_PROMPT,
        user,
        strict=strict,
    )


def relink_with_llm(
    stale_sql: str,
    diff_text: str,
    schema_text: str,
    *,
    question: str = "",
    guidance: str = "",
    model: str = "auto",
    force_mock: bool = False,
    force_backend: str = "",
    strict: bool = False,
) -> LLMResponse:
    user = build_relink_prompt(
        stale_sql, diff_text, schema_text,
        question=question, guidance=guidance,
    )
    backend = resolve_backend(
        model,
        force_mock=force_mock,
        force_backend=force_backend,
    )
    return _complete_with_fallback(
        backend,
        RELINK_SYSTEM_PROMPT,
        user,
        strict=strict,
    )

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()
