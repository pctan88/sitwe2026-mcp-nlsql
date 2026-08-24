"""Scale-up Module 5: SFD evaluation on the expanded 150-case set.

Evaluates silent-failure detection on ``data/sfd_labels_expanded.json``
(and, for comparison, on the canonical 60-case ``data/sfd_labels.json``):

  * v1 token-Jaccard backend at its default threshold,
  * v2 embedding backend swept over thresholds 0.35–0.60 (step 0.05),
  * a recall OR-ensemble (G1 gate target: recall > 0.5 at precision >= 0.9)
    combining the embedding verdict with three zero-cost rule signals:
      (b) empty/zero result on an affirmative question (reuses validate's
          own heuristics),
      (c) row-count plausibility: a scalar-type question ("how many",
          "what is the average/highest ...") whose result has > 1 row,
      (a) result-arity: a multi-attribute ask ("name and country ...")
          answered with a single output column.

Everything runs locally (MiniLM on CPU); zero API cost. This is EVIDENCE
ONLY — no default threshold or ensemble is locked without sign-off.

Outputs: results/scaleup/sfd_expanded_eval.json + SFD_EXPANSION_REPORT.md

Usage::

    python tools/eval_sfd_expanded.py
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_server import validate as vl

OUT_DIR = ROOT / "results" / "scaleup"
THRESHOLDS = [0.35, 0.40, 0.45, 0.50, 0.55, 0.60]

# A question whose natural answer is a single scalar row: an aggregate ask
# ("count/average/total/how many ...") with no per-group breakdown ("by X" /
# "per X" / "for each X" asks yield one row per group and are excluded).
_SCALAR_Q = re.compile(
    r"^\s*(count|how many|how much|average|total|sum|number of|"
    r"max(imum)?|min(imum)?|what is the (average|total|number|count|"
    r"maximum|minimum|highest|lowest|oldest|youngest|largest|smallest))\b",
    re.IGNORECASE,
)
_GROUPED_Q = re.compile(r"\b(by|per|for each|in each)\b", re.IGNORECASE)
# A multi-attribute ask ("list name and country of ...", "show X, Y") whose
# answer needs >= 2 output columns.
_MULTI_ATTR_Q = re.compile(
    r"^\s*(list|show|find|give|what are|display)\b[^?]*?\w(,\s*\w| and \w)",
    re.IGNORECASE,
)
# A list-type ask whose natural answer is a multi-row set; exactly one
# result row suggests silent truncation (the spurious-LIMIT failure mode).
_LIST_Q = re.compile(r"^\s*(list|show|what are|display)\b", re.IGNORECASE)


def _exec(db: str, sql: str) -> tuple[bool, Any]:
    try:
        con = sqlite3.connect(db)
        try:
            cur = con.execute(sql)
            rows = cur.fetchall()
            n_cols = len(cur.description) if cur.description else 0
        finally:
            con.close()
        return True, (rows, n_cols)
    except Exception as exc:
        return False, str(exc)


def _signals(case: dict[str, Any], db: str) -> Optional[dict[str, Any]]:
    """Per-case signals computed ONCE (the sweep then re-thresholds them)."""
    ok, res = _exec(db, case["sql"])
    if not ok:
        return None  # exec errors are H0's, not SFD's
    rows, n_cols = res
    question = case["question"]
    para = vl.sql_to_text(case["sql"])
    sim = vl.embedding_similarity(para, question)

    affirmative_empty = vl._affirmative(question) and (
        len(rows) == 0 or vl._is_zero_count(rows, case["sql"]))
    scalar_multi_rows = (
        bool(_SCALAR_Q.match(question))
        and not _GROUPED_Q.search(question)
        and len(rows) > 1
    )
    arity_low = bool(_MULTI_ATTR_Q.match(question)) and n_cols == 1
    list_single_row = bool(_LIST_Q.match(question)) and len(rows) == 1

    return {
        "id": case["id"],
        "label": case["label"],
        "mutation": case["mutation"],
        "similarity": sim,
        "rule_b_affirmative_empty": affirmative_empty,
        "rule_c_scalar_multi_rows": scalar_multi_rows,
        "rule_a_arity_low": arity_low,
        "rule_d_list_single_row": list_single_row,
    }


def _pr(cases: list[dict[str, Any]], predict) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for c in cases:
        pred = predict(c)
        actual = c["label"] == "silent_failure"
        if pred and actual:
            tp += 1
        elif pred and not actual:
            fp += 1
        elif not pred and actual:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else None)
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": None if precision is None else round(precision, 4),
        "recall": None if recall is None else round(recall, 4),
        "f1": None if f1 is None else round(f1, 4),
    }


def evaluate_set(labels_path: Path) -> dict[str, Any]:
    payload = json.loads(labels_path.read_text())
    cases = payload["cases"]

    work = Path(tempfile.mkdtemp(prefix="sfd_exp_"))
    db_cache: dict[str, str] = {}

    def _db(rel: str) -> str:
        if rel not in db_cache:
            dst = work / rel.replace("/", "_")
            shutil.copyfile(ROOT / "data" / rel, dst)
            db_cache[rel] = str(dst)
        return db_cache[rel]

    sigs: list[dict[str, Any]] = []
    n_exec_error = 0
    for c in cases:
        s = _signals(c, _db(c["db"]))
        if s is None:
            n_exec_error += 1
            continue
        sigs.append(s)

    v2 = {t: (lambda c, t=t: c["similarity"] < t
              or c["rule_b_affirmative_empty"]) for t in THRESHOLDS}
    # validate()'s SILENT covers both low-sim and affirmative-empty; the
    # standalone rules below isolate each signal's contribution.
    predictors: dict[str, Any] = {}
    for t in THRESHOLDS:
        predictors[f"v2@{t:.2f}"] = v2[t]
        predictors[f"v2@{t:.2f}+c"] = (
            lambda c, t=t: v2[t](c) or c["rule_c_scalar_multi_rows"])
        predictors[f"v2@{t:.2f}+c+a"] = (
            lambda c, t=t: v2[t](c) or c["rule_c_scalar_multi_rows"]
            or c["rule_a_arity_low"])
        predictors[f"v2@{t:.2f}+all_rules"] = (
            lambda c, t=t: v2[t](c) or c["rule_c_scalar_multi_rows"]
            or c["rule_a_arity_low"] or c["rule_d_list_single_row"])
    predictors["rule_b_alone"] = lambda c: c["rule_b_affirmative_empty"]
    predictors["rule_c_alone"] = lambda c: c["rule_c_scalar_multi_rows"]
    predictors["rule_a_alone"] = lambda c: c["rule_a_arity_low"]
    predictors["rule_d_alone"] = lambda c: c["rule_d_list_single_row"]

    frontier = {name: _pr(sigs, fn) for name, fn in predictors.items()}

    # Per-mutation recall for the reference point v2@0.45 and its ensembles.
    def recall_by_mutation(predict) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mut in sorted({s["mutation"] for s in sigs if s["label"] == "silent_failure"}):
            sub = [s for s in sigs
                   if s["mutation"] == mut and s["label"] == "silent_failure"]
            hit = sum(1 for s in sub if predict(s))
            out[mut] = {"n": len(sub), "recall": round(hit / len(sub), 4)}
        return out

    return {
        "labels_file": labels_path.name,
        "n_cases": len(cases),
        "n_scored": len(sigs),
        "n_exec_error": n_exec_error,
        "n_valid": sum(1 for s in sigs if s["label"] == "valid"),
        "n_silent": sum(1 for s in sigs if s["label"] == "silent_failure"),
        "frontier": frontier,
        "recall_by_mutation": {
            "v2@0.45": recall_by_mutation(predictors["v2@0.45"]),
            "v2@0.45+all_rules": recall_by_mutation(
                predictors["v2@0.45+all_rules"]),
        },
    }


def render_md(orig: dict[str, Any], exp: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# SFD expansion + recall ensemble (scale-up Module 5)")
    add("")
    add(f"Expanded labelled set: **{exp['n_cases']} cases** "
        f"({exp['n_valid']} valid / {exp['n_silent']} silent, 1:2 ratio "
        f"kept), built deterministically by `data/build_sfd_labels.py "
        f"--expanded --valid-per-dir 5 --silent-per-dir 10` — adds the "
        f"off-by-one-LIMIT (incl. spurious LIMIT 1) and wrong-JOIN-key "
        f"mutation families with dir-level family balancing. The canonical "
        f"60-case set is unchanged (default rebuild verified byte-identical)."
        f" All evaluation is local (MiniLM CPU), zero API cost.")
    add("")
    add("**This is evidence only — no threshold or ensemble is locked "
        "without sign-off.** The G1 gate target is recall > 0.5 at "
        "precision ≥ 0.9.")
    add("")

    def table(pool: dict[str, Any], title: str) -> None:
        add(f"## {title}")
        add("")
        add("| Detector | TP | FP | FN | TN | Precision | Recall | F1 |")
        add("|---|---|---|---|---|---|---|---|")
        for name, m in pool["frontier"].items():
            add(f"| {name} | {m['tp']} | {m['fp']} | {m['fn']} | {m['tn']} | "
                f"{m['precision']} | {m['recall']} | {m['f1']} |")
        add("")

    table(exp, f"Precision/recall frontier — expanded set "
               f"(n={exp['n_scored']})")
    table(orig, f"Same frontier — canonical 60-case set "
                f"(n={orig['n_scored']}, for comparison)")

    add("## Recall by mutation family (expanded set)")
    add("")
    add("| Mutation | n | recall v2@0.45 | recall v2@0.45+all_rules |")
    add("|---|---|---|---|")
    r1 = exp["recall_by_mutation"]["v2@0.45"]
    r2 = exp["recall_by_mutation"]["v2@0.45+all_rules"]
    for mut in r1:
        add(f"| {mut} | {r1[mut]['n']} | {r1[mut]['recall']} | "
            f"{r2[mut]['recall']} |")
    add("")

    # G1 verdict.
    winners = [
        (name, m) for name, m in exp["frontier"].items()
        if (m["precision"] or 0) >= 0.9 and (m["recall"] or 0) > 0.5
    ]
    add("## G1 gate check (recall > 0.5 at precision ≥ 0.9)")
    add("")
    if winners:
        best = max(winners, key=lambda x: x[1]["recall"])
        add(f"**Met** by {len(winners)} detector(s); best recall: "
            f"`{best[0]}` (precision {best[1]['precision']}, recall "
            f"{best[1]['recall']}).")
    else:
        closest = max(
            (x for x in exp["frontier"].items() if (x[1]["precision"] or 0) >= 0.9),
            key=lambda x: x[1]["recall"] or 0, default=None)
        add("**Not met** on the expanded set. "
            + (f"Best recall at precision ≥ 0.9: `{closest[0]}` "
               f"(precision {closest[1]['precision']}, recall "
               f"{closest[1]['recall']})." if closest else
               "No detector reached precision ≥ 0.9."))
    add("")
    return "\n".join(lines)


def main() -> None:
    exp = evaluate_set(ROOT / "data" / "sfd_labels_expanded.json")
    orig = evaluate_set(ROOT / "data" / "sfd_labels.json")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "sfd_expanded_eval.json").open("w") as f:
        json.dump({"expanded": exp, "canonical": orig}, f, indent=2)
    (OUT_DIR / "SFD_EXPANSION_REPORT.md").write_text(render_md(orig, exp))
    print(f"[done] wrote {OUT_DIR / 'sfd_expanded_eval.json'}")
    print(f"[done] wrote {OUT_DIR / 'SFD_EXPANSION_REPORT.md'}")


if __name__ == "__main__":
    main()
