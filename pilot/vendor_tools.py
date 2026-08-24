"""Vendor-neutral tool definitions + dispatcher for the vendor-native arm.

The three tools expose the *identical* middleware the MCP server uses
(fingerprint / relink / validate), but over the vendor's own function-calling
transport instead of MCP. The JSON schemas below are vendor-neutral dicts;
``anthropic_tools()`` / ``openai_tools()`` convert them to each vendor's
wire format.

The dispatcher is pure-local: it never calls an LLM. The diff is precomputed
once per database config (exactly like ``run_pilot.py`` does) and carried in
a ``ToolContext``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mcp_server import relink as rl
from mcp_server import validate as vl


# --------------------------------------------------------------------------- #
# Tool schemas (vendor-neutral)                                               #
# --------------------------------------------------------------------------- #

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "get_schema_diff",
        "description": (
            "Check whether the database schema has changed since the schema "
            "text you were given. Returns the classified change events and "
            "the current (post-change) schema text."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "relink_sql",
        "description": (
            "Deterministically rewrite a SQL query written against the old "
            "schema so it targets the current schema (AST-based rename "
            "rewriting). Returns the rewritten SQL and the method used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL query to rewrite.",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "validate_sql",
        "description": (
            "Execute a SQL query against the current database and validate "
            "it against the original question (back-translation similarity "
            "and empty-result heuristics). Returns a verdict of 'valid', "
            "'execution_error', or 'silent_failure_suspected', plus a reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL query to validate.",
                },
            },
            "required": ["sql"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_DEFS]


def anthropic_tools() -> list[dict[str, Any]]:
    """TOOL_DEFS in Anthropic tool-use format (``input_schema``)."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in TOOL_DEFS
    ]


def openai_tools() -> list[dict[str, Any]]:
    """TOOL_DEFS in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_DEFS
    ]


# --------------------------------------------------------------------------- #
# Dispatcher                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class ToolContext:
    """Per-config state the tools close over.

    ``diff_changed`` / ``diff_events`` come from ``fingerprint.classify`` on
    the pre/post databases, computed once per config. ``question`` is set per
    query before the tool loop starts (validate_sql needs it for the
    back-translation check).
    """

    diff_changed: bool
    diff_events: list[dict[str, Any]]
    post_schema_text: str
    post_db: str
    validate_mode: str = "token"          # "token" (v1) or "embedding" (v2)
    question: str = ""
    llm_guidance: str = field(default="", repr=False)


def dispatch(tool_name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Execute one tool call locally and return a JSON-serialisable result.

    Malformed arguments return an ``{"error": ...}`` payload (fed back to the
    model as the tool result) instead of raising, so a bad model-generated
    call cannot crash the harness mid-run. An unknown tool name raises — that
    is a harness bug, not a model error.
    """
    if tool_name == "get_schema_diff":
        return {
            "changed": ctx.diff_changed,
            "events": ctx.diff_events,
            "post_schema_text": ctx.post_schema_text,
        }

    if tool_name == "relink_sql":
        sql = (args or {}).get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            return {"error": "relink_sql requires a non-empty 'sql' string argument"}
        result = rl.relink(sql, ctx.diff_events, schema_text=ctx.post_schema_text)
        return {"sql": result["sql"], "method": result["method"]}

    if tool_name == "validate_sql":
        sql = (args or {}).get("sql", "")
        if not isinstance(sql, str) or not sql.strip():
            return {"error": "validate_sql requires a non-empty 'sql' string argument"}
        validate_fn = vl.validate_v2 if ctx.validate_mode == "embedding" else vl.validate
        res = validate_fn(ctx.post_db, sql, ctx.question)
        return {"verdict": res.verdict, "reason": res.reason or res.error or ""}

    raise ValueError(f"Unknown tool {tool_name!r}; expected one of {TOOL_NAMES}")


def tool_result_json(tool_name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    """Dispatch and serialise — convenience for transports that want a string."""
    return json.dumps(dispatch(tool_name, args, ctx))
