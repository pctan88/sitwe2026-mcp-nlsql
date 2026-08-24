"""Build the labelled silent-failure detection (SFD) test set — Gate G1 input.

Constructs ``data/sfd_labels.json`` deterministically from the per-operator
fixture dirs. For every source query the gold post-schema SQL is a *valid*
case; deterministic mutators then produce candidates that EXECUTE WITHOUT
ERROR but return a result set different from gold — i.e. genuine silent
failures, labelled by construction rather than by hand:

  drop_where    — strip the WHERE clause (result superset / distortion)
  swap_agg      — MIN <-> MAX, AVG -> MAX (plausible aggregate confusion)
  wrong_column  — project a different column of the same table
  stale_merge_eq— equality filter with a merge-component value on a merged
                  column (the classic COLUMN_MERGE empty-result failure)

Every candidate is verified against the post DB: exec errors are discarded
(those are caught by H0, not SFD); result == gold is discarded (not a
failure). Labels therefore cannot be wrong, only the *coverage* of failure
modes is a design choice — review `mutation` field distribution before
citing numbers.

Usage::

    python -m data.build_sfd_labels            # writes data/sfd_labels.json
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GENERATOR_VERSION = "1.0.0"

# Per-operator fixture dirs (post-schema DBs + gold_post SQL).
FIXTURE_DIRS = [
    "concert_singer_TABLE_RENAME", "concert_singer_TABLE_SPLIT",
    "concert_singer_TABLE_MERGE", "concert_singer_COLUMN_RENAME",
    "concert_singer_COLUMN_MERGE",
    "hr_1_TABLE_RENAME", "hr_1_TABLE_SPLIT", "hr_1_TABLE_MERGE",
    "hr_1_COLUMN_RENAME", "hr_1_COLUMN_MERGE",
]

# Cap per fixture dir so no single operator dominates the set.
MAX_VALID_PER_DIR = 2
MAX_SILENT_PER_DIR = 4


def _load_queries(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        data = json.load(f)
    return data["queries"] if isinstance(data, dict) else data


def _exec(db: str, sql: str):
    """Execute; return (ok, rows-or-error)."""
    try:
        con = sqlite3.connect(db)
        rows = con.execute(sql).fetchall()
        con.close()
        return True, rows
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------- #
# Deterministic mutators — each returns a list of (mutation_name, sql)        #
# --------------------------------------------------------------------------- #

def _mut_drop_where(sql: str) -> list[tuple[str, str]]:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return []
    select = tree.find(exp.Select)
    if select is None or not select.args.get("where"):
        return []
    select.set("where", None)
    return [("drop_where", tree.sql(dialect="sqlite"))]


_AGG_SWAP = {exp.Min: "MAX", exp.Max: "MIN", exp.Avg: "MAX"}


def _mut_swap_agg(sql: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for klass, repl in _AGG_SWAP.items():
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
        except Exception:
            return []
        nodes = list(tree.find_all(klass))
        if not nodes:
            continue
        for node in nodes:
            node.replace(exp.func(repl, *node.args.values()))
        out.append((f"swap_agg_{klass.__name__.lower()}", tree.sql(dialect="sqlite")))
    return out


def _table_columns(db: str) -> dict[str, list[str]]:
    con = sqlite3.connect(db)
    cols: dict[str, list[str]] = {}
    for (tname,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        cols[tname.lower()] = [
            r[1] for r in con.execute(f"PRAGMA table_info('{tname}')").fetchall()
        ]
    con.close()
    return cols


def _mut_wrong_column(sql: str, db: str) -> list[tuple[str, str]]:
    """Swap the first plain projected column for its table-neighbour."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return []
    select = tree.find(exp.Select)
    if select is None:
        return []
    tables = [t.name.lower() for t in tree.find_all(exp.Table) if t.name]
    if not tables:
        return []
    schema_cols = _table_columns(db)
    for proj in select.selects:
        col = proj if isinstance(proj, exp.Column) else proj.find(exp.Column)
        if col is None or not col.name:
            continue
        for tbl in tables:
            candidates = schema_cols.get(tbl, [])
            lower = [c.lower() for c in candidates]
            if col.name.lower() in lower:
                for alt in candidates:
                    if alt.lower() != col.name.lower():
                        col.set("this", exp.to_identifier(alt, quoted=False))
                        return [("wrong_column", tree.sql(dialect="sqlite"))]
    return []


def _mut_off_by_one_limit(sql: str) -> list[tuple[str, str]]:
    """LIMIT-truncation family (scale-up Module 5).

    With a ``LIMIT n`` present: n -> n-1 (silent truncation; LIMIT 1 ->
    LIMIT 0 returns nothing) and n -> n+1 (row leak). Without one: append a
    spurious ``LIMIT 1`` — the classic premature-LIMIT truncation on a
    multi-row answer. All candidates are verified against gold downstream —
    ones equal to gold are discarded there.
    """
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return []
    limit = tree.find(exp.Limit)
    if limit is None:
        return [("spurious_limit_one",
                 tree.sql(dialect="sqlite") + " LIMIT 1")]
    lit = limit.find(exp.Literal)
    if lit is None or not str(lit.this).isdigit():
        return []
    n = int(str(lit.this))
    out: list[tuple[str, str]] = []
    for delta, name in ((-1, "limit_minus_one"), (1, "limit_plus_one")):
        if n + delta < 0:
            continue
        try:
            t2 = sqlglot.parse_one(sql, read="sqlite")
        except Exception:
            return out
        l2 = t2.find(exp.Limit).find(exp.Literal)
        l2.replace(exp.Literal.number(n + delta))
        out.append((name, t2.sql(dialect="sqlite")))
    return out


def _mut_wrong_join_key(sql: str, db: str) -> list[tuple[str, str]]:
    """Wrong-JOIN-key family (scale-up Module 5): in ``JOIN t ON a.x = b.y``
    replace one side's column with a different column of the same table —
    executes without error, silently joins on the wrong key."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return []
    joins = list(tree.find_all(exp.Join))
    if not joins:
        return []
    # alias -> table map
    alias_map: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        if not t.name:
            continue
        alias = t.alias or t.name
        alias_map[alias.lower()] = t.name.lower()
    schema_cols = _table_columns(db)
    for join in joins:
        on = join.args.get("on")
        if on is None:
            continue
        eq = on if isinstance(on, exp.EQ) else on.find(exp.EQ)
        if eq is None:
            continue
        right = eq.expression if isinstance(eq.expression, exp.Column) else None
        if right is None or not right.name:
            continue
        tbl = alias_map.get((right.table or "").lower(), "")
        for alt in schema_cols.get(tbl, []):
            if alt.lower() == right.name.lower():
                continue
            right.set("this", exp.to_identifier(alt, quoted=False))
            return [("wrong_join_key", tree.sql(dialect="sqlite"))]
    return []


def _mut_stale_merge_eq(
    q: dict[str, Any], manifest: dict[str, Any]
) -> list[tuple[str, str]]:
    """COLUMN_MERGE-specific: equality filter on the merged column with a
    single-component value — executes fine, returns nothing."""
    out: list[tuple[str, str]] = []
    for spec in manifest.get("specs", []):
        if spec.get("op") != "COLUMN_MERGE":
            continue
        target = spec["target"]
        table = spec["table"]
        sql = q["gold_post"]
        if target.lower() not in sql.lower():
            continue
        # Filter the merged column by a value that only ever matches one
        # component — by construction never equal to '<a><sep><b>'.
        mutated = (
            f"SELECT {target} FROM {table} WHERE {target} = "
            f"(SELECT SUBSTR({target}, 1, 1) FROM {table} LIMIT 1)"
        )
        out.append(("stale_merge_eq", mutated))
        break
    return out


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def build(
    data_root: Path,
    out_path: Path,
    *,
    max_valid_per_dir: int = MAX_VALID_PER_DIR,
    max_silent_per_dir: int = MAX_SILENT_PER_DIR,
    expanded_mutators: bool = False,
) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix="sfd_"))
    cases: list[dict[str, Any]] = []

    for dirname in FIXTURE_DIRS:
        fdir = data_root / dirname
        cfg = json.loads((fdir / "config.json").read_text())
        post_rel = cfg.get("post_db", "post.sqlite")
        post_src = fdir / post_rel
        if not post_src.exists():
            # some configs store bare names, some prefixed
            alt = list(fdir.glob("*post*.sqlite"))
            if not alt:
                print(f"[skip] {dirname}: no post DB")
                continue
            post_src = alt[0]
        post_db = str(work / f"{dirname}_{post_src.name}")
        shutil.copyfile(post_src, post_db)
        manifest = cfg.get("perturbation_manifest", {})
        queries = _load_queries(fdir / "queries.json")

        n_valid = n_silent = 0
        dir_pool: dict[int, list] = {}
        for q in queries:
            if q.get("expected_failure"):
                continue
            gold_sql = q["gold_post"]
            ok, gold_rows = _exec(post_db, gold_sql)
            if not ok:
                continue

            # Valid case — gold post-schema SQL.
            if n_valid < max_valid_per_dir:
                cases.append({
                    "id": f"{q['id']}__gold",
                    "database_id": dirname,
                    "db": f"{dirname}/{post_src.name}",
                    "question": q["question"],
                    "sql": gold_sql,
                    "label": "valid",
                    "mutation": "none",
                    "source_query_id": q["id"],
                })
                n_valid += 1

            # Silent-failure candidates.
            families = [
                _mut_drop_where(gold_sql),
                _mut_swap_agg(gold_sql),
                _mut_wrong_column(gold_sql, post_db),
                _mut_stale_merge_eq(q, manifest),
            ]
            if expanded_mutators:
                families += [
                    _mut_off_by_one_limit(gold_sql),
                    _mut_wrong_join_key(gold_sql, post_db),
                ]

            def _verify_and_add(name: str, msql: str) -> bool:
                """Verify a candidate and append it; True when added."""
                if msql.strip().lower() == gold_sql.strip().lower():
                    return False
                ok, rows = _exec(post_db, msql)
                if not ok:
                    return False      # exec errors are H0's job, not SFD's
                if rows == gold_rows:
                    return False      # not actually a failure
                cases.append({
                    "id": f"{q['id']}__{name}",
                    "database_id": dirname,
                    "db": f"{dirname}/{post_src.name}",
                    "question": q["question"],
                    "sql": msql,
                    "label": "silent_failure",
                    "mutation": name,
                    "source_query_id": q["id"],
                })
                return True

            if expanded_mutators:
                # Defer selection to the dir level so the per-dir cap can be
                # filled with family balance (the canonical mutators would
                # otherwise crowd out the new families entirely).
                for fam_idx, fam in enumerate(families):
                    for name, msql in fam:
                        dir_pool.setdefault(fam_idx, []).append(
                            (name, msql, q, gold_sql, gold_rows))
            else:
                for name, msql in (c for f in families for c in f):
                    if n_silent >= max_silent_per_dir:
                        break
                    if _verify_and_add(name, msql):
                        n_silent += 1

        if expanded_mutators:
            # Dir-level round-robin over mutation families.
            idx = 0
            while n_silent < max_silent_per_dir and any(
                    len(v) > idx for v in dir_pool.values()):
                for fam_idx in sorted(dir_pool):
                    if n_silent >= max_silent_per_dir:
                        break
                    pool = dir_pool[fam_idx]
                    if len(pool) <= idx:
                        continue
                    name, msql, q, gold_sql, gold_rows = pool[idx]
                    ok_g, rows_g = True, gold_rows

                    def _verify_pool() -> bool:
                        if msql.strip().lower() == gold_sql.strip().lower():
                            return False
                        ok, rows = _exec(post_db, msql)
                        if not ok or rows == rows_g:
                            return False
                        cases.append({
                            "id": f"{q['id']}__{name}",
                            "database_id": dirname,
                            "db": f"{dirname}/{post_src.name}",
                            "question": q["question"],
                            "sql": msql,
                            "label": "silent_failure",
                            "mutation": name,
                            "source_query_id": q["id"],
                        })
                        return True

                    if _verify_pool():
                        n_silent += 1
                idx += 1

    payload = {
        "generator_version": GENERATOR_VERSION,
        "n_cases": len(cases),
        "n_valid": sum(1 for c in cases if c["label"] == "valid"),
        "n_silent": sum(1 for c in cases if c["label"] == "silent_failure"),
        "labelling_rule": (
            "silent_failure := executes without error on the post DB AND "
            "result set != gold_post result set; valid := gold_post SQL"
        ),
        "cases": cases,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[done] {out_path}: {payload['n_cases']} cases "
          f"({payload['n_valid']} valid / {payload['n_silent']} silent)")
    by_mut: dict[str, int] = {}
    for c in cases:
        by_mut[c["mutation"]] = by_mut.get(c["mutation"], 0) + 1
    print("[mutations]", json.dumps(by_mut, indent=2))
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build SFD labelled set.")
    parser.add_argument("--out", default=str(ROOT / "data" / "sfd_labels.json"))
    parser.add_argument("--valid-per-dir", type=int, default=MAX_VALID_PER_DIR)
    parser.add_argument("--silent-per-dir", type=int, default=MAX_SILENT_PER_DIR)
    parser.add_argument(
        "--expanded", action="store_true",
        help="Enable the scale-up mutator families (off-by-one LIMIT, "
             "wrong JOIN key). Defaults reproduce the canonical 60-case set.")
    args = parser.parse_args()
    build(
        ROOT / "data", Path(args.out),
        max_valid_per_dir=args.valid_per_dir,
        max_silent_per_dir=args.silent_per_dir,
        expanded_mutators=args.expanded,
    )
