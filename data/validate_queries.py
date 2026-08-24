"""Validate all fixture queries against pre/post SQLite databases."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DATA_ROOT = Path(__file__).resolve().parent

REQUIRED_KEYS = {"id", "question", "gold_pre", "gold_post", "perturbation", "touches"}


def _run_query(db_path: Path, sql: str, qid: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise AssertionError(f"SQLite error for {qid} on {db_path.name}: {exc}\nSQL: {sql}")
    finally:
        con.close()


def _validate_query(query: dict[str, Any], pre_db: Path, post_db: Path) -> None:
    missing = REQUIRED_KEYS - set(query.keys())
    if missing:
        raise AssertionError(f"Missing keys {missing} in query {query.get('id')}")

    qid = query["id"]
    gold_pre = query["gold_pre"]
    gold_post = query["gold_post"]
    expected_failure = query.get("expected_failure", False)

    if not isinstance(gold_pre, str) or not gold_pre.strip():
        raise AssertionError(f"gold_pre must be a non-empty string for {qid}")

    if expected_failure:
        if gold_post is not None:
            raise AssertionError(f"gold_post must be null for expected_failure {qid}")
        if not query.get("failure_reason"):
            raise AssertionError(f"failure_reason is required for expected_failure {qid}")
    else:
        if not isinstance(gold_post, str) or not gold_post.strip():
            raise AssertionError(f"gold_post must be a non-empty string for {qid}")

    pre_rows = _run_query(pre_db, gold_pre, qid)
    if not pre_rows:
        raise AssertionError(f"Empty result set for {qid} on pre DB")

    if not expected_failure:
        post_rows = _run_query(post_db, gold_post, qid)
        if not post_rows:
            raise AssertionError(f"Empty result set for {qid} on post DB")


def main() -> None:
    fixture_dirs = sorted(
        p for p in DATA_ROOT.iterdir()
        if p.is_dir() and (p / "queries.json").exists() and (p / "config.json").exists()
    )

    if not fixture_dirs:
        raise FileNotFoundError("No fixture directories with queries.json found.")

    for fixture in fixture_dirs:
        config = json.loads((fixture / "config.json").read_text())
        pre_db = fixture / config["pre_db"]
        post_db = fixture / config["post_db"]
        queries = json.loads((fixture / "queries.json").read_text())
        if not isinstance(queries, list) or not queries:
            raise AssertionError(f"No queries found in {fixture / 'queries.json'}")

        for query in queries:
            _validate_query(query, pre_db, post_db)

    print(f"[ok] validated queries for {len(fixture_dirs)} fixtures")


if __name__ == "__main__":
    main()

