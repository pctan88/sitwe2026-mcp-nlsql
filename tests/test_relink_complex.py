"""Tests for relink behaviour with complex (non-rename) operators.

Verifies:
  1. LLM fallback IS invoked for TABLE_SPLIT and COLUMN_MERGE-with-expression.
  2. AST rewrite IS applied before the LLM fallback (e.g. TABLE_RENAME is
     rewritten via AST, then the rewritten SQL is passed to the LLM for
     TABLE_SPLIT handling).
  3. AST rewrite is deterministic: same (stale_sql, diff) → byte-identical
     relinked output across repeated runs.
"""

from __future__ import annotations

import pytest

from mcp_server.relink import relink


# --------------------------------------------------------------------------- #
# LLM fallback invocation                                                     #
# --------------------------------------------------------------------------- #

class TestLLMFallbackInvocation:
    def test_table_split_invokes_llm_fallback(self):
        """TABLE_SPLIT events must route through the llm_fallback callable
        because the AST rewriter cannot infer JOIN structure from a split.
        """
        stale_sql = "SELECT Name FROM singer"
        diff_events = [
            {"op": "TABLE_SPLIT", "source": "singer",
             "targets": ["singer_info", "singer_songs"],
             "key": "Singer_ID"},
        ]

        callback_invocations: list[tuple] = []

        def mock_fallback(sql: str, events: list, schema: str) -> str:
            callback_invocations.append((sql, events, schema))
            return "SELECT T1.Name FROM singer_info T1"

        result = relink(
            stale_sql, diff_events,
            schema_text="TABLE singer_info (...)",
            llm_fallback=mock_fallback,
        )

        assert len(callback_invocations) == 1, (
            "LLM fallback must be called exactly once for TABLE_SPLIT"
        )
        assert result["method"] == "llm"
        assert result["sql"] == "SELECT T1.Name FROM singer_info T1"

    def test_column_merge_invokes_llm_fallback(self):
        """COLUMN_MERGE events must route through the llm_fallback callable
        because the AST rewriter cannot infer the merge expression.
        """
        stale_sql = "SELECT Name, Country FROM singer"
        diff_events = [
            {"op": "COLUMN_MERGE", "table": "singer",
             "sources": ["Name", "Country"], "target": "Name_Country",
             "expression": "Name || ' - ' || Country"},
        ]

        callback_invocations: list[tuple] = []

        def mock_fallback(sql: str, events: list, schema: str) -> str:
            callback_invocations.append((sql, events, schema))
            return "SELECT Name_Country FROM singer"

        result = relink(
            stale_sql, diff_events,
            schema_text="TABLE singer (...)",
            llm_fallback=mock_fallback,
        )

        assert len(callback_invocations) == 1
        assert result["method"] == "llm"

    def test_table_merge_invokes_llm_fallback(self):
        """TABLE_MERGE events must also route through the LLM fallback."""
        stale_sql = "SELECT concert_Name FROM concert"
        diff_events = [
            {"op": "TABLE_MERGE", "sources": ["concert", "singer_in_concert"],
             "target": "concert_performance", "key": "Concert_ID"},
        ]

        called = []

        def mock_fallback(sql, events, schema):
            called.append(True)
            return "SELECT concert_Name FROM concert_performance"

        result = relink(
            stale_sql, diff_events,
            schema_text="...",
            llm_fallback=mock_fallback,
        )

        assert len(called) == 1
        assert result["method"] == "llm"

    def test_no_fallback_without_callback(self):
        """When llm_fallback is None, complex events are not applied and the
        method is 'ast' (the AST rewriter does what it can).
        """
        stale_sql = "SELECT Name FROM singer"
        diff_events = [
            {"op": "TABLE_SPLIT", "source": "singer",
             "targets": ["singer_info", "singer_songs"],
             "key": "Singer_ID"},
        ]

        result = relink(stale_sql, diff_events, schema_text=None)

        # Without a fallback, method stays "ast" and SQL is unchanged
        # (TABLE_SPLIT is not an AST-rewritable op).
        assert result["method"] == "ast"


# --------------------------------------------------------------------------- #
# AST rewrite applied BEFORE LLM fallback                                    #
# --------------------------------------------------------------------------- #

class TestASTBeforeFallback:
    def test_table_rename_applied_before_split_fallback(self):
        """When a diff contains both TABLE_RENAME and TABLE_SPLIT, the AST
        rewriter applies the rename first, then the rewritten SQL is passed
        to the LLM fallback for the split.
        """
        stale_sql = "SELECT Name FROM singer"
        diff_events = [
            {"op": "TABLE_RENAME", "from": "singer", "to": "artist"},
            {"op": "TABLE_SPLIT", "source": "artist",
             "targets": ["artist_info", "artist_songs"],
             "key": "Singer_ID"},
        ]

        received_sql: list[str] = []

        def mock_fallback(sql, events, schema):
            received_sql.append(sql)
            return sql  # pass-through

        result = relink(
            stale_sql, diff_events,
            schema_text="...",
            llm_fallback=mock_fallback,
        )

        assert len(received_sql) == 1
        # The SQL received by the LLM should have "artist" not "singer".
        assert "artist" in received_sql[0].lower()
        assert "singer" not in received_sql[0].lower()
        assert result["method"] == "llm"


# --------------------------------------------------------------------------- #
# AST determinism                                                             #
# --------------------------------------------------------------------------- #

class TestASTDeterminism:
    def test_same_input_produces_identical_output(self):
        """The AST rewrite path must produce byte-identical output across
        repeated runs with the same input.  The paper claims determinism
        for the AST path, so this must be tested explicitly.
        """
        stale_sql = (
            "SELECT T2.Name, T3.concert_Name "
            "FROM singer_in_concert AS T1 "
            "JOIN singer AS T2 ON T1.Singer_ID = T2.Singer_ID "
            "JOIN concert AS T3 ON T1.Concert_ID = T3.Concert_ID"
        )
        diff_events = [
            {"op": "TABLE_RENAME", "from": "singer", "to": "artist"},
            {"op": "COLUMN_RENAME", "table": "artist",
             "from": "Song_Name", "to": "song_title"},
        ]

        outputs = set()
        for _ in range(10):
            result = relink(stale_sql, diff_events)
            outputs.add(result["sql"])

        assert len(outputs) == 1, (
            f"AST rewrite produced {len(outputs)} distinct outputs for "
            f"the same input — expected exactly 1 (determinism)."
        )

    def test_noop_when_no_events(self):
        """Empty diff events → noop, SQL unchanged."""
        sql = "SELECT * FROM singer"
        result = relink(sql, [])
        assert result["sql"] == sql
        assert result["method"] == "noop"

    def test_column_rename_determinism(self):
        """Column rename AST rewrite is deterministic."""
        stale_sql = "SELECT Song_Name, Song_release_year FROM singer"
        diff_events = [
            {"op": "COLUMN_RENAME", "table": "singer",
             "from": "Song_Name", "to": "song_title"},
            {"op": "COLUMN_RENAME", "table": "singer",
             "from": "Song_release_year", "to": "debut_year"},
        ]

        results = [relink(stale_sql, diff_events)["sql"] for _ in range(10)]
        assert len(set(results)) == 1
        assert "song_title" in results[0]
        assert "debut_year" in results[0]
