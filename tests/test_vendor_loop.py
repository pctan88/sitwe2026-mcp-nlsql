"""Vendor-native loop tests — scripted fake backends, no API, no network.

Covers:
* the Anthropic-shaped tool loop (terminates, logs tool calls, extracts SQL)
* the OpenAI-shaped tool loop
* turn-cap behaviour (scores the last SQL produced)
* a full mock-mode run of the harness: per-query row shape, and the
  guarantee that canonical results/ files are byte-identical afterwards
"""

from __future__ import annotations

import csv
import hashlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_server import fingerprint as fp
from pilot import run_vendor_native as rvn
from pilot import vendor_tools as vt

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*args, **kwargs):
        raise RuntimeError("Network access blocked for vendor loop tests")
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def ctx(pre_db: str, post_db: str) -> vt.ToolContext:
    pre = fp.introspect(pre_db)
    post = fp.introspect(post_db)
    diff = fp.classify(pre, post)
    c = vt.ToolContext(
        diff_changed=diff.changed,
        diff_events=diff.events,
        post_schema_text=fp.to_prompt_block(post),
        post_db=post_db,
        validate_mode="token",
    )
    c.question = "List all artist names."
    return c


# --------------------------------------------------------------------------- #
# Anthropic-shaped scripted loop                                              #
# --------------------------------------------------------------------------- #

def _anthropic_msg(stop_reason: str, blocks: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=blocks,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def _tool_use(bid: str, name: str, args: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=bid, name=name, input=args)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def test_anthropic_loop_terminates_and_extracts_sql(ctx: vt.ToolContext) -> None:
    script = [
        _anthropic_msg("tool_use", [_tool_use("t1", "get_schema_diff", {})]),
        _anthropic_msg("tool_use", [
            _tool_use("t2", "relink_sql", {"sql": "SELECT Name FROM singer"}),
        ]),
        _anthropic_msg("end_turn", [_text("```sql\nSELECT Name FROM artist\n```")]),
    ]
    calls: list[list[dict[str, Any]]] = []

    def fake_call(model_id, system, messages, tools):
        calls.append([dict(m) for m in messages])
        return script[len(calls) - 1]

    out = rvn.run_tool_loop_anthropic(
        "fake-model", "sys", "user", ctx, max_turns=8, call_fn=fake_call,
    )
    assert out["final_sql"] == "SELECT Name FROM artist"
    assert out["capped"] is False
    assert out["n_api_calls"] == 3
    assert [c["tool"] for c in out["tool_calls"]] == [
        "get_schema_diff", "relink_sql",
    ]
    assert out["tokens_in"] == 300 and out["tokens_out"] == 60
    # Tool results must have been fed back as tool_result blocks.
    last_messages = calls[-1]
    assert last_messages[-1]["role"] == "user"
    assert last_messages[-1]["content"][0]["type"] == "tool_result"


def test_anthropic_loop_cap_scores_last_sql(ctx: vt.ToolContext) -> None:
    def always_tool(model_id, system, messages, tools):
        return _anthropic_msg("tool_use", [
            _tool_use("t", "validate_sql", {"sql": "SELECT Name FROM artist"}),
        ])

    out = rvn.run_tool_loop_anthropic(
        "fake-model", "sys", "user", ctx, max_turns=3, call_fn=always_tool,
    )
    assert out["capped"] is True
    assert out["n_api_calls"] == 3
    assert out["final_sql"] == "SELECT Name FROM artist"


# --------------------------------------------------------------------------- #
# OpenAI-shaped scripted loop                                                 #
# --------------------------------------------------------------------------- #

def _openai_body(finish_reason: str, content: str = "",
                 tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": content, "tool_calls": tool_calls or []},
        }],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
    }


def test_openai_loop_terminates_and_extracts_sql(ctx: vt.ToolContext) -> None:
    script = [
        _openai_body("tool_calls", tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "get_schema_diff", "arguments": "{}"},
        }]),
        _openai_body("tool_calls", tool_calls=[{
            "id": "c2", "type": "function",
            "function": {
                "name": "relink_sql",
                "arguments": json.dumps({"sql": "SELECT Name FROM singer"}),
            },
        }]),
        _openai_body("stop", content="SELECT Name FROM artist"),
    ]
    seen: list[list[dict[str, Any]]] = []

    def fake_call(model_id, messages, tools):
        seen.append(list(messages))
        return script[len(seen) - 1]

    out = rvn.run_tool_loop_openai(
        "fake-model", "sys", "user", ctx, max_turns=8, call_fn=fake_call,
    )
    assert out["final_sql"] == "SELECT Name FROM artist"
    assert out["capped"] is False
    assert out["n_api_calls"] == 3
    assert [c["tool"] for c in out["tool_calls"]] == [
        "get_schema_diff", "relink_sql",
    ]
    assert out["tokens_in"] == 600 and out["tokens_out"] == 90
    # role:"tool" results must have been appended.
    assert seen[-1][-1]["role"] == "tool"


def test_openai_loop_malformed_arguments_survive(ctx: vt.ToolContext) -> None:
    script = [
        _openai_body("tool_calls", tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "relink_sql", "arguments": "{not json"},
        }]),
        _openai_body("stop", content="SELECT Name FROM artist"),
    ]
    n = {"i": 0}

    def fake_call(model_id, messages, tools):
        n["i"] += 1
        return script[n["i"] - 1]

    out = rvn.run_tool_loop_openai(
        "fake-model", "sys", "user", ctx, max_turns=8, call_fn=fake_call,
    )
    assert out["final_sql"] == "SELECT Name FROM artist"
    assert out["tool_calls"][0]["result"].get("error")


# --------------------------------------------------------------------------- #
# Mock loop + full mock-mode harness run                                      #
# --------------------------------------------------------------------------- #

def test_mock_loop_relinks_stale_sql(ctx: vt.ToolContext) -> None:
    out = rvn.run_tool_loop_mock("SELECT Name FROM singer", ctx)
    assert "artist" in out["final_sql"].lower()
    assert [c["tool"] for c in out["tool_calls"]] == [
        "get_schema_diff", "relink_sql", "validate_sql",
    ]
    assert out["capped"] is False


def _canonical_digests() -> dict[str, str]:
    results = ROOT / "results"
    out: dict[str, str] = {}
    for pattern in ("pilot_results*.csv", "summary*.json"):
        for path in sorted(results.glob(pattern)):
            out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_mock_run_row_shape_and_canonical_files_untouched(tmp_path: Path) -> None:
    before = _canonical_digests()
    assert before, "expected canonical results files to exist"

    summary = rvn.run_config(
        "concert_singer_TABLE_RENAME",
        tmp_path / "vendor_native",
        model_name="mock",
        validate_mode="token",
        strict=True,
        force_mock=True,
    )

    assert _canonical_digests() == before, (
        "a mock vendor run modified canonical results files"
    )

    csv_path = tmp_path / "vendor_native" / "vendor_mock_concert_singer_TABLE_RENAME.csv"
    assert csv_path.exists()
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 16
    assert list(rows[0].keys()) == rvn.CSV_COLUMNS
    for row in rows:
        assert row["vendor_sql"]
        assert row["tool_calls_json"]
        assert row["vendor_ok"] in ("0", "1", "")

    assert summary["n_queries"] == 16
    assert summary["contaminated_queries"] == 0
    assert summary["ex_vendor_native"] is not None
    assert summary["tool_loop"]["mean_tool_calls_per_query"] == 3.0

    runlog = json.loads(
        (tmp_path / "vendor_native" /
         "runlog_vendor_mock_concert_singer_TABLE_RENAME.json").read_text()
    )
    assert runlog["status"] == "complete"
    assert runlog["failed_ids"] == []


def test_resume_reload_round_trip(tmp_path: Path) -> None:
    out_dir = tmp_path / "vendor_native"
    rvn.run_config(
        "concert_singer_TABLE_RENAME",
        out_dir,
        model_name="mock",
        validate_mode="token",
        strict=True,
        force_mock=True,
    )
    csv_path = out_dir / "vendor_mock_concert_singer_TABLE_RENAME.csv"
    reloaded = rvn._reload_existing_rows(csv_path)
    assert len(reloaded) == 16
    row = next(iter(reloaded.values()))
    assert isinstance(row["vendor_ok"], int) or row["vendor_ok"] is None
    assert isinstance(row["vendor_latency_s"], float)
    assert row["expected_failure"] in (True, False)
