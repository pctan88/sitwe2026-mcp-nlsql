"""Aggregate results/vendor_native/*.json|csv into VENDOR_NATIVE_REPORT.md.

Pools the vendor-native (Arm F) and diff-in-prompt (Arm D) results across
both DBs and all five operators, pairs them with the canonical MCP-arm
outcomes per query id, and emits the §4 report tables plus
vendor_summary.json.

Usage::

    python tools/build_vendor_report.py [--allow-partial]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pilot import metrics
from pilot.run_vendor_native import (
    PRICES_USD_PER_MTOK,
    SWEEP_DATABASES,
    canonical_paths,
)

VENDOR_DIR = ROOT / "results" / "vendor_native"
MODELS = ["haiku", "gpt-4o"]
OPERATORS = ["TABLE_RENAME", "TABLE_SPLIT", "TABLE_MERGE",
             "COLUMN_RENAME", "COLUMN_MERGE"]


def _tag(model: str) -> str:
    return model.replace("-", "_")


def _to_int(val: Any) -> Optional[int]:
    s = str(val).strip()
    if s in ("", "None"):
        return None
    return int(float(s))


def _is_scored(row: dict[str, Any]) -> bool:
    return str(row.get("expected_failure", "")).strip().lower() not in ("true", "1")


def _load_vendor_rows(model: str) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for db in SWEEP_DATABASES:
        path = VENDOR_DIR / f"vendor_{_tag(model)}_{db}.csv"
        if not path.exists():
            missing.append(db)
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                row["_db"] = db
                rows.append(row)
    return rows, missing


def _load_canonical_rows(model: str) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for db in SWEEP_DATABASES:
        csv_path, _ = canonical_paths(model, db)
        if not csv_path.exists():
            continue
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                out[(db, row["id"])] = row
    return out


def _load_vendor_summaries(model: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for db in SWEEP_DATABASES:
        path = VENDOR_DIR / f"summary_vendor_{_tag(model)}_{db}.json"
        if path.exists():
            out[db] = json.loads(path.read_text())
    return out


def _pool_model(model: str) -> dict[str, Any]:
    vendor_rows, missing = _load_vendor_rows(model)
    canonical = _load_canonical_rows(model)
    summaries = _load_vendor_summaries(model)

    scored = [r for r in vendor_rows if _is_scored(r)]
    paired = []
    for r in scored:
        can = canonical.get((r["_db"], r["id"]))
        if can is None or not _is_scored(can):
            continue
        paired.append({
            "db": r["_db"],
            "op": r["perturbation"],
            "sanity_ok": _to_int(r["sanity_ok"]),
            "diff_ok": _to_int(r["diff_ok"]),
            "vendor_ok": _to_int(r["vendor_ok"]),
            "baseline_ok": _to_int(can["baseline_ok"]),
            "refreshed_ok": _to_int(can["refreshed_schema_ok"]),
            "mcp_ok": _to_int(can["mcp_ok"]),
            "mcp_latency_s": float(can["latency_mcp_s"]),
        })

    n = len(paired)

    def ex(key: str) -> Optional[float]:
        return None if n == 0 else sum(p[key] or 0 for p in paired) / n

    def cnt(key: str) -> int:
        return sum(p[key] or 0 for p in paired)

    pooled = {
        "model": model,
        "missing_configs": missing,
        "n_configs": len(SWEEP_DATABASES) - len(missing),
        "n_scored": n,
        "ex": {
            "stale_canonical": ex("baseline_ok"),
            "stale_sanity": ex("sanity_ok"),
            "refreshed_canonical": ex("refreshed_ok"),
            "diff_in_prompt": ex("diff_ok"),
            "vendor_native": ex("vendor_ok"),
            "mcp_canonical": ex("mcp_ok"),
        },
        "agreement_sanity_vs_canonical": (
            None if n == 0 else
            sum(1 for p in paired if p["sanity_ok"] == p["baseline_ok"]) / n
        ),
        "wilson_ci": {
            "stale_sanity": metrics.wilson_ci(cnt("sanity_ok"), n),
            "diff_in_prompt": metrics.wilson_ci(cnt("diff_ok"), n),
            "vendor_native": metrics.wilson_ci(cnt("vendor_ok"), n),
            "mcp_canonical": metrics.wilson_ci(cnt("mcp_ok"), n),
        },
        "mcnemar": {
            "vendor_vs_mcp": metrics.mcnemar_test(
                [p["mcp_ok"] for p in paired], [p["vendor_ok"] for p in paired]),
            "diff_vs_mcp": metrics.mcnemar_test(
                [p["mcp_ok"] for p in paired], [p["diff_ok"] for p in paired]),
            "vendor_vs_diff": metrics.mcnemar_test(
                [p["diff_ok"] for p in paired], [p["vendor_ok"] for p in paired]),
        },
    }

    # RR (locked): canonical pooled stale floor, canonical pooled refreshed ceiling.
    stale = pooled["ex"]["stale_canonical"]
    refreshed = pooled["ex"]["refreshed_canonical"]
    pooled["rr"] = {}
    if stale is not None and refreshed is not None:
        for arm in ("diff_in_prompt", "vendor_native", "mcp_canonical"):
            arm_ex = pooled["ex"][arm]
            pooled["rr"][arm] = (
                None if arm_ex is None
                else metrics.recovery_rate(stale, refreshed, arm_ex)
            )

    # Per-operator RR (pooled over the two DBs).
    per_op: dict[str, Any] = {}
    for op in OPERATORS:
        sub = [p for p in paired if p["op"] == op]
        m = len(sub)
        if m == 0:
            continue
        s = sum(p["baseline_ok"] or 0 for p in sub) / m
        rfr = sum(p["refreshed_ok"] or 0 for p in sub) / m
        entry: dict[str, Any] = {"n": m, "ex_stale": round(s, 4),
                                 "ex_refreshed": round(rfr, 4)}
        for arm, key in (("diff_in_prompt", "diff_ok"),
                         ("vendor_native", "vendor_ok"),
                         ("mcp_canonical", "mcp_ok")):
            arm_ex = sum(p[key] or 0 for p in sub) / m
            entry[f"ex_{arm}"] = round(arm_ex, 4)
            rr = metrics.recovery_rate(s, rfr, arm_ex)
            entry[f"rr_{arm}"] = None if rr is None else round(rr, 4)
        per_op[op] = entry
    pooled["per_operator"] = per_op

    # Cost & latency, from the per-query vendor rows.
    def arm_stats(prefix: str) -> dict[str, Any]:
        tin = sum(int(_to_int(r[f"{prefix}_tokens_in"]) or 0) for r in scored)
        tout = sum(int(_to_int(r[f"{prefix}_tokens_out"]) or 0) for r in scored)
        lats = [float(r[f"{prefix}_latency_s"]) for r in scored]
        prices = PRICES_USD_PER_MTOK[model]
        usd = tin * prices["input"] / 1e6 + tout * prices["output"] / 1e6
        return {
            "mean_tokens_in": round(tin / max(n, 1), 1),
            "mean_tokens_out": round(tout / max(n, 1), 1),
            "total_usd": round(usd, 4),
            "usd_per_query": round(usd / max(n, 1), 6),
            "latency": metrics.latency_stats(lats),
        }

    pooled["cost_latency"] = {
        "sanity": arm_stats("sanity"),
        "diff_in_prompt": arm_stats("diff"),
        "vendor_native": arm_stats("vendor"),
        "mcp_canonical_latency": metrics.latency_stats(
            [p["mcp_latency_s"] for p in paired]),
    }

    # Tool-loop behaviour.
    tool_usage = {"get_schema_diff": 0, "relink_sql": 0, "validate_sql": 0}
    no_tools = capped = api_calls = tool_calls = 0
    for r in scored:
        calls = json.loads(r["tool_calls_json"]) if r.get("tool_calls_json") else []
        for name in {c["tool"] for c in calls}:
            if name in tool_usage:
                tool_usage[name] += 1
        no_tools += int(not calls)
        capped += int(_to_int(r["vendor_capped"]) or 0)
        api_calls += int(_to_int(r["vendor_n_api_calls"]) or 0)
        tool_calls += len(calls)
    pooled["tool_loop"] = {
        "mean_api_calls_per_query": round(api_calls / max(n, 1), 3),
        "mean_tool_calls_per_query": round(tool_calls / max(n, 1), 3),
        "pct_queries_using_tool": {
            k: round(v / max(n, 1), 4) for k, v in tool_usage.items()},
        "pct_queries_no_tools": round(no_tools / max(n, 1), 4),
        "pct_queries_capped": round(capped / max(n, 1), 4),
    }

    pooled["contaminated_queries"] = sum(
        s.get("contaminated_queries", 0) for s in summaries.values())
    pooled["strict_all_runs"] = all(
        s.get("strict_mode", False) for s in summaries.values()) if summaries else False
    pooled["low_agreement_configs"] = sorted(
        db for db, s in summaries.items()
        if (s.get("sanity_agreement_with_canonical") or 0) < 0.9
    )
    pooled["per_config_agreement"] = {
        db: s.get("sanity_agreement_with_canonical")
        for db, s in sorted(summaries.items())
    }
    return pooled


# --------------------------------------------------------------------------- #
# Markdown rendering                                                          #
# --------------------------------------------------------------------------- #

def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.3f}"


def _ci(ci: dict[str, Any]) -> str:
    if ci.get("lower") is None:
        return "—"
    return f"[{ci['lower']:.3f}, {ci['upper']:.3f}]"


def _mcnemar_row(label: str, m: dict[str, Any]) -> str:
    stat = "—" if m.get("statistic") is None else f"{m['statistic']:.3f}"
    return (f"| {label} | {m['b']} | {m['c']} | {stat} | {m['p_value']:.4f} |")


def render_markdown(pools: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Vendor-Native Function-Calling Report (SITWE 2026 revision)")
    add("")
    add("Reviewer comments R1-2 / R2-2: direct comparison between the "
        "MCP-mediated pipeline and vendor-native function calling "
        "(Anthropic tool use for Haiku 4.5, OpenAI function calling for "
        "GPT-4o), plus a diff-in-prompt arm that isolates *information* "
        "from *protocol*.")
    add("")
    add("Notes on method: the stale baseline was re-run as a sanity check "
        "(agreement with the canonical baseline reported below); the "
        "**refreshed ceiling is reused from the canonical "
        "`results/summary_{config}.json` files and was not re-run** "
        "(saves ~30% of the API cost). RR uses the locked canonical "
        "formula with the canonical EX_stale floor and canonical "
        "EX_refreshed ceiling. All live runs used `--strict` "
        "(zero mock contamination) at temperature 0.")
    add("")

    # 1. Headline
    add("## 1. Headline results (pooled: 2 DBs × 5 operators)")
    add("")
    add("| Model | N | EX stale (canon) | EX stale (sanity) | Agreement | "
        "EX refreshed (canon) | EX diff-in-prompt (D) | EX vendor-native (F) | "
        "EX MCP (canon) | RR D | RR F | RR MCP |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for model, p in pools.items():
        e, r = p["ex"], p["rr"]
        add(f"| {model} | {p['n_scored']} | {_pct(e['stale_canonical'])} | "
            f"{_pct(e['stale_sanity'])} | "
            f"{_pct(p['agreement_sanity_vs_canonical'])} | "
            f"{_pct(e['refreshed_canonical'])} | {_pct(e['diff_in_prompt'])} | "
            f"{_pct(e['vendor_native'])} | {_pct(e['mcp_canonical'])} | "
            f"{_pct(r.get('diff_in_prompt'))} | {_pct(r.get('vendor_native'))} | "
            f"{_pct(r.get('mcp_canonical'))} |")
    add("")
    add("Wilson 95% CIs:")
    add("")
    add("| Model | stale (sanity) | diff-in-prompt | vendor-native | MCP (canon) |")
    add("|---|---|---|---|---|")
    for model, p in pools.items():
        w = p["wilson_ci"]
        add(f"| {model} | {_ci(w['stale_sanity'])} | {_ci(w['diff_in_prompt'])} | "
            f"{_ci(w['vendor_native'])} | {_ci(w['mcp_canonical'])} |")
    add("")

    # 2. McNemar
    add("## 2. McNemar tests (paired by query id, pooled)")
    add("")
    add("`b` = first arm wrong & second arm right; `c` = first arm right & "
        "second arm wrong; exact two-sided p, continuity-corrected χ².")
    add("")
    for model, p in pools.items():
        add(f"**{model}**")
        add("")
        add("| Comparison | b | c | χ² | p |")
        add("|---|---|---|---|---|")
        m = p["mcnemar"]
        add(_mcnemar_row("F (vendor-native) vs MCP", m["vendor_vs_mcp"]))
        add(_mcnemar_row("D (diff-in-prompt) vs MCP", m["diff_vs_mcp"]))
        add(_mcnemar_row("F vs D", m["vendor_vs_diff"]))
        add("")

    # 3. Per-operator
    add("## 3. Per-operator recovery rates (pooled over both DBs)")
    add("")
    for model, p in pools.items():
        add(f"**{model}**")
        add("")
        add("| Operator | n | EX stale | EX refreshed | RR D | RR F | RR MCP |")
        add("|---|---|---|---|---|---|---|")
        for op, e in p["per_operator"].items():
            add(f"| {op} | {e['n']} | {e['ex_stale']:.3f} | "
                f"{e['ex_refreshed']:.3f} | {_pct(e['rr_diff_in_prompt'])} | "
                f"{_pct(e['rr_vendor_native'])} | {_pct(e['rr_mcp_canonical'])} |")
        add("")

    # 4. Cost & latency
    add("## 4. Cost & latency per arm")
    add("")
    add("Prices (USD per MTok, list, Aug 2026): " + ", ".join(
        f"{m}: in ${PRICES_USD_PER_MTOK[m]['input']:.2f} / "
        f"out ${PRICES_USD_PER_MTOK[m]['output']:.2f}"
        for m in pools))
    add("")
    add("| Model | Arm | mean tok in | mean tok out | USD/query | "
        "mean lat (s) | median lat (s) |")
    add("|---|---|---|---|---|---|---|")
    for model, p in pools.items():
        for arm in ("sanity", "diff_in_prompt", "vendor_native"):
            c = p["cost_latency"][arm]
            add(f"| {model} | {arm} | {c['mean_tokens_in']} | "
                f"{c['mean_tokens_out']} | {c['usd_per_query']:.6f} | "
                f"{c['latency']['mean']:.3f} | {c['latency']['median']:.3f} |")
        mcp_lat = p["cost_latency"]["mcp_canonical_latency"]
        add(f"| {model} | MCP (canonical, reference) | — | — | — | "
            f"{mcp_lat['mean']:.3f} | {mcp_lat['median']:.3f} |")
    add("")

    # 5. Tool-loop behaviour
    add("## 5. Tool-loop behaviour (Arm F)")
    add("")
    add("| Model | mean API calls | mean tool calls | % get_schema_diff | "
        "% relink_sql | % validate_sql | % no tools | % capped |")
    add("|---|---|---|---|---|---|---|---|")
    for model, p in pools.items():
        t = p["tool_loop"]
        u = t["pct_queries_using_tool"]
        add(f"| {model} | {t['mean_api_calls_per_query']} | "
            f"{t['mean_tool_calls_per_query']} | "
            f"{u['get_schema_diff']:.1%} | {u['relink_sql']:.1%} | "
            f"{u['validate_sql']:.1%} | {t['pct_queries_no_tools']:.1%} | "
            f"{t['pct_queries_capped']:.1%} |")
    add("")

    # Integrity
    add("## Integrity checks")
    add("")
    for model, p in pools.items():
        add(f"- **{model}**: {p['n_configs']}/10 configs present; "
            f"contaminated_queries = {p['contaminated_queries']}; "
            f"strict mode in all runs = {p['strict_all_runs']}; "
            f"configs with sanity agreement < 0.9: "
            f"{p['low_agreement_configs'] or 'none'}.")
    add("")

    # 6. Interpretation
    add("## 6. Interpretation")
    add("")
    for model, p in pools.items():
        m = p["mcnemar"]["vendor_vs_mcp"]
        e = p["ex"]
        p_f_mcp = m["p_value"]
        direction = (
            "higher than" if (e["vendor_native"] or 0) > (e["mcp_canonical"] or 0)
            else "lower than" if (e["vendor_native"] or 0) < (e["mcp_canonical"] or 0)
            else "equal to"
        )
        verdict = (
            "statistically indistinguishable from"
            if p_f_mcp >= 0.05 else "statistically different from"
        )
        add(f"For **{model}**, vendor-native function calling reached "
            f"EX = {_pct(e['vendor_native'])} — numerically {direction} the "
            f"MCP arm's canonical EX = {_pct(e['mcp_canonical'])} — and is "
            f"{verdict} the MCP arm under McNemar's exact test "
            f"(b = {m['b']}, c = {m['c']}, p = {p_f_mcp:.4f}). "
            f"The diff-in-prompt arm reached EX = {_pct(e['diff_in_prompt'])} "
            f"(vs MCP: p = {p['mcnemar']['diff_vs_mcp']['p_value']:.4f}; "
            f"vs vendor-native: "
            f"p = {p['mcnemar']['vendor_vs_diff']['p_value']:.4f}), showing "
            f"how much of the recovery is attributable to the *information* "
            f"(the classified diff) rather than the delivery *protocol*.")
        add("")
    add("These results are reported as measured, whichever direction they "
        "fall; the paper's §VI prediction is that vendor-native accuracy is "
        "statistically indistinguishable from the MCP-mediated pipeline, "
        "with MCP's contribution being standardised discovery/transport "
        "rather than accuracy.")
    add("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-partial", action="store_true",
                    help="Build the report even if some configs are missing.")
    args = ap.parse_args()

    pools: dict[str, dict[str, Any]] = {}
    for model in MODELS:
        pool = _pool_model(model)
        if pool["missing_configs"] and not args.allow_partial:
            raise SystemExit(
                f"[error] model {model}: missing vendor results for "
                f"{pool['missing_configs']}. Run the sweep first or pass "
                f"--allow-partial."
            )
        pools[model] = pool

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    report_path = VENDOR_DIR / "VENDOR_NATIVE_REPORT.md"
    report_path.write_text(render_markdown(pools))
    summary_path = VENDOR_DIR / "vendor_summary.json"
    with summary_path.open("w") as f:
        json.dump(pools, f, indent=2)
    print(f"[done] wrote {report_path}")
    print(f"[done] wrote {summary_path}")


if __name__ == "__main__":
    main()
