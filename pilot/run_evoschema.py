"""EvoSchema-subset harness — scale-up brief Module 1 (R1-1, R2-1).

Runs the standard 4-configuration pilot harness (stale / refreshed /
error-feedback / MCP) on a deterministic stratified subset of the real
EvoSchema benchmark (BIRD-dev substrate), one item = one NL question with
its own pre/post database pair.

Differences from ``run_pilot.py``, stated for the report:
  * the pre-EX sanity column reuses the STALE generation's SQL scored on the
    pre-DB (run_pilot issues a second identical call at T=0; skipping it
    saves ~20% of the API cost),
  * the post-DB is materialised per item from the item's EvoSchema spec
    (see ``pilot/evoschema_data.py``),
  * items failing the gold-SQL gate are excluded BEFORE any LLM call and
    tabulated in the summary.

Outputs (results/scaleup/):
  evoschema_{model}.csv, summary_evoschema_{model}.json,
  evoschema_excluded.json, runlog_evoschema_{model}.json

Usage::

    python -m pilot.run_evoschema --model haiku --validate embedding
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server import fingerprint as fp
from mcp_server import relink as rl
from mcp_server import validate as vl
from pilot import evoschema_data as ed
from pilot import llm_client
from pilot import metrics
from pilot.run_vendor_native import PRICES_USD_PER_MTOK, _load_env

OUT_DIR_DEFAULT = ROOT / "results" / "scaleup"

CSV_COLUMNS = [
    "id", "perturbation", "db_id", "train_idx", "question",
    "pre_ok",
    "baseline_sql", "baseline_ok", "latency_baseline_s",
    "baseline_tokens_in", "baseline_tokens_out",
    "refreshed_schema_sql", "refreshed_schema_ok", "latency_refreshed_schema_s",
    "refreshed_tokens_in", "refreshed_tokens_out",
    "error_feedback_sql", "error_feedback_ok", "error_feedback_retry",
    "latency_error_feedback_s", "error_feedback_tokens_in",
    "error_feedback_tokens_out",
    "mcp_sql", "mcp_method", "mcp_verdict", "mcp_ok", "latency_mcp_s",
    "mcp_tokens_in", "mcp_tokens_out",
    "backend",
]


def _row_int(row: dict[str, Any], key: str) -> Optional[int]:
    val = row.get(key)
    if val is None or str(val).strip() in ("", "None"):
        return None
    return int(float(val))


def _reload_existing(csv_path: Path) -> dict[str, dict[str, Any]]:
    if not csv_path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            for key in ("pre_ok", "baseline_ok", "refreshed_schema_ok",
                        "error_feedback_ok", "mcp_ok", "error_feedback_retry",
                        "baseline_tokens_in", "baseline_tokens_out",
                        "refreshed_tokens_in", "refreshed_tokens_out",
                        "error_feedback_tokens_in", "error_feedback_tokens_out",
                        "mcp_tokens_in", "mcp_tokens_out"):
                row[key] = _row_int(row, key)
            for key in ("latency_baseline_s", "latency_refreshed_schema_s",
                        "latency_error_feedback_s", "latency_mcp_s"):
                row[key] = float(row[key]) if str(row.get(key, "")).strip() else None
            out[row["id"]] = row
    return out


def _write(out_dir: Path, model_tag: str, rows: list[dict[str, Any]],
           summary: Optional[dict[str, Any]], runlog: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"evoschema_{model_tag}.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with (out_dir / f"runlog_evoschema_{model_tag}.json").open("w") as f:
        json.dump(runlog, f, indent=2)
    if summary is not None:
        with (out_dir / f"summary_evoschema_{model_tag}.json").open("w") as f:
            json.dump(summary, f, indent=2)


def run(
    *,
    model_name: str,
    out_dir: Path = OUT_DIR_DEFAULT,
    per_db: int = 4,
    validate_mode: str = "embedding",
    strict: bool = True,
    force_mock: bool = False,
    resume: bool = False,
    max_items: int = 0,
) -> dict[str, Any]:
    llm_client.reset_fallback_stats()
    spec = llm_client.resolve_model(model_name, force_mock=force_mock)
    model_tag = ("mock" if force_mock else model_name).replace("-", "_")
    _validate = vl.validate_v2 if validate_mode == "embedding" else vl.validate

    sample = ed.stratified_sample(ed.load_items(), per_db=per_db)
    if max_items:
        sample = sample[:max_items]
    print(f"[evoschema] sample={len(sample)} items, model={spec.name} "
          f"({spec.model_id}), validate={validate_mode}, strict={strict}")

    # ---- Gold gate (no LLM calls) + exclusion accounting.
    work = Path(tempfile.mkdtemp(prefix="evoschema_"))
    included: list[ed.EvoItem] = []
    excluded: list[dict[str, Any]] = []
    post_paths: dict[str, Path] = {}
    for it in sample:
        pre = str(ed.pre_db_path(it.db_id))
        post = work / f"{it.uid}.sqlite"
        try:
            ed.materialize_post_db(it, post)
            ok, reason = ed.gold_gate(it, pre, str(post))
        except Exception as exc:
            ok, reason = False, f"materialize: {type(exc).__name__}: {exc}"
        if ok:
            included.append(it)
            post_paths[it.uid] = post
        else:
            excluded.append({"id": it.uid, "op": it.op, "db_id": it.db_id,
                             "train_idx": it.train_idx, "reason": reason})
            post.unlink(missing_ok=True)
    print(f"[gate] included={len(included)} excluded={len(excluded)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "evoschema_excluded.json").open("w") as f:
        json.dump({
            "sampling_rule": (
                f"per operator: first {per_db} items per db_id sorted by "
                f"train_idx (COLUMN_MERGE: all merge-subset items); "
                f"seed-free and deterministic"),
            "n_sampled": len(sample),
            "n_included": len(included),
            "n_excluded": len(excluded),
            "excluded": excluded,
        }, f, indent=2)

    csv_out = out_dir / f"evoschema_{model_tag}.csv"
    existing = _reload_existing(csv_out) if resume else {}
    if existing:
        print(f"[resume] {len(existing)} completed items loaded")

    # Pre-DB schema text cache (constant per db_id).
    pre_schema_cache: dict[str, str] = {}

    rows: list[dict[str, Any]] = []
    failed_ids: list[str] = []
    t_start = time.time()

    for idx, it in enumerate(included):
        if it.uid in existing:
            rows.append(existing[it.uid])
            post_paths[it.uid].unlink(missing_ok=True)
            continue
        pre_db = str(ed.pre_db_path(it.db_id))
        post_db = str(post_paths[it.uid])
        try:
            if it.db_id not in pre_schema_cache:
                pre_schema_cache[it.db_id] = fp.to_prompt_block(
                    fp.introspect(pre_db))
            pre_schema_text = pre_schema_cache[it.db_id]
            post_schema = fp.introspect(post_db)
            post_schema_text = fp.to_prompt_block(post_schema)
            diff = fp.classify(fp.introspect(pre_db), post_schema)
            diff_text = "\n".join(json.dumps(e) for e in diff.events)
            llm_guidance = rl.build_llm_guidance(diff.events)

            # ---- (a) stale baseline (its SQL also scores the pre-EX column).
            t0 = time.perf_counter()
            bl = llm_client.generate_sql(
                pre_schema_text, it.question,
                model=model_name, force_mock=force_mock, strict=strict)
            bl_ok = int(metrics.exec_match(post_db, bl.text, it.gold_post))
            dt_bl = time.perf_counter() - t0
            pre_ok = int(metrics.exec_match(pre_db, bl.text, it.gold_pre))

            # ---- (b1) refreshed schema.
            t0 = time.perf_counter()
            ref = llm_client.generate_sql(
                post_schema_text, it.question,
                model=model_name, force_mock=force_mock, strict=strict)
            ref_ok = int(metrics.exec_match(post_db, ref.text, it.gold_post))
            dt_ref = time.perf_counter() - t0

            # ---- (b2) error feedback: one retry on execution error.
            t0 = time.perf_counter()
            ef_sql, ef_retry = bl.text, 0
            ef_tin, ef_tout = 0, 0
            ef_exec_ok, ef_err = metrics.execute_with_workaround(post_db, ef_sql)
            if not ef_exec_ok:
                ef_retry = 1
                ef_resp = llm_client.generate_sql_with_error(
                    pre_schema_text, it.question, ef_sql, str(ef_err),
                    model=model_name, force_mock=force_mock, strict=strict)
                ef_sql = ef_resp.text
                ef_tin = ef_resp.input_tokens or 0
                ef_tout = ef_resp.output_tokens or 0
            ef_ok = int(metrics.exec_match(post_db, ef_sql, it.gold_post))
            dt_ef = time.perf_counter() - t0

            # ---- (c) MCP: relink + validate + LLM fallback (run_pilot logic).
            t0 = time.perf_counter()
            mcp_tin, mcp_tout = 0, 0
            relink_result = rl.relink(
                bl.text, diff.events, schema_text=post_schema_text)
            mcp_sql = relink_result["sql"]
            val = _validate(post_db, mcp_sql, it.question)
            if val.verdict in (vl.SILENT, vl.EXEC_ERROR):
                fix = llm_client.relink_with_llm(
                    mcp_sql, diff_text, post_schema_text,
                    question=it.question, guidance=llm_guidance,
                    model=model_name, force_mock=force_mock, strict=strict)
                mcp_tin += fix.input_tokens or 0
                mcp_tout += fix.output_tokens or 0
                if fix.text:
                    mcp_sql = fix.text
                    val = _validate(post_db, mcp_sql, it.question)
            if (val.verdict == vl.SILENT
                    and val.reason == "empty_result_on_affirmative_question"
                    and llm_guidance):
                cue = (
                    "The previous rewrite executed without error but returned "
                    "an empty result for a question that implies a non-empty "
                    "answer. The filter value most likely sits inside a "
                    "merged/concatenated column — re-check the rewrite rules "
                    "and use a LIKE pattern over the merge separator instead "
                    "of an equality filter.\n" + llm_guidance
                )
                fix2 = llm_client.relink_with_llm(
                    mcp_sql, diff_text, post_schema_text,
                    question=it.question, guidance=cue,
                    model=model_name, force_mock=force_mock, strict=strict)
                mcp_tin += fix2.input_tokens or 0
                mcp_tout += fix2.output_tokens or 0
                if fix2.text:
                    cand_val = _validate(post_db, fix2.text, it.question)
                    if cand_val.verdict != vl.EXEC_ERROR:
                        mcp_sql = fix2.text
                        val = cand_val
            mcp_ok = int(metrics.exec_match(post_db, mcp_sql, it.gold_post))
            dt_mcp = time.perf_counter() - t0
        except Exception as exc:
            failed_ids.append(it.uid)
            _write(out_dir, model_tag, rows, None, {
                "status": "failed", "model": model_tag,
                "failed_ids": failed_ids,
                "completed_ids": [r["id"] for r in rows],
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[abort] {it.uid} failed ({type(exc).__name__}); partial "
                  f"results saved, re-run with --resume.")
            raise
        finally:
            post_paths[it.uid].unlink(missing_ok=True)

        rows.append({
            "id": it.uid, "perturbation": it.op, "db_id": it.db_id,
            "train_idx": it.train_idx, "question": it.question,
            "pre_ok": pre_ok,
            "baseline_sql": bl.text, "baseline_ok": bl_ok,
            "latency_baseline_s": round(dt_bl, 4),
            "baseline_tokens_in": bl.input_tokens or 0,
            "baseline_tokens_out": bl.output_tokens or 0,
            "refreshed_schema_sql": ref.text, "refreshed_schema_ok": ref_ok,
            "latency_refreshed_schema_s": round(dt_ref, 4),
            "refreshed_tokens_in": ref.input_tokens or 0,
            "refreshed_tokens_out": ref.output_tokens or 0,
            "error_feedback_sql": ef_sql, "error_feedback_ok": ef_ok,
            "error_feedback_retry": ef_retry,
            "latency_error_feedback_s": round(dt_ef, 4),
            "error_feedback_tokens_in": ef_tin,
            "error_feedback_tokens_out": ef_tout,
            "mcp_sql": mcp_sql, "mcp_method": relink_result["method"],
            "mcp_verdict": val.verdict, "mcp_ok": mcp_ok,
            "latency_mcp_s": round(dt_mcp, 4),
            "mcp_tokens_in": mcp_tin, "mcp_tokens_out": mcp_tout,
            "backend": bl.backend,
        })
        if (idx + 1) % 10 == 0 or idx == len(included) - 1:
            done = len(rows)
            print(f"  [{done}/{len(included)}] elapsed "
                  f"{time.time() - t_start:.0f}s  last: {it.uid} "
                  f"bl={bl_ok} ref={ref_ok} ef={ef_ok} mcp={mcp_ok}")

    summary = _summarise(rows, excluded, model_tag, spec,
                         validate_mode=validate_mode, strict=strict,
                         per_db=per_db, n_sampled=len(sample))
    _write(out_dir, model_tag, rows, summary, {
        "status": "complete", "model": model_tag, "failed_ids": [],
        "completed_ids": [r["id"] for r in rows],
    })
    print(f"[done] wrote {out_dir / f'evoschema_{model_tag}.csv'}")
    return summary


def _summarise(rows, excluded, model_tag, spec, *, validate_mode, strict,
               per_db, n_sampled) -> dict[str, Any]:
    n = len(rows)

    def ex(key: str) -> Optional[float]:
        return None if n == 0 else round(
            sum(int(r[key] or 0) for r in rows) / n, 4)

    def cnt(key: str) -> int:
        return sum(int(r[key] or 0) for r in rows)

    ex_stale = ex("baseline_ok")
    ex_ref = ex("refreshed_schema_ok")
    ex_mcp = ex("mcp_ok")
    rr = (metrics.recovery_rate(ex_stale, ex_ref, ex_mcp)
          if None not in (ex_stale, ex_ref, ex_mcp) else None)
    rr_ef = (metrics.recovery_rate(ex_stale, ex_ref, ex("error_feedback_ok"))
             if n else None)

    def group_metrics(key: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for val in sorted({r[key] for r in rows}):
            sub = [r for r in rows if r[key] == val]
            m = len(sub)
            s = sum(int(r["baseline_ok"] or 0) for r in sub) / m
            f = sum(int(r["refreshed_schema_ok"] or 0) for r in sub) / m
            c = sum(int(r["mcp_ok"] or 0) for r in sub) / m
            e = sum(int(r["error_feedback_ok"] or 0) for r in sub) / m
            rr_g = metrics.recovery_rate(s, f, c)
            out[val] = {
                "n": m, "ex_stale": round(s, 4), "ex_refreshed": round(f, 4),
                "ex_error_feedback": round(e, 4), "ex_mcp": round(c, 4),
                "rr_mcp": None if rr_g is None else round(rr_g, 4),
                "wilson_mcp": metrics.wilson_ci(
                    sum(int(r["mcp_ok"] or 0) for r in sub), m),
            }
        return out

    prices = PRICES_USD_PER_MTOK.get(model_tag.replace("_", "-"),
                                     {"input": 0.0, "output": 0.0})
    tin = sum(cnt(k) for k in ("baseline_tokens_in", "refreshed_tokens_in",
                               "error_feedback_tokens_in", "mcp_tokens_in"))
    tout = sum(cnt(k) for k in ("baseline_tokens_out", "refreshed_tokens_out",
                                "error_feedback_tokens_out", "mcp_tokens_out"))

    contaminated = sum(
        1 for r in rows if "mock-after-" in str(r.get("backend", "")))

    return {
        "benchmark": "EvoSchema (BIRD-dev substrate)",
        "model": model_tag,
        "model_id": spec.model_id,
        "backend": spec.backend,
        "validate_mode": validate_mode,
        "strict_mode": strict,
        "sampling_rule": (
            f"per operator: first {per_db} per db_id by train_idx; "
            f"COLUMN_MERGE: all merge-subset items"),
        "n_sampled": n_sampled,
        "n_excluded_by_gate": len(excluded),
        "n_evaluated": n,
        "n_databases": len({r["db_id"] for r in rows}),
        "full_benchmark_size_in_scope": 5768,
        "ex_pre": ex("pre_ok"),
        "ex_post_baseline": ex_stale,
        "ex_post_refreshed_schema": ex_ref,
        "ex_post_error_feedback": ex("error_feedback_ok"),
        "ex_post_mcp": ex_mcp,
        "recovery_rate": None if rr is None else round(rr, 4),
        "recovery_rate_error_feedback": (
            None if rr_ef is None else round(rr_ef, 4)),
        "wilson_ci": {
            "baseline": metrics.wilson_ci(cnt("baseline_ok"), n),
            "refreshed_schema": metrics.wilson_ci(cnt("refreshed_schema_ok"), n),
            "error_feedback": metrics.wilson_ci(cnt("error_feedback_ok"), n),
            "mcp": metrics.wilson_ci(cnt("mcp_ok"), n),
        },
        "mcnemar": {
            "baseline_vs_mcp": metrics.mcnemar_test(
                [r["baseline_ok"] for r in rows],
                [r["mcp_ok"] for r in rows]),
            "baseline_vs_refreshed_schema": metrics.mcnemar_test(
                [r["baseline_ok"] for r in rows],
                [r["refreshed_schema_ok"] for r in rows]),
            "baseline_vs_error_feedback": metrics.mcnemar_test(
                [r["baseline_ok"] for r in rows],
                [r["error_feedback_ok"] for r in rows]),
            "mcp_vs_error_feedback": metrics.mcnemar_test(
                [r["mcp_ok"] for r in rows],
                [r["error_feedback_ok"] for r in rows]),
        },
        "per_operator": group_metrics("perturbation"),
        "per_database": group_metrics("db_id"),
        "latency_stats": {
            arm: metrics.latency_stats(
                [r[f"latency_{arm}_s"] for r in rows])
            for arm in ("baseline", "refreshed_schema", "error_feedback",
                        "mcp")
        },
        "cost": {
            "prices_usd_per_mtok": prices,
            "tokens_in": tin, "tokens_out": tout,
            "usd": round(tin * prices["input"] / 1e6
                         + tout * prices["output"] / 1e6, 4),
        },
        "fallback_counts": dict(llm_client.FALLBACK_STATS),
        "contaminated_queries": contaminated,
        "notes": [
            "pre-EX reuses the stale generation's SQL scored on the pre-DB "
            "(no separate call; T=0)",
            "EvoSchema perturbs BIRD-dev, not Spider — the brief's mention "
            "of Spider databases was corrected against the benchmark "
            "inventory",
            "COLUMN_MERGE separator is a single space, fixed by EvoSchema's "
            "own gold SQL literals and verified per item by the gate",
        ],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="EvoSchema subset harness.")
    p.add_argument("--model", default="haiku", choices=["haiku", "gpt-4o", "mock"])
    p.add_argument("--per-db", type=int, default=4)
    p.add_argument("--validate", default="embedding",
                   choices=["token", "embedding"], dest="validate_mode")
    p.add_argument("--strict", action="store_true", default=True)
    p.add_argument("--no-strict", dest="strict", action="store_false")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--max-items", type=int, default=0,
                   help="Cap the sample (smoke tests only).")
    p.add_argument("--out-dir", default=str(OUT_DIR_DEFAULT))
    args = p.parse_args()
    if not args.mock:
        _load_env(ROOT / ".env")
    run(
        model_name="mock" if args.mock else args.model,
        out_dir=Path(args.out_dir),
        per_db=args.per_db,
        validate_mode=args.validate_mode,
        strict=args.strict,
        force_mock=args.mock,
        resume=args.resume,
        max_items=args.max_items,
    )


if __name__ == "__main__":
    main()
