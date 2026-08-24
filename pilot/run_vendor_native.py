"""Vendor-native function-calling harness — SITWE 2026 reviewer revision.

Adds two experimental arms on top of the canonical pilot (reviewer comments
R1-2 / R2-2), evaluated per query against ``gold_post`` on the
post-perturbation DB exactly like ``pilot/run_pilot.py``:

  Sanity — stale-schema baseline re-run (1 call). Confirms agreement with the
           canonical stale baseline; the canonical refreshed ceiling is
           REUSED from ``results/summary_{...}.json`` and not re-run.
  Arm D  — "diff-in-prompt": stale schema text + classified diff events +
           question in a single call. No tools, no MCP — isolates
           *information* (the diff) from *protocol* (MCP).
  Arm F  — "vendor-native function calling": agentic tool loop over the
           SAME middleware primitives the MCP server uses (fingerprint /
           relink / validate), transported via Anthropic tool use (haiku)
           or OpenAI function calling (gpt-4o). Capped at --max-turns.

MCP-arm outcomes for McNemar come from the canonical per-query CSVs, paired
by query id. All outputs go under ``results/vendor_native/`` — canonical
files are never touched.

Usage::

    python -m pilot.run_vendor_native --model haiku \\
        --database concert_singer_TABLE_RENAME --validate embedding
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.database_config import DatabaseConfig
from mcp_server import fingerprint as fp
from mcp_server import relink as rl
from pilot import llm_client
from pilot import metrics
from pilot import vendor_tools as vt

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

# List prices as of Aug 2026, USD per million tokens.
# Haiku 4.5: https://docs.claude.com/en/docs/about-claude/pricing
# GPT-4o:    https://platform.openai.com/docs/pricing
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "haiku": {"input": 1.00, "output": 5.00},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "mock": {"input": 0.0, "output": 0.0},
}

SUPPORTED_MODELS = ("haiku", "gpt-4o")

# The 10 canonical db×operator configs of the pilot sweep.
SWEEP_DATABASES = [
    f"{db}_{op}"
    for db in ("concert_singer", "hr_1")
    for op in ("TABLE_RENAME", "TABLE_SPLIT", "TABLE_MERGE",
               "COLUMN_RENAME", "COLUMN_MERGE")
]

DIFF_IN_PROMPT_SYSTEM = (
    "You are an NL-to-SQL assistant. You are given a database schema, a log "
    "of schema-change events that were applied to the database AFTER that "
    "schema text was written, and a natural-language question. Output a "
    "single SQLite SQL query that answers the question and is valid for the "
    "CURRENT (post-change) schema. Output ONLY the SQL with no commentary "
    "or markdown."
)

VENDOR_LOOP_SYSTEM = (
    "You are an NL-to-SQL assistant. You are given a database schema and a "
    "natural-language question. The schema text you were given may be stale: "
    "the database schema may have changed since it was written. You have "
    "tools to check for schema changes (get_schema_diff), to mechanically "
    "rewrite a query for the current schema (relink_sql), and to execute and "
    "validate a query against the current database (validate_sql). Use them "
    "as needed to produce a correct SQLite SQL query for the CURRENT schema. "
    "Your final message must contain ONLY the final SQL with no commentary "
    "or markdown."
)

MAX_COMPLETION_TOKENS = 1024


def _load_env(path: Path) -> None:
    """Load KEY=VALUE lines from .env without overriding the environment."""
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


def _model_file_tag(model_short: str) -> str:
    return model_short.replace("-", "_")


def _diff_text(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def build_diff_in_prompt(pre_schema_text: str, diff_text: str, question: str) -> str:
    return (
        f"-- Schema --\n{pre_schema_text}\n\n"
        f"-- Schema change events (JSON lines, applied after the schema above) --\n"
        f"{diff_text}\n\n"
        f"-- Question --\n{question}\n\n-- SQL --\n"
    )


def build_vendor_user_prompt(pre_schema_text: str, question: str) -> str:
    return llm_client.build_sql_prompt(pre_schema_text, question)


# --------------------------------------------------------------------------- #
# Vendor transports (reuse llm_client retry/backoff + endpoint helpers)       #
# --------------------------------------------------------------------------- #

def _anthropic_tools_create(
    model_id: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_retries: int = 5,
    base_delay_s: float = 1.0,
) -> Any:
    """messages.create with tools, using the same backoff as _call_anthropic."""
    import anthropic  # type: ignore
    client = anthropic.Anthropic()
    transient = llm_client._transient_anthropic_errors()

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model=model_id,
                max_tokens=MAX_COMPLETION_TOKENS,
                temperature=0.0,
                system=system,
                messages=messages,
                tools=tools,
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
    assert last_exc is not None
    raise last_exc


_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def _openai_tools_chat(
    model_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_retries: int = 5,
    base_delay_s: float = 1.0,
) -> dict[str, Any]:
    """chat/completions with tools, reusing _call_openai_compat's helpers."""
    base_url = llm_client._openai_compat_base_url(model_id)
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model_id,
        "messages": messages,
        "tools": tools,
        "temperature": 0.0,
        "max_tokens": MAX_COMPLETION_TOKENS,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Pilot-Harness/1.0",
    }
    key = llm_client._openai_compat_api_key(model_id)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=120, context=llm_client._ssl_context()
            ) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code not in _TRANSIENT_HTTP_CODES:
                raise
            last_exc = exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last_exc = exc
        if attempt == max_retries - 1:
            break
        delay = base_delay_s * (2 ** attempt)
        print(
            f"  [retry] openai_compat {type(last_exc).__name__} on attempt "
            f"{attempt + 1}/{max_retries}, sleeping {delay:.1f}s..."
        )
        time.sleep(delay)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# Tool loops                                                                  #
# --------------------------------------------------------------------------- #

def _summarise_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Compact per-call log entry (never embed the full schema text)."""
    if tool_name == "get_schema_diff":
        return {"changed": result.get("changed"), "n_events": len(result.get("events", []))}
    return {k: v for k, v in result.items() if k in ("method", "verdict", "reason", "error")}


def run_tool_loop_anthropic(
    model_id: str,
    system: str,
    user: str,
    ctx: vt.ToolContext,
    *,
    max_turns: int = 8,
    call_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Anthropic tool-use loop: iterate while stop_reason == 'tool_use'."""
    call = call_fn or _anthropic_tools_create
    tools = vt.anthropic_tools()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    tool_calls: list[dict[str, Any]] = []
    tokens_in = 0
    tokens_out = 0
    n_api_calls = 0
    last_sql = ""
    final_sql = ""
    capped = True

    for _turn in range(max_turns):
        msg = call(model_id, system, messages, tools)
        n_api_calls += 1
        usage = getattr(msg, "usage", None)
        if usage is not None:
            tokens_in += int(getattr(usage, "input_tokens", 0) or 0)
            tokens_out += int(getattr(usage, "output_tokens", 0) or 0)

        if getattr(msg, "stop_reason", None) != "tool_use":
            text = "".join(
                getattr(b, "text", "") for b in msg.content
                if getattr(b, "type", "") == "text"
            )
            final_sql = llm_client._strip_fences(text)
            capped = False
            break

        assistant_content: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in msg.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                args = dict(block.input or {})
                assistant_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": args,
                })
                result = vt.dispatch(block.name, args, ctx)
                tool_calls.append({
                    "tool": block.name,
                    "args": args,
                    "result": _summarise_result(block.name, result),
                })
                if block.name == "relink_sql" and result.get("sql"):
                    last_sql = result["sql"]
                elif block.name == "validate_sql" and args.get("sql"):
                    last_sql = args["sql"]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})

    if capped:
        final_sql = last_sql

    return {
        "final_sql": final_sql,
        "tool_calls": tool_calls,
        "n_api_calls": n_api_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "capped": capped,
    }


def run_tool_loop_openai(
    model_id: str,
    system: str,
    user: str,
    ctx: vt.ToolContext,
    *,
    max_turns: int = 8,
    call_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """OpenAI function-calling loop: iterate while finish_reason == 'tool_calls'."""
    call = call_fn or _openai_tools_chat
    tools = vt.openai_tools()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tool_calls: list[dict[str, Any]] = []
    tokens_in = 0
    tokens_out = 0
    n_api_calls = 0
    last_sql = ""
    final_sql = ""
    capped = True

    for _turn in range(max_turns):
        body = call(model_id, messages, tools)
        n_api_calls += 1
        usage = body.get("usage") or {}
        tokens_in += int(usage.get("prompt_tokens") or 0)
        tokens_out += int(usage.get("completion_tokens") or 0)

        choice = body["choices"][0]
        message = choice.get("message") or {}
        requested = message.get("tool_calls") or []
        if choice.get("finish_reason") != "tool_calls" and not requested:
            final_sql = llm_client._strip_fences(message.get("content") or "")
            capped = False
            break

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": requested,
        })
        for tc in requested:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            result = vt.dispatch(name, args, ctx)
            tool_calls.append({
                "tool": name,
                "args": args,
                "result": _summarise_result(name, result),
            })
            if name == "relink_sql" and result.get("sql"):
                last_sql = result["sql"]
            elif name == "validate_sql" and args.get("sql"):
                last_sql = args["sql"]
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result),
            })

    if capped:
        final_sql = last_sql

    return {
        "final_sql": final_sql,
        "tool_calls": tool_calls,
        "n_api_calls": n_api_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "capped": capped,
    }


def run_tool_loop_mock(
    stale_sql: str,
    ctx: vt.ToolContext,
    *,
    max_turns: int = 8,
) -> dict[str, Any]:
    """Deterministic mock loop: diff -> relink(stale) -> validate -> final.

    Exercises the dispatcher and the row plumbing without any API. The
    'model' behaves like an ideal tool user: it checks the diff, relinks its
    stale candidate, validates, and returns the relinked SQL.
    """
    tool_calls: list[dict[str, Any]] = []

    diff_res = vt.dispatch("get_schema_diff", {}, ctx)
    tool_calls.append({
        "tool": "get_schema_diff", "args": {},
        "result": _summarise_result("get_schema_diff", diff_res),
    })
    relink_res = vt.dispatch("relink_sql", {"sql": stale_sql}, ctx)
    tool_calls.append({
        "tool": "relink_sql", "args": {"sql": stale_sql},
        "result": _summarise_result("relink_sql", relink_res),
    })
    final_sql = relink_res.get("sql") or stale_sql
    val_res = vt.dispatch("validate_sql", {"sql": final_sql}, ctx)
    tool_calls.append({
        "tool": "validate_sql", "args": {"sql": final_sql},
        "result": _summarise_result("validate_sql", val_res),
    })

    return {
        "final_sql": final_sql,
        "tool_calls": tool_calls,
        "n_api_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "capped": False,
    }


# --------------------------------------------------------------------------- #
# Canonical pairing                                                           #
# --------------------------------------------------------------------------- #

def canonical_paths(model_short: str, database_id: str) -> tuple[Path, Path]:
    """Canonical per-query CSV + summary JSON for (model, db config)."""
    results = ROOT / "results"
    if model_short == "haiku":
        return (
            results / f"pilot_results_{database_id}.csv",
            results / f"summary_{database_id}.json",
        )
    tag = _model_file_tag(model_short)
    return (
        results / f"pilot_results_{tag}_{database_id}.csv",
        results / f"summary_{tag}_{database_id}.json",
    )


def load_canonical(
    model_short: str, database_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return ({query_id: canonical_row}, canonical_summary)."""
    csv_path, summary_path = canonical_paths(model_short, database_id)
    if not csv_path.exists() or not summary_path.exists():
        raise FileNotFoundError(
            f"Canonical files missing for model={model_short} db={database_id}: "
            f"{csv_path.name} / {summary_path.name}"
        )
    rows: dict[str, dict[str, Any]] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            rows[row["id"]] = row
    with summary_path.open() as f:
        summary = json.load(f)
    return rows, summary


def _canonical_int(row: dict[str, Any], key: str) -> Optional[int]:
    val = str(row.get(key, "")).strip()
    if val in ("", "None"):
        return None
    return int(float(val))


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #

CSV_COLUMNS = [
    "id", "perturbation", "question", "expected_failure",
    "sanity_sql", "sanity_ok", "sanity_latency_s",
    "sanity_tokens_in", "sanity_tokens_out",
    "canonical_baseline_ok", "canonical_mcp_ok",
    "diff_sql", "diff_ok", "diff_latency_s",
    "diff_tokens_in", "diff_tokens_out",
    "vendor_sql", "vendor_ok", "vendor_latency_s",
    "vendor_tokens_in", "vendor_tokens_out",
    "vendor_n_api_calls", "vendor_n_tool_calls", "vendor_capped",
    "tool_calls_json", "backend",
]


def _load_queries(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "queries" in data:
        return data["queries"]
    raise ValueError(f"Unsupported queries format in {path}")


def _row_int(row: dict[str, Any], key: str) -> Optional[int]:
    val = row.get(key)
    if val is None or str(val).strip() in ("", "None"):
        return None
    return int(float(val))


def _row_float(row: dict[str, Any], key: str) -> Optional[float]:
    val = row.get(key)
    if val is None or str(val).strip() in ("", "None"):
        return None
    return float(val)


def _reload_existing_rows(csv_path: Path) -> dict[str, dict[str, Any]]:
    """Load a partial CSV from a prior (failed) run for --resume."""
    if not csv_path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            for key in ("sanity_ok", "canonical_baseline_ok", "canonical_mcp_ok",
                        "diff_ok", "vendor_ok", "sanity_tokens_in",
                        "sanity_tokens_out", "diff_tokens_in", "diff_tokens_out",
                        "vendor_tokens_in", "vendor_tokens_out",
                        "vendor_n_api_calls", "vendor_n_tool_calls",
                        "vendor_capped"):
                row[key] = _row_int(row, key)
            for key in ("sanity_latency_s", "diff_latency_s", "vendor_latency_s"):
                row[key] = _row_float(row, key)
            row["expected_failure"] = str(row.get("expected_failure")).lower() in (
                "true", "1")
            out[row["id"]] = row
    return out


def _write_outputs(
    out_dir: Path,
    stem: str,
    rows: list[dict[str, Any]],
    summary: Optional[dict[str, Any]],
    runlog: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"vendor_{stem}.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with (out_dir / f"runlog_vendor_{stem}.json").open("w") as f:
        json.dump(runlog, f, indent=2)
    if summary is not None:
        with (out_dir / f"summary_vendor_{stem}.json").open("w") as f:
            json.dump(summary, f, indent=2)


def _usd(tokens_in: int, tokens_out: int, model_short: str) -> float:
    prices = PRICES_USD_PER_MTOK[model_short]
    return tokens_in * prices["input"] / 1e6 + tokens_out * prices["output"] / 1e6


def run_config(
    database: str,
    out_dir: Path,
    *,
    model_name: str,
    validate_mode: str = "embedding",
    strict: bool = True,
    max_turns: int = 8,
    force_mock: bool = False,
    resume: bool = False,
    data_root: Optional[Path] = None,
    anthropic_call_fn: Optional[Callable[..., Any]] = None,
    openai_call_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Run sanity + Arm D + Arm F for one db×operator config."""
    llm_client.reset_fallback_stats()
    config = DatabaseConfig.load(database, data_root or (ROOT / "data"))
    queries = _load_queries(config.queries_path)

    spec = llm_client.resolve_model(model_name, force_mock=force_mock)
    model_short = "mock" if force_mock else model_name
    stem = f"{_model_file_tag(model_short)}_{config.database_id}"
    csv_out = out_dir / f"vendor_{stem}.csv"

    # Canonical pairing (haiku / gpt-4o only; mock runs skip it).
    canonical_rows: dict[str, dict[str, Any]] = {}
    canonical_summary: dict[str, Any] = {}
    if not force_mock:
        canonical_rows, canonical_summary = load_canonical(
            model_short, config.database_id
        )

    # Mirror DBs to a private temp dir (same FUSE workaround as run_pilot).
    work = Path(tempfile.mkdtemp(prefix="vendor_"))
    pre_db = str(work / config.pre_db.name)
    post_db = str(work / config.post_db.name)
    shutil.copyfile(str(config.pre_db), pre_db)
    shutil.copyfile(str(config.post_db), post_db)

    # Precompute the diff once per config, like run_pilot.py.
    pre_schema = fp.introspect(pre_db)
    post_schema = fp.introspect(post_db)
    diff = fp.classify(pre_schema, post_schema)
    pre_schema_text = fp.to_prompt_block(pre_schema)
    post_schema_text = fp.to_prompt_block(post_schema)
    diff_text = _diff_text(diff.events)

    ctx = vt.ToolContext(
        diff_changed=diff.changed,
        diff_events=diff.events,
        post_schema_text=post_schema_text,
        post_db=post_db,
        validate_mode=validate_mode,
        llm_guidance=rl.build_llm_guidance(diff.events),
    )

    backend = llm_client.resolve_backend(model_name, force_mock=force_mock)
    print(f"[vendor] model={spec.name} provider={spec.backend} id={spec.model_id}")
    print(f"[vendor] database={config.database_id} events={len(diff.events)} "
          f"validate={validate_mode} strict={strict} max_turns={max_turns}")

    existing = _reload_existing_rows(csv_out) if resume else {}
    if existing:
        print(f"[resume] {len(existing)} completed queries loaded from {csv_out.name}")

    rows: list[dict[str, Any]] = []
    failed_ids: list[str] = []

    for q in queries:
        qid = q["id"]
        if qid in existing:
            rows.append(existing[qid])
            continue
        question = q["question"]
        gold_post = q["gold_post"]
        expected_failure = bool(q.get("expected_failure", False))
        ctx.question = question

        try:
            # ---- (1) Stale baseline sanity re-run.
            t0 = time.perf_counter()
            sanity_resp = llm_client.generate_sql(
                pre_schema_text, question,
                model=model_name, force_mock=force_mock, strict=strict,
            )
            sanity_ok = None if expected_failure else int(metrics.exec_match(
                post_db, sanity_resp.text, gold_post))
            dt_sanity = time.perf_counter() - t0

            # ---- (2) Arm D: diff-in-prompt, single call.
            t0 = time.perf_counter()
            diff_resp = llm_client._complete_with_fallback(
                backend, DIFF_IN_PROMPT_SYSTEM,
                build_diff_in_prompt(pre_schema_text, diff_text, question),
                strict=strict,
            )
            diff_ok = None if expected_failure else int(metrics.exec_match(
                post_db, diff_resp.text, gold_post))
            dt_diff = time.perf_counter() - t0

            # ---- (3) Arm F: vendor-native tool loop.
            user_prompt = build_vendor_user_prompt(pre_schema_text, question)
            t0 = time.perf_counter()
            if force_mock:
                loop = run_tool_loop_mock(sanity_resp.text, ctx, max_turns=max_turns)
            elif spec.backend == "anthropic":
                loop = run_tool_loop_anthropic(
                    spec.model_id, VENDOR_LOOP_SYSTEM, user_prompt, ctx,
                    max_turns=max_turns, call_fn=anthropic_call_fn,
                )
            elif spec.backend == "openai_compat":
                loop = run_tool_loop_openai(
                    spec.model_id, VENDOR_LOOP_SYSTEM, user_prompt, ctx,
                    max_turns=max_turns, call_fn=openai_call_fn,
                )
            else:
                raise ValueError(
                    f"No vendor-native binding for backend {spec.backend!r}"
                )
            vendor_ok = None if expected_failure else int(metrics.exec_match(
                post_db, loop["final_sql"], gold_post))
            dt_vendor = time.perf_counter() - t0
        except Exception as exc:
            failed_ids.append(qid)
            runlog = {
                "database_id": config.database_id,
                "model": model_short,
                "status": "failed",
                "failed_ids": failed_ids,
                "completed_ids": [r["id"] for r in rows],
                "error": f"{type(exc).__name__}: {exc}",
            }
            _write_outputs(out_dir, stem, rows, None, runlog)
            print(
                f"[abort] query {qid} failed after retry budget "
                f"({type(exc).__name__}). Partial results saved; re-run with "
                f"--resume to continue from here."
            )
            raise

        can_row = canonical_rows.get(qid, {})
        rows.append({
            "id": qid,
            "perturbation": q["perturbation"],
            "question": question,
            "expected_failure": expected_failure,
            "sanity_sql": sanity_resp.text,
            "sanity_ok": sanity_ok,
            "sanity_latency_s": round(dt_sanity, 4),
            "sanity_tokens_in": sanity_resp.input_tokens,
            "sanity_tokens_out": sanity_resp.output_tokens,
            "canonical_baseline_ok": _canonical_int(can_row, "baseline_ok"),
            "canonical_mcp_ok": _canonical_int(can_row, "mcp_ok"),
            "diff_sql": diff_resp.text,
            "diff_ok": diff_ok,
            "diff_latency_s": round(dt_diff, 4),
            "diff_tokens_in": diff_resp.input_tokens,
            "diff_tokens_out": diff_resp.output_tokens,
            "vendor_sql": loop["final_sql"],
            "vendor_ok": vendor_ok,
            "vendor_latency_s": round(dt_vendor, 4),
            "vendor_tokens_in": loop["tokens_in"],
            "vendor_tokens_out": loop["tokens_out"],
            "vendor_n_api_calls": loop["n_api_calls"],
            "vendor_n_tool_calls": len(loop["tool_calls"]),
            "vendor_capped": int(loop["capped"]),
            "tool_calls_json": json.dumps(loop["tool_calls"]),
            "backend": sanity_resp.backend,
        })
        print(
            f"  {qid:>10}  sanity={sanity_ok} diff={diff_ok} "
            f"vendor={vendor_ok}  calls={loop['n_api_calls']} "
            f"tools={len(loop['tool_calls'])}"
            f"{' CAPPED' if loop['capped'] else ''}"
        )

    summary = _summarise_config(
        rows, config.database_id, model_short, spec, canonical_summary,
        validate_mode=validate_mode, strict=strict, max_turns=max_turns,
    )
    runlog = {
        "database_id": config.database_id,
        "model": model_short,
        "status": "complete",
        "failed_ids": [],
        "completed_ids": [r["id"] for r in rows],
    }
    _write_outputs(out_dir, stem, rows, summary, runlog)
    print(f"[done] wrote {out_dir / f'vendor_{stem}.csv'}")
    print(f"[done] wrote {out_dir / f'summary_vendor_{stem}.json'}")
    return summary


def _summarise_config(
    rows: list[dict[str, Any]],
    database_id: str,
    model_short: str,
    spec: Any,
    canonical_summary: dict[str, Any],
    *,
    validate_mode: str,
    strict: bool,
    max_turns: int,
) -> dict[str, Any]:
    scored = [r for r in rows if not r["expected_failure"]]
    n = len(scored)

    def ex(key: str) -> Optional[float]:
        if n == 0:
            return None
        return sum(int(r[key] or 0) for r in scored) / n

    def count(key: str) -> int:
        return sum(int(r[key] or 0) for r in scored)

    ex_sanity = ex("sanity_ok")
    ex_diff = ex("diff_ok")
    ex_vendor = ex("vendor_ok")

    can_stale = canonical_summary.get("ex_post_baseline")
    can_refreshed = canonical_summary.get("ex_post_refreshed_schema")
    can_mcp = canonical_summary.get("ex_post_mcp")

    # Agreement of the sanity re-run with the canonical stale baseline.
    paired = [r for r in scored if r["canonical_baseline_ok"] is not None]
    agreement = (
        sum(1 for r in paired if int(r["sanity_ok"]) == int(r["canonical_baseline_ok"]))
        / len(paired) if paired else None
    )

    # RR per the locked decision: canonical EX_stale as floor, canonical
    # EX_refreshed as ceiling (metrics.recovery_rate).
    rr_diff = rr_vendor = None
    if can_stale is not None and can_refreshed is not None:
        if ex_diff is not None:
            rr_diff = metrics.recovery_rate(can_stale, can_refreshed, ex_diff)
        if ex_vendor is not None:
            rr_vendor = metrics.recovery_rate(can_stale, can_refreshed, ex_vendor)

    # McNemar pairings against the canonical MCP arm, by query id.
    mcnemar: dict[str, Any] = {}
    mcp_paired = [r for r in scored if r["canonical_mcp_ok"] is not None]
    if mcp_paired:
        mcnemar["vendor_vs_mcp"] = metrics.mcnemar_test(
            [r["canonical_mcp_ok"] for r in mcp_paired],
            [r["vendor_ok"] for r in mcp_paired],
        )
        mcnemar["diff_vs_mcp"] = metrics.mcnemar_test(
            [r["canonical_mcp_ok"] for r in mcp_paired],
            [r["diff_ok"] for r in mcp_paired],
        )
    if scored:
        mcnemar["vendor_vs_diff"] = metrics.mcnemar_test(
            [r["diff_ok"] for r in scored],
            [r["vendor_ok"] for r in scored],
        )

    def tokens(prefix: str) -> dict[str, Any]:
        tin = sum(int(r[f"{prefix}_tokens_in"] or 0) for r in scored)
        tout = sum(int(r[f"{prefix}_tokens_out"] or 0) for r in scored)
        return {
            "tokens_in": tin,
            "tokens_out": tout,
            "usd": round(_usd(tin, tout, model_short), 6),
            "usd_per_query": round(_usd(tin, tout, model_short) / max(n, 1), 6),
            "mean_tokens_in": round(tin / max(n, 1), 1),
            "mean_tokens_out": round(tout / max(n, 1), 1),
        }

    tool_usage = {name: 0 for name in vt.TOOL_NAMES}
    n_no_tools = 0
    n_capped = 0
    total_tool_calls = 0
    for r in scored:
        calls = json.loads(r["tool_calls_json"]) if r.get("tool_calls_json") else []
        seen = {c["tool"] for c in calls}
        for name in seen:
            if name in tool_usage:
                tool_usage[name] += 1
        if not calls:
            n_no_tools += 1
        n_capped += int(r["vendor_capped"] or 0)
        total_tool_calls += len(calls)

    contaminated = sum(
        1 for r in rows if "mock-after-" in str(r.get("backend", ""))
    )

    return {
        "database_id": database_id,
        "model": model_short,
        "model_id": spec.model_id,
        "backend": spec.backend,
        "validate_mode": validate_mode,
        "strict_mode": strict,
        "max_turns": max_turns,
        "n_queries": len(rows),
        "n_queries_scored": n,
        "ex_stale_sanity": None if ex_sanity is None else round(ex_sanity, 4),
        "sanity_agreement_with_canonical": (
            None if agreement is None else round(agreement, 4)),
        "ex_diff_in_prompt": None if ex_diff is None else round(ex_diff, 4),
        "ex_vendor_native": None if ex_vendor is None else round(ex_vendor, 4),
        "canonical_ex_stale": can_stale,
        "canonical_ex_refreshed": can_refreshed,
        "canonical_ex_mcp": can_mcp,
        "canonical_recovery_rate": canonical_summary.get("recovery_rate"),
        "rr_diff_in_prompt": None if rr_diff is None else round(rr_diff, 4),
        "rr_vendor_native": None if rr_vendor is None else round(rr_vendor, 4),
        "wilson_ci": {
            "stale_sanity": metrics.wilson_ci(count("sanity_ok"), n),
            "diff_in_prompt": metrics.wilson_ci(count("diff_ok"), n),
            "vendor_native": metrics.wilson_ci(count("vendor_ok"), n),
        },
        "mcnemar": mcnemar,
        "latency_stats": {
            "sanity_s": metrics.latency_stats(
                [r["sanity_latency_s"] for r in scored]),
            "diff_in_prompt_s": metrics.latency_stats(
                [r["diff_latency_s"] for r in scored]),
            "vendor_native_s": metrics.latency_stats(
                [r["vendor_latency_s"] for r in scored]),
        },
        "cost": {
            "prices_usd_per_mtok": PRICES_USD_PER_MTOK.get(model_short),
            "sanity": tokens("sanity"),
            "diff_in_prompt": tokens("diff"),
            "vendor_native": tokens("vendor"),
        },
        "tool_loop": {
            "mean_tool_calls_per_query": round(total_tool_calls / max(n, 1), 3),
            "mean_api_calls_per_query": round(
                sum(int(r["vendor_n_api_calls"] or 0) for r in scored) / max(n, 1), 3),
            "pct_queries_using_tool": {
                name: round(used / max(n, 1), 4)
                for name, used in tool_usage.items()
            },
            "pct_queries_no_tools": round(n_no_tools / max(n, 1), 4),
            "pct_queries_capped": round(n_capped / max(n, 1), 4),
        },
        "fallback_counts": dict(llm_client.FALLBACK_STATS),
        "contaminated_queries": contaminated,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SITWE 2026 vendor-native function-calling harness."
    )
    p.add_argument("--model", default="haiku",
                   choices=[*SUPPORTED_MODELS, "mock"])
    p.add_argument("--database", default="concert_singer_TABLE_RENAME",
                   help="Config name under data/, or 'all' for the 10-config sweep.")
    p.add_argument("--validate", default="embedding",
                   choices=["token", "embedding"], dest="validate_mode")
    p.add_argument("--strict", action="store_true", default=True,
                   help="Raise on API failure after retries (default ON).")
    p.add_argument("--no-strict", dest="strict", action="store_false")
    p.add_argument("--max-turns", type=int, default=8)
    p.add_argument("--resume", action="store_true",
                   help="Skip queries already present in the output CSV.")
    p.add_argument("--mock", action="store_true",
                   help="Force the deterministic mock (no API calls).")
    p.add_argument("--out-dir", default=str(ROOT / "results" / "vendor_native"))
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.mock:
        _load_env(ROOT / ".env")
    databases = SWEEP_DATABASES if args.database == "all" else [args.database]
    out_dir = Path(args.out_dir)
    for db in databases:
        run_config(
            db,
            out_dir,
            model_name="mock" if args.mock else args.model,
            validate_mode=args.validate_mode,
            strict=args.strict,
            max_turns=args.max_turns,
            force_mock=args.mock,
            resume=args.resume,
        )


if __name__ == "__main__":
    main()
