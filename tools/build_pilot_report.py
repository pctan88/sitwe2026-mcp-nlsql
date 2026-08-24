"""Build the Phase 6 pilot run report from summary/CSV outputs."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pilot import metrics

RESULTS = ROOT / "results"
REPORT_PATH = RESULTS / "PILOT_RUN_REPORT.md"

OPS = [
    "TABLE_RENAME",
    "TABLE_SPLIT",
    "TABLE_MERGE",
    "COLUMN_RENAME",
    "COLUMN_MERGE",
]


@dataclass
class ModelRun:
    name: str
    suffix: str
    aggregate_path: Path


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def _base_db_and_op(database_id: str) -> tuple[str | None, str | None]:
    for op in OPS:
        suffix = f"_{op}"
        if database_id.endswith(suffix):
            return database_id[: -len(suffix)], op
    return None, None


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes"}
    return bool(val)


def _load_fixture_summaries(model: ModelRun) -> dict[str, dict[str, Any]]:
    agg = _load_json(model.aggregate_path)
    return agg.get("databases", {})


def _load_fixture_rows(model: ModelRun, database_id: str) -> list[dict[str, Any]]:
    suffix = f"{model.suffix}_{database_id}" if model.suffix else database_id
    path = RESULTS / f"pilot_results_{suffix}.csv"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _aggregate_database(
    model: ModelRun, summaries: dict[str, dict[str, Any]], base_db: str
) -> dict[str, Any]:
    total_scored = 0
    totals = {
        "ex_pre": 0.0,
        "ex_post_baseline": 0.0,
        "ex_post_refreshed_schema": 0.0,
        "ex_post_error_feedback": 0.0,
        "ex_post_mcp": 0.0,
    }
    expected = {"n": 0, "path_d_flagged": 0}

    fingerprint_vals: list[float] = []
    relink_vals: list[float] = []

    per_op: dict[str, dict[str, Any]] = {}

    for db_id, summary in summaries.items():
        db_base, op = _base_db_and_op(db_id)
        if db_base != base_db or op is None:
            continue
        n_scored = summary.get("n_queries_scored", 0) or 0
        total_scored += n_scored
        for key in totals:
            val = summary.get(key)
            if val is None:
                continue
            totals[key] += float(val) * n_scored
        ef = summary.get("expected_failures", {})
        expected["n"] += int(ef.get("n", 0))
        expected["path_d_flagged"] += int(ef.get("path_d_flagged", 0))

        fp = summary.get("fingerprint_diff_ms")
        if fp is not None:
            fingerprint_vals.append(float(fp))
        relink_vals.extend(float(v) for v in summary.get("ast_relink_ms", []) or [])

        per_op[op] = {
            "ex_post_baseline": summary.get("ex_post_baseline"),
            "ex_post_refreshed_schema": summary.get("ex_post_refreshed_schema"),
            "ex_post_error_feedback": summary.get("ex_post_error_feedback"),
            "ex_post_mcp": summary.get("ex_post_mcp"),
        }

    ex = {
        key: (totals[key] / total_scored if total_scored else None)
        for key in totals
    }
    rr = None
    if ex["ex_post_baseline"] is not None:
        rr = metrics.recovery_rate(
            ex["ex_post_baseline"],
            ex["ex_post_refreshed_schema"] or 0.0,
            ex["ex_post_mcp"] or 0.0,
        )

    # McNemar p-values from raw rows across all fixtures for this base DB.
    baseline_ok: list[bool] = []
    mcp_ok: list[bool] = []
    error_feedback_ok: list[bool] = []
    for db_id in summaries:
        db_base, op = _base_db_and_op(db_id)
        if db_base != base_db or op is None:
            continue
        rows = _load_fixture_rows(model, db_id)
        for row in rows:
            if _coerce_bool(row.get("expected_failure")):
                continue
            baseline_ok.append(_coerce_bool(row.get("baseline_ok")))
            mcp_ok.append(_coerce_bool(row.get("mcp_ok")))
            error_feedback_ok.append(_coerce_bool(row.get("error_feedback_ok")))

    mcnemar_base = metrics.mcnemar_test(baseline_ok, mcp_ok) if baseline_ok else {}
    mcnemar_err = (
        metrics.mcnemar_test(mcp_ok, error_feedback_ok) if mcp_ok else {}
    )

    fp_stats = metrics.latency_stats([v / 1000.0 for v in fingerprint_vals])
    relink_stats = metrics.latency_stats([v / 1000.0 for v in relink_vals])

    return {
        "n_scored": total_scored,
        "ex": ex,
        "rr": rr,
        "expected": expected,
        "per_operator": per_op,
        "mcnemar": {
            "baseline_vs_mcp": mcnemar_base,
            "mcp_vs_error_feedback": mcnemar_err,
        },
        "latency": {
            "fingerprint": fp_stats,
            "ast_relink": relink_stats,
        },
    }


def _format_float(val: Any) -> str:
    if val is None:
        return "n/a"
    return f"{float(val):.4f}"


def _format_p(val: Any) -> str:
    if val is None:
        return "n/a"
    return f"{float(val):.6g}"


def _append_lines(lines: list[str], block: list[str]) -> None:
    lines.extend(block)
    lines.append("")


def main() -> None:
    models: list[ModelRun] = []
    if (RESULTS / "summary.json").exists():
        models.append(ModelRun("haiku", "", RESULTS / "summary.json"))
    if (RESULTS / "summary_gpt_4o.json").exists():
        models.append(ModelRun("gpt-4o", "gpt_4o", RESULTS / "summary_gpt_4o.json"))
    if (RESULTS / "summary_gemini.json").exists():
        models.append(ModelRun("gemini", "gemini", RESULTS / "summary_gemini.json"))
    if (RESULTS / "summary_llama31.json").exists():
        models.append(ModelRun("llama31", "llama31", RESULTS / "summary_llama31.json"))
    if (RESULTS / "summary_grok.json").exists():
        models.append(ModelRun("grok", "grok", RESULTS / "summary_grok.json"))

    failures: list[str] = []
    exit_gemini = RESULTS / "exit_gemini.txt"
    if (
        exit_gemini.exists()
        and exit_gemini.read_text().strip() != "0"
        and not (RESULTS / "summary_gemini.json").exists()
    ):
        failures.append("gemini (503 UNAVAILABLE)")
    exit_grok = RESULTS / "exit_grok.txt"
    if (
        exit_grok.exists()
        and exit_grok.read_text().strip() != "0"
        and not (RESULTS / "summary_grok.json").exists()
    ):
        failures.append("grok (429 rate limit)")

    lines: list[str] = [
        "# Pilot Run Report",
        "",
        f"Generated from results in `{RESULTS}`.",
        "",
    ]

    if failures:
        lines.append("**Skipped/Failed Models**: " + ", ".join(failures))
        lines.append("")

    for model in models:
        summaries = _load_fixture_summaries(model)
        lines.append(f"## Model: {model.name}")
        lines.append("")

        for base_db in ("concert_singer", "hr_1"):
            agg = _aggregate_database(model, summaries, base_db)
            lines.append(f"### Database: {base_db}")
            lines.append("")
            lines.append("**EX / RR**")
            lines.append("")
            lines.append("| Path | EX |")
            lines.append("| --- | --- |")
            lines.append(f"| Stale | {_format_float(agg['ex']['ex_post_baseline'])} |")
            lines.append(
                f"| Refreshed | {_format_float(agg['ex']['ex_post_refreshed_schema'])} |"
            )
            lines.append(
                f"| Error-Feedback | {_format_float(agg['ex']['ex_post_error_feedback'])} |"
            )
            lines.append(f"| MCP | {_format_float(agg['ex']['ex_post_mcp'])} |")
            lines.append("")
            lines.append(f"Recovery Rate (RR): {_format_float(agg['rr'])}")
            lines.append("")

            lines.append("**McNemar p-values**")
            lines.append("")
            lines.append("| Comparison | p-value |")
            lines.append("| --- | --- |")
            lines.append(
                f"| MCP vs Stale | {_format_p(agg['mcnemar']['baseline_vs_mcp'].get('p_value'))} |"
            )
            lines.append(
                f"| MCP vs Error-Feedback | {_format_p(agg['mcnemar']['mcp_vs_error_feedback'].get('p_value'))} |"
            )
            lines.append("")

            lines.append("**Per-Operator EX**")
            lines.append("")
            lines.append("| Operator | Stale | Refreshed | Error-Feedback | MCP |")
            lines.append("| --- | --- | --- | --- | --- |")
            for op in OPS:
                op_metrics = agg["per_operator"].get(op, {})
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            op,
                            _format_float(op_metrics.get("ex_post_baseline")),
                            _format_float(op_metrics.get("ex_post_refreshed_schema")),
                            _format_float(op_metrics.get("ex_post_error_feedback")),
                            _format_float(op_metrics.get("ex_post_mcp")),
                        ]
                    )
                    + " |"
                )
            lines.append("")

            expected = agg["expected"]
            lines.append("**Expected Failures**")
            lines.append("")
            lines.append(
                f"Unanswerable queries: {expected['n']} | "
                f"Path D flagged (non-valid verdicts): {expected['path_d_flagged']}"
            )
            lines.append("")

            latency = agg["latency"]
            lines.append("**Latency (median / IQR, seconds)**")
            lines.append("")
            lines.append(
                "| Metric | Median | IQR |\n"
                "| --- | --- | --- |"
            )
            lines.append(
                f"| fingerprint_diff | {_format_float(latency['fingerprint']['median'])} | "
                f"{_format_float(latency['fingerprint']['iqr'])} |"
            )
            lines.append(
                f"| ast_relink | {_format_float(latency['ast_relink']['median'])} | "
                f"{_format_float(latency['ast_relink']['iqr'])} |"
            )
            lines.append("")

        lines.append("")

    # Scalability benchmark
    summary_path = RESULTS / "summary.json"
    if summary_path.exists():
        summary = _load_json(summary_path)
        scalability = summary.get("scalability", {})
        cases = scalability.get("cases", [])
        if cases:
            lines.append("## Scalability Benchmarks")
            lines.append("")
            lines.append("| Tables | Fingerprint Median (s) | Fingerprint IQR (s) | Relink Median (s) | Relink IQR (s) |")
            lines.append("| --- | --- | --- | --- | --- |")
            for case in cases:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(case.get("tables")),
                            _format_float(case.get("fingerprint_median_s")),
                            _format_float(case.get("fingerprint_iqr_s")),
                            _format_float(case.get("relink_median_s")),
                            _format_float(case.get("relink_iqr_s")),
                        ]
                    )
                    + " |"
                )
            lines.append("")

    REPORT_PATH.write_text("\n".join(lines))
    print(f"[ok] wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
