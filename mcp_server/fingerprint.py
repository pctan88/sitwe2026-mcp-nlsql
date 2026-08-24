"""schema/fingerprint Resource — canonical schema hashing and diff classification.

Implements the formula from SHARED_NOTES.md §2.4:

    F(S) = H( ⊕_{T in S} [ name(T) || ⊕_{C in T} ( name(C) || type(C) ) ] )

where H is BLAKE3 and ⊕ is order-independent canonical concatenation
(sorted by table name, then by column name).

Diff classification covers the five EvoSchema operator types this research
recognises: TABLE_RENAME, TABLE_SPLIT, TABLE_MERGE, COLUMN_RENAME, COLUMN_MERGE.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

try:
    from blake3 import blake3 as _blake3  # type: ignore
    def _hash(data: bytes) -> str:
        return _blake3(data).hexdigest()
except Exception:  # pragma: no cover - fallback for environments without blake3
    import hashlib
    def _hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Schema introspection                                                        #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Column:
    name: str
    type: str

    def canonical(self) -> bytes:
        return f"{self.name.lower()}|{self.type.lower()}".encode()


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)

    def canonical(self) -> bytes:
        cols = sorted(self.columns, key=lambda c: c.name.lower())
        col_bytes = b"||".join(c.canonical() for c in cols)
        return self.name.lower().encode() + b":" + col_bytes


@dataclass
class Schema:
    tables: list[Table] = field(default_factory=list)

    def canonical(self) -> bytes:
        tables = sorted(self.tables, key=lambda t: t.name.lower())
        return b"$$".join(t.canonical() for t in tables)

    def fingerprint(self) -> str:
        return _hash(self.canonical())

    def table_names(self) -> set[str]:
        return {t.name for t in self.tables}

    def column_map(self) -> dict[str, dict[str, str]]:
        return {t.name: {c.name: c.type for c in t.columns} for t in self.tables}


def introspect(db_path: str) -> Schema:
    """Read a SQLite DB and return its Schema."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        tables: list[Table] = []
        rows = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (tname,) in rows:
            # Quote the identifier: table names may collide with SQL
            # keywords (e.g. BIRD financial's ``order`` table).
            quoted = '"' + tname.replace('"', '""') + '"'
            cols = cur.execute(f"PRAGMA table_info({quoted})").fetchall()
            tables.append(
                Table(
                    name=tname,
                    columns=[Column(name=c[1], type=(c[2] or "")) for c in cols],
                )
            )
        return Schema(tables=tables)
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Diff classification                                                         #
# --------------------------------------------------------------------------- #

@dataclass
class SchemaDiff:
    """A classified diff between two Schema objects.

    Each entry in ``events`` is a dict of the form
    ``{"op": "TABLE_RENAME", "from": "singer", "to": "artist"}`` or
    ``{"op": "COLUMN_RENAME", "table": "artist",
       "from": "Song_Name", "to": "song_title"}``.
    """

    pre_fingerprint: str
    post_fingerprint: str
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.pre_fingerprint != self.post_fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "pre_fingerprint": self.pre_fingerprint,
            "post_fingerprint": self.post_fingerprint,
            "changed": self.changed,
            "events": self.events,
        }


def _column_set(t: Table) -> set[tuple[str, str]]:
    return {(c.name.lower(), c.type.lower()) for c in t.columns}


def classify(pre: Schema, post: Schema) -> SchemaDiff:
    """Compare two schemas and emit a list of classified events."""
    diff = SchemaDiff(
        pre_fingerprint=pre.fingerprint(),
        post_fingerprint=post.fingerprint(),
    )
    if not diff.changed:
        return diff

    pre_tables = {t.name: t for t in pre.tables}
    post_tables = {t.name: t for t in post.tables}

    removed = set(pre_tables) - set(post_tables)
    added = set(post_tables) - set(pre_tables)
    common = set(pre_tables) & set(post_tables)

    # ----- TABLE_RENAME: heuristic = removed table whose column-set is identical
    # to an added table's column-set (case-insensitive).
    matched_added: set[str] = set()
    for r in list(removed):
        r_cols = _column_set(pre_tables[r])
        candidates = [a for a in added - matched_added
                      if _column_set(post_tables[a]) == r_cols]
        if len(candidates) == 1:
            a = candidates[0]
            diff.events.append({"op": "TABLE_RENAME", "from": r, "to": a})
            removed.discard(r)
            matched_added.add(a)

    # ----- TABLE_RENAME + COLUMN_RENAME (combined): same column count and
    # the same type multiset, but some column names differ. We pair only the
    # columns whose names differ, leaving unchanged columns alone.
    for r in list(removed):
        r_tbl = pre_tables[r]
        r_types_sorted = sorted(c.type.lower() for c in r_tbl.columns)
        for a in list(added - matched_added):
            a_tbl = post_tables[a]
            a_types_sorted = sorted(c.type.lower() for c in a_tbl.columns)
            if (len(r_tbl.columns) == len(a_tbl.columns)
                    and r_types_sorted == a_types_sorted):
                diff.events.append({"op": "TABLE_RENAME", "from": r, "to": a})
                pre_cols = {c.name.lower(): c.type.lower() for c in r_tbl.columns}
                post_cols = {c.name.lower(): c.type.lower() for c in a_tbl.columns}
                only_pre = set(pre_cols) - set(post_cols)
                only_post = set(post_cols) - set(pre_cols)
                # Pair by matching type.
                for cr in list(only_pre):
                    matches = [
                        ca for ca in only_post if post_cols[ca] == pre_cols[cr]
                    ]
                    if len(matches) == 1:
                        ca = matches[0]
                        old = next(c.name for c in r_tbl.columns
                                   if c.name.lower() == cr)
                        new = next(c.name for c in a_tbl.columns
                                   if c.name.lower() == ca)
                        diff.events.append({
                            "op": "COLUMN_RENAME",
                            "table": a,
                            "from": old,
                            "to": new,
                        })
                        only_pre.discard(cr)
                        only_post.discard(ca)
                # Remaining unmatched columns become add/remove.
                for cr in only_pre:
                    old = next(c.name for c in r_tbl.columns
                               if c.name.lower() == cr)
                    diff.events.append({
                        "op": "COLUMN_REMOVE", "table": a, "column": old
                    })
                for ca in only_post:
                    new = next(c.name for c in a_tbl.columns
                               if c.name.lower() == ca)
                    diff.events.append({
                        "op": "COLUMN_ADD", "table": a, "column": new
                    })
                removed.discard(r)
                matched_added.add(a)
                break

    # ----- TABLE_SPLIT: heuristic = one removed table whose columns are
    # partitioned across two or more added tables that share a common key
    # column.  Disambiguation: each added table's columns must be a SUBSET
    # of the removed table's columns (ruling out coincidental overlaps from
    # unrelated TABLE_ADDs).  The union of all added tables' columns must
    # COVER the removed table's columns.  Tables already matched as renames
    # by the type-signature heuristic above are excluded.
    for r in list(removed):
        r_tbl = pre_tables[r]
        r_col_names = {c.name.lower() for c in r_tbl.columns}
        candidates: list[tuple[str, set[str]]] = []
        for a in added - matched_added:
            a_tbl = post_tables[a]
            a_col_names = {c.name.lower() for c in a_tbl.columns}
            # Each candidate's columns must be a strict subset of the source.
            if a_col_names and a_col_names < r_col_names:
                candidates.append((a, a_col_names))
        if len(candidates) >= 2:
            # All candidates must share at least one key column with the source.
            shared_keys = candidates[0][1]
            for _, a_cols in candidates[1:]:
                shared_keys = shared_keys & a_cols
            shared_keys = shared_keys & r_col_names
            if shared_keys:
                # The union of candidate columns must cover the source.
                union_cols: set[str] = set()
                for _, a_cols in candidates:
                    union_cols |= a_cols
                if r_col_names <= union_cols:
                    key_lower = sorted(shared_keys)[0]
                    key_orig = next(
                        c.name for c in r_tbl.columns
                        if c.name.lower() == key_lower
                    )
                    target_names = sorted(a for a, _ in candidates)
                    diff.events.append({
                        "op": "TABLE_SPLIT",
                        "source": r,
                        "targets": target_names,
                        "key": key_orig,
                    })
                    removed.discard(r)
                    for a, _ in candidates:
                        matched_added.add(a)

    # ----- TABLE_MERGE: heuristic = two or more removed tables whose column
    # union exactly equals a single added table's columns.  The removed
    # tables must share at least one join key column.  Each removed table's
    # columns must be a subset of the added table's columns.
    if removed and (added - matched_added):
        for a in list(added - matched_added):
            a_tbl = post_tables[a]
            a_col_names = {c.name.lower() for c in a_tbl.columns}
            merge_candidates: list[tuple[str, set[str]]] = []
            for r in list(removed):
                r_tbl = pre_tables[r]
                r_col_names = {c.name.lower() for c in r_tbl.columns}
                if r_col_names <= a_col_names:
                    merge_candidates.append((r, r_col_names))
            if len(merge_candidates) >= 2:
                shared = merge_candidates[0][1]
                for _, r_cols in merge_candidates[1:]:
                    shared = shared & r_cols
                if shared:
                    union_cols2: set[str] = set()
                    for _, r_cols in merge_candidates:
                        union_cols2 |= r_cols
                    if a_col_names <= union_cols2:
                        key_lower = sorted(shared)[0]
                        key_orig = next(
                            c.name
                            for c in pre_tables[merge_candidates[0][0]].columns
                            if c.name.lower() == key_lower
                        )
                        source_names = sorted(r for r, _ in merge_candidates)
                        diff.events.append({
                            "op": "TABLE_MERGE",
                            "sources": source_names,
                            "target": a,
                            "key": key_orig,
                        })
                        for r, _ in merge_candidates:
                            removed.discard(r)
                        matched_added.add(a)

    # ----- TABLE_ADD / TABLE_REMOVE: anything left in `removed`/`added`.
    for r in removed:
        diff.events.append({"op": "TABLE_REMOVE", "table": r})
    for a in added - matched_added:
        diff.events.append({"op": "TABLE_ADD", "table": a})

    # ----- COLUMN_RENAME / COLUMN_ADD / COLUMN_REMOVE on common tables.
    for name in common:
        pre_t = pre_tables[name]
        post_t = post_tables[name]
        pre_cols = {c.name.lower(): c.type.lower() for c in pre_t.columns}
        post_cols = {c.name.lower(): c.type.lower() for c in post_t.columns}
        c_removed = set(pre_cols) - set(post_cols)
        c_added = set(post_cols) - set(pre_cols)

        # Pair by matching type (rename heuristic).
        for cr in list(c_removed):
            matches = [ca for ca in c_added if post_cols[ca] == pre_cols[cr]]
            if len(matches) == 1:
                ca = matches[0]
                # Recover original casing.
                old = next(c.name for c in pre_t.columns if c.name.lower() == cr)
                new = next(c.name for c in post_t.columns if c.name.lower() == ca)
                diff.events.append({
                    "op": "COLUMN_RENAME",
                    "table": name,
                    "from": old,
                    "to": new,
                })
                c_removed.discard(cr)
                c_added.discard(ca)

        # ----- COLUMN_MERGE: heuristic = two or more columns removed and
        # exactly one column added (after COLUMN_RENAME pairing).  This is a
        # simple structural heuristic; the actual merge expression cannot be
        # inferred from schema snapshots alone.
        if len(c_removed) >= 2 and len(c_added) == 1:
            ca = next(iter(c_added))
            sources_orig = sorted(
                next(c.name for c in pre_t.columns if c.name.lower() == cr)
                for cr in c_removed
            )
            target_orig = next(
                c.name for c in post_t.columns if c.name.lower() == ca
            )
            diff.events.append({
                "op": "COLUMN_MERGE",
                "table": name,
                "sources": sources_orig,
                "target": target_orig,
            })
        else:
            for cr in c_removed:
                old = next(c.name for c in pre_t.columns if c.name.lower() == cr)
                diff.events.append({"op": "COLUMN_REMOVE", "table": name, "column": old})
            for ca in c_added:
                new = next(c.name for c in post_t.columns if c.name.lower() == ca)
                diff.events.append({"op": "COLUMN_ADD", "table": name, "column": new})

    return diff


# --------------------------------------------------------------------------- #
# Compact schema description (compact column-list format).                    #
# --------------------------------------------------------------------------- #

def to_prompt_block(schema: Schema) -> str:
    """Compact textual description suitable for LLM prompts.

    Format mirrors the column-list style used by Spider/BIRD prompts:

        TABLE singer ( Singer_ID INTEGER, Name TEXT, ... )
        TABLE concert ( Concert_ID INTEGER, ... )
    """
    parts: list[str] = []
    for t in sorted(schema.tables, key=lambda t: t.name.lower()):
        cols = ", ".join(f"{c.name} {c.type}" for c in t.columns)
        parts.append(f"TABLE {t.name} ( {cols} )")
    return "\n".join(parts)
