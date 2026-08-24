"""Ablation harness — measures the individual contribution of each MCP
primitive to execution accuracy.

Four configurations are evaluated against the same stale-schema LLM
generation per query:

  A — Full MCP        : fingerprint + relink + validate (+ LLM re-prompt)
  B — No Fingerprint  : relink called with empty event list -> no-op,
                        validate still runs (+ LLM re-prompt with placeholder
                        diff)
  C — No Relink       : fingerprint runs, stale SQL passed straight to
                        validate (+ LLM re-prompt with real diff on failure)
  D — No Validate     : fingerprint + relink, no validate, no LLM re-prompt

Outputs:
  results/ablation_results_{database_id}.csv
  results/summary_ablation_{database_id}.json

Usage::

    cd Pilot_Study_SITWE2026
    python -m pilot.run_ablation --model anthropic --database all \\
        --strict --seeds 1
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
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


CONFIG_KEYS = ("A", "B", "C", "D")
CONFIG_LABELS = {
    "A": "A_full_mcp",
    "B": "B_no_fingerprint",
    "C": "C_no_relink",
    "D": "D_no_validate",
}
NO_DIFF_PLACEHOLDER = "(fingerprint disabled — no diff available)"


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


def _parse_configs(spec: str) -> list[str]:
    if not spec:
        return list(CONFIG_KEYS)
    out: list[str] = []
    for tok in spec.split(","):
        tok = tok.strip().upper()
        if tok and tok in CONFIG_KEYS and tok not in out:
            out.append(tok)
    if not out:
        raise ValueError(f"--configs {spec!r} resolved to no valid configs")
    return out


def run(
    db_dir: Path,
    queries_path: Path,
    out_dir: Path,
    *,
    force_mock: bool = False,
    model_name: str = "auto",
    out_suffix: str = "",
    strict: bool = False,
    allow_contamination: bool = False,
    n_seeds: int = 1,
    configs: list[str] | None = None,
    database_id: str = "concert_singer",
    pre_db_path: Path | None = None,
    post_db_path: Path | None = None,
    perturbation_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg_keys = configs or list(CONFIG_KEYS)
    llm_client.reset_fallback_stats()
    selected_backend = llm_client.resolve_backend(
        model_name,
        force_mock=force_mock,
    )
    queries = _load_queries(queries_path)

    work = Path(tempfile.mkdtemp(prefix="ablation_"))
    pre_src = pre_db_path or (db_dir / "concert_singer_pre.sqlite")
    post_src = post_db_path or (db_dir / "concert_singer_post.sqlite")
    pre_db = str(work / pre_src.name)
    post_db = str(work / post_src.name)
    shutil.copyfile(str(pre_src), pre_db)
    shutil.copyfile(str(post_src), post_db)

    pre_schema = fp.introspect(pre_db)
    post_schema = fp.introspect(post_db)
    diff = fp.classify(pre_schema, post_schema)
    pre_schema_text = fp.to_prompt_block(pre_schema)
    post_schema_text = fp.to_prompt_block(post_schema)
    diff_text = _diff_text(diff.events)

    print(f"[ablation] database={database_id}")
    print(
        f"[ablation] model={selected_backend.name} "
        f"provider={selected_backend.backend_id} id={selected_backend.model_id}"
    )
    print(f"[ablation] configs={','.join(cfg_keys)}")
    print(f"[ablation] diff events = {len(diff.events)}")

    rows: list[dict[str, Any]] = []
    n_scored = 0
    n_baseline_ok = 0
    n_correct = {k: 0 for k in cfg_keys}

    for q in queries:
        qid = q["id"]
        question = q["question"]
        gold_post = q["gold_post"]
        pert = q["perturbation"]
        expected_failure = bool(q.get("expected_failure", False))

        # Aggregate over seeds — we record the first seed in the CSV row
        # (matching the run_pilot.py shape) but average correctness across
        # seeds for the summary counters.
        first_per_seed: dict[str, Any] = {}
        per_seed_ok: dict[str, list[int]] = {k: [] for k in cfg_keys}
        bl_ok_seeds: list[int] = []

        for seed_i in range(max(1, n_seeds)):
            bl_resp = llm_client.generate_sql(
                pre_schema_text,
                question,
                model=model_name,
                force_mock=force_mock,
                strict=strict,
            )
            bl_sql = bl_resp.text
            bl_ok = (
                None if expected_failure
                else int(metrics.exec_match(post_db, bl_sql, gold_post))
            )

            # Config B sanity check (the prompt's required guard).
            # mcp_server.relink names the parameter ``diff_events``; the spec
            # uses ``events=[]`` as a shorthand. We pass an empty list as the
            # second positional argument so the call works either way.
            if "B" in cfg_keys:
                _b_test = rl.relink(
                    bl_sql, [], schema_text=post_schema_text
                )["sql"]
                assert _b_test == bl_sql, (
                    f"Config B assumption violated for {qid}: "
                    f"relinker mutated SQL with empty events"
                )

            per_config: dict[str, dict[str, Any]] = {}

            if "A" in cfg_keys:
                relinked = rl.relink(
                    bl_sql, diff.events, schema_text=post_schema_text
                )["sql"]
                val = vl.validate(post_db, relinked, question)
                if val.verdict in (vl.SILENT, vl.EXEC_ERROR):
                    fix = llm_client.relink_with_llm(
                        relinked,
                        diff_text,
                        post_schema_text,
                        model=model_name,
                        force_mock=force_mock,
                        strict=strict,
                    )
                    if fix.text:
                        relinked = fix.text
                ok = (
                    None if expected_failure
                    else int(metrics.exec_match(post_db, relinked, gold_post))
                )
                per_config["A"] = {"sql": relinked, "verdict": val.verdict, "ok": ok}

            if "B" in cfg_keys:
                relinked = rl.relink(
                    bl_sql, [], schema_text=post_schema_text
                )["sql"]
                # Assertion repeated here for the spec — covers the case where
                # only B is requested without A enabled.
                assert relinked == bl_sql, (
                    f"Config B assumption violated for {qid}: "
                    f"relinker mutated SQL with empty events"
                )
                val = vl.validate(post_db, relinked, question)
                if val.verdict in (vl.SILENT, vl.EXEC_ERROR):
                    fix = llm_client.relink_with_llm(
                        relinked,
                        NO_DIFF_PLACEHOLDER,
                        post_schema_text,
                        model=model_name,
                        force_mock=force_mock,
                        strict=strict,
                    )
                    if fix.text:
                        relinked = fix.text
                ok = (
                    None if expected_failure
                    else int(metrics.exec_match(post_db, relinked, gold_post))
                )
                per_config["B"] = {"sql": relinked, "verdict": val.verdict, "ok": ok}

            if "C" in cfg_keys:
                # Skip the AST relink — feed stale SQL straight to validate.
                cand = bl_sql
                val = vl.validate(post_db, cand, question)
                if val.verdict in (vl.SILENT, vl.EXEC_ERROR):
                    fix = llm_client.relink_with_llm(
                        cand,
                        diff_text,
                        post_schema_text,
                        model=model_name,
                        force_mock=force_mock,
                        strict=strict,
                    )
                    if fix.text:
                        cand = fix.text
                ok = (
                    None if expected_failure
                    else int(metrics.exec_match(post_db, cand, gold_post))
                )
                per_config["C"] = {"sql": cand, "verdict": val.verdict, "ok": ok}

            if "D" in cfg_keys:
                relinked = rl.relink(
                    bl_sql, diff.events, schema_text=post_schema_text
                )["sql"]
                ok = (
                    None if expected_failure
                    else int(metrics.exec_match(post_db, relinked, gold_post))
                )
                per_config["D"] = {"sql": relinked, "verdict": "skipped", "ok": ok}

            if seed_i == 0:
                first_per_seed = {
                    "baseline_sql": bl_sql,
                    "baseline_ok": bl_ok,
                    "per_config": per_config,
                }
            if not expected_failure:
                bl_ok_seeds.append(bl_ok)
                for k in cfg_keys:
                    per_seed_ok[k].append(per_config[k]["ok"])

        first_cfg = first_per_seed["per_config"]
        row = {
            "query_id": qid,
            "operator": pert,
            "expected_failure": expected_failure,
            "baseline_sql": first_per_seed["baseline_sql"],
            "baseline_ok": first_per_seed["baseline_ok"],
        }
        for k in cfg_keys:
            row[f"mcp_ok_{k}"] = first_cfg[k]["ok"]
            row[f"sql_{k}"] = first_cfg[k]["sql"]
            row[f"verdict_{k}"] = first_cfg[k]["verdict"]
        rows.append(row)

        if not expected_failure:
            n_scored += 1
            # First-seed counter — matches run_pilot.py's convention.
            n_baseline_ok += first_per_seed["baseline_ok"]
            for k in cfg_keys:
                n_correct[k] += first_cfg[k]["ok"]

        print(
            f"  {qid:>10}  bl={first_per_seed['baseline_ok']}  "
            + "  ".join(f"{k}={first_cfg[k]['ok']}" for k in cfg_keys)
            + f"  ({pert})"
        )

    fb_csv = sum(
        1 for r in rows
        if any(
            "mock-after-" in str(v)
            for k, v in r.items()
            if isinstance(v, str)
        )
    )
    contamination_fraction = fb_csv / max(len(queries), 1)

    summary = {
        "database_id": database_id,
        "n_queries": len(queries),
        "n_queries_scored": n_scored,
        "backend": selected_backend.backend_id,
        "model": selected_backend.model_id,
        "model_short_name": selected_backend.name,
        "contaminated_queries": fb_csv,
        "contamination_fraction": round(contamination_fraction, 4),
        "strict_mode": strict,
        "n_seeds": n_seeds,
        "configs": {
            CONFIG_LABELS[k]: {
                "ex": (
                    None if n_scored == 0
                    else round(n_correct[k] / n_scored, 4)
                ),
                "n_correct": n_correct[k],
            }
            for k in cfg_keys
        },
        "baseline": {
            "ex": (
                None if n_scored == 0 else round(n_baseline_ok / n_scored, 4)
            ),
            "n_correct": n_baseline_ok,
        },
        "diff_events": diff.events,
        "perturbation_manifest": perturbation_manifest or {},
        "fallback_counts": dict(llm_client.FALLBACK_STATS),
    }

    if (
        contamination_fraction > 0
        and not allow_contamination
        and not force_mock
        and out_suffix == ""
    ):
        suffix = "_contaminated"
        print(
            f"[guard] {fb_csv}/{len(queries)} queries fell back to mock LLM "
            f"(contamination = {contamination_fraction:.0%}); writing to "
            f"*{suffix}.* to protect the canonical files."
        )
    else:
        suffix = out_suffix

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"ablation_results_{database_id}{suffix}.csv"
    summary_path = out_dir / f"summary_ablation_{database_id}{suffix}.json"

    with csv_path.open("w", newline="") as f:
        cols = list(rows[0].keys()) if rows else [
            "query_id", "operator", "expected_failure",
            "baseline_sql", "baseline_ok",
        ]
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"[done] wrote {csv_path}")
    print(f"[done] wrote {summary_path}")
    return summary


def run_database_config(
    config: DatabaseConfig,
    out_dir: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return run(
        config.root,
        config.queries_path,
        out_dir,
        database_id=config.database_id,
        pre_db_path=config.pre_db,
        post_db_path=config.post_db,
        perturbation_manifest=config.perturbation_manifest,
        **kwargs,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SITWE 2026 ablation harness.")
    p.add_argument("--db-dir", default=str(ROOT / "data"))
    p.add_argument(
        "--database",
        default="concert_singer",
        help="Database config name under --db-dir, or 'all'.",
    )
    p.add_argument("--out-dir", default=str(ROOT / "results"))
    p.add_argument("--mock", action="store_true")
    p.add_argument(
        "--model",
        default="auto",
        choices=["auto", *sorted(llm_client.MODEL_REGISTRY), "anthropic"],
        help=(
            "Model short name. 'anthropic' is accepted as an alias for "
            "'haiku' to match run_pilot.py's --llm flag."
        ),
    )
    p.add_argument("--strict", action="store_true")
    p.add_argument("--allow-contamination", action="store_true")
    p.add_argument("--seeds", type=int, default=1, metavar="N")
    p.add_argument(
        "--configs",
        default=",".join(CONFIG_KEYS),
        help="Comma-separated subset of A,B,C,D (default: A,B,C,D).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    model_name = "mock" if args.mock else args.model
    if model_name == "anthropic":
        model_name = "haiku"

    spec = llm_client.resolve_model(model_name, force_mock=args.mock)
    suffix = ""
    if spec.name == "gemini":
        suffix = "_gemini"
    elif spec.name == "mock":
        suffix = "_mock"
    elif spec.name not in ("auto", "haiku"):
        suffix = "_" + spec.name.replace("-", "_")

    cfg_keys = _parse_configs(args.configs)
    data_root = Path(args.db_dir)
    out_dir = Path(args.out_dir)

    configs = (
        list_database_configs(data_root)
        if args.database == "all"
        else [DatabaseConfig.load(args.database, data_root)]
    )

    summaries: dict[str, Any] = {}
    for cfg in configs:
        summaries[cfg.database_id] = run_database_config(
            cfg,
            out_dir,
            force_mock=args.mock,
            model_name=model_name,
            out_suffix=suffix,
            strict=args.strict,
            allow_contamination=args.allow_contamination,
            n_seeds=max(1, args.seeds),
            configs=cfg_keys,
        )

    if len(configs) > 1:
        aggregate_path = out_dir / f"summary_ablation{suffix}.json"
        with aggregate_path.open("w") as f:
            json.dump({"databases": summaries}, f, indent=2)
        print(f"[done] wrote aggregate {aggregate_path}")


if __name__ == "__main__":
    main()
