"""Tests for the Stage 4 scalability harness."""

from __future__ import annotations

import json

from pilot import run_scalability


def test_scalability_benchmark_output_format():
    result = run_scalability.benchmark(table_counts=(3,), iterations=2)

    assert list(result) == ["cases"]
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    assert case["db_uri"] == "sqlite:///:memory:"
    assert case["tables"] == 3
    assert case["iterations"] == 2
    assert len(case["fingerprint_latency_s"]) == 2
    assert len(case["fingerprint_hash_latency_s"]) == 2
    assert len(case["relink_latency_s"]) == 2
    for prefix in ("fingerprint", "relink"):
        assert f"{prefix}_mean_s" in case
        assert f"{prefix}_median_s" in case
        assert f"{prefix}_q1_s" in case
        assert f"{prefix}_q3_s" in case
        assert f"{prefix}_iqr_s" in case
    assert case["relink_idempotent"] is True
    assert "renamed_00000" in case["sample_relinked_sql"]


def test_scalability_summary_update_is_idempotent_shape(tmp_path):
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({"n_queries": 1}))

    first = run_scalability.update_summary(
        summary_path,
        table_counts=(2,),
        iterations=1,
    )
    second = run_scalability.update_summary(
        summary_path,
        table_counts=(2,),
        iterations=1,
    )

    assert first["n_queries"] == 1
    assert second["n_queries"] == 1
    assert len(first["scalability"]["cases"]) == 1
    assert len(second["scalability"]["cases"]) == 1
    persisted = json.loads(summary_path.read_text())
    assert len(persisted["scalability"]["cases"]) == 1
