"""Unit tests for Stage 4 statistical metrics."""

from __future__ import annotations

import pytest

from pilot import metrics


def test_mcnemar_exact_known_input():
    result = metrics.mcnemar_test(
        [1, 1, 1, 1, 1],
        [0, 0, 0, 0, 0],
    )

    assert result["b"] == 0
    assert result["c"] == 5
    assert result["statistic"] == pytest.approx(3.2)
    assert result["p_value"] == pytest.approx(0.0625)


def test_mcnemar_mcp_vs_error_feedback_known_input():
    result = metrics.mcnemar_test(
        [1, 1, 0, 1],
        [1, 0, 1, 0],
    )

    assert result["b"] == 1
    assert result["c"] == 2
    assert result["p_value"] == pytest.approx(1.0)


def test_recovery_rate_uses_refreshed_schema_ceiling():
    assert metrics.recovery_rate(0.2, 0.8, 0.5) == pytest.approx(0.5)
    assert metrics.recovery_rate(0.2, 0.2, 0.5) is None


def test_wilson_ci_known_input():
    ci = metrics.wilson_ci(5, 10)

    assert ci["lower"] == pytest.approx(0.23659, abs=1e-5)
    assert ci["upper"] == pytest.approx(0.76341, abs=1e-5)


def test_latency_stats_known_input():
    stats = metrics.latency_stats([4.0, 1.0, 3.0, 2.0])

    assert stats == {
        "n": 4,
        "mean": 2.5,
        "median": 2.5,
        "q1": 1.5,
        "q3": 3.5,
        "iqr": 2.0,
        "p95": 4.0,
        "min": 1.0,
        "max": 4.0,
    }


def test_bootstrap_ci_degenerate_input_is_exact():
    ci = metrics.bootstrap_ci([1.0, 1.0, 1.0], n_boot=50, seed=123)

    assert ci["estimate"] == 1.0
    assert ci["lower"] == 1.0
    assert ci["upper"] == 1.0


def test_per_operator_metrics_and_error_categories():
    rows = [
        {
            "perturbation": "TABLE_RENAME",
            "baseline_ok": 0,
            "refreshed_schema_ok": 1,
            "error_feedback_ok": 1,
            "mcp_ok": 1,
            "error_feedback_error": "no such table: singer",
            "mcp_verdict": "valid",
            "baseline_sql": "SELECT * FROM singer",
        },
        {
            "perturbation": "NONE",
            "baseline_ok": 1,
            "refreshed_schema_ok": 1,
            "error_feedback_ok": 1,
            "mcp_ok": 1,
            "error_feedback_error": "",
            "mcp_verdict": "valid",
            "baseline_sql": "SELECT * FROM stadium",
        },
    ]

    per_op = metrics.per_operator_metrics(rows)
    cats = metrics.error_category_counts(rows)

    assert per_op["TABLE_RENAME"]["n"] == 1
    assert per_op["TABLE_RENAME"]["recovered"] == 1
    assert per_op["TABLE_RENAME"]["mcp_ex"] == 1.0
    assert cats[metrics.ErrorCategory.EXECUTION_ERROR.value] == 1
    assert cats[metrics.ErrorCategory.CORRECT.value] == 1


def test_expected_failure_summary_and_filtering():
    rows = [
        {
            "perturbation": "TABLE_MERGE",
            "baseline_ok": 0,
            "refreshed_schema_ok": 0,
            "error_feedback_ok": 0,
            "mcp_ok": 0,
            "mcp_verdict": "valid",
            "expected_failure": True,
        },
        {
            "perturbation": "COLUMN_MERGE",
            "baseline_ok": 1,
            "refreshed_schema_ok": 1,
            "error_feedback_ok": 1,
            "mcp_ok": 0,
            "mcp_verdict": "silent_failure_suspected",
            "expected_failure": True,
        },
        {
            "perturbation": "TABLE_RENAME",
            "baseline_ok": 0,
            "refreshed_schema_ok": 1,
            "error_feedback_ok": 1,
            "mcp_ok": 1,
            "mcp_verdict": "valid",
            "expected_failure": False,
        },
    ]

    scored = metrics.filter_expected_failures(rows)
    summary = metrics.expected_failure_summary(rows)
    per_op = metrics.per_operator_metrics(rows)

    assert len(scored) == 1
    assert summary["n"] == 2
    assert summary["by_operator"]["TABLE_MERGE"] == 1
    assert summary["by_operator"]["COLUMN_MERGE"] == 1
    assert summary["path_d_flagged"] == 1
    assert per_op["TABLE_RENAME"]["n"] == 1
