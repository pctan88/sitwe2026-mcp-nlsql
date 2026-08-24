"""Evaluation metrics for the pilot.

Implements the SHARED_NOTES §2.3 formulas:

  EX = #{i : exec(y_hat_i, D) = exec(y_i, D)} / N
  RR = (EX_mcp - EX_stale) / (EX_refreshed - EX_stale)
"""

from __future__ import annotations

import math
import random
import shutil
import sqlite3
from enum import Enum
from statistics import mean, median
from typing import Any


def _run(db_path: str, sql: str) -> tuple[bool, Any]:
    """Execute SQL; return (ok, rows_or_error)."""
    try:
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(sql).fetchall()
        finally:
            con.close()
        return True, rows
    except Exception as exc:
        return False, str(exc)


def execute_with_workaround(db_path: str, sql: str) -> tuple[bool, Any]:
    """Some mounted filesystems block SQLite journal creation. Copy to /tmp first.

    Falls back to direct execution if /tmp is unavailable.
    """
    try:
        ok, rows = _run(db_path, sql)
        if ok:
            return ok, rows
    except Exception:
        pass
    try:
        import os, tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tmp:
            shutil.copyfile(db_path, tmp.name)
            return _run(tmp.name, sql)
    except Exception as exc:
        return False, str(exc)


def equal_results(a: list, b: list) -> bool:
    """Spider-style result-set equality: row-order independent."""
    try:
        sa = sorted(map(repr, a))
        sb = sorted(map(repr, b))
        return sa == sb
    except Exception:
        return a == b


def exec_match(db_path: str, predicted_sql: str, gold_sql: str) -> bool:
    ok_pred, pred = execute_with_workaround(db_path, predicted_sql)
    ok_gold, gold = execute_with_workaround(db_path, gold_sql)
    if not ok_pred or not ok_gold:
        return False
    return equal_results(pred, gold)


def recovery_rate(
    ex_post_baseline: float,
    ex_post_refreshed: float,
    ex_post_module: float,
) -> float | None:
    """Canonical RR with refreshed-schema accuracy as the ceiling.

    RR = (EX_mcp - EX_stale) / (EX_refreshed - EX_stale).
    Returns None when the denominator is 0.
    """
    denom = ex_post_refreshed - ex_post_baseline
    if denom == 0:
        return None
    return (ex_post_module - ex_post_baseline) / denom


class ErrorCategory(str, Enum):
    CORRECT = "correct"
    EXECUTION_ERROR = "execution_error"
    SILENT_FAILURE = "silent_failure"
    WRONG_RESULT = "wrong_result"
    UNKNOWN = "unknown"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _is_expected_failure(row: dict[str, Any]) -> bool:
    return _as_bool(row.get("expected_failure"))


def filter_expected_failures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not _is_expected_failure(row)]


def expected_failure_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    expected = [row for row in rows if _is_expected_failure(row)]
    by_operator: dict[str, int] = {}
    flagged = 0
    for row in expected:
        op = str(row.get("perturbation") or "UNKNOWN")
        by_operator[op] = by_operator.get(op, 0) + 1
        verdict = str(row.get("mcp_verdict") or "").strip().lower()
        if verdict != "valid":
            flagged += 1
    return {
        "n": len(expected),
        "by_operator": by_operator,
        "path_d_flagged": flagged,
    }


def per_operator_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate EX/recovery/degradation counts by perturbation operator."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _is_expected_failure(row):
            continue
        op = str(row.get("perturbation") or "UNKNOWN")
        bucket = out.setdefault(
            op,
            {
                "n": 0,
                "baseline_correct": 0,
                "refreshed_schema_correct": 0,
                "error_feedback_correct": 0,
                "mcp_correct": 0,
                "recovered": 0,
                "degraded": 0,
            },
        )
        baseline_ok = _as_bool(row.get("baseline_ok"))
        refreshed_ok = _as_bool(row.get("refreshed_schema_ok"))
        error_feedback_ok = _as_bool(row.get("error_feedback_ok"))
        mcp_ok = _as_bool(row.get("mcp_ok"))
        bucket["n"] += 1
        bucket["baseline_correct"] += int(baseline_ok)
        bucket["refreshed_schema_correct"] += int(refreshed_ok)
        bucket["error_feedback_correct"] += int(error_feedback_ok)
        bucket["mcp_correct"] += int(mcp_ok)
        bucket["recovered"] += int(mcp_ok and not baseline_ok)
        bucket["degraded"] += int(baseline_ok and not mcp_ok)

    for bucket in out.values():
        n = max(bucket["n"], 1)
        bucket["baseline_ex"] = round(bucket["baseline_correct"] / n, 4)
        bucket["refreshed_schema_ex"] = round(
            bucket["refreshed_schema_correct"] / n, 4
        )
        bucket["error_feedback_ex"] = round(
            bucket["error_feedback_correct"] / n, 4
        )
        bucket["mcp_ex"] = round(bucket["mcp_correct"] / n, 4)
    return out


def categorize_error(row: dict[str, Any]) -> ErrorCategory:
    if _as_bool(row.get("baseline_ok")):
        return ErrorCategory.CORRECT
    error = str(row.get("error_feedback_error") or "")
    if error:
        return ErrorCategory.EXECUTION_ERROR
    verdict = str(row.get("mcp_verdict") or "")
    if "silent" in verdict:
        return ErrorCategory.SILENT_FAILURE
    if "sql" in row or "baseline_sql" in row:
        return ErrorCategory.WRONG_RESULT
    return ErrorCategory.UNKNOWN


def error_category_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {cat.value: 0 for cat in ErrorCategory}
    for row in rows:
        if _is_expected_failure(row):
            continue
        counts[categorize_error(row).value] += 1
    return counts


def mcnemar_test(
    baseline_ok: list[bool | int],
    challenger_ok: list[bool | int],
) -> dict[str, float | int | None]:
    """Exact two-sided McNemar test plus continuity-corrected statistic."""
    if len(baseline_ok) != len(challenger_ok):
        raise ValueError("baseline_ok and challenger_ok must have the same length")
    b = sum(1 for a, c in zip(baseline_ok, challenger_ok) if not _as_bool(a) and _as_bool(c))
    c = sum(1 for a, c2 in zip(baseline_ok, challenger_ok) if _as_bool(a) and not _as_bool(c2))
    n = b + c
    if n == 0:
        return {
            "b": b,
            "c": c,
            "statistic": None,
            "p_value": 1.0,
        }
    statistic = ((abs(b - c) - 1) ** 2) / n
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / (2 ** n)
    return {
        "b": b,
        "c": c,
        "statistic": round(statistic, 6),
        "p_value": round(min(1.0, 2 * tail), 6),
    }


def latency_stats(values: list[float]) -> dict[str, float | int | None]:
    vals = sorted(float(v) for v in values)
    if not vals:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "q1": None,
            "q3": None,
            "iqr": None,
            "p95": None,
            "min": None,
            "max": None,
        }
    idx95 = min(len(vals) - 1, math.ceil(0.95 * len(vals)) - 1)
    lower_half = vals[: len(vals) // 2]
    upper_half = vals[(len(vals) + 1) // 2 :]
    q1 = median(lower_half) if lower_half else vals[0]
    q3 = median(upper_half) if upper_half else vals[-1]
    return {
        "n": len(vals),
        "mean": round(mean(vals), 6),
        "median": round(median(vals), 6),
        "q1": round(q1, 6),
        "q3": round(q3, 6),
        "iqr": round(q3 - q1, 6),
        "p95": round(vals[idx95], 6),
        "min": round(vals[0], 6),
        "max": round(vals[-1], 6),
    }


def wilson_ci(
    successes: int,
    total: int,
    *,
    z: float = 1.96,
) -> dict[str, float | int | None]:
    if total <= 0:
        return {"successes": successes, "total": total, "lower": None, "upper": None}
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (
        z
        * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
        / denom
    )
    return {
        "successes": successes,
        "total": total,
        "lower": round(max(0.0, center - margin), 6),
        "upper": round(min(1.0, center + margin), 6),
    }


def bootstrap_ci(
    values: list[float],
    *,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "estimate": None, "lower": None, "upper": None}
    vals = [float(v) for v in values]
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(max(1, n_boot)):
        sample = [rng.choice(vals) for _ in vals]
        estimates.append(mean(sample))
    estimates.sort()
    lo_idx = max(0, min(len(estimates) - 1, int((alpha / 2) * len(estimates))))
    hi_idx = max(
        0,
        min(len(estimates) - 1, int((1 - alpha / 2) * len(estimates)) - 1),
    )
    return {
        "n": len(vals),
        "estimate": round(mean(vals), 6),
        "lower": round(estimates[lo_idx], 6),
        "upper": round(estimates[hi_idx], 6),
    }
