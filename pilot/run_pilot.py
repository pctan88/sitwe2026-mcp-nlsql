"""Pilot evaluation harness — SITWE 2026 preliminary results.

Compares two configurations on the 20-query pilot dataset:

  (a) Baseline:        stale schema prompt -> LLM -> execute on post-DB
  (b) MCP-mediated:    schema/fingerprint -> query/relink -> query/validate
                       -> execute on post-DB

Outputs:
  results/pilot_results.csv   (per-query rows)
  results/summary.json        (aggregate metrics)

Usage::

    cd Pilot_Study_SITWE2026
    pip install -r requirements.txt
    # Optional: export ANTHROPIC_API_KEY=sk-ant-...
    python -m pilot.run_pilot
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.database_config import DatabaseConfig, list_database_configs
from mcp_server import fingerprint as fp
from mcp_server import relink as rl
from mcp_server import validate as vl
from pilot import llm_client
from pilot import metrics


def _load_queries(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "queries" in data:
        return data["queries"]
    raise ValueError(f"Unsupported queries format in {path}")


def _diff_text(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(e) for e in events)


def run(
    db_dir: Path,
    queries_path: Path,
    out_dir: Path,
    force_mock: bool = False,
    force_backend: str = "",
    model_name: str = "auto",
    out_suffix: str = "",
    strict: bool = False,
    allow_contamination: bool = False,
    n_seeds: int = 1,
    database_id: str = "concert_singer",
    pre_db_path: Path | None = None,
    post_db_path: Path | None = None,
    perturbation_manifest: dict[str, Any] | None = None,
    validate_mode: str = "token",
) -> dict[str, Any]:
    # Reset per-run fallback counters so this run's stats are clean.
    llm_client.reset_fallback_stats()
    selected_backend = llm_client.resolve_backend(
        model_name,
        force_mock=force_mock,
        force_backend=force_backend,
    )
    queries = _load_queries(queries_path)

    # On some FUSE-mounted filesystems SQLite cannot create its journal file
    # next to the original DB. Mirror both DBs to a private temp dir before
    # the run so introspection and execution work reliably. This is a no-op
    # cost on a real Linux/macOS filesystem.
    work = Path(tempfile.mkdtemp(prefix="pilot_"))
    pre_src = pre_db_path or (db_dir / "concert_singer_pre.sqlite")
    post_src = post_db_path or (db_dir / "concert_singer_post.sqlite")
    pre_db = str(work / pre_src.name)
    post_db = str(work / post_src.name)
    shutil.copyfile(str(pre_src), pre_db)
    shutil.copyfile(str(post_src), post_db)

    # Pre-schema snapshot.
    pre_schema = fp.introspect(pre_db)
    post_schema = fp.introspect(post_db)
    t_diff = time.perf_counter()
    diff = fp.classify(pre_schema, post_schema)
    fingerprint_diff_ms = round((time.perf_counter() - t_diff) * 1000.0, 6)
    pre_schema_text = fp.to_prompt_block(pre_schema)
    post_schema_text = fp.to_prompt_block(post_schema)
    diff_text = _diff_text(diff.events)

    # Validate backend: v1 token-Jaccard (canonical pilot) or v2 embedding
    # (sentence-transformers MiniLM, local). validate_v2 degrades to v1 with
    # a warning when the optional dependency is missing.
    _validate = vl.validate_v2 if validate_mode == "embedding" else vl.validate
    # Deterministic per-operator rewrite rules for the LLM fallback prompt
    # (COLUMN_MERGE / TABLE_SPLIT / TABLE_MERGE); empty for rename-only diffs.
    llm_guidance = rl.build_llm_guidance(diff.events)

    print(f"[fingerprint] pre  = {diff.pre_fingerprint[:16]}...")
    print(f"[fingerprint] post = {diff.post_fingerprint[:16]}...")
    print(f"[fingerprint] changed = {diff.changed}; events = {len(diff.events)}")
    for ev in diff.events:
        print("              ", ev)
    print(
        f"[llm] model={selected_backend.name} "
        f"provider={selected_backend.backend_id} id={selected_backend.model_id}"
    )
    print(f"[database] {database_id}")

    rows: list[dict[str, Any]] = []
    seed_rows: list[dict[str, Any]] = []   # all seeds × all queries, for *_seeds.csv
    n_pre_ok = 0
    n_post_baseline_ok = 0
    n_post_refreshed_ok = 0
    n_post_error_feedback_ok = 0
    n_post_mcp_ok = 0
    n_scored = 0
    total_baseline_latency = 0.0
    total_refreshed_latency = 0.0
    total_error_feedback_latency = 0.0
    total_mcp_latency = 0.0
    # Stability counters — only meaningful when n_seeds > 1.
    n_stable_pre = 0
    n_stable_bl  = 0
    n_stable_mcp = 0
    seed_disagreements: list[dict[str, Any]] = []

    for q in queries:
        qid = q["id"]
        question = q["question"]
        gold_pre = q["gold_pre"]
        gold_post = q["gold_post"]
        pert = q["perturbation"]
        expected_failure = bool(q.get("expected_failure", False))
        failure_reason = q.get("failure_reason", "")
        # Per-query, per-seed accumulator. The first seed becomes the
        # canonical row (preserving the N=1 CSV shape); seeds 1..n_seeds-1
        # exist only to estimate stability under temperature=0 sampling
        # noise as recommended by the reviewer audit.
        per_seed: list[dict[str, Any]] = []
        for seed_i in range(max(1, n_seeds)):
            # ---- (0) Pre-EX sanity check.
            t_pre = time.perf_counter()
            pre_resp = llm_client.generate_sql(
                pre_schema_text, question,
                model=model_name,
                force_mock=force_mock, force_backend=force_backend,
                strict=strict,
            )
            pre_ok_s = None if expected_failure else metrics.exec_match(
                pre_db, pre_resp.text, gold_pre
            )
            dt_pre_s = time.perf_counter() - t_pre

            # ---- (a) Stale-schema baseline.
            t_bl = time.perf_counter()
            bl_resp = llm_client.generate_sql(
                pre_schema_text, question,
                model=model_name,
                force_mock=force_mock, force_backend=force_backend,
                strict=strict,
            )
            bl_sql_s = bl_resp.text
            bl_ok_s = None if expected_failure else metrics.exec_match(
                post_db, bl_sql_s, gold_post
            )
            dt_bl_s = time.perf_counter() - t_bl

            # ---- (b1) Refreshed-schema baseline: developer updates schema text.
            t_ref = time.perf_counter()
            ref_resp = llm_client.generate_sql(
                post_schema_text, question,
                model=model_name,
                force_mock=force_mock, force_backend=force_backend,
                strict=strict,
            )
            ref_sql_s = ref_resp.text
            ref_ok_s = None if expected_failure else metrics.exec_match(
                post_db, ref_sql_s, gold_post
            )
            dt_ref_s = time.perf_counter() - t_ref

            # ---- (b2) Error-feedback baseline: one retry after execution error.
            t_ef = time.perf_counter()
            ef_sql_s = bl_sql_s
            ef_retry_s = 0
            ef_error_s = ""
            ef_backend_s = bl_resp.backend
            ef_exec_ok_s, ef_exec_result_s = metrics.execute_with_workaround(
                post_db, ef_sql_s
            )
            if not ef_exec_ok_s:
                ef_retry_s = 1
                ef_error_s = str(ef_exec_result_s)
                ef_resp = llm_client.generate_sql_with_error(
                    pre_schema_text, question, ef_sql_s, ef_error_s,
                    model=model_name,
                    force_mock=force_mock, force_backend=force_backend,
                    strict=strict,
                )
                ef_sql_s = ef_resp.text
                ef_backend_s = ef_resp.backend
            ef_ok_s = None if expected_failure else metrics.exec_match(
                post_db, ef_sql_s, gold_post
            )
            dt_ef_s = time.perf_counter() - t_ef

            # ---- (c) MCP-mediated: same stale generation, then relink + validate.
            t_mcp = time.perf_counter()
            t_relink = time.perf_counter()
            relink_result_s = rl.relink(
                bl_sql_s, diff.events, schema_text=post_schema_text,
            )
            dt_relink_ms_s = (time.perf_counter() - t_relink) * 1000.0
            mcp_sql_s = relink_result_s["sql"]
            val_s = _validate(post_db, mcp_sql_s, question)
            if val_s.verdict == vl.SILENT or val_s.verdict == vl.EXEC_ERROR:
                llm_fix = llm_client.relink_with_llm(
                    mcp_sql_s, diff_text, post_schema_text,
                    question=question,
                    guidance=llm_guidance,
                    model=model_name,
                    force_mock=force_mock, force_backend=force_backend,
                    strict=strict,
                )
                if llm_fix.text:
                    mcp_sql_s = llm_fix.text
                    val_s = _validate(post_db, mcp_sql_s, question)
            # Empty-result-on-affirmative cue (2026-07, COLUMN_MERGE
            # remediation): if the fixed SQL still returns an empty/zero
            # result on an affirmative question, retry ONCE more telling the
            # LLM the likely cause — the filter value lives inside a
            # merged/concatenated column and needs a LIKE pattern. Bounded at
            # one extra call per query to cap cost and latency.
            if (
                val_s.verdict == vl.SILENT
                and val_s.reason == "empty_result_on_affirmative_question"
                and llm_guidance
            ):
                cue = (
                    "The previous rewrite executed without error but returned "
                    "an empty result for a question that implies a non-empty "
                    "answer. The filter value most likely sits inside a "
                    "merged/concatenated column — re-check the rewrite rules "
                    "and use a LIKE pattern over the merge separator instead "
                    "of an equality filter.\n" + llm_guidance
                )
                llm_fix2 = llm_client.relink_with_llm(
                    mcp_sql_s, diff_text, post_schema_text,
                    question=question,
                    guidance=cue,
                    model=model_name,
                    force_mock=force_mock, force_backend=force_backend,
                    strict=strict,
                )
                if llm_fix2.text:
                    cand = llm_fix2.text
                    cand_val = _validate(post_db, cand, question)
                    # Only adopt the retry if it is not strictly worse.
                    if cand_val.verdict != vl.EXEC_ERROR:
                        mcp_sql_s = cand
                        val_s = cand_val
            mcp_ok_s = None if expected_failure else metrics.exec_match(
                post_db, mcp_sql_s, gold_post
            )
            dt_mcp_s = time.perf_counter() - t_mcp

            per_seed.append({
                "seed": seed_i,
                "pre_sql": pre_resp.text, "pre_ok": None if pre_ok_s is None else int(pre_ok_s),
                "baseline_sql": bl_sql_s, "baseline_ok": None if bl_ok_s is None else int(bl_ok_s),
                "refreshed_schema_sql": ref_sql_s,
                "refreshed_schema_ok": None if ref_ok_s is None else int(ref_ok_s),
                "error_feedback_sql": ef_sql_s,
                "error_feedback_ok": None if ef_ok_s is None else int(ef_ok_s),
                "error_feedback_retry": ef_retry_s,
                "error_feedback_error": ef_error_s,
                "error_feedback_backend": ef_backend_s,
                "mcp_sql": mcp_sql_s, "mcp_method": relink_result_s["method"],
                "mcp_verdict": val_s.verdict, "mcp_ok": None if mcp_ok_s is None else int(mcp_ok_s),
                "latency_pre_s":      round(dt_pre_s, 4),
                "latency_baseline_s": round(dt_bl_s, 4),
                "latency_refreshed_schema_s": round(dt_ref_s, 4),
                "latency_error_feedback_s": round(dt_ef_s, 4),
                "latency_mcp_s":      round(dt_mcp_s, 4),
                "ast_relink_ms": round(dt_relink_ms_s, 6),
                "backend": pre_resp.backend,
            })

        # Stability bookkeeping — does every seed agree on the binary outcomes?
        if n_seeds > 1 and not expected_failure:
            pre_set = {r["pre_ok"]      for r in per_seed}
            bl_set  = {r["baseline_ok"] for r in per_seed}
            mcp_set = {r["mcp_ok"]      for r in per_seed}
            n_stable_pre += int(len(pre_set) == 1)
            n_stable_bl  += int(len(bl_set)  == 1)
            n_stable_mcp += int(len(mcp_set) == 1)
            if len(pre_set | bl_set | mcp_set) > 1 and (
                len(pre_set) > 1 or len(bl_set) > 1 or len(mcp_set) > 1
            ):
                seed_disagreements.append({
                    "id": qid,
                    "pre_ok": sorted({r["pre_ok"]      for r in per_seed}),
                    "baseline_ok": sorted({r["baseline_ok"] for r in per_seed}),
                    "mcp_ok": sorted({r["mcp_ok"]      for r in per_seed}),
                })

        # The first seed populates the canonical CSV row; seed_rows captures
        # the full per-seed detail for the auxiliary CSV.
        first = per_seed[0]
        rec_bin = 0 if expected_failure else int(first["mcp_ok"] and not first["baseline_ok"])
        deg_bin = 0 if expected_failure else int(first["baseline_ok"] and not first["mcp_ok"])

        rows.append({
            "id": qid,
            "perturbation": pert,
            "question": question,
            "gold_post": gold_post,
            "expected_failure": expected_failure,
            "failure_reason": failure_reason,
            "pre_sql": first["pre_sql"],
            "pre_ok": first["pre_ok"],
            "baseline_sql": first["baseline_sql"],
            "baseline_ok": first["baseline_ok"],
            "refreshed_schema_sql": first["refreshed_schema_sql"],
            "refreshed_schema_ok": first["refreshed_schema_ok"],
            "error_feedback_sql": first["error_feedback_sql"],
            "error_feedback_ok": first["error_feedback_ok"],
            "error_feedback_retry": first["error_feedback_retry"],
            "error_feedback_error": first["error_feedback_error"],
            "error_feedback_backend": first["error_feedback_backend"],
            "mcp_sql": first["mcp_sql"],
            "mcp_method": first["mcp_method"],
            "mcp_verdict": first["mcp_verdict"],
            "mcp_ok": first["mcp_ok"],
            "recovered": rec_bin,
            "degraded": deg_bin,
            "latency_pre_s": first["latency_pre_s"],
            "latency_baseline_s": first["latency_baseline_s"],
            "latency_refreshed_schema_s": first["latency_refreshed_schema_s"],
            "latency_error_feedback_s": first["latency_error_feedback_s"],
            "latency_mcp_s": first["latency_mcp_s"],
            "ast_relink_ms": first["ast_relink_ms"],
            "backend": first["backend"],
        })
        for s in per_seed:
            seed_rows.append({"id": qid, "perturbation": pert, "expected_failure": expected_failure, **s})

        if not expected_failure:
            n_scored += 1
            n_pre_ok          += first["pre_ok"]
            n_post_baseline_ok += first["baseline_ok"]
            n_post_refreshed_ok += first["refreshed_schema_ok"]
            n_post_error_feedback_ok += first["error_feedback_ok"]
            n_post_mcp_ok     += first["mcp_ok"]
            total_baseline_latency += first["latency_baseline_s"]
            total_refreshed_latency += first["latency_refreshed_schema_s"]
            total_error_feedback_latency += first["latency_error_feedback_s"]
            total_mcp_latency      += first["latency_mcp_s"]

            if n_seeds > 1:
                mcp_oks = [r["mcp_ok"] for r in per_seed]
                print(
                    f"  {qid:>3}  pre={first['pre_ok']} bl={first['baseline_ok']} "
                    f"ref={first['refreshed_schema_ok']} "
                    f"err={first['error_feedback_ok']} mcp={first['mcp_ok']}  ({pert})  "
                    f"[{first['mcp_method']}/{first['mcp_verdict']}]  "
                    f"seeds_mcp={mcp_oks}"
                )
            else:
                print(
                    f"  {qid:>3}  pre={first['pre_ok']} bl={first['baseline_ok']} "
                    f"ref={first['refreshed_schema_ok']} "
                    f"err={first['error_feedback_ok']} mcp={first['mcp_ok']}  ({pert})  "
                    f"[{first['mcp_method']}/{first['mcp_verdict']}]"
                )

    n_total = len(queries)
    ex_pre = None if n_scored == 0 else n_pre_ok / n_scored
    ex_post_bl = None if n_scored == 0 else n_post_baseline_ok / n_scored
    ex_post_refreshed = None if n_scored == 0 else n_post_refreshed_ok / n_scored
    ex_post_error_feedback = None if n_scored == 0 else n_post_error_feedback_ok / n_scored
    ex_post_mcp = None if n_scored == 0 else n_post_mcp_ok / n_scored
    rr = None if ex_post_bl is None else metrics.recovery_rate(
        ex_post_bl, ex_post_refreshed or 0.0, ex_post_mcp or 0.0
    )

    scored_rows = metrics.filter_expected_failures(rows)
    expected_summary = metrics.expected_failure_summary(rows)

    # Silent-failure detection rate: how many baseline successes flipped to
    # MCP successes when the underlying perturbation would have produced wrong
    # rows? In the pilot we approximate by counting verdicts on the MCP path
    # against the ground-truth correctness flag.
    n_tp = sum(
        1 for r in scored_rows
        if r["mcp_verdict"] == vl.SILENT and r["baseline_ok"] == 0
    )
    n_fn = sum(
        1 for r in scored_rows
        if r["mcp_verdict"] == vl.VALID and r["baseline_ok"] == 0 and r["mcp_ok"] == 0
    )
    silent_det = (
        n_tp / (n_tp + n_fn) if (n_tp + n_fn) > 0 else None
    )

    # Per-run contamination accounting: how many queries silently fell back
    # to the mock LLM after the live API exhausted retries. Both the explicit
    # FALLBACK_STATS counters and a CSV-derived count are stored — the CSV
    # count survives reload of an old run.
    fb_csv = sum(
        1 for r in rows
        if any(
            "mock-after-" in str(v)
            for k, v in r.items()
            if k.endswith("backend") or k == "backend"
        )
    )
    contamination_fraction = fb_csv / max(n_total, 1)
    latency_arrays = {
        "pre_s": [r["latency_pre_s"] for r in scored_rows],
        "baseline_s": [r["latency_baseline_s"] for r in scored_rows],
        "refreshed_schema_s": [r["latency_refreshed_schema_s"] for r in scored_rows],
        "error_feedback_s": [r["latency_error_feedback_s"] for r in scored_rows],
        "mcp_s": [r["latency_mcp_s"] for r in scored_rows],
    }
    ast_relink_ms = [r["ast_relink_ms"] for r in scored_rows]

    summary = {
        "database_id": database_id,
        "n_queries": n_total,
        "n_queries_scored": n_scored,
        "expected_failures": expected_summary,
        "ex_pre": None if ex_pre is None else round(ex_pre, 4),
        "ex_post_baseline": None if ex_post_bl is None else round(ex_post_bl, 4),
        "ex_post_refreshed_schema": None if ex_post_refreshed is None else round(ex_post_refreshed, 4),
        "ex_post_error_feedback": None if ex_post_error_feedback is None else round(ex_post_error_feedback, 4),
        "ex_post_mcp": None if ex_post_mcp is None else round(ex_post_mcp, 4),
        "recovery_rate": None if rr is None else round(rr, 4),
        "silent_failure_detection_rate": (
            None if silent_det is None else round(silent_det, 4)
        ),
        "mean_baseline_latency_s": None if n_scored == 0 else round(total_baseline_latency / n_scored, 4),
        "mean_refreshed_schema_latency_s": None if n_scored == 0 else round(total_refreshed_latency / n_scored, 4),
        "mean_error_feedback_latency_s": None if n_scored == 0 else round(total_error_feedback_latency / n_scored, 4),
        "mean_mcp_latency_s": None if n_scored == 0 else round(total_mcp_latency / n_scored, 4),
        "latencies": latency_arrays,
        "latency_stats": {
            name: metrics.latency_stats(values)
            for name, values in latency_arrays.items()
        },
        "fingerprint_diff_ms": fingerprint_diff_ms,
        "ast_relink_ms": ast_relink_ms,
        "step_latency_stats_ms": {
            "fingerprint_diff_ms": metrics.latency_stats([fingerprint_diff_ms]),
            "ast_relink_ms": metrics.latency_stats(ast_relink_ms),
        },
        "per_operator": metrics.per_operator_metrics(scored_rows),
        "error_categories": metrics.error_category_counts(scored_rows),
        "mcnemar": {
            "baseline_vs_mcp": metrics.mcnemar_test(
                [r["baseline_ok"] for r in scored_rows],
                [r["mcp_ok"] for r in scored_rows],
            ),
            "baseline_vs_refreshed_schema": metrics.mcnemar_test(
                [r["baseline_ok"] for r in scored_rows],
                [r["refreshed_schema_ok"] for r in scored_rows],
            ),
            "baseline_vs_error_feedback": metrics.mcnemar_test(
                [r["baseline_ok"] for r in scored_rows],
                [r["error_feedback_ok"] for r in scored_rows],
            ),
            "mcp_vs_error_feedback": metrics.mcnemar_test(
                [r["mcp_ok"] for r in scored_rows],
                [r["error_feedback_ok"] for r in scored_rows],
            ),
        },
        "wilson_ci": {
            "baseline": metrics.wilson_ci(n_post_baseline_ok, n_scored),
            "refreshed_schema": metrics.wilson_ci(n_post_refreshed_ok, n_scored),
            "error_feedback": metrics.wilson_ci(n_post_error_feedback_ok, n_scored),
            "mcp": metrics.wilson_ci(n_post_mcp_ok, n_scored),
        },
        "bootstrap_ci": {
            "mcp_latency_mean_s": metrics.bootstrap_ci(
                latency_arrays["mcp_s"], n_boot=500, seed=0
            ),
        },
        "diff_events": diff.events,
        "perturbation_manifest": perturbation_manifest or {},
        "fingerprint_pre": diff.pre_fingerprint,
        "fingerprint_post": diff.post_fingerprint,
        "backend": selected_backend.backend_id,
        "model": selected_backend.model_id,
        "model_short_name": selected_backend.name,
        "anthropic_api_key_present": bool(os.getenv("ANTHROPIC_API_KEY")) and not force_mock,
        "gemini_api_key_present": bool(
            os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        ) and not force_mock,
        "fallback_counts": dict(llm_client.FALLBACK_STATS),
        "contaminated_queries": fb_csv,
        "contamination_fraction": round(contamination_fraction, 4),
        "strict_mode": strict,
        # Multi-seed stability fields (populated only when n_seeds > 1).
        # `seed_stability_*` is the per-query agreement rate: 1.00 means every
        # query produced identical binary outcomes across all seeds. Anything
        # below 1.00 means at least one query flipped between seeds and is
        # listed in `seed_disagreements`.
        "n_seeds": n_seeds,
        "seed_stability_pre":      None if n_seeds <= 1 else round(n_stable_pre / max(n_scored, 1), 4),
        "seed_stability_baseline": None if n_seeds <= 1 else round(n_stable_bl  / max(n_scored, 1), 4),
        "seed_stability_mcp":      None if n_seeds <= 1 else round(n_stable_mcp / max(n_scored, 1), 4),
        "seed_disagreements":      seed_disagreements,
    }

    # Contamination gate. A "clean" canonical run must have zero mock-fallback
    # rows. If the run is contaminated and the caller has not explicitly opted
    # in via allow_contamination, refuse to overwrite the canonical files.
    if (
        contamination_fraction > 0
        and not allow_contamination
        and not force_mock
        and out_suffix == ""
    ):
        suffix = "_contaminated"
        print(
            f"[guard] {fb_csv}/{n_total} queries fell back to mock LLM "
            f"(contamination = {contamination_fraction:.0%}); writing to "
            f"*{suffix}.* to protect the canonical paper-driving files."
        )
    else:
        suffix = out_suffix

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path     = out_dir / f"pilot_results{suffix}.csv"
    summary_path = out_dir / f"summary{suffix}.json"

    with csv_path.open("w", newline="") as f:
        cols = list(rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    # Per-seed CSV is written only when --seeds > 1, so the canonical
    # single-seed file stays the same shape across the existing runs.
    if n_seeds > 1 and seed_rows:
        seeds_csv_path = out_dir / f"pilot_results_seeds{suffix}.csv"
        seed_cols = list(seed_rows[0].keys())
        with seeds_csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=seed_cols)
            writer.writeheader()
            writer.writerows(seed_rows)
        print(f"[done] wrote {seeds_csv_path}  ({len(seed_rows)} per-seed rows)")

    print()
    print(f"[done] wrote {csv_path}")
    print(f"[done] wrote {summary_path}")
    print(json.dumps(summary, indent=2))
    return summary


def run_database_config(
    config: DatabaseConfig,
    out_dir: Path,
    *,
    force_mock: bool = False,
    force_backend: str = "",
    model_name: str = "auto",
    out_suffix: str = "",
    strict: bool = False,
    allow_contamination: bool = False,
    n_seeds: int = 1,
    validate_mode: str = "token",
) -> dict[str, Any]:
    return run(
        config.root,
        config.queries_path,
        out_dir,
        force_mock=force_mock,
        force_backend=force_backend,
        model_name=model_name,
        out_suffix=out_suffix,
        strict=strict,
        allow_contamination=allow_contamination,
        n_seeds=n_seeds,
        database_id=config.database_id,
        pre_db_path=config.pre_db,
        post_db_path=config.post_db,
        perturbation_manifest=config.perturbation_manifest,
        validate_mode=validate_mode,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SITWE 2026 pilot harness.")
    p.add_argument("--db-dir",  default=str(ROOT / "data"))
    p.add_argument("--queries", default="")
    p.add_argument(
        "--database",
        default="concert_singer",
        help="Database config name under --db-dir, or 'all' to run every config.",
    )
    p.add_argument("--out-dir", default=str(ROOT / "results"))
    p.add_argument(
        "--mock",
        action="store_true",
        help="Force mock LLM even if an API key is set.",
    )
    p.add_argument(
        "--llm",
        default="auto",
        choices=["auto", "anthropic", "gemini", "mock"],
        help=(
            "Legacy backend selector. Prefer --model for Stage 3 cross-model "
            "runs. Used only when --model is left at 'auto'."
        ),
    )
    p.add_argument(
        "--model",
        default="auto",
        choices=["auto", *sorted(llm_client.MODEL_REGISTRY)],
        help=(
            "Model short name. 'auto' chooses by available API key, then mock. "
            "Supported: haiku, gpt4o-mini, gpt-4o, llama31, gemini, "
            "qwen-coder, qwen, qwen-small (local via Ollama/LM Studio), mock."
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Raise on any API failure after the internal retry budget is "
            "exhausted, instead of falling back to the mock. Use this for "
            "paper-driving runs to guarantee zero mock contamination."
        ),
    )
    p.add_argument(
        "--allow-contamination",
        action="store_true",
        help=(
            "Allow the canonical output files to be overwritten even when "
            "the run contains mock-fallback rows. Defaults to OFF; mixed "
            "runs are diverted to *_contaminated.{csv,json}."
        ),
    )
    p.add_argument(
        "--seeds",
        type=int, default=1, metavar="N",
        help=(
            "Run each query N times under temperature=0 to estimate "
            "stability of the binary EX outcomes. N=1 (default) reproduces "
            "the canonical single-seed sweep; N>1 additionally writes a "
            "pilot_results_seeds{suffix}.csv with every seed's per-query "
            "trace and populates seed_stability_* fields in summary.json. "
            "Cost scales linearly in N — at Haiku 4.5 pricing, N=3 over "
            "20 queries is roughly USD 0.15."
        ),
    )
    p.add_argument(
        "--validate",
        default="token",
        choices=["token", "embedding"],
        dest="validate_mode",
        help=(
            "Back-translation similarity backend for query/validate. "
            "'token' = v1 token-Jaccard stub (canonical pilot). "
            "'embedding' = v2 sentence-embedding cosine (requires "
            "`pip install sentence-transformers`; local inference, no API "
            "cost; falls back to v1 with a warning if unavailable)."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    # When --llm gemini is selected, route output to *_gemini.* so the
    # SITWE paper's locked Haiku tables remain reproducible from the
    # un-suffixed canonical files.
    legacy_model_map = {
        "anthropic": "haiku",
        "gemini": "gemini",
        "mock": "mock",
    }
    model_name = args.model
    backend = args.llm
    if args.mock:
        model_name = "mock"
    elif model_name == "auto" and backend != "auto":
        model_name = legacy_model_map[backend]

    spec = llm_client.resolve_model(model_name, force_mock=args.mock)
    suffix = ""
    if spec.name == "gemini":
        suffix = "_gemini"
    elif spec.name == "mock":
        suffix = "_mock"
    elif spec.name not in ("auto", "haiku"):
        suffix = "_" + spec.name.replace("-", "_")

    data_root = Path(args.db_dir)
    out_dir = Path(args.out_dir)
    if args.queries:
        legacy_cfg = DatabaseConfig.from_legacy_paths(
            args.database,
            data_root,
            Path(args.queries),
        )
        run_database_config(
            legacy_cfg,
            out_dir,
            force_mock=args.mock,
            model_name=model_name,
            out_suffix=suffix,
            strict=args.strict,
            allow_contamination=args.allow_contamination,
            n_seeds=max(1, args.seeds),
            validate_mode=args.validate_mode,
        )
        return

    configs = (
        list_database_configs(data_root)
        if args.database == "all"
        else [DatabaseConfig.load(args.database, data_root)]
    )
    summaries: dict[str, Any] = {}
    for cfg in configs:
        cfg_suffix = suffix
        if len(configs) > 1:
            cfg_suffix = f"{suffix}_{cfg.database_id}" if suffix else f"_{cfg.database_id}"
        summaries[cfg.database_id] = run_database_config(
            cfg,
            out_dir,
            force_mock=args.mock,
            model_name=model_name,
            out_suffix=cfg_suffix,
            strict=args.strict,
            allow_contamination=args.allow_contamination,
            n_seeds=max(1, args.seeds),
            validate_mode=args.validate_mode,
        )

    if len(configs) > 1:
        aggregate_path = out_dir / f"summary{suffix}.json"
        with aggregate_path.open("w") as f:
            json.dump({"databases": summaries}, f, indent=2)
        print(f"[done] wrote aggregate {aggregate_path}")


if __name__ == "__main__":
    main()
