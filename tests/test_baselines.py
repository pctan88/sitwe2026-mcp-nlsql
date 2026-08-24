"""Tests for Stage 2 baseline paths.

The tests use the deterministic mock backend so they exercise the harness
control flow without depending on external LLM APIs.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from data.build_dbs import build_post, build_pre
from mcp_server import fingerprint as fp
from pilot import llm_client
from pilot.run_pilot import run


def _write_query_file(path: Path, queries: list[dict]) -> Path:
    path.write_text(json.dumps({"queries": queries}, indent=2))
    return path


def _build_db_dir(path: Path) -> Path:
    path.mkdir()
    build_pre(path / "concert_singer_pre.sqlite")
    build_post(path / "concert_singer_post.sqlite")
    return path


def test_refreshed_schema_baseline_uses_post_schema(tmp_path):
    db_dir = _build_db_dir(tmp_path / "dbs")
    queries_path = _write_query_file(
        tmp_path / "queries.json",
        [
            {
                "id": "Q13",
                "question": "Show each singer's name and their song name.",
                "gold_pre": "SELECT Name, Song_Name FROM singer",
                "gold_post": "SELECT Name, song_title FROM artist",
                "perturbation": "TABLE_AND_COLUMN_RENAME",
            }
        ],
    )

    summary = run(
        db_dir,
        queries_path,
        tmp_path / "results",
        force_backend="mock",
        out_suffix="_baselines",
    )

    rows = list(csv.DictReader((tmp_path / "results" / "pilot_results_baselines.csv").open()))
    assert summary["ex_post_refreshed_schema"] == 1.0
    assert rows[0]["refreshed_schema_ok"] == "1"
    assert rows[0]["refreshed_schema_sql"] == "SELECT Name, song_title FROM artist"


def test_error_feedback_retries_once_after_execution_error(tmp_path, monkeypatch):
    db_dir = _build_db_dir(tmp_path / "dbs")
    queries_path = _write_query_file(
        tmp_path / "queries.json",
        [
            {
                "id": "Q01",
                "question": "How many singers do we have?",
                "gold_pre": "SELECT COUNT(*) FROM singer",
                "gold_post": "SELECT COUNT(*) FROM artist",
                "perturbation": "TABLE_RENAME",
            }
        ],
    )

    captured_schema_texts: list[str] = []
    original_generate_sql_with_error = llm_client.generate_sql_with_error

    def capture_generate_sql_with_error(schema_text, *args, **kwargs):
        captured_schema_texts.append(schema_text)
        return original_generate_sql_with_error(schema_text, *args, **kwargs)

    monkeypatch.setattr(
        llm_client,
        "generate_sql_with_error",
        capture_generate_sql_with_error,
    )

    summary = run(
        db_dir,
        queries_path,
        tmp_path / "results",
        force_backend="mock",
        out_suffix="_baselines",
    )

    rows = list(csv.DictReader((tmp_path / "results" / "pilot_results_baselines.csv").open()))
    assert summary["ex_post_error_feedback"] == 0.0
    assert rows[0]["baseline_sql"] == "SELECT COUNT(*) FROM singer"
    assert rows[0]["baseline_ok"] == "0"
    assert rows[0]["error_feedback_retry"] == "1"
    assert "no such table: singer" in rows[0]["error_feedback_error"]
    assert rows[0]["error_feedback_sql"] == "SELECT COUNT(*) FROM singer"
    assert rows[0]["error_feedback_ok"] == "0"
    assert len(captured_schema_texts) == 1
    assert "TABLE singer" in captured_schema_texts[0]
    assert "Song_Name" in captured_schema_texts[0]
    assert "TABLE artist" not in captured_schema_texts[0]
    assert "song_title" not in captured_schema_texts[0]


def test_error_feedback_mock_column_repair_is_deterministic(post_db):
    schema_text = fp.to_prompt_block(fp.introspect(post_db))
    args = (
        schema_text,
        "Count songs released after 2010.",
        "SELECT COUNT(*) FROM artist WHERE Song_release_year > 2010",
        "no such column: Song_release_year",
    )

    first = llm_client.generate_sql_with_error(*args, force_backend="mock")
    second = llm_client.generate_sql_with_error(*args, force_backend="mock")

    assert first.text == second.text
    assert first.text == "SELECT COUNT(*) FROM artist WHERE debut_year > 2010"
