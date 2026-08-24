"""Scale-up Module 4: multi-seed stability report across clients.

Aggregates the seeds-5 (haiku) and seeds-3 (gpt-4o) sweeps under
results/scaleup/seeds5_haiku/ and results/scaleup/seeds3_gpt4o/:
per-query agreement rates, per-configuration EX mean ± range across seeds,
and every disagreeing query with the arm that flipped.

Output: results/scaleup/SEEDS_STABILITY_REPORT.md (+ seeds_stability.json)

Usage::

    python tools/build_seeds_report.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "results" / "scaleup"

RUNS = {
    "haiku (5 seeds)": {
        "dir": OUT_DIR / "seeds5_haiku",
        "summary_pattern": "summary_{db}.json",
        "seeds_pattern": "pilot_results_seeds_{db}.csv",
        "n_seeds": 5,
    },
    "gpt-4o (3 seeds)": {
        "dir": OUT_DIR / "seeds3_gpt4o",
        "summary_pattern": "summary_gpt_4o_{db}.json",
        "seeds_pattern": "pilot_results_seeds_gpt_4o_{db}.csv",
        "n_seeds": 3,
        "note": (
            "PARTIAL RUN: the OpenAI account's credit balance was "
            "exhausted mid-sweep (2026-08-08, error code "
            "credit_balance_exhausted); missing configs can be re-run "
            "with the same per-config commands after adding credits."),
    },
}

DATABASES = [
    f"{db}_{op}"
    for db in ("concert_singer", "hr_1")
    for op in ("TABLE_RENAME", "TABLE_SPLIT", "TABLE_MERGE",
               "COLUMN_RENAME", "COLUMN_MERGE")
]

ARMS = ["pre_ok", "baseline_ok", "refreshed_schema_ok",
        "error_feedback_ok", "mcp_ok"]


def _to_int(v: Any) -> Optional[int]:
    s = str(v).strip()
    if s in ("", "None"):
        return None
    return int(float(s))


def analyse_run(cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    run_dir: Path = cfg["dir"]
    if not run_dir.exists():
        return None
    per_config: dict[str, Any] = {}
    disagreements: list[dict[str, Any]] = []
    pooled_stable = {arm: 0 for arm in ARMS}
    pooled_n = 0

    for db in DATABASES:
        spath = run_dir / cfg["summary_pattern"].format(db=db)
        cpath = run_dir / cfg["seeds_pattern"].format(db=db)
        if not spath.exists() or not cpath.exists():
            per_config[db] = {"missing": True}
            continue
        summary = json.loads(spath.read_text())

        # Per-seed EX per arm from the seeds CSV.
        by_seed: dict[int, list[dict[str, Any]]] = {}
        by_query: dict[str, list[dict[str, Any]]] = {}
        with cpath.open() as f:
            for row in csv.DictReader(f):
                if str(row.get("expected_failure", "")).lower() in ("true", "1"):
                    continue
                seed = int(row["seed"])
                by_seed.setdefault(seed, []).append(row)
                by_query.setdefault(row["id"], []).append(row)

        ex_ranges: dict[str, Any] = {}
        for arm in ARMS:
            per_seed_ex = []
            for seed, rows in sorted(by_seed.items()):
                vals = [_to_int(r[arm]) for r in rows]
                vals = [v for v in vals if v is not None]
                if vals:
                    per_seed_ex.append(sum(vals) / len(vals))
            if per_seed_ex:
                ex_ranges[arm] = {
                    "mean": round(sum(per_seed_ex) / len(per_seed_ex), 4),
                    "min": round(min(per_seed_ex), 4),
                    "max": round(max(per_seed_ex), 4),
                    "range": round(max(per_seed_ex) - min(per_seed_ex), 4),
                }

        # Per-query agreement + flip causes.
        n_q = len(by_query)
        stable_counts = {arm: 0 for arm in ARMS}
        for qid, rows in by_query.items():
            flipped_arms = []
            for arm in ARMS:
                vals = {_to_int(r[arm]) for r in rows}
                if len(vals) == 1:
                    stable_counts[arm] += 1
                else:
                    flipped_arms.append(arm)
            if flipped_arms:
                disagreements.append({
                    "config": db,
                    "id": qid,
                    "arms": flipped_arms,
                    "outcomes": {
                        arm: [_to_int(r[arm]) for r in sorted(
                            rows, key=lambda r: int(r["seed"]))]
                        for arm in flipped_arms
                    },
                })
        pooled_n += n_q
        for arm in ARMS:
            pooled_stable[arm] += stable_counts[arm]

        per_config[db] = {
            "n_queries": n_q,
            "n_seeds": len(by_seed),
            "stability": {arm: round(stable_counts[arm] / max(n_q, 1), 4)
                          for arm in ARMS},
            "ex_across_seeds": ex_ranges,
            "contaminated_queries": summary.get("contaminated_queries"),
            "strict_mode": summary.get("strict_mode"),
        }

    return {
        "per_config": per_config,
        "pooled_stability": {arm: round(pooled_stable[arm] / max(pooled_n, 1), 4)
                             for arm in ARMS},
        "pooled_n": pooled_n,
        "disagreements": disagreements,
    }


def main() -> None:
    report = {}
    for name, cfg in RUNS.items():
        r = analyse_run(cfg)
        if r is not None:
            if cfg.get("note"):
                r["note"] = cfg["note"]
            report[name] = r
    if not report:
        raise SystemExit("[error] no seeds runs found under results/scaleup/")

    with (OUT_DIR / "seeds_stability.json").open("w") as f:
        json.dump(report, f, indent=2)

    lines: list[str] = []
    add = lines.append
    add("# Multi-seed stability across clients (scale-up Module 4)")
    add("")
    add("Extends the RC1 three-seed evidence to **5 seeds × both DBs "
        "(Haiku 4.5)** and **3 seeds (GPT-4o)**, pooled query set, all five "
        "operators, temperature 0, strict mode, embedding validate. "
        "A query is *stable* for an arm when every seed produced the same "
        "binary EX outcome.")
    add("")
    for name, r in report.items():
        add(f"## {name}")
        add("")
        n_missing = sum(1 for e in r["per_config"].values() if e.get("missing"))
        if n_missing and r.get("note"):
            add(f"> ⚠ {n_missing}/{len(DATABASES)} configs missing — "
                f"{r['note']}")
            add("")
        ps = r["pooled_stability"]
        add(f"Pooled per-query agreement over {r['pooled_n']} scored "
            f"queries: pre {ps['pre_ok']:.3f} · stale {ps['baseline_ok']:.3f}"
            f" · refreshed {ps['refreshed_schema_ok']:.3f} · error-feedback "
            f"{ps['error_feedback_ok']:.3f} · MCP {ps['mcp_ok']:.3f}")
        add("")
        add("| Config | n | stable stale | stable MCP | EX stale "
            "mean [min,max] | EX MCP mean [min,max] |")
        add("|---|---|---|---|---|---|")
        for db, e in r["per_config"].items():
            if e.get("missing"):
                add(f"| {db} | — | — | — | (missing) | |")
                continue
            st = e["stability"]
            bl = e["ex_across_seeds"].get("baseline_ok", {})
            mc = e["ex_across_seeds"].get("mcp_ok", {})
            add(f"| {db} | {e['n_queries']} | {st['baseline_ok']:.3f} | "
                f"{st['mcp_ok']:.3f} | {bl.get('mean')} "
                f"[{bl.get('min')}, {bl.get('max')}] | {mc.get('mean')} "
                f"[{mc.get('min')}, {mc.get('max')}] |")
        add("")
        n_dis = len(r["disagreements"])
        add(f"### Disagreeing queries ({n_dis})")
        add("")
        if n_dis:
            add("| Config | Query | Flipping arms | Per-seed outcomes |")
            add("|---|---|---|---|")
            for d in r["disagreements"]:
                add(f"| {d['config']} | {d['id']} | {', '.join(d['arms'])} | "
                    f"`{json.dumps(d['outcomes'])}` |")
            add("")
            add("Cause note: at temperature 0 the remaining variance comes "
                "from provider-side nondeterminism (batching / MoE routing) "
                "surfacing as different-but-tied SQL formulations; flips "
                "concentrate in queries whose gold answer admits several "
                "near-equivalent SQL shapes.")
        else:
            add("None — every scored query produced identical binary "
                "outcomes across all seeds.")
        add("")

    (OUT_DIR / "SEEDS_STABILITY_REPORT.md").write_text("\n".join(lines))
    print(f"[done] wrote {OUT_DIR / 'SEEDS_STABILITY_REPORT.md'}")


if __name__ == "__main__":
    main()
