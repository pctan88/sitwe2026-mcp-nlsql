"""Tests for the schema diff classifier (mcp_server/fingerprint.py).

Critical disambiguation tests verify that:
  - A rename is NOT misclassified as drop + add.
  - A genuine drop + add (different types) is NOT misclassified as rename.
  - TABLE_SPLIT, TABLE_MERGE, and COLUMN_MERGE are detected correctly.
  - The classifier's priority order is: rename → split → merge → fallthrough.

The disambiguation heuristic is *type-signature matching*: a rename preserves
the exact column types and count, while a drop + add typically changes them.
A same-structure drop+add is *formally indistinguishable* from a rename —
this is a documented limitation the classifier conservatively treats as a rename.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_server.fingerprint import (
    Column,
    Schema,
    SchemaDiff,
    Table,
    classify,
    introspect,
)
from data.perturbations import (
    PerturbationOperator,
    PerturbationSpec,
    apply_perturbations,
)
from tests.conftest import make_simple_db


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _ops(diff: SchemaDiff) -> list[str]:
    """Extract op codes from a diff's events."""
    return [e["op"] for e in diff.events]


def _schema_from_tables(tables: list[Table]) -> Schema:
    return Schema(tables=tables)


def _make_table(name: str, cols: list[tuple[str, str]]) -> Table:
    return Table(name=name, columns=[Column(n, t) for n, t in cols])


# --------------------------------------------------------------------------- #
# TABLE_RENAME — not misclassified as drop + add                              #
# --------------------------------------------------------------------------- #

class TestTableRenameDisambiguation:
    """The classifier uses type-signature matching to distinguish renames from
    drop+add.  A rename preserves the column set (names+types).  A genuine
    drop+add has different column types or counts.
    """

    def test_exact_column_set_match_is_rename(self):
        """Identical column names and types → TABLE_RENAME."""
        pre = _schema_from_tables([
            _make_table("users", [("id", "INTEGER"), ("name", "TEXT"), ("age", "INTEGER")]),
        ])
        post = _schema_from_tables([
            _make_table("people", [("id", "INTEGER"), ("name", "TEXT"), ("age", "INTEGER")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "TABLE_RENAME" in ops
        assert "TABLE_REMOVE" not in ops
        assert "TABLE_ADD" not in ops

    def test_different_column_types_is_not_rename(self):
        """Different column types → TABLE_REMOVE + TABLE_ADD, NOT TABLE_RENAME.

        This is the CRITICAL disambiguation test: ensures the classifier does
        not conflate a genuine schema restructure with a simple rename.
        """
        pre = _schema_from_tables([
            _make_table("users", [("id", "INTEGER"), ("name", "TEXT"), ("age", "INTEGER")]),
        ])
        post = _schema_from_tables([
            _make_table("accounts", [("id", "INTEGER"), ("username", "VARCHAR"), ("created", "TEXT")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "TABLE_RENAME" not in ops
        assert "TABLE_REMOVE" in ops
        assert "TABLE_ADD" in ops

    def test_different_column_count_is_not_rename(self):
        """Different column count → TABLE_REMOVE + TABLE_ADD."""
        pre = _schema_from_tables([
            _make_table("x", [("a", "INT"), ("b", "TEXT")]),
        ])
        post = _schema_from_tables([
            _make_table("y", [("a", "INT"), ("b", "TEXT"), ("c", "REAL")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "TABLE_RENAME" not in ops

    def test_same_structure_drop_add_classified_as_rename_known_limitation(self):
        """A drop+add with IDENTICAL column types is indistinguishable from
        a rename.  The classifier conservatively treats it as a rename,
        which is the safer assumption for query relinking.

        This is a DOCUMENTED limitation of the type-signature heuristic.
        """
        pre = _schema_from_tables([
            _make_table("old_table", [("id", "INT"), ("val", "TEXT")]),
        ])
        post = _schema_from_tables([
            _make_table("new_table", [("id", "INT"), ("val", "TEXT")]),
        ])
        diff = classify(pre, post)
        # The classifier WILL say TABLE_RENAME — this is expected.
        assert "TABLE_RENAME" in _ops(diff)


# --------------------------------------------------------------------------- #
# COLUMN_RENAME — not misclassified as drop + add                             #
# --------------------------------------------------------------------------- #

class TestColumnRenameDisambiguation:
    def test_same_type_column_is_rename(self):
        """Column with same type but different name → COLUMN_RENAME."""
        pre = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("old_col", "TEXT")]),
        ])
        post = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("new_col", "TEXT")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "COLUMN_RENAME" in ops
        assert "COLUMN_REMOVE" not in ops
        assert "COLUMN_ADD" not in ops

    def test_different_type_column_is_not_rename(self):
        """Column with different type → COLUMN_REMOVE + COLUMN_ADD,
        NOT COLUMN_RENAME.

        CRITICAL: ensures a type-changing restructure is not misclassified.
        """
        pre = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("data", "TEXT")]),
        ])
        post = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("payload", "BLOB")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "COLUMN_RENAME" not in ops
        assert "COLUMN_REMOVE" in ops
        assert "COLUMN_ADD" in ops


# --------------------------------------------------------------------------- #
# TABLE_SPLIT detection                                                       #
# --------------------------------------------------------------------------- #

class TestTableSplitDetection:
    def test_split_detected(self):
        """One table removed, two added tables that are strict subsets of the
        original and whose union covers the original → TABLE_SPLIT.
        """
        pre = _schema_from_tables([
            _make_table("singer", [
                ("Singer_ID", "INTEGER"), ("Name", "TEXT"),
                ("Country", "TEXT"), ("Song_Name", "TEXT"),
            ]),
        ])
        post = _schema_from_tables([
            _make_table("singer_info", [
                ("Singer_ID", "INTEGER"), ("Name", "TEXT"), ("Country", "TEXT"),
            ]),
            _make_table("singer_songs", [
                ("Singer_ID", "INTEGER"), ("Song_Name", "TEXT"),
            ]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "TABLE_SPLIT" in ops
        assert "TABLE_REMOVE" not in ops
        assert "TABLE_ADD" not in ops

        split_ev = next(e for e in diff.events if e["op"] == "TABLE_SPLIT")
        assert split_ev["source"] == "singer"
        assert sorted(split_ev["targets"]) == ["singer_info", "singer_songs"]
        assert split_ev["key"] == "Singer_ID"

    def test_unrelated_add_not_classified_as_split(self):
        """Removed table + added tables with NO column name overlap → not split.

        Uses distinct column types and names so the rename heuristic (same
        type multiset) does not match either.
        """
        pre = _schema_from_tables([
            _make_table("users", [
                ("user_id", "INTEGER"), ("email", "VARCHAR"), ("bio", "BLOB"),
            ]),
        ])
        post = _schema_from_tables([
            _make_table("logs", [("log_id", "BIGINT"), ("msg", "CLOB")]),
            _make_table("config", [("key", "TEXT"), ("val", "JSON")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "TABLE_SPLIT" not in ops
        assert "TABLE_REMOVE" in ops

    def test_split_with_live_db(self, pre_db, tmp_path):
        """End-to-end: apply TABLE_SPLIT via perturbation engine, then run
        the classifier on the pre/post schemas and verify it detects the split.
        """
        post_path = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post_path, [
            PerturbationSpec(PerturbationOperator.TABLE_SPLIT, {
                "source": "singer",
                "targets": ["singer_info", "singer_songs"],
                "key": "Singer_ID",
                "partition": {
                    "singer_info": ["Name", "Country", "Age", "Is_male"],
                    "singer_songs": ["Song_Name", "Song_release_year"],
                },
            }),
        ])
        pre_schema = introspect(pre_db)
        post_schema = introspect(post_path)
        diff = classify(pre_schema, post_schema)
        ops = _ops(diff)
        assert "TABLE_SPLIT" in ops
        assert "TABLE_REMOVE" not in ops


# --------------------------------------------------------------------------- #
# TABLE_MERGE detection                                                       #
# --------------------------------------------------------------------------- #

class TestTableMergeDetection:
    def test_merge_detected(self):
        """Two removed tables whose column union = one added table → TABLE_MERGE."""
        pre = _schema_from_tables([
            _make_table("concert", [
                ("Concert_ID", "INTEGER"), ("Name", "TEXT"), ("Year", "INTEGER"),
            ]),
            _make_table("singer_in_concert", [
                ("Concert_ID", "INTEGER"), ("Singer_ID", "INTEGER"),
            ]),
        ])
        post = _schema_from_tables([
            _make_table("concert_perf", [
                ("Concert_ID", "INTEGER"), ("Name", "TEXT"),
                ("Year", "INTEGER"), ("Singer_ID", "INTEGER"),
            ]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "TABLE_MERGE" in ops
        assert "TABLE_REMOVE" not in ops
        assert "TABLE_ADD" not in ops

        merge_ev = next(e for e in diff.events if e["op"] == "TABLE_MERGE")
        assert sorted(merge_ev["sources"]) == ["concert", "singer_in_concert"]
        assert merge_ev["target"] == "concert_perf"

    def test_merge_with_live_db(self, pre_db, tmp_path):
        """End-to-end: apply TABLE_MERGE via perturbation engine, then verify
        the classifier detects it.
        """
        post_path = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post_path, [
            PerturbationSpec(PerturbationOperator.TABLE_MERGE, {
                "sources": ["concert", "singer_in_concert"],
                "target": "concert_performance",
                "join_key": "Concert_ID",
            }),
        ])
        pre_schema = introspect(pre_db)
        post_schema = introspect(post_path)
        diff = classify(pre_schema, post_schema)
        ops = _ops(diff)
        assert "TABLE_MERGE" in ops
        assert "TABLE_REMOVE" not in ops


# --------------------------------------------------------------------------- #
# COLUMN_MERGE detection                                                      #
# --------------------------------------------------------------------------- #

class TestColumnMergeDetection:
    def test_column_merge_detected(self):
        """2+ columns removed, 1 added in same table → COLUMN_MERGE.

        Uses columns with DISTINCT types so the rename heuristic (which
        pairs by matching type) cannot consume them one-by-one.
        """
        pre = _schema_from_tables([
            _make_table("t", [
                ("id", "INT"), ("lat", "REAL"), ("lon", "REAL"),
                ("alt", "REAL"),
            ]),
        ])
        post = _schema_from_tables([
            _make_table("t", [
                ("id", "INT"), ("coordinates", "TEXT"),
            ]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "COLUMN_MERGE" in ops
        assert "COLUMN_REMOVE" not in ops
        assert "COLUMN_ADD" not in ops

        merge_ev = next(e for e in diff.events if e["op"] == "COLUMN_MERGE")
        assert merge_ev["table"] == "t"
        assert sorted(merge_ev["sources"]) == ["alt", "lat", "lon"]
        assert merge_ev["target"] == "coordinates"

    def test_single_column_drop_add_not_column_merge(self):
        """1 column removed + 1 added → COLUMN_RENAME (if types match) or
        COLUMN_REMOVE + COLUMN_ADD (if types differ), NOT COLUMN_MERGE.
        """
        pre = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("old", "TEXT")]),
        ])
        post = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("new", "TEXT")]),
        ])
        diff = classify(pre, post)
        ops = _ops(diff)
        assert "COLUMN_MERGE" not in ops
        # Same type → should be COLUMN_RENAME.
        assert "COLUMN_RENAME" in ops

    def test_column_merge_with_live_db(self, pre_db, tmp_path):
        """End-to-end: apply COLUMN_MERGE on columns of a DIFFERENT type
        than the target column, so the rename heuristic cannot consume the
        target column.

        Merges Age (INTEGER) and Song_release_year (INTEGER) into a
        single Age_Year (TEXT) column. Rename pairing fails (INT != TEXT),
        so we get 2 removed, 1 added → COLUMN_MERGE.
        """
        post_path = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post_path, [
            PerturbationSpec(PerturbationOperator.COLUMN_MERGE, {
                "table": "singer",
                "sources": ["Age", "Song_release_year"],
                "target": "Age_Year",
                "expression": "Age || '-' || Song_release_year",
                "target_type": "TEXT",
                "reversible": False,
            }),
        ])
        pre_schema = introspect(pre_db)
        post_schema = introspect(post_path)
        diff = classify(pre_schema, post_schema)
        ops = _ops(diff)
        assert "COLUMN_MERGE" in ops


# --------------------------------------------------------------------------- #
# No changes                                                                  #
# --------------------------------------------------------------------------- #

class TestNoChange:
    def test_identical_schemas_no_events(self):
        s = _schema_from_tables([
            _make_table("t", [("id", "INT"), ("val", "TEXT")]),
        ])
        diff = classify(s, s)
        assert not diff.changed
        assert diff.events == []


# --------------------------------------------------------------------------- #
# Mixed operations in one diff                                                #
# --------------------------------------------------------------------------- #

class TestMixedOperations:
    def test_rename_plus_column_rename(self, pre_db, post_db):
        """The existing pilot applies TABLE_RENAME + COLUMN_RENAME.
        Verify the classifier detects all three events.
        """
        pre_schema = introspect(pre_db)
        post_schema = introspect(post_db)
        diff = classify(pre_schema, post_schema)
        ops = _ops(diff)
        assert ops.count("TABLE_RENAME") == 1
        assert ops.count("COLUMN_RENAME") == 2
        assert len(diff.events) == 3

    def test_classifier_matches_perturbation_manifest(self, pre_db, tmp_path):
        """Classifier output for each operator matches the ground-truth
        manifest from the perturbation engine — testing TABLE_SPLIT.
        """
        post_path = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post_path, [
            PerturbationSpec(PerturbationOperator.TABLE_SPLIT, {
                "source": "singer",
                "targets": ["singer_info", "singer_songs"],
                "key": "Singer_ID",
                "partition": {
                    "singer_info": ["Name", "Country", "Age", "Is_male"],
                    "singer_songs": ["Song_Name", "Song_release_year"],
                },
            }),
        ])
        pre_schema = introspect(pre_db)
        post_schema = introspect(post_path)
        diff = classify(pre_schema, post_schema)

        # The op type must match.
        manifest_ops = sorted(e["op"] for e in manifest.events)
        classifier_ops = sorted(e["op"] for e in diff.events)
        assert manifest_ops == classifier_ops
