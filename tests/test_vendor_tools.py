"""Unit tests for the vendor-native tool dispatcher (pure local, no API)."""

from __future__ import annotations

import socket

import pytest

from mcp_server import fingerprint as fp
from pilot import vendor_tools as vt


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must never open a socket, regardless of BLOCK_NETWORK."""
    def _blocked(*args, **kwargs):
        raise RuntimeError("Network access blocked for vendor tool tests")
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def ctx(pre_db: str, post_db: str) -> vt.ToolContext:
    pre = fp.introspect(pre_db)
    post = fp.introspect(post_db)
    diff = fp.classify(pre, post)
    return vt.ToolContext(
        diff_changed=diff.changed,
        diff_events=diff.events,
        post_schema_text=fp.to_prompt_block(post),
        post_db=post_db,
        validate_mode="token",
    )


# --------------------------------------------------------------------------- #
# Schemas                                                                     #
# --------------------------------------------------------------------------- #

def test_tool_defs_shape() -> None:
    assert vt.TOOL_NAMES == ["get_schema_diff", "relink_sql", "validate_sql"]
    for t in vt.TOOL_DEFS:
        assert t["parameters"]["type"] == "object"


def test_anthropic_format_uses_input_schema() -> None:
    tools = vt.anthropic_tools()
    assert all("input_schema" in t for t in tools)
    assert all("parameters" not in t for t in tools)
    assert tools[1]["input_schema"]["required"] == ["sql"]


def test_openai_format_wraps_function() -> None:
    tools = vt.openai_tools()
    assert all(t["type"] == "function" for t in tools)
    assert tools[2]["function"]["name"] == "validate_sql"
    assert tools[2]["function"]["parameters"]["required"] == ["sql"]


# --------------------------------------------------------------------------- #
# Dispatcher                                                                  #
# --------------------------------------------------------------------------- #

def test_get_schema_diff_returns_events_and_post_schema(ctx: vt.ToolContext) -> None:
    result = vt.dispatch("get_schema_diff", {}, ctx)
    assert result["changed"] is True
    ops = {e["op"] for e in result["events"]}
    assert "TABLE_RENAME" in ops
    assert "artist" in result["post_schema_text"]


def test_relink_sql_rewrites_renamed_table(ctx: vt.ToolContext) -> None:
    result = vt.dispatch("relink_sql", {"sql": "SELECT Name FROM singer"}, ctx)
    assert "artist" in result["sql"].lower()
    assert "singer" not in result["sql"].lower()
    assert result["method"] == "ast"


def test_relink_sql_missing_arg_returns_error(ctx: vt.ToolContext) -> None:
    assert "error" in vt.dispatch("relink_sql", {}, ctx)
    assert "error" in vt.dispatch("relink_sql", {"sql": "   "}, ctx)
    assert "error" in vt.dispatch("relink_sql", {"sql": 42}, ctx)


def test_validate_sql_execution_error(ctx: vt.ToolContext) -> None:
    ctx.question = "List all singer names."
    result = vt.dispatch("validate_sql", {"sql": "SELECT Name FROM singer"}, ctx)
    assert result["verdict"] == "execution_error"
    assert "no such table" in result["reason"]


def test_validate_sql_valid(ctx: vt.ToolContext) -> None:
    ctx.question = "List all artist names."
    result = vt.dispatch("validate_sql", {"sql": "SELECT Name FROM artist"}, ctx)
    assert result["verdict"] == "valid"


def test_validate_sql_missing_arg_returns_error(ctx: vt.ToolContext) -> None:
    assert "error" in vt.dispatch("validate_sql", {}, ctx)


def test_unknown_tool_raises(ctx: vt.ToolContext) -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        vt.dispatch("drop_all_tables", {}, ctx)


def test_dispatch_results_are_json_serialisable(ctx: vt.ToolContext) -> None:
    ctx.question = "List all artist names."
    for name, args in [
        ("get_schema_diff", {}),
        ("relink_sql", {"sql": "SELECT Name FROM singer"}),
        ("validate_sql", {"sql": "SELECT Name FROM artist"}),
    ]:
        assert vt.tool_result_json(name, args, ctx)
