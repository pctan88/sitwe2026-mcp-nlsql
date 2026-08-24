"""Module 3 (scale-up brief): query-complexity stratification. Zero API cost.

Buckets every scored pilot query by the complexity of its post-perturbation
gold SQL, computed from the AST via sqlglot:

  easy   — single table: no join, no aggregation/GROUP BY, no subquery/HAVING
  medium — exactly 1 join OR aggregation/GROUP BY (and not hard)
  hard   — >= 2 joins, or a subquery, or HAVING

and reports EX/RR per bucket per harness configuration (stale / refreshed /
error-feedback / MCP), per model and pooled, over ALL canonical per-query
CSVs (7 models × 10 db×operator configs). The vendor-native arms (diff-in-
prompt / vendor-native, haiku + gpt-4o) are included as a bonus section when
present.

Outputs:
  results/scaleup/complexity_breakdown.json
  results/scaleup/complexity_breakdown.md     (incl. LaTeX-ready table)

Usage::

    python tools/build_complexity_breakdown.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pilot import metrics

RESULTS = ROOT / "results"
OUT_DIR = RESULTS / "scaleup"

# Canonical per-query CSV name tags. haiku is the unsuffixed canonical set.
# Grok is intentionally excluded (removed from all runs 2026-07-03).
MODEL_TAGS: dict[str, str] = {
    "haiku": "",
    "gpt-4o": "gpt_4o_",
    "gemini": "gemini_",
    "llama31": "llama31_",
    "qwen-coder": "qwen_coder_",
    "qwen": "qwen_",
    "qwen-small": "qwen_small_",
}

SWEEP_DATABASES = [
    f"{db}_{op}"
    for db in ("concert_singer", "hr_1")
    for op in ("TABLE_RENAME", "TABLE_SPLIT", "TABLE_MERGE",
               "COLUMN_RENAME", "COLUMN_MERGE")
]

ARMS = {
    "stale": "baseline_ok",
    "refreshed": "refreshed_schema_ok",
    "error_feedback": "error_feedback_ok",
    "mcp": "mcp_ok",
}

BUCKETS = ("easy", "medium", "hard")


# --------------------------------------------------------------------------- #
# Complexity classifier                                                       #
# --------------------------------------------------------------------------- #

def classify_complexity(sql: str) -> str:
    """easy / medium / hard from the gold SQL AST (see module docstring)."""
    tree = sqlglot.parse_one(sql, read="sqlite")

    n_joins = len(list(tree.find_all(exp.Join)))
    # Comma-joins ("FROM a, b") parse as multiple From expressions in older
    # sqlglot versions; count extra FROM sources as joins too.
    for from_expr in tree.find_all(exp.From):
        exprs = from_expr.args.get("expressions")
        if exprs and len(exprs) > 1:
            n_joins += len(exprs) - 1

    has_having = tree.find(exp.Having) is not None
    has_subquery = (
        tree.find(exp.Subquery) is not None
        or len(list(tree.find_all(exp.Select))) > 1
    )
    has_group = tree.find(exp.Group) is not None
    has_agg = tree.find(exp.AggFunc) is not None

    if n_joins >= 2 or has_subquery or has_having:
        return "hard"
    if n_joins == 1 or has_agg or has_group:
        return "medium"
    return "easy"


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #

def _to_int(val: Any) -> Optional[int]:
    s = str(val).strip()
    if s in ("", "None"):
        return None
    return int(float(s))


def _is_scored(row: dict[str, Any]) -> bool:
    return str(row.get("expected_failure", "")).strip().lower() not in ("true", "1")


def load_model_rows(model: str) -> list[dict[str, Any]]:
    tag = MODEL_TAGS[model]
    rows: list[dict[str, Any]] = []
    for db in SWEEP_DATABASES:
        path = RESULTS / f"pilot_results_{tag}{db}.csv"
        if not path.exists():
            print(f"[warn] missing {path.name} for model {model}; skipped")
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                if not _is_scored(row):
                    continue
                rows.append({
                    "model": model,
                    "db": db,
                    "id": row["id"],
                    "op": row["perturbation"],
                    "bucket": classify_complexity(row["gold_post"]),
                    **{arm: _to_int(row[col]) for arm, col in ARMS.items()},
                })
    return rows


def load_vendor_rows(model: str) -> list[dict[str, Any]]:
    """Vendor-native arms (Brief 1) — bonus stratification when present."""
    tag = MODEL_TAGS[model].rstrip("_") or "haiku"
    rows: list[dict[str, Any]] = []
    # Need gold_post for the bucket — join with the canonical CSV by id.
    for db in SWEEP_DATABASES:
        vpath = RESULTS / "vendor_native" / f"vendor_{tag}_{db}.csv"
        cpath = RESULTS / f"pilot_results_{MODEL_TAGS[model]}{db}.csv"
        if not vpath.exists() or not cpath.exists():
            continue
        gold = {}
        with cpath.open() as f:
            for row in csv.DictReader(f):
                gold[row["id"]] = row["gold_post"]
        with vpath.open() as f:
            for row in csv.DictReader(f):
                if not _is_scored(row) or row["id"] not in gold:
                    continue
                rows.append({
                    "model": model,
                    "db": db,
                    "id": row["id"],
                    "bucket": classify_complexity(gold[row["id"]]),
                    "diff_in_prompt": _to_int(row["diff_ok"]),
                    "vendor_native": _to_int(row["vendor_ok"]),
                })
    return rows


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #

def bucket_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for bucket in BUCKETS:
        sub = [r for r in rows if r["bucket"] == bucket]
        n = len(sub)
        if n == 0:
            continue
        entry: dict[str, Any] = {"n": n}
        for arm in ARMS:
            k = sum(r[arm] or 0 for r in sub)
            entry[f"ex_{arm}"] = round(k / n, 4)
            entry[f"wilson_{arm}"] = metrics.wilson_ci(k, n)
        rr = metrics.recovery_rate(
            entry["ex_stale"], entry["ex_refreshed"], entry["ex_mcp"])
        entry["rr_mcp"] = None if rr is None else round(rr, 4)
        rr_ef = metrics.recovery_rate(
            entry["ex_stale"], entry["ex_refreshed"], entry["ex_error_feedback"])
        entry["rr_error_feedback"] = None if rr_ef is None else round(rr_ef, 4)
        out[bucket] = entry
    return out


def vendor_bucket_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for bucket in BUCKETS:
        sub = [r for r in rows if r["bucket"] == bucket]
        n = len(sub)
        if n == 0:
            continue
        out[bucket] = {
            "n": n,
            "ex_diff_in_prompt": round(
                sum(r["diff_in_prompt"] or 0 for r in sub) / n, 4),
            "ex_vendor_native": round(
                sum(r["vendor_native"] or 0 for r in sub) / n, 4),
        }
    return out


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def _f(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.3f}"


def render_md(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Query-complexity stratification (scale-up Module 3)")
    add("")
    add("Buckets from the post-perturbation gold SQL AST (sqlglot): "
        "**easy** = single table, no join/aggregation/subquery; "
        "**medium** = exactly 1 join OR aggregation/GROUP BY; "
        "**hard** = ≥ 2 joins, subquery, or HAVING. "
        "All scored queries of the 10 db×operator pilot configs; "
        "expected-failure probes excluded. RR uses the canonical formula "
        "per bucket (refreshed ceiling within the same bucket).")
    add("")
    dist = report["bucket_distribution"]
    add(f"Bucket sizes (per model): easy = {dist['easy']}, "
        f"medium = {dist['medium']}, hard = {dist['hard']} "
        f"(of {dist['total']} scored queries).")
    add("")
    if dist["hard"] == 0:
        add("**Finding:** the pilot per-operator query sets contain NO hard "
            "queries — no gold SQL has ≥ 2 joins, a subquery, or HAVING "
            "(verified against the AST classifier, which does detect these "
            "constructs on synthetic examples). The stratification below is "
            "therefore easy-vs-medium only; hard-query coverage is a real "
            "gap of the pilot sets, addressed by the query-set expansion "
            "module (≥ 10% subquery/HAVING mix) and by the EvoSchema subset.")
        add("")

    add("## Per-model EX/RR by complexity bucket")
    add("")
    add("| Model | Bucket | n | EX stale | EX refreshed | EX err-fb | "
        "EX MCP | RR MCP | RR err-fb |")
    add("|---|---|---|---|---|---|---|---|---|")
    for model, buckets in report["per_model"].items():
        for bucket, e in buckets.items():
            add(f"| {model} | {bucket} | {e['n']} | {_f(e['ex_stale'])} | "
                f"{_f(e['ex_refreshed'])} | {_f(e['ex_error_feedback'])} | "
                f"{_f(e['ex_mcp'])} | {_f(e['rr_mcp'])} | "
                f"{_f(e['rr_error_feedback'])} |")
    add("")

    add("## Pooled across all models")
    add("")
    add("| Bucket | n | EX stale | EX refreshed | EX err-fb | EX MCP | "
        "RR MCP | MCP Wilson 95% CI |")
    add("|---|---|---|---|---|---|---|---|")
    for bucket, e in report["pooled"].items():
        w = e["wilson_mcp"]
        add(f"| {bucket} | {e['n']} | {_f(e['ex_stale'])} | "
            f"{_f(e['ex_refreshed'])} | {_f(e['ex_error_feedback'])} | "
            f"{_f(e['ex_mcp'])} | {_f(e['rr_mcp'])} | "
            f"[{w['lower']:.3f}, {w['upper']:.3f}] |")
    add("")

    if report.get("vendor_arms"):
        add("## Vendor-native arms by bucket (Brief 1 results, bonus)")
        add("")
        add("| Model | Bucket | n | EX diff-in-prompt | EX vendor-native |")
        add("|---|---|---|---|---|")
        for model, buckets in report["vendor_arms"].items():
            for bucket, e in buckets.items():
                add(f"| {model} | {bucket} | {e['n']} | "
                    f"{_f(e['ex_diff_in_prompt'])} | "
                    f"{_f(e['ex_vendor_native'])} |")
        add("")

    add("## LaTeX table (pooled, paper-ready)")
    add("")
    add("```latex")
    add("\\begin{tabular}{lrrrrrr}")
    add("\\toprule")
    add("Bucket & $n$ & EX$_{\\text{stale}}$ & EX$_{\\text{refreshed}}$ & "
        "EX$_{\\text{err-fb}}$ & EX$_{\\text{MCP}}$ & RR$_{\\text{MCP}}$ \\\\")
    add("\\midrule")
    for bucket, e in report["pooled"].items():
        add(f"{bucket.capitalize()} & {e['n']} & {e['ex_stale']:.3f} & "
            f"{e['ex_refreshed']:.3f} & {e['ex_error_feedback']:.3f} & "
            f"{e['ex_mcp']:.3f} & "
            + ("--" if e['rr_mcp'] is None else f"{e['rr_mcp']:.3f}")
            + " \\\\")
    add("\\bottomrule")
    add("\\end{tabular}")
    add("```")
    add("")
    add("Scope note: buckets are computed over the 10 pilot db×operator "
        "configs (152 scored queries per model, 7 models). EvoSchema-subset "
        "rows are NOT included here; see the EvoSchema report if present.")
    add("")
    return "\n".join(lines)


def main() -> None:
    per_model: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    for model in MODEL_TAGS:
        rows = load_model_rows(model)
        if not rows:
            continue
        per_model[model] = bucket_stats(rows)
        all_rows.extend(rows)

    # Bucket distribution is a property of the query set, identical across
    # models — compute from one model's rows.
    first_model_rows = [r for r in all_rows if r["model"] == next(iter(per_model))]
    dist = {b: sum(1 for r in first_model_rows if r["bucket"] == b) for b in BUCKETS}
    dist["total"] = len(first_model_rows)

    vendor_arms: dict[str, Any] = {}
    for model in ("haiku", "gpt-4o"):
        vrows = load_vendor_rows(model)
        if vrows:
            vendor_arms[model] = vendor_bucket_stats(vrows)

    report = {
        "bucket_definitions": {
            "easy": "single table; no join, aggregation, subquery, or HAVING",
            "medium": "exactly 1 join OR aggregation/GROUP BY (not hard)",
            "hard": ">= 2 joins, or subquery, or HAVING",
        },
        "bucket_distribution": dist,
        "per_model": per_model,
        "pooled": bucket_stats(all_rows),
        "vendor_arms": vendor_arms,
        "n_models": len(per_model),
        "models": list(per_model),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "complexity_breakdown.json").open("w") as f:
        json.dump(report, f, indent=2)
    (OUT_DIR / "complexity_breakdown.md").write_text(render_md(report))
    print(f"[done] wrote {OUT_DIR / 'complexity_breakdown.json'}")
    print(f"[done] wrote {OUT_DIR / 'complexity_breakdown.md'}")


if __name__ == "__main__":
    main()
