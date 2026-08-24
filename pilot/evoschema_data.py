"""EvoSchema (BIRD-dev) loading, deterministic sampling, and materialization.

Scale-up brief Module 1. EvoSchema ships perturbation *specs* + post-change
gold SQL as JSON over BIRD-dev; the SQLite databases come from BIRD-dev
itself. This module:

  1. loads the five in-scope operator files and normalises each record to a
     harness item,
  2. draws a deterministic, seed-free stratified sample (first K per db_id
     per operator, sorted by ``train_idx``),
  3. materialises the post-perturbation database by applying the record's
     mapping to a copy of the BIRD-dev database,
  4. gates every item: both gold queries must execute with non-empty
     results, and (arity permitting) produce equal result sets pre/post.

NOTE the brief said "Spider SQLite databases" — the benchmark inventory and
the JSON db_ids confirm EvoSchema perturbs **BIRD-dev** (11 databases), so
BIRD-dev is what we download and use. COLUMN_MERGE merged values use a
single-space separator; this is fixed by EvoSchema's own gold SQL (e.g.
``WHERE full_name = 'Angela Sanders'``) and is verified per item by the
non-empty gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent

EVOSCHEMA_DIR = Path(os.getenv(
    "EVOSCHEMA_DIR",
    str(ROOT.parent / "Benchmarks" / "EvoSchema" / "eval_benchmark"),
))
BIRD_DB_DIR = Path(os.getenv(
    "BIRD_DB_DIR",
    str(ROOT.parent / "Benchmarks" / "bird_dev_download" / "dev_20240627"
        / "dev_databases"),
))

# Operator file → our taxonomy. COLUMN_MERGE uses only the 'merge' subset of
# the split_or_merge file (the 37 column-SPLIT cases are out of scope, as in
# the paper).
OPERATOR_FILES = {
    "TABLE_RENAME": "rename_tables_bird_dev.json",
    "COLUMN_RENAME": "rename_columns_bird_dev.json",
    "TABLE_SPLIT": "split_tables_bird_dev.json",
    "TABLE_MERGE": "merge_tables_bird_dev_revised_finally.json",
    "COLUMN_MERGE": "bird_split_or_merge_columns.json",
}

OP_SHORT = {
    "TABLE_RENAME": "tr",
    "COLUMN_RENAME": "cr",
    "TABLE_SPLIT": "ts",
    "TABLE_MERGE": "tm",
    "COLUMN_MERGE": "cm",
}

COLUMN_MERGE_SEPARATOR = " "

# SQLite column type tokens that terminate a column entry in the EvoSchema
# DDL strings ("<name possibly with spaces> <type> [primary key]").
_TYPE_TOKENS = {"text", "integer", "real", "blob", "numeric", "date",
                "datetime", "int", "float", "boolean", "time", "timestamp"}


@dataclass
class EvoItem:
    uid: str
    op: str
    db_id: str
    train_idx: int
    question: str
    gold_pre: str
    gold_post: str
    spec: dict[str, Any] = field(repr=False)

    @property
    def spec_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.spec, sort_keys=True).encode()
        ).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Loading + sampling                                                          #
# --------------------------------------------------------------------------- #

def load_items(evoschema_dir: Path = EVOSCHEMA_DIR) -> list[EvoItem]:
    items: list[EvoItem] = []
    for op, fname in OPERATOR_FILES.items():
        with (evoschema_dir / fname).open() as f:
            records = json.load(f)
        for rec in records:
            if op == "COLUMN_MERGE" and rec.get("split_or_merge") != "merge":
                continue
            items.append(EvoItem(
                uid=f"evo_{OP_SHORT[op]}_{rec['train_idx']:05d}",
                op=op,
                db_id=rec["db_id"],
                train_idx=int(rec["train_idx"]),
                question=rec["question"],
                gold_pre=rec["query"],
                gold_post=rec["new_gold_sql"],
                spec=rec,
            ))
    return items


def stratified_sample(items: list[EvoItem], per_db: int = 4) -> list[EvoItem]:
    """Deterministic, seed-free rule (recorded in the report):

    For each operator, group items by db_id, sort by ``train_idx`` ascending,
    and take the FIRST ``per_db`` items of each database. COLUMN_MERGE has
    only 47 usable (merge-subset) items across 2 databases — take all of
    them. No randomness anywhere.
    """
    sample: list[EvoItem] = []
    by_op: dict[str, list[EvoItem]] = {}
    for it in items:
        by_op.setdefault(it.op, []).append(it)
    for op, group in by_op.items():
        if op == "COLUMN_MERGE":
            sample.extend(sorted(group, key=lambda i: i.train_idx))
            continue
        by_db: dict[str, list[EvoItem]] = {}
        for it in group:
            by_db.setdefault(it.db_id, []).append(it)
        for db in sorted(by_db):
            picked = sorted(by_db[db], key=lambda i: i.train_idx)[:per_db]
            sample.extend(picked)
    return sorted(sample, key=lambda i: (i.op, i.db_id, i.train_idx))


def pre_db_path(db_id: str, bird_dir: Path = BIRD_DB_DIR) -> Path:
    return bird_dir / db_id / f"{db_id}.sqlite"


# --------------------------------------------------------------------------- #
# DDL / column-list helpers                                                   #
# --------------------------------------------------------------------------- #

def _split_top_level(body: str) -> list[str]:
    """Split a DDL column body on commas outside parentheses."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


_FK_RE = re.compile(r"foreign key\s*\(\s*([^)]+?)\s*\)", re.IGNORECASE)


def _add_col(cols: list[str], seen: set, name: str) -> None:
    name = name.strip()
    if name and name.lower() not in seen:
        seen.add(name.lower())
        cols.append(name)


def parse_ddl_columns(ddl: str) -> list[str]:
    """Ordered data-column names from an EvoSchema DDL string.

    Entries look like ``<name possibly with spaces/parens> <type>`` with an
    optional trailing ``primary key``. EvoSchema's DDL strings often declare
    a column ONLY through a ``foreign key(X) references ...`` entry (e.g.
    ``foreign key(CustomerID) references customers(CustomerID) integer``) —
    the FK's local column X is extracted as a real column; duplicates of an
    already-declared column are dropped. Column names may contain spaces and
    parentheses (BIRD style), e.g. ``Percent (%) Eligible Free (K-12)``.
    """
    m = re.search(r"\(", ddl)
    if not m:
        return []
    body = ddl[m.start() + 1:]
    # Strip the final closing paren of the CREATE TABLE.
    body = body.rstrip()
    if body.endswith(")"):
        body = body[:-1]
    cols: list[str] = []
    seen: set = set()
    for entry in _split_top_level(body):
        low = entry.lower()
        if low.startswith("primary key("):
            continue
        fk = _FK_RE.match(entry)
        if fk:
            _add_col(cols, seen, fk.group(1))
            continue
        tokens = entry.split()
        # Drop trailing "primary key".
        if len(tokens) >= 2 and tokens[-2].lower() == "primary" and \
                tokens[-1].lower() == "key":
            tokens = tokens[:-2]
        if tokens and tokens[-1].lower() in _TYPE_TOKENS:
            tokens = tokens[:-1]
        _add_col(cols, seen, " ".join(tokens))
    return cols


def _columns_from_spec_entry(entry: Any) -> list[str]:
    """Column names from a new_relevant_table value.

    Handles all three shapes seen in the EvoSchema eval files: a raw DDL
    string, ``{'ddl': ...}``, and ``{'columns': [...]}`` (preferred when
    present — some records ship a corrupted ddl alongside a clean columns
    list). ``foreign key(X) references ...`` entries contribute their local
    column X.
    """
    if isinstance(entry, str):
        return parse_ddl_columns(entry)
    if not isinstance(entry, dict):
        return []
    if entry.get("columns"):
        cols: list[str] = []
        seen: set = set()
        for c in entry["columns"]:
            c = str(c)
            fk = _FK_RE.match(c)
            _add_col(cols, seen, fk.group(1) if fk else c)
        return cols
    if entry.get("ddl"):
        return parse_ddl_columns(entry["ddl"])
    return []


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({_q(table)})")]


# --------------------------------------------------------------------------- #
# Materialization                                                             #
# --------------------------------------------------------------------------- #

class MaterializeError(RuntimeError):
    pass


def materialize_post_db(item: EvoItem, out_path: Path,
                        bird_dir: Path = BIRD_DB_DIR) -> Path:
    """Copy the BIRD pre-DB and apply the item's perturbation spec."""
    src = pre_db_path(item.db_id, bird_dir)
    if not src.exists():
        raise MaterializeError(f"BIRD database missing: {src}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out_path)
    con = sqlite3.connect(str(out_path))
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        if item.op == "TABLE_RENAME":
            _apply_table_rename(con, item.spec)
        elif item.op == "COLUMN_RENAME":
            _apply_column_rename(con, item.spec)
        elif item.op == "TABLE_SPLIT":
            _apply_table_split(con, item.spec)
        elif item.op == "TABLE_MERGE":
            _apply_table_merge(con, item.spec)
        elif item.op == "COLUMN_MERGE":
            _apply_column_merge(con, item.spec)
        else:
            raise MaterializeError(f"Unsupported operator {item.op}")
        con.commit()
    finally:
        con.close()
    return out_path


def _apply_table_rename(con: sqlite3.Connection, spec: dict[str, Any]) -> None:
    for old, new in spec["old_new_table_mappings"].items():
        con.execute(f"ALTER TABLE {_q(old)} RENAME TO {_q(new)}")


def _apply_column_rename(con: sqlite3.Connection, spec: dict[str, Any]) -> None:
    """Positional old→new column pairing from the pre/post DDL strings."""
    for table, old_entry in spec["relevant_table"].items():
        new_entry = spec["new_relevant_table"].get(table)
        if new_entry is None:
            raise MaterializeError(f"no post DDL for table {table}")
        old_cols = _columns_from_spec_entry(old_entry)
        new_cols = _columns_from_spec_entry(new_entry)
        if len(old_cols) != len(new_cols):
            raise MaterializeError(
                f"column count mismatch for {table}: "
                f"{len(old_cols)} pre vs {len(new_cols)} post"
            )
        actual = {c.lower() for c in _table_columns(con, table)}
        for old, new in zip(old_cols, new_cols):
            if old == new:
                continue
            if old.lower() not in actual:
                raise MaterializeError(
                    f"column {old!r} not present in {table}")
            con.execute(
                f"ALTER TABLE {_q(table)} RENAME COLUMN {_q(old)} TO {_q(new)}"
            )


def _apply_table_split(con: sqlite3.Connection, spec: dict[str, Any]) -> None:
    for old_table, new_tables in spec["old_new_table_column_mapping"].items():
        src_cols = {c.lower(): c for c in _table_columns(con, old_table)}
        for new_table in new_tables:
            entry = spec["new_relevant_table"].get(new_table)
            if entry is None:
                raise MaterializeError(f"no spec for split table {new_table}")
            cols = _columns_from_spec_entry(entry)
            missing = [c for c in cols if c.lower() not in src_cols]
            if missing:
                raise MaterializeError(
                    f"split table {new_table} wants columns not in "
                    f"{old_table}: {missing[:3]}"
                )
            select = ", ".join(_q(src_cols[c.lower()]) for c in cols)
            con.execute(
                f"CREATE TABLE {_q(new_table)} AS "
                f"SELECT {select} FROM {_q(old_table)}"
            )
        con.execute(f"DROP TABLE {_q(old_table)}")


def _join_condition_from_gold(gold_pre: str, t1: str, t2: str) -> Optional[tuple[str, str]]:
    """Extract (t1_col, t2_col) join columns for t1/t2 from the pre gold SQL."""
    # Build alias → table map (T1/T2 style and bare names).
    alias_map: dict[str, str] = {}
    for m in re.finditer(
        r"(?:FROM|JOIN)\s+`?(\w+)`?(?:\s+AS\s+(\w+)|\s+(\w+))?",
        gold_pre, re.IGNORECASE,
    ):
        tbl, a1, a2 = m.group(1), m.group(2), m.group(3)
        alias = a1 or a2
        alias_map[tbl.lower()] = tbl.lower()
        if alias and alias.upper() not in ("ON", "WHERE", "INNER", "LEFT",
                                           "JOIN", "GROUP", "ORDER"):
            alias_map[alias.lower()] = tbl.lower()
    for m in re.finditer(
        r"ON\s+(\w+)\.`?([\w ()%/-]+?)`?\s*=\s*(\w+)\.`?([\w ()%/-]+?)`?(?:\s|$)",
        gold_pre, re.IGNORECASE,
    ):
        a, ca, b, cb = m.groups()
        ta, tb = alias_map.get(a.lower()), alias_map.get(b.lower())
        if ta == t1.lower() and tb == t2.lower():
            return ca, cb
        if ta == t2.lower() and tb == t1.lower():
            return cb, ca
    return None


def _apply_table_merge(con: sqlite3.Connection, spec: dict[str, Any]) -> None:
    mapping = spec["old_new_table_mapping"]
    merged_name = next(iter(mapping.values()))
    old_tables = list(mapping.keys())
    if len(old_tables) != 2:
        raise MaterializeError(
            f"expected 2 source tables, got {old_tables}")
    t1, t2 = old_tables
    entry = spec["new_relevant_table"].get(merged_name)
    if entry is None:
        raise MaterializeError(f"no spec for merged table {merged_name}")
    merged_cols = _columns_from_spec_entry(entry)

    dedup = spec.get(
        "deduplicate_new_column_to_old_table_old_table_mapping") or {}
    cols1 = {c.lower(): c for c in _table_columns(con, t1)}
    cols2 = {c.lower(): c for c in _table_columns(con, t2)}

    # Join condition: prefer the pre gold SQL's ON clause; fall back to the
    # declared primary key when both tables carry it; then to any shared
    # column name.
    join = _join_condition_from_gold(spec.get("query", ""), t1, t2)
    if join is None:
        pk = str(spec.get("new_table_primary_key", "")).strip().strip("'\"")
        if pk and pk.lower() in cols1 and pk.lower() in cols2:
            join = (cols1[pk.lower()], cols2[pk.lower()])
        else:
            shared = [c for c in cols1 if c in cols2]
            if len(shared) == 1:
                join = (cols1[shared[0]], cols2[shared[0]])
    if join is None:
        raise MaterializeError(
            f"cannot determine join key for {t1} x {t2}")
    j1, j2 = join

    select_parts: list[str] = []
    for col in merged_cols:
        if col in dedup:
            src = dedup[col]
            select_parts.append(
                f"{_q('__t' + ('1' if src['old_table'] == t1 else '2'))}."
                f"{_q(src['old_column'])} AS {_q(col)}"
            )
        elif col.lower() in cols1:
            select_parts.append(f'"__t1".{_q(cols1[col.lower()])}')
        elif col.lower() in cols2:
            select_parts.append(f'"__t2".{_q(cols2[col.lower()])}')
        elif col.lower().endswith("_" + t1.lower()) and \
                col[: -len(t1) - 1].lower() in cols1:
            # Dedup-by-suffix convention: <column>_<source_table>.
            select_parts.append(
                f'"__t1".{_q(cols1[col[: -len(t1) - 1].lower()])} AS {_q(col)}')
        elif col.lower().endswith("_" + t2.lower()) and \
                col[: -len(t2) - 1].lower() in cols2:
            select_parts.append(
                f'"__t2".{_q(cols2[col[: -len(t2) - 1].lower()])} AS {_q(col)}')
        else:
            raise MaterializeError(
                f"merged column {col!r} not found in {t1} or {t2}")

    con.execute(
        f"CREATE TABLE {_q(merged_name)} AS SELECT "
        + ", ".join(select_parts)
        + f' FROM {_q(t1)} AS "__t1" JOIN {_q(t2)} AS "__t2" '
        + f'ON "__t1".{_q(j1)} = "__t2".{_q(j2)}'
    )
    con.execute(f"DROP TABLE {_q(t1)}")
    con.execute(f"DROP TABLE {_q(t2)}")


def _derive_column_merge_changes(spec: dict[str, Any]) -> tuple[str, dict[str, list[str]]]:
    """(table, {merged_col: [source_cols]}) from the pre/post column diff.

    Most merge-subset records ship an EMPTY ``column_changes``; the mapping
    is recoverable by diffing relevant_table vs new_relevant_table: the
    removed columns are the merge sources (kept in pre-DDL order, which
    fixes the concatenation order), the single added column is the target.
    """
    rt, nrt = spec.get("relevant_table"), spec.get("new_relevant_table")
    if not isinstance(rt, dict) or not isinstance(nrt, dict):
        raise MaterializeError("malformed COLUMN_MERGE spec (no table dicts)")
    for table, entry in nrt.items():
        old = _columns_from_spec_entry(rt.get(table, {}))
        new = _columns_from_spec_entry(entry)
        new_set = {c.lower() for c in new}
        old_set = {c.lower() for c in old}
        removed = [c for c in old if c.lower() not in new_set]
        added = [c for c in new if c.lower() not in old_set]
        if len(added) == 1 and len(removed) >= 2:
            return table, {added[0]: removed}
    raise MaterializeError("cannot derive column merge mapping from diff")


def _apply_column_merge(con: sqlite3.Connection, spec: dict[str, Any]) -> None:
    changes = spec["column_changes"]  # {merged_col: [source_cols]}
    target_table = None
    if isinstance(changes, dict) and changes:
        # The affected table is the one whose post DDL contains the merged
        # column.
        nrt = spec.get("new_relevant_table")
        if not isinstance(nrt, dict):
            raise MaterializeError("malformed COLUMN_MERGE spec")
        for table, entry in nrt.items():
            post_cols = {c.lower() for c in _columns_from_spec_entry(entry)}
            if all(str(new).lower() in post_cols for new in changes):
                target_table = table
                break
    else:
        target_table, changes = _derive_column_merge_changes(spec)
    if target_table is None:
        raise MaterializeError("cannot locate table for column merge")

    src_cols = {c.lower(): c for c in _table_columns(con, target_table)}
    for new_col, sources in changes.items():
        missing = [s for s in sources if s.lower() not in src_cols]
        if missing:
            raise MaterializeError(
                f"merge sources missing from {target_table}: {missing}")
        con.execute(
            f"ALTER TABLE {_q(target_table)} ADD COLUMN {_q(new_col)} text")
        concat = f" || '{COLUMN_MERGE_SEPARATOR}' || ".join(
            f"COALESCE(CAST({_q(src_cols[s.lower()])} AS TEXT), '')"
            for s in sources
        )
        con.execute(
            f"UPDATE {_q(target_table)} SET {_q(new_col)} = {concat}")
        for s in sources:
            con.execute(
                f"ALTER TABLE {_q(target_table)} DROP COLUMN "
                f"{_q(src_cols[s.lower()])}"
            )


# --------------------------------------------------------------------------- #
# Gold gate                                                                   #
# --------------------------------------------------------------------------- #

def _run_sql(db: str, sql: str) -> tuple[bool, Any]:
    try:
        con = sqlite3.connect(db)
        try:
            rows = con.execute(sql).fetchall()
        finally:
            con.close()
        return True, rows
    except Exception as exc:
        return False, str(exc)


def gold_gate(item: EvoItem, pre_db: str, post_db: str) -> tuple[bool, str]:
    """(passes, reason). Both golds must execute with non-empty results;
    result sets must match pre/post except for COLUMN_MERGE (the merged
    column changes arity by design — row-count equality is required there).
    """
    ok_pre, pre_rows = _run_sql(pre_db, item.gold_pre)
    if not ok_pre:
        return False, f"gold_pre execution error: {pre_rows}"
    if not pre_rows:
        return False, "gold_pre empty result"
    ok_post, post_rows = _run_sql(post_db, item.gold_post)
    if not ok_post:
        return False, f"gold_post execution error: {post_rows}"
    if not post_rows:
        return False, "gold_post empty result"
    if item.op == "COLUMN_MERGE":
        if len(pre_rows) != len(post_rows):
            return False, (
                f"row count mismatch: {len(pre_rows)} pre vs "
                f"{len(post_rows)} post"
            )
        return True, ""
    sa = sorted(map(repr, pre_rows))
    sb = sorted(map(repr, post_rows))
    if sa != sb:
        return False, "result-set mismatch pre vs post"
    return True, ""
