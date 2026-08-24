"""Scale-up Module 6: statistics hardening appendix (zero API cost).

Across all result sets (canonical pilot CSVs for all 7 models, the
vendor-native arms from Brief 1, and the EvoSchema subset when present):

  * Holm-corrected McNemar families — per model, the family is
    {MCP vs stale, MCP vs error-feedback, MCP vs vendor-native,
     MCP vs diff-in-prompt} (the last two exist for haiku/gpt-4o only),
  * bootstrap 95% CI on the pooled RR (10,000 query-level resamples,
    deterministic seed 0),
  * per-operator Wilson 95% CIs on EX_MCP.

Output: results/scaleup/stats_appendix.md (+ .json) with LaTeX-ready
tables. Doubles as deferred thesis Ch4 material.

Usage::

    python tools/build_stats_appendix.py
"""

from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pilot import metrics
from tools.build_complexity_breakdown import (
    MODEL_TAGS, SWEEP_DATABASES, _is_scored, _to_int,
)

OUT_DIR = ROOT / "results" / "scaleup"
N_BOOT = 10_000


def load_pilot_rows(model: str) -> list[dict[str, Any]]:
    tag = MODEL_TAGS[model]
    rows = []
    for db in SWEEP_DATABASES:
        path = ROOT / "results" / f"pilot_results_{tag}{db}.csv"
        if not path.exists():
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                if not _is_scored(row):
                    continue
                rows.append({
                    "db": db, "id": row["id"], "op": row["perturbation"],
                    "stale": _to_int(row["baseline_ok"]),
                    "refreshed": _to_int(row["refreshed_schema_ok"]),
                    "error_feedback": _to_int(row["error_feedback_ok"]),
                    "mcp": _to_int(row["mcp_ok"]),
                })
    return rows


def load_vendor_rows(model: str) -> dict[tuple[str, str], dict[str, Any]]:
    tag = MODEL_TAGS[model].rstrip("_") or "haiku"
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for db in SWEEP_DATABASES:
        path = ROOT / "results" / "vendor_native" / f"vendor_{tag}_{db}.csv"
        if not path.exists():
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                if not _is_scored(row):
                    continue
                out[(db, row["id"])] = {
                    "vendor": _to_int(row["vendor_ok"]),
                    "diff": _to_int(row["diff_ok"]),
                }
    return out


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values."""
    items = sorted(pvals.items(), key=lambda x: x[1])
    m = len(items)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(items):
        running = max(running, (m - i) * p)
        adjusted[name] = round(min(1.0, running), 6)
    return adjusted


def bootstrap_rr(rows: list[dict[str, Any]], arm: str = "mcp",
                 n_boot: int = N_BOOT, seed: int = 0) -> dict[str, Any]:
    """Query-level bootstrap of RR = (EX_arm - EX_stale)/(EX_refr - EX_stale)."""
    rng = random.Random(seed)
    n = len(rows)
    estimates: list[float] = []
    degenerate = 0
    for _ in range(n_boot):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        s = sum(r["stale"] or 0 for r in sample) / n
        f = sum(r["refreshed"] or 0 for r in sample) / n
        a = sum(r[arm] or 0 for r in sample) / n
        rr = metrics.recovery_rate(s, f, a)
        if rr is None:
            degenerate += 1
            continue
        estimates.append(rr)
    estimates.sort()
    if not estimates:
        return {"n_boot": n_boot, "degenerate": degenerate,
                "point": None, "lower": None, "upper": None}
    lo = estimates[int(0.025 * len(estimates))]
    hi = estimates[min(len(estimates) - 1, int(0.975 * len(estimates)))]
    s = sum(r["stale"] or 0 for r in rows) / n
    f = sum(r["refreshed"] or 0 for r in rows) / n
    a = sum(r[arm] or 0 for r in rows) / n
    point = metrics.recovery_rate(s, f, a)
    return {
        "n_boot": n_boot, "degenerate": degenerate,
        "point": None if point is None else round(point, 4),
        "lower": round(lo, 4), "upper": round(hi, 4),
    }


def main() -> None:
    report: dict[str, Any] = {"models": {}}
    for model in MODEL_TAGS:
        rows = load_pilot_rows(model)
        if not rows:
            continue
        vendor = load_vendor_rows(model)
        entry: dict[str, Any] = {"n": len(rows)}

        # --- McNemar family + Holm.
        tests: dict[str, dict[str, Any]] = {
            "mcp_vs_stale": metrics.mcnemar_test(
                [r["stale"] for r in rows], [r["mcp"] for r in rows]),
            "mcp_vs_error_feedback": metrics.mcnemar_test(
                [r["error_feedback"] for r in rows],
                [r["mcp"] for r in rows]),
        }
        if vendor:
            paired = [(r, vendor[(r["db"], r["id"])]) for r in rows
                      if (r["db"], r["id"]) in vendor]
            tests["mcp_vs_vendor_native"] = metrics.mcnemar_test(
                [v["vendor"] for _, v in paired],
                [r["mcp"] for r, _ in paired])
            tests["mcp_vs_diff_in_prompt"] = metrics.mcnemar_test(
                [v["diff"] for _, v in paired],
                [r["mcp"] for r, _ in paired])
        adjusted = holm({k: t["p_value"] for k, t in tests.items()})
        for k in tests:
            tests[k]["p_holm"] = adjusted[k]
        entry["mcnemar_family"] = tests

        # --- Bootstrap RR.
        entry["bootstrap_rr_mcp"] = bootstrap_rr(rows, "mcp")
        entry["bootstrap_rr_error_feedback"] = bootstrap_rr(
            rows, "error_feedback")

        # --- Per-operator Wilson CIs (EX_MCP).
        per_op: dict[str, Any] = {}
        for op in sorted({r["op"] for r in rows}):
            sub = [r for r in rows if r["op"] == op]
            k = sum(r["mcp"] or 0 for r in sub)
            per_op[op] = {"n": len(sub),
                          "ex_mcp": round(k / len(sub), 4),
                          "wilson": metrics.wilson_ci(k, len(sub))}
        entry["per_operator_wilson_mcp"] = per_op
        report["models"][model] = entry

    # --- EvoSchema subset (if present): include its family too.
    evo: dict[str, Any] = {}
    for m in ("haiku", "gpt_4o"):
        path = OUT_DIR / f"summary_evoschema_{m}.json"
        if not path.exists():
            continue
        s = json.loads(path.read_text())
        fam = {k: dict(v) for k, v in s["mcnemar"].items()}
        adjusted = holm({k: t["p_value"] for k, t in fam.items()})
        for k in fam:
            fam[k]["p_holm"] = adjusted[k]
        evo[m] = {"n": s["n_evaluated"], "mcnemar_family": fam,
                  "recovery_rate": s["recovery_rate"]}
    if evo:
        report["evoschema"] = evo

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "stats_appendix.json").open("w") as f:
        json.dump(report, f, indent=2)

    # ---------------- Markdown ----------------
    lines: list[str] = []
    add = lines.append
    add("# Statistics appendix (scale-up Module 6)")
    add("")
    add(f"Holm step-down correction applied within each model's McNemar "
        f"family; bootstrap RR CIs use {N_BOOT:,} query-level resamples "
        f"(seed 0, deterministic). Pilot rows: 152 scored queries per model "
        f"(10 db×operator configs).")
    add("")
    add("## 1. Holm-corrected McNemar families (pilot, pooled)")
    add("")
    add("| Model | Comparison | b | c | p (raw) | p (Holm) |")
    add("|---|---|---|---|---|---|")
    for model, e in report["models"].items():
        for name, t in e["mcnemar_family"].items():
            add(f"| {model} | {name.replace('_', ' ')} | {t['b']} | "
                f"{t['c']} | {t['p_value']:.6f} | {t['p_holm']:.6f} |")
    add("")
    if evo:
        add("## 1b. EvoSchema subset families (Holm within model)")
        add("")
        add("| Model | Comparison | b | c | p (raw) | p (Holm) |")
        add("|---|---|---|---|---|---|")
        for model, e in evo.items():
            for name, t in e["mcnemar_family"].items():
                add(f"| {model} | {name.replace('_', ' ')} | {t['b']} | "
                    f"{t['c']} | {t['p_value']:.6f} | {t['p_holm']:.6f} |")
        add("")
    add("## 2. Bootstrap 95% CIs on pooled RR")
    add("")
    add("| Model | RR MCP [95% CI] | RR error-feedback [95% CI] | "
        "degenerate resamples |")
    add("|---|---|---|---|")
    for model, e in report["models"].items():
        b1, b2 = e["bootstrap_rr_mcp"], e["bootstrap_rr_error_feedback"]
        add(f"| {model} | {b1['point']} [{b1['lower']}, {b1['upper']}] | "
            f"{b2['point']} [{b2['lower']}, {b2['upper']}] | "
            f"{b1['degenerate']} |")
    add("")
    add("## 3. Per-operator Wilson 95% CIs on EX_MCP")
    add("")
    add("| Model | Operator | n | EX MCP | 95% CI |")
    add("|---|---|---|---|---|")
    for model, e in report["models"].items():
        for op, o in e["per_operator_wilson_mcp"].items():
            w = o["wilson"]
            add(f"| {model} | {op} | {o['n']} | {o['ex_mcp']:.3f} | "
                f"[{w['lower']:.3f}, {w['upper']:.3f}] |")
    add("")
    add("## LaTeX: Holm-corrected family (haiku + gpt-4o)")
    add("")
    add("```latex")
    add("\\begin{tabular}{llrrrr}")
    add("\\toprule")
    add("Model & Test & $b$ & $c$ & $p$ & $p_{\\text{Holm}}$ \\\\")
    add("\\midrule")
    for model in ("haiku", "gpt-4o"):
        e = report["models"].get(model)
        if not e:
            continue
        for name, t in e["mcnemar_family"].items():
            add(f"{model} & {name.replace('_', ' ')} & {t['b']} & {t['c']} & "
                f"{t['p_value']:.4f} & {t['p_holm']:.4f} \\\\")
    add("\\bottomrule")
    add("\\end{tabular}")
    add("```")
    add("")
    (OUT_DIR / "stats_appendix.md").write_text("\n".join(lines))
    print(f"[done] wrote {OUT_DIR / 'stats_appendix.md'}")
    print(f"[done] wrote {OUT_DIR / 'stats_appendix.json'}")


if __name__ == "__main__":
    main()
