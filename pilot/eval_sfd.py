"""Evaluate query/validate silent-failure detection on the labelled set.

Gate G1 (fast-track plan): SFD precision > 0.5 on ``data/sfd_labels.json``.

Positive class = silent_failure. A prediction is positive when validate
returns verdict ``silent_failure_suspected``. Cases that hit an execution
error at eval time are reported separately (they indicate a stale fixture,
not an SFD outcome).

Usage::

    python -m pilot.eval_sfd                # v1 token backend
    python -m pilot.eval_sfd --v2           # v2 embedding backend
    python -m pilot.eval_sfd --v2 --threshold 0.40

Writes ``results/sfd_eval_{v1|v2}.json`` (never overwrites the other
backend's file, per canonical-results discipline).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server import validate as vl  # noqa: E402


def evaluate(
    labels_path: Path,
    out_dir: Path,
    use_v2: bool = False,
    threshold: float | None = None,
) -> dict[str, Any]:
    payload = json.loads(labels_path.read_text())
    cases = payload["cases"]

    # FUSE-safe DB mirror (same workaround as run_pilot).
    work = Path(tempfile.mkdtemp(prefix="sfd_eval_"))
    db_cache: dict[str, str] = {}

    def _db(rel: str) -> str:
        if rel not in db_cache:
            src = ROOT / "data" / rel
            dst = work / rel.replace("/", "_")
            shutil.copyfile(src, dst)
            db_cache[rel] = str(dst)
        return db_cache[rel]

    tp = fp_ = fn = tn = 0
    exec_errors = 0
    per_case: list[dict[str, Any]] = []
    t0 = time.perf_counter()

    for c in cases:
        if use_v2:
            kw = {} if threshold is None else {"similarity_threshold": threshold}
            res = vl.validate_v2(_db(c["db"]), c["sql"], c["question"], **kw)
        else:
            kw = {} if threshold is None else {"similarity_threshold": threshold}
            res = vl.validate(_db(c["db"]), c["sql"], c["question"], **kw)

        if res.verdict == vl.EXEC_ERROR:
            exec_errors += 1
            pred = "exec_error"
        else:
            pred = "silent_failure" if res.verdict == vl.SILENT else "valid"
            actual = c["label"]
            if pred == "silent_failure" and actual == "silent_failure":
                tp += 1
            elif pred == "silent_failure" and actual == "valid":
                fp_ += 1
            elif pred == "valid" and actual == "silent_failure":
                fn += 1
            else:
                tn += 1
        per_case.append({
            "id": c["id"], "label": c["label"], "pred": pred,
            "reason": res.reason, "similarity": res.similarity,
            "mutation": c["mutation"],
        })

    precision = tp / (tp + fp_) if (tp + fp_) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)

    backend = "v2_embedding" if use_v2 else "v1_token"
    summary = {
        "backend": backend,
        "threshold": threshold if threshold is not None else (
            vl.EMBEDDING_THRESHOLD if use_v2 else 0.10),
        "labels_file": str(labels_path.name),
        "generator_version": payload.get("generator_version"),
        "n_cases": len(cases),
        "n_scored": tp + fp_ + fn + tn,
        "exec_errors": exec_errors,
        "confusion": {"tp": tp, "fp": fp_, "fn": fn, "tn": tn},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "gate_g1_precision_gt_0.5": precision > 0.5,
        "wall_time_s": round(time.perf_counter() - t0, 2),
        "per_case": per_case,
    }

    out_path = out_dir / f"sfd_eval_{'v2' if use_v2 else 'v1'}.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"[done] {out_path}")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_case"},
                     indent=2))
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="SFD precision/recall evaluation.")
    p.add_argument("--labels", default=str(ROOT / "data" / "sfd_labels.json"))
    p.add_argument("--out-dir", default=str(ROOT / "results"))
    p.add_argument("--v2", action="store_true",
                   help="Use the sentence-embedding backend (validate_v2).")
    p.add_argument("--threshold", type=float, default=None,
                   help="Override the similarity threshold.")
    args = p.parse_args()
    evaluate(Path(args.labels), Path(args.out_dir),
             use_v2=args.v2, threshold=args.threshold)
