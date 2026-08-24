"""Tests for the perturbation engine (data/perturbations.py).

Verifies that each of the five EvoSchema operators:
  1. Produces a valid SQLite post-DB (PRAGMA integrity_check = ok).
  2. Generates a correct manifest entry matching classifier expectations.
  3. Preserves data through the transformation.
  4. Stamps manifests with engine version and content hash.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data.perturbations import (
    ENGINE_VERSION,
    PerturbationManifest,
    PerturbationOperator,
    PerturbationSpec,
    apply_perturbations,
)
from tests.conftest import make_simple_db


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _tables(db_path: str) -> set[str]:
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    con.close()
    return {r[0] for r in rows}


def _columns(db_path: str, table: str) -> list[str]:
    con = sqlite3.connect(db_path)
    rows = con.execute(f"PRAGMA table_info([{table}])").fetchall()
    con.close()
    return [r[1] for r in rows]


def _row_count(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    n = con.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    con.close()
    return n


def _query(db_path: str, sql: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    rows = con.execute(sql).fetchall()
    con.close()
    return rows


# --------------------------------------------------------------------------- #
# TABLE_RENAME                                                                #
# --------------------------------------------------------------------------- #

class TestTableRename:
    def test_produces_valid_db(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        assert "artist" in _tables(post)
        assert "singer" not in _tables(post)

    def test_manifest_event(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        assert len(manifest.events) == 1
        ev = manifest.events[0]
        assert ev["op"] == "TABLE_RENAME"
        assert ev["from"] == "singer"
        assert ev["to"] == "artist"

    def test_preserves_data(self, pre_db, tmp_path):
        pre_count = _row_count(pre_db, "singer")
        post = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        assert _row_count(post, "artist") == pre_count

    def test_preserves_columns(self, pre_db, tmp_path):
        pre_cols = _columns(pre_db, "singer")
        post = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        assert _columns(post, "artist") == pre_cols


# --------------------------------------------------------------------------- #
# COLUMN_RENAME                                                               #
# --------------------------------------------------------------------------- #

class TestColumnRename:
    def test_produces_valid_db(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.COLUMN_RENAME,
                             {"table": "singer", "from": "Song_Name",
                              "to": "song_title"}),
        ])
        cols = _columns(post, "singer")
        assert "song_title" in cols
        assert "Song_Name" not in cols

    def test_preserves_data(self, pre_db, tmp_path):
        pre_data = _query(pre_db, "SELECT Song_Name FROM singer ORDER BY Singer_ID")
        post = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.COLUMN_RENAME,
                             {"table": "singer", "from": "Song_Name",
                              "to": "song_title"}),
        ])
        post_data = _query(post, "SELECT song_title FROM singer ORDER BY Singer_ID")
        assert pre_data == post_data


# --------------------------------------------------------------------------- #
# TABLE_SPLIT                                                                 #
# --------------------------------------------------------------------------- #

class TestTableSplit:
    @pytest.fixture
    def split_post(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
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
        return post, manifest

    def test_produces_valid_db(self, split_post):
        post, _ = split_post
        assert "singer" not in _tables(post)
        assert "singer_info" in _tables(post)
        assert "singer_songs" in _tables(post)

    def test_manifest_event(self, split_post):
        _, manifest = split_post
        assert len(manifest.events) == 1
        ev = manifest.events[0]
        assert ev["op"] == "TABLE_SPLIT"
        assert ev["source"] == "singer"
        assert sorted(ev["targets"]) == ["singer_info", "singer_songs"]
        assert ev["key"] == "Singer_ID"

    def test_key_column_in_both_tables(self, split_post):
        post, _ = split_post
        assert "Singer_ID" in _columns(post, "singer_info")
        assert "Singer_ID" in _columns(post, "singer_songs")

    def test_preserves_data_via_join(self, split_post, pre_db):
        """After a split, an INNER JOIN on the shared key must reconstruct
        the original data (1:1 vertical partition)."""
        post, _ = split_post
        pre_data = sorted(_query(
            pre_db,
            "SELECT Singer_ID, Name, Song_Name FROM singer ORDER BY Singer_ID"
        ))
        post_data = sorted(_query(
            post,
            "SELECT T1.Singer_ID, T1.Name, T2.Song_Name "
            "FROM singer_info T1 "
            "JOIN singer_songs T2 ON T1.Singer_ID = T2.Singer_ID "
            "ORDER BY T1.Singer_ID"
        ))
        assert pre_data == post_data

    def test_each_target_has_correct_columns(self, split_post):
        post, _ = split_post
        info_cols = _columns(post, "singer_info")
        assert set(info_cols) == {"Singer_ID", "Name", "Country", "Age", "Is_male"}
        songs_cols = _columns(post, "singer_songs")
        assert set(songs_cols) == {"Singer_ID", "Song_Name", "Song_release_year"}


# --------------------------------------------------------------------------- #
# TABLE_MERGE                                                                 #
# --------------------------------------------------------------------------- #

class TestTableMerge:
    @pytest.fixture
    def merge_post(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_MERGE, {
                "sources": ["concert", "singer_in_concert"],
                "target": "concert_performance",
                "join_key": "Concert_ID",
            }),
        ])
        return post, manifest

    def test_produces_valid_db(self, merge_post):
        post, _ = merge_post
        assert "concert" not in _tables(post)
        assert "singer_in_concert" not in _tables(post)
        assert "concert_performance" in _tables(post)

    def test_manifest_event(self, merge_post):
        _, manifest = merge_post
        assert len(manifest.events) == 1
        ev = manifest.events[0]
        assert ev["op"] == "TABLE_MERGE"
        assert sorted(ev["sources"]) == ["concert", "singer_in_concert"]
        assert ev["target"] == "concert_performance"
        assert ev["key"] == "Concert_ID"

    def test_merged_table_has_all_columns(self, merge_post):
        post, _ = merge_post
        cols = set(_columns(post, "concert_performance"))
        # concert has: Concert_ID, concert_Name, Theme, Stadium_ID, Year
        # singer_in_concert has: Concert_ID, Singer_ID
        # merged (dedup key): Concert_ID, concert_Name, Theme, Stadium_ID, Year, Singer_ID
        expected = {"Concert_ID", "concert_Name", "Theme", "Stadium_ID", "Year", "Singer_ID"}
        assert cols == expected

    def test_preserves_data(self, merge_post, pre_db):
        post, _ = merge_post
        # The merged table should have one row per (concert, singer) pair.
        pre_pairs = sorted(_query(
            pre_db,
            "SELECT c.Concert_ID, c.concert_Name, sc.Singer_ID "
            "FROM concert c "
            "JOIN singer_in_concert sc ON c.Concert_ID = sc.Concert_ID"
        ))
        post_rows = sorted(_query(
            post,
            "SELECT Concert_ID, concert_Name, Singer_ID FROM concert_performance"
        ))
        assert pre_pairs == post_rows


# --------------------------------------------------------------------------- #
# COLUMN_MERGE                                                                #
# --------------------------------------------------------------------------- #

class TestColumnMerge:
    @pytest.fixture
    def merge_col_post(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.COLUMN_MERGE, {
                "table": "singer",
                "sources": ["Name", "Country"],
                "target": "Name_Country",
                "expression": "Name || ' - ' || Country",
                "target_type": "TEXT",
                "reversible": False,
            }),
        ])
        return post, manifest

    def test_produces_valid_db(self, merge_col_post):
        post, _ = merge_col_post
        cols = _columns(post, "singer")
        assert "Name_Country" in cols
        assert "Name" not in cols
        assert "Country" not in cols

    def test_manifest_event(self, merge_col_post):
        _, manifest = merge_col_post
        ev = manifest.events[0]
        assert ev["op"] == "COLUMN_MERGE"
        assert ev["table"] == "singer"
        assert sorted(ev["sources"]) == ["Country", "Name"]
        assert ev["target"] == "Name_Country"
        assert ev["reversible"] is False

    def test_expression_evaluated_correctly(self, merge_col_post, pre_db):
        post, _ = merge_col_post
        pre_data = _query(
            pre_db,
            "SELECT Name || ' - ' || Country FROM singer ORDER BY Singer_ID"
        )
        post_data = _query(
            post,
            "SELECT Name_Country FROM singer ORDER BY Singer_ID"
        )
        assert pre_data == post_data

    def test_other_columns_preserved(self, merge_col_post, pre_db):
        post, _ = merge_col_post
        pre_ages = _query(pre_db, "SELECT Age FROM singer ORDER BY Singer_ID")
        post_ages = _query(post, "SELECT Age FROM singer ORDER BY Singer_ID")
        assert pre_ages == post_ages


# --------------------------------------------------------------------------- #
# Manifest versioning                                                         #
# --------------------------------------------------------------------------- #

class TestManifest:
    def test_has_engine_version(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        assert manifest.engine_version == ENGINE_VERSION

    def test_has_content_hash(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        assert len(manifest.content_hash) == 16
        assert all(c in "0123456789abcdef" for c in manifest.content_hash)

    def test_same_inputs_same_hash(self, pre_db, tmp_path):
        """Determinism: same pre-DB + same specs → same content hash."""
        specs = [PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                                  {"from": "singer", "to": "artist"})]
        m1 = apply_perturbations(pre_db, str(tmp_path / "p1.sqlite"), specs)
        m2 = apply_perturbations(pre_db, str(tmp_path / "p2.sqlite"), specs)
        assert m1.content_hash == m2.content_hash

    def test_to_dict_roundtrip(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
        ])
        d = manifest.to_dict()
        assert d["engine_version"] == ENGINE_VERSION
        assert isinstance(d["events"], list)
        assert isinstance(d["specs"], list)


# --------------------------------------------------------------------------- #
# Multiple perturbations                                                      #
# --------------------------------------------------------------------------- #

class TestMultiplePerturbations:
    def test_rename_then_column_rename(self, pre_db, tmp_path):
        """Reproduces the existing pilot setup: TABLE_RENAME + COLUMN_RENAME."""
        post = str(tmp_path / "post.sqlite")
        manifest = apply_perturbations(pre_db, post, [
            PerturbationSpec(PerturbationOperator.TABLE_RENAME,
                             {"from": "singer", "to": "artist"}),
            PerturbationSpec(PerturbationOperator.COLUMN_RENAME,
                             {"table": "artist", "from": "Song_Name",
                              "to": "song_title"}),
            PerturbationSpec(PerturbationOperator.COLUMN_RENAME,
                             {"table": "artist", "from": "Song_release_year",
                              "to": "debut_year"}),
        ])
        assert len(manifest.events) == 3
        assert "artist" in _tables(post)
        assert "singer" not in _tables(post)
        cols = _columns(post, "artist")
        assert "song_title" in cols
        assert "debut_year" in cols
        assert "Song_Name" not in cols
        assert "Song_release_year" not in cols

    def test_post_db_passes_integrity_check(self, pre_db, tmp_path):
        post = str(tmp_path / "post.sqlite")
        apply_perturbations(pre_db, post, [
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
        con = sqlite3.connect(post)
        result = con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        assert result[0] == "ok"
