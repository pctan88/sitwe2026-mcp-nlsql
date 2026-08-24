"""Scalability benchmark for schema fingerprinting and SQL relinking."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from mcp_server import fingerprint as fp
from mcp_server import relink
from pilot import metrics

ROOT = Path(__file__).resolve().parent.parent


def _build_memory_db(table_count: int) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for i in range(table_count):
        con.execute(
            f"CREATE TABLE table_{i:05d} "
            "(id INTEGER PRIMARY KEY, c1 TEXT, c2 INTEGER, c3 REAL)"
        )
    con.commit()
    return con


def _introspect_connection(con: sqlite3.Connection) -> fp.Schema:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    tables: list[fp.Table] = []
    for (name,) in rows:
        cols = con.execute(f"PRAGMA table_info({name})").fetchall()
        tables.append(
            fp.Table(
                name=name,
                columns=[fp.Column(name=c[1], type=(c[2] or "")) for c in cols],
            )
        )
    return fp.Schema(tables=tables)


def _schema_with_first_table_renamed(schema: fp.Schema) -> fp.Schema:
    tables: list[fp.Table] = []
    for idx, table in enumerate(schema.tables):
        name = "renamed_00000" if idx == 0 else table.name
        tables.append(
            fp.Table(
                name=name,
                columns=[fp.Column(c.name, c.type) for c in table.columns],
            )
        )
    return fp.Schema(tables=tables)


def benchmark(
    *,
    table_counts: tuple[int, ...] = (10, 100, 1000),
    iterations: int = 5,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for table_count in table_counts:
        con = _build_memory_db(table_count)
        try:
            schema = _introspect_connection(con)
        finally:
            con.close()
        post_schema = _schema_with_first_table_renamed(schema)

        fingerprint_hash_latencies: list[float] = []
        fingerprint_diff_latencies: list[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            schema.fingerprint()
            fingerprint_hash_latencies.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            fp.classify(schema, post_schema)
            fingerprint_diff_latencies.append(time.perf_counter() - t0)

        diff_events = [
            {
                "op": "TABLE_RENAME",
                "from": f"table_{i:05d}",
                "to": f"renamed_{i:05d}",
            }
            for i in range(table_count)
        ]
        stale_sql = "SELECT c1 FROM table_00000 WHERE id = 1"
        relink_latencies: list[float] = []
        relinked_sql = ""
        for _ in range(iterations):
            t0 = time.perf_counter()
            relinked_sql = relink.relink(stale_sql, diff_events)["sql"]
            relink_latencies.append(time.perf_counter() - t0)

        fingerprint_stats = metrics.latency_stats(fingerprint_diff_latencies)
        fingerprint_hash_stats = metrics.latency_stats(fingerprint_hash_latencies)
        relink_stats = metrics.latency_stats(relink_latencies)
        cases.append({
            "db_uri": "sqlite:///:memory:",
            "tables": table_count,
            "iterations": iterations,
            "fingerprint_latency_s": [round(v, 6) for v in fingerprint_diff_latencies],
            "fingerprint_hash_latency_s": [
                round(v, 6) for v in fingerprint_hash_latencies
            ],
            "relink_latency_s": [round(v, 6) for v in relink_latencies],
            "fingerprint_mean_s": fingerprint_stats["mean"],
            "fingerprint_median_s": fingerprint_stats["median"],
            "fingerprint_q1_s": fingerprint_stats["q1"],
            "fingerprint_q3_s": fingerprint_stats["q3"],
            "fingerprint_iqr_s": fingerprint_stats["iqr"],
            "fingerprint_hash_mean_s": fingerprint_hash_stats["mean"],
            "relink_mean_s": relink_stats["mean"],
            "relink_median_s": relink_stats["median"],
            "relink_q1_s": relink_stats["q1"],
            "relink_q3_s": relink_stats["q3"],
            "relink_iqr_s": relink_stats["iqr"],
            "relink_idempotent": all(
                relink.relink(stale_sql, diff_events)["sql"] == relinked_sql
                for _ in range(3)
            ),
            "sample_relinked_sql": relinked_sql,
        })
    return {"cases": cases}


def update_summary(
    summary_path: Path,
    *,
    table_counts: tuple[int, ...] = (10, 100, 1000),
    iterations: int = 5,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if summary_path.exists():
        with summary_path.open() as f:
            summary = json.load(f)
    summary["scalability"] = benchmark(
        table_counts=table_counts,
        iterations=iterations,
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)
    return summary


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run scalability microbenchmarks.")
    p.add_argument("--summary", default=str(ROOT / "results" / "summary.json"))
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument(
        "--table-counts",
        default="10,100,1000",
        help="Comma-separated synthetic table counts.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    table_counts = tuple(
        int(part.strip())
        for part in args.table_counts.split(",")
        if part.strip()
    )
    summary = update_summary(
        Path(args.summary),
        table_counts=table_counts,
        iterations=max(1, args.iterations),
    )
    print(json.dumps(summary["scalability"], indent=2))


if __name__ == "__main__":
    main()
