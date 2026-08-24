"""Build EVOSCHEMA_SUBSET_REPORT.md from the Module 1 run outputs.

Reads results/scaleup/summary_evoschema_{model}.json (+ the excluded-items
log) and renders the report per the scale-up brief: EX stale / refreshed /
error-feedback / MCP, RR pooled + per operator + per database, Wilson CIs,
McNemar vs stale, exclusion table, and an explicit subset-vs-full-benchmark
scope statement. Includes a LaTeX-ready pooled table.

Usage::

    python tools/build_evoschema_report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "results" / "scaleup"
MODELS = ["haiku", "gpt_4o"]


def _f(x: Optional[float], nd: int = 3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _ci(ci: dict[str, Any]) -> str:
    if ci.get("lower") is None:
        return "—"
    return f"[{ci['lower']:.3f}, {ci['upper']:.3f}]"


def render(summaries: dict[str, dict[str, Any]],
           excluded: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    any_s = next(iter(summaries.values()))

    add("# EvoSchema subset evaluation (scale-up Module 1, R1-1/R2-1)")
    add("")
    add(f"Real-benchmark run on a deterministic stratified subset of "
        f"**EvoSchema** (BIRD-dev substrate, 11 databases). Sampling rule "
        f"(seed-free): {excluded['sampling_rule']}. "
        f"Sampled {excluded['n_sampled']} items; "
        f"**{excluded['n_excluded']} excluded by the gold-SQL gate** "
        f"(both golds must execute with non-empty results and match "
        f"pre/post, row-count equality for COLUMN_MERGE); "
        f"**{excluded['n_included']} items evaluated** across "
        f"{any_s['n_databases']} databases. The five in-scope operator "
        f"files total {any_s['full_benchmark_size_in_scope']} items — this "
        f"is a subset evaluation; the full benchmark remains future work.")
    add("")
    add("Method notes: 4-configuration harness (stale / refreshed / "
        "error-feedback / MCP) exactly as the pilot; temperature 0, strict "
        "mode (zero mock contamination); embedding validate backend; the "
        "pre-EX column reuses the stale generation's SQL scored on the "
        "pre-DB (no separate call at T=0). The brief's mention of Spider "
        "databases was corrected against the benchmark inventory — "
        "EvoSchema perturbs BIRD-dev.")
    add("")

    add("## 1. Headline (pooled over all operators and databases)")
    add("")
    add("| Model | n | EX pre (sanity) | EX stale | EX refreshed | "
        "EX err-fb | EX MCP | RR MCP | RR err-fb |")
    add("|---|---|---|---|---|---|---|---|---|")
    for m, s in summaries.items():
        add(f"| {m} | {s['n_evaluated']} | {_f(s['ex_pre'])} | "
            f"{_f(s['ex_post_baseline'])} | "
            f"{_f(s['ex_post_refreshed_schema'])} | "
            f"{_f(s['ex_post_error_feedback'])} | {_f(s['ex_post_mcp'])} | "
            f"{_f(s['recovery_rate'])} | "
            f"{_f(s['recovery_rate_error_feedback'])} |")
    add("")
    add("Wilson 95% CIs:")
    add("")
    add("| Model | stale | refreshed | error-feedback | MCP |")
    add("|---|---|---|---|---|")
    for m, s in summaries.items():
        w = s["wilson_ci"]
        add(f"| {m} | {_ci(w['baseline'])} | {_ci(w['refreshed_schema'])} | "
            f"{_ci(w['error_feedback'])} | {_ci(w['mcp'])} |")
    add("")

    add("## 2. McNemar (paired, exact two-sided)")
    add("")
    add("| Model | Comparison | b | c | χ² | p |")
    add("|---|---|---|---|---|---|")
    for m, s in summaries.items():
        for label, key in (("MCP vs stale", "baseline_vs_mcp"),
                           ("refreshed vs stale", "baseline_vs_refreshed_schema"),
                           ("error-feedback vs stale", "baseline_vs_error_feedback"),
                           ("MCP vs error-feedback", "mcp_vs_error_feedback")):
            t = s["mcnemar"][key]
            stat = "—" if t.get("statistic") is None else f"{t['statistic']:.3f}"
            add(f"| {m} | {label} | {t['b']} | {t['c']} | {stat} | "
                f"{t['p_value']:.6f} |")
    add("")

    add("## 3. Per-operator breakdown")
    add("")
    for m, s in summaries.items():
        add(f"**{m}**")
        add("")
        add("| Operator | n | EX stale | EX refreshed | EX err-fb | EX MCP | "
            "RR MCP | MCP Wilson CI |")
        add("|---|---|---|---|---|---|---|---|")
        for op, e in s["per_operator"].items():
            add(f"| {op} | {e['n']} | {_f(e['ex_stale'])} | "
                f"{_f(e['ex_refreshed'])} | {_f(e['ex_error_feedback'])} | "
                f"{_f(e['ex_mcp'])} | {_f(e['rr_mcp'])} | "
                f"{_ci(e['wilson_mcp'])} |")
        add("")

    add("## 4. Per-database breakdown")
    add("")
    for m, s in summaries.items():
        add(f"**{m}**")
        add("")
        add("| Database | n | EX stale | EX refreshed | EX MCP | RR MCP |")
        add("|---|---|---|---|---|---|")
        for db, e in s["per_database"].items():
            add(f"| {db} | {e['n']} | {_f(e['ex_stale'])} | "
                f"{_f(e['ex_refreshed'])} | {_f(e['ex_mcp'])} | "
                f"{_f(e['rr_mcp'])} |")
        add("")

    add("## 5. Excluded items (gold-SQL gate)")
    add("")
    by_reason: dict[str, int] = {}
    for e in excluded["excluded"]:
        key = e["reason"].split(":")[0]
        by_reason[key] = by_reason.get(key, 0) + 1
    add("| Reason (category) | n |")
    add("|---|---|")
    for r, n in sorted(by_reason.items(), key=lambda x: -x[1]):
        add(f"| {r} | {n} |")
    add("")
    add("<details><summary>Full exclusion list</summary>")
    add("")
    add("| id | op | db | reason |")
    add("|---|---|---|---|")
    for e in excluded["excluded"]:
        add(f"| {e['id']} | {e['op']} | {e['db_id']} | {e['reason'][:90]} |")
    add("")
    add("</details>")
    add("")

    add("## 6. Cost")
    add("")
    add("| Model | tokens in | tokens out | USD | contaminated |")
    add("|---|---|---|---|---|")
    for m, s in summaries.items():
        c = s["cost"]
        add(f"| {m} | {c['tokens_in']} | {c['tokens_out']} | "
            f"{c['usd']:.4f} | {s['contaminated_queries']} |")
    add("")

    add("## LaTeX table (paper-ready, pooled)")
    add("")
    add("```latex")
    add("\\begin{tabular}{lrrrrrr}")
    add("\\toprule")
    add("Model & $n$ & EX$_{\\text{stale}}$ & EX$_{\\text{refr}}$ & "
        "EX$_{\\text{err-fb}}$ & EX$_{\\text{MCP}}$ & RR \\\\")
    add("\\midrule")
    for m, s in summaries.items():
        add(f"{m} & {s['n_evaluated']} & {s['ex_post_baseline']:.3f} & "
            f"{s['ex_post_refreshed_schema']:.3f} & "
            f"{s['ex_post_error_feedback']:.3f} & "
            f"{s['ex_post_mcp']:.3f} & {s['recovery_rate']:.3f} \\\\")
    add("\\bottomrule")
    add("\\end{tabular}")
    add("```")
    add("")
    return "\n".join(lines)


def main() -> None:
    summaries: dict[str, dict[str, Any]] = {}
    for m in MODELS:
        path = OUT_DIR / f"summary_evoschema_{m}.json"
        if path.exists():
            summaries[m] = json.loads(path.read_text())
    if not summaries:
        raise SystemExit("[error] no summary_evoschema_*.json found — run "
                         "pilot.run_evoschema first")
    excluded = json.loads((OUT_DIR / "evoschema_excluded.json").read_text())
    report = OUT_DIR / "EVOSCHEMA_SUBSET_REPORT.md"
    report.write_text(render(summaries, excluded))
    print(f"[done] wrote {report}")


if __name__ == "__main__":
    main()
