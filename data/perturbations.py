"""Programmatic perturbation engine for EvoSchema operators.

Supports all five operators from the manifest:
  TABLE_RENAME, TABLE_SPLIT, TABLE_MERGE, COLUMN_RENAME, COLUMN_MERGE.

Each perturbation transforms a pre-schema SQLite database into a post-schema
database and produces a ground-truth manifest entry for diff-classifier scoring.

Every manifest is stamped with the engine version and a content hash
(SHA-256 of pre-DB bytes + spec JSON) so a results run can be tied back to
the exact fixture that produced it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


ENGINE_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Spec types                                                                  #
# --------------------------------------------------------------------------- #

class PerturbationOperator(Enum):
    TABLE_RENAME = "TABLE_RENAME"
    TABLE_SPLIT = "TABLE_SPLIT"
    TABLE_MERGE = "TABLE_MERGE"
    COLUMN_RENAME = "COLUMN_RENAME"
    COLUMN_MERGE = "COLUMN_MERGE"


@dataclass
class PerturbationSpec:
    """One perturbation to apply.

    ``params`` is operator-specific:

    TABLE_RENAME::

        {"from": "singer", "to": "artist"}

    TABLE_SPLIT::

        {"source": "singer",
         "targets": ["singer_info", "singer_songs"],
         "key": "Singer_ID",
         "partition": {"singer_info": ["Name", "Country", "Age", "Is_male"],
                       "singer_songs": ["Song_Name", "Song_release_year"]}}

    TABLE_MERGE::

        {"sources": ["concert", "singer_in_concert"],
         "target": "concert_performance",
         "join_key": "Concert_ID"}

    COLUMN_RENAME::

        {"table": "singer", "from": "Song_Name", "to": "song_title"}

    COLUMN_MERGE::

        {"table": "singer",
         "sources": ["Name", "Country"],
         "target": "Name_Country",
         "expression": "Name || ' - ' || Country",
         "target_type": "TEXT",
         "reversible": false}
    """

    operator: PerturbationOperator
    params: dict[str, Any]


@dataclass
class PerturbationManifest:
    """Ground-truth manifest for classifier scoring.

    ``events`` mirrors the format emitted by ``fingerprint.classify()`` so
    the two can be compared directly.
    """

    engine_version: str
    content_hash: str
    specs: list[dict[str, Any]]
    events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "content_hash": self.content_hash,
            "specs": self.specs,
            "events": self.events,
        }


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _content_hash(pre_db_path: str, specs: list[PerturbationSpec]) -> str:
    """SHA-256 of pre-DB bytes + canonical spec JSON (first 16 hex chars)."""
    h = hashlib.sha256()
    with open(pre_db_path, "rb") as f:
        h.update(f.read())
    spec_json = json.dumps(
        [{"op": s.operator.value, **s.params} for s in specs],
        sort_keys=True,
    )
    h.update(spec_json.encode())
    return h.hexdigest()[:16]


def _get_table_info(con: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """Return ``[(col_name, col_type), ...]`` for *table*."""
    rows = con.execute(f"PRAGMA table_info([{table}])").fetchall()
    return [(r[1], r[2] or "") for r in rows]


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Operator implementations                                                    #
# --------------------------------------------------------------------------- #

def apply_table_rename(con: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a table.  ``ALTER TABLE … RENAME TO …`` (SQLite ≥ 3.25)."""
    old, new = params["from"], params["to"]
    con.execute(f"ALTER TABLE [{old}] RENAME TO [{new}]")
    con.commit()
    return {"op": "TABLE_RENAME", "from": old, "to": new}


def apply_column_rename(con: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    """Rename a column.  ``ALTER TABLE … RENAME COLUMN …`` (SQLite ≥ 3.25)."""
    table, old, new = params["table"], params["from"], params["to"]
    con.execute(f"ALTER TABLE [{table}] RENAME COLUMN [{old}] TO [{new}]")
    con.commit()
    return {"op": "COLUMN_RENAME", "table": table, "from": old, "to": new}


def apply_table_split(con: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    """Split one table into two by vertical column partition with a shared key.

    The key column appears in **both** target tables. The partition dict maps
    each target table name to its non-key columns. The union of all partition
    columns should equal the source table's non-key columns.
    """
    source = params["source"]
    targets = params["targets"]
    key = params["key"]
    partition = params["partition"]

    # Read source column types.
    info = _get_table_info(con, source)
    col_types: dict[str, str] = {name: typ for name, typ in info}

    for tgt in targets:
        tgt_cols = partition[tgt]
        col_defs = [f"[{key}] {col_types[key]}"]
        for col in tgt_cols:
            col_defs.append(f"[{col}] {col_types[col]}")
        con.execute(f"CREATE TABLE [{tgt}] ({', '.join(col_defs)})")

        all_cols = [key] + tgt_cols
        cols_str = ", ".join(f"[{c}]" for c in all_cols)
        con.execute(
            f"INSERT INTO [{tgt}] ({cols_str}) SELECT {cols_str} FROM [{source}]"
        )

    con.execute(f"DROP TABLE [{source}]")
    con.commit()

    return {
        "op": "TABLE_SPLIT",
        "source": source,
        "targets": sorted(targets),
        "key": key,
        "partition": partition,
    }


def apply_table_merge(con: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    """Merge two tables via JOIN into one denormalised table.

    The join key appears once in the target table. Rows are the cross-product
    of matching keys (INNER JOIN), so the target may have more rows than
    either source when the join is 1:N.
    """
    sources = params["sources"]
    target = params["target"]
    join_key = params["join_key"]

    # Collect columns from both sources, deduplicating the join key.
    seen_lower: set[str] = set()
    all_cols: list[tuple[str, str, str]] = []  # (name, type, source_table)
    for src in sources:
        for name, typ in _get_table_info(con, src):
            if name.lower() not in seen_lower:
                all_cols.append((name, typ, src))
                seen_lower.add(name.lower())

    # CREATE target.
    col_defs = ", ".join(f"[{n}] {t}" for n, t, _ in all_cols)
    con.execute(f"CREATE TABLE [{target}] ({col_defs})")

    # INSERT … SELECT … JOIN.
    select_parts = [f"[{src}].[{n}]" for n, _, src in all_cols]
    select_str = ", ".join(select_parts)
    join_sql = (
        f"INSERT INTO [{target}] SELECT {select_str} "
        f"FROM [{sources[0]}] "
        f"JOIN [{sources[1]}] "
        f"ON [{sources[0]}].[{join_key}] = [{sources[1]}].[{join_key}]"
    )
    con.execute(join_sql)

    for src in sources:
        con.execute(f"DROP TABLE [{src}]")
    con.commit()

    return {
        "op": "TABLE_MERGE",
        "sources": sorted(sources),
        "target": target,
        "key": join_key,
    }


def apply_column_merge(con: sqlite3.Connection, params: dict[str, Any]) -> dict[str, Any]:
    """Merge two or more columns into one via an SQL expression.

    Recreates the table (no ``ALTER TABLE DROP COLUMN`` in older SQLite).
    The ``reversible`` flag records whether the merge preserves enough
    information for a lossless reverse split — concatenation-style merges
    (``Name || ' - ' || Country``) are typically **not** reversible.
    """
    table = params["table"]
    sources = params["sources"]
    target = params["target"]
    expression = params["expression"]
    target_type = params.get("target_type", "TEXT")
    reversible = params.get("reversible", False)

    info = _get_table_info(con, table)
    source_set = {s.lower() for s in sources}

    # Build new column list: keep non-source columns, append merged column.
    new_cols: list[tuple[str, str]] = []
    for name, typ in info:
        if name.lower() not in source_set:
            new_cols.append((name, typ))
    new_cols.append((target, target_type))

    # Create temp table.
    tmp = f"__{table}_merge_tmp"
    col_defs = ", ".join(f"[{n}] {t}" for n, t in new_cols)
    con.execute(f"CREATE TABLE [{tmp}] ({col_defs})")

    # Populate: non-source columns + expression.
    select_parts: list[str] = []
    for name, _ in info:
        if name.lower() not in source_set:
            select_parts.append(f"[{name}]")
    select_parts.append(f"({expression}) AS [{target}]")
    select_str = ", ".join(select_parts)
    con.execute(f"INSERT INTO [{tmp}] SELECT {select_str} FROM [{table}]")

    # Swap.
    con.execute(f"DROP TABLE [{table}]")
    con.execute(f"ALTER TABLE [{tmp}] RENAME TO [{table}]")
    con.commit()

    return {
        "op": "COLUMN_MERGE",
        "table": table,
        "sources": sorted(sources),
        "target": target,
        "expression": expression,
        "reversible": reversible,
    }


# --------------------------------------------------------------------------- #
# Dispatch + public entry point                                               #
# --------------------------------------------------------------------------- #

_APPLY_FNS = {
    PerturbationOperator.TABLE_RENAME:  apply_table_rename,
    PerturbationOperator.TABLE_SPLIT:   apply_table_split,
    PerturbationOperator.TABLE_MERGE:   apply_table_merge,
    PerturbationOperator.COLUMN_RENAME: apply_column_rename,
    PerturbationOperator.COLUMN_MERGE:  apply_column_merge,
}


def apply_perturbations(
    pre_db_path: str,
    post_db_path: str,
    specs: list[PerturbationSpec],
) -> PerturbationManifest:
    """Copy *pre_db_path* to *post_db_path*, apply all *specs* in order,
    and return a :class:`PerturbationManifest` recording ground-truth events.

    Each manifest is stamped with :data:`ENGINE_VERSION` and a content hash
    so that results runs can be tied back to the exact fixture that produced
    them (reproducibility across multi-day experiment runs).
    """
    chash = _content_hash(pre_db_path, specs)
    shutil.copyfile(pre_db_path, post_db_path)

    events: list[dict[str, Any]] = []
    con = sqlite3.connect(post_db_path)
    try:
        for spec in specs:
            fn = _APPLY_FNS[spec.operator]
            event = fn(con, spec.params)
            events.append(event)
    finally:
        con.close()

    # Verify post-DB integrity.
    con = sqlite3.connect(post_db_path)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        assert result and result[0] == "ok", (
            f"Post-DB integrity check failed: {result}"
        )
    finally:
        con.close()

    return PerturbationManifest(
        engine_version=ENGINE_VERSION,
        content_hash=chash,
        specs=[{"op": s.operator.value, **s.params} for s in specs],
        events=events,
    )
