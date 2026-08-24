"""Tests for validate v2 (embedding back-translation) and the fallback
guidance builder — all offline (similarity functions injected; no model
download, no API calls).
"""

from __future__ import annotations

import sqlite3

import pytest

from mcp_server import validate as vl
from mcp_server.relink import build_llm_guidance
from pilot.llm_client import build_relink_prompt


@pytest.fixture()
def toy_db(tmp_path):
    db = str(tmp_path / "toy.sqlite")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE singer (Singer_ID INTEGER, Name_Country TEXT, Age INTEGER)"
    )
    con.executemany(
        "INSERT INTO singer VALUES (?, ?, ?)",
        [(1, "Joe - France", 52), (2, "Ann - Japan", 41)],
    )
    con.commit()
    con.close()
    return db


class TestSimilarityFnInjection:
    def test_low_similarity_flags_silent(self, toy_db):
        res = vl.validate(
            toy_db,
            "SELECT Name_Country FROM singer",
            "How many concerts were held?",
            similarity_threshold=0.5,
            similarity_fn=lambda a, b: 0.1,
        )
        assert res.verdict == vl.SILENT
        assert res.reason == "low_semantic_similarity"
        assert res.similarity == 0.1

    def test_high_similarity_passes(self, toy_db):
        res = vl.validate(
            toy_db,
            "SELECT Name_Country FROM singer",
            "List singer names with countries.",
            similarity_threshold=0.5,
            similarity_fn=lambda a, b: 0.9,
        )
        assert res.verdict == vl.VALID
        assert res.similarity == 0.9

    def test_default_token_path_unchanged(self, toy_db):
        """No similarity_fn -> v1 token behaviour with its original reason."""
        res = vl.validate(
            toy_db,
            "SELECT Name_Country FROM singer",
            "zzz qqq xxx yyy",  # zero token overlap
        )
        assert res.verdict == vl.SILENT
        assert res.reason == "low_token_overlap"

    def test_exec_error_takes_priority(self, toy_db):
        res = vl.validate(
            toy_db,
            "SELECT missing_col FROM singer",
            "List names.",
            similarity_fn=lambda a, b: 1.0,
        )
        assert res.verdict == vl.EXEC_ERROR

    def test_empty_result_on_affirmative_still_fires(self, toy_db):
        """H2 must still run under the v2 similarity backend."""
        res = vl.validate(
            toy_db,
            "SELECT Name_Country FROM singer WHERE Age > 99",
            "List singer names with countries.",
            similarity_threshold=0.5,
            similarity_fn=lambda a, b: 0.9,
        )
        assert res.verdict == vl.SILENT
        assert res.reason == "empty_result_on_affirmative_question"


class TestValidateV2Wrapper:
    def test_v2_uses_injected_fn(self, toy_db):
        res = vl.validate_v2(
            toy_db,
            "SELECT Name_Country FROM singer",
            "List singer names with countries.",
            similarity_fn=lambda a, b: 0.8,
        )
        assert res.verdict == vl.VALID
        assert res.similarity == 0.8

    def test_v2_default_threshold_is_embedding_scale(self, toy_db):
        res = vl.validate_v2(
            toy_db,
            "SELECT Name_Country FROM singer",
            "List singer names with countries.",
            similarity_fn=lambda a, b: vl.EMBEDDING_THRESHOLD - 0.01,
        )
        assert res.verdict == vl.SILENT

    def test_v2_falls_back_without_dependency(self, toy_db, monkeypatch):
        """Missing sentence-transformers must degrade to v1, not crash."""
        def _boom():
            raise ImportError("no sentence_transformers")
        monkeypatch.setattr(vl, "_load_embedder", _boom)
        with pytest.warns(RuntimeWarning):
            res = vl.validate_v2(
                toy_db,
                "SELECT Name_Country FROM singer",
                "List singer names with countries merged.",
            )
        assert res.verdict in (vl.VALID, vl.SILENT)  # v1 semantics
        assert res.reason != "low_semantic_similarity"


class TestGuidanceBuilder:
    def test_column_merge_guidance_has_like_rule(self):
        events = [{
            "op": "COLUMN_MERGE", "table": "singer",
            "sources": ["Name", "Country"], "target": "Name_Country",
            "expression": "Name || ' - ' || Country",
        }]
        g = build_llm_guidance(events)
        assert "Name_Country" in g
        assert "LIKE" in g
        assert "' - '" in g or " - " in g          # separator surfaced
        assert "SUBSTR" in g                        # component recovery rule

    def test_table_split_guidance_names_key(self):
        events = [{
            "op": "TABLE_SPLIT", "source": "singer",
            "targets": ["singer_info", "singer_songs"], "key": "Singer_ID",
        }]
        g = build_llm_guidance(events)
        assert "singer_info" in g and "singer_songs" in g
        assert "Singer_ID" in g and "JOIN" in g

    def test_table_merge_guidance(self):
        events = [{
            "op": "TABLE_MERGE", "sources": ["concert", "singer_in_concert"],
            "target": "concert_performance", "key": "Concert_ID",
        }]
        g = build_llm_guidance(events)
        assert "concert_performance" in g

    def test_rename_only_diff_produces_no_guidance(self):
        events = [
            {"op": "TABLE_RENAME", "from": "singer", "to": "artist"},
            {"op": "COLUMN_RENAME", "table": "artist",
             "from": "Song_Name", "to": "song_title"},
        ]
        assert build_llm_guidance(events) == ""

    def test_guidance_deterministic(self):
        events = [{
            "op": "COLUMN_MERGE", "table": "singer",
            "sources": ["Name", "Country"], "target": "Name_Country",
            "expression": "Name || ' - ' || Country",
        }]
        outputs = {build_llm_guidance(events) for _ in range(10)}
        assert len(outputs) == 1


class TestRelinkPromptBackwardCompat:
    def test_default_prompt_unchanged(self):
        """Existing 3-arg calls must produce the original prompt exactly."""
        p = build_relink_prompt("SELECT 1", "diff", "schema")
        assert p == (
            "-- Stale SQL --\nSELECT 1\n\n"
            "-- Schema diff --\ndiff\n\n"
            "-- Current schema --\nschema\n\n"
            "-- Rewritten SQL --\n"
        )

    def test_enriched_prompt_contains_sections(self):
        p = build_relink_prompt(
            "SELECT 1", "diff", "schema",
            question="How many?", guidance="use LIKE",
        )
        assert "-- Original question --\nHow many?" in p
        assert "-- Rewrite rules --\nuse LIKE" in p
        # Section order: stale SQL, question, diff, rules, schema.
        assert p.index("Stale SQL") < p.index("Original question") \
            < p.index("Schema diff") < p.index("Rewrite rules") \
            < p.index("Current schema")
