# Gate G2 re-run report — RC1 hardening (fallback + embedding validate)

**Date:** 2026-07-03
**Command:** `python -m pilot.run_pilot --database all --strict --validate embedding`
**Backend:** `claude-haiku-4-5-20251001` (auto-resolved, `ANTHROPIC_API_KEY` present), strict mode on throughout.
**Configs run:** 11 (`concert_singer` base 20-query legacy config + 5 operators × 2 DBs). `contaminated_queries = 0` on every config; no strict-mode failures.

This report is **informational only**. Per the RC1 hardening runbook, these
numbers are **not** folded into the paper's canonical tables — the SITWE
2026 tables remain locked to the accepted-version pooled run (N=76,
EX_stale=0.1053, EX_refreshed=0.7105, EX_mcp=0.5921, RR=0.8043). All
canonical `results/summary*.json` / `results/pilot_results*.csv` files were
restored to their pre-rerun (accepted-version) state after this run; the
raw G2 output is preserved at
`results/archive/2026-07-03_G2_rerun/`.

## Gate criterion

G2 passes if all five operators have pooled RR > 0.5 across both databases,
with no regression vs. the pre-fallback-fix numbers. **Result: PASS.**

## Per-operator RR, pooled across concert_singer + hr_1

| Operator | N (pooled) | EX_stale | EX_refreshed | EX_mcp | Pooled RR | concert_singer RR | hr_1 RR |
|---|---|---|---|---|---|---|---|
| TABLE_RENAME | 32 | 0.1250 | 0.6562 | 0.5625 | 0.8235 | 0.7273 | 1.0 |
| TABLE_SPLIT | 32 | 0.1250 | 0.6250 | 0.5938 | 0.9375 | 0.9 | 1.0 |
| TABLE_MERGE | 28 | 0.1429 | 0.7143 | 0.7143 | 1.0000 | 1.125 | 0.875 |
| COLUMN_RENAME | 32 | 0.1875 | 0.6875 | 0.6562 | 0.9375 | 0.9167 | 1.0 |
| COLUMN_MERGE | 28 | 0.2143 | 0.7500 | 0.7143 | 0.9333 | 1.0 | 0.9091 |
| **All 10 configs (pooled)** | **152** | **0.1579** | **0.6842** | **0.6447** | **0.9250** | — | — |

All five operators clear RR > 0.5 on both databases individually and pooled.

## Comparison vs. pre-fallback-fix (May 2026) per-operator RR

| Config | Old RR | New RR | Δ |
|---|---|---|---|
| concert_singer_TABLE_RENAME | 0.7273 | 0.7273 | — |
| hr_1_TABLE_RENAME | 1.0 | 1.0 | — |
| concert_singer_TABLE_SPLIT | 0.8182 | 0.9000 | **+0.0818** |
| hr_1_TABLE_SPLIT | 1.0 | 1.0 | — |
| concert_singer_TABLE_MERGE | 1.125 | 1.125 | — |
| hr_1_TABLE_MERGE | 0.875 | 0.875 | — |
| concert_singer_COLUMN_RENAME | 0.9167 | 0.9167 | — |
| hr_1_COLUMN_RENAME | 1.0 | 1.0 | — |
| **concert_singer_COLUMN_MERGE** | **0.0000** | **1.0000** | **+1.0000** |
| **hr_1_COLUMN_MERGE** | **0.7273** | **0.9091** | **+0.1818** |

**No regressions on any config.** The `COLUMN_MERGE` fallback fix
(`mcp_server.relink.build_llm_guidance` + empty-result retry cue, added
2026-07-03) is confirmed working live: concert_singer COLUMN_MERGE went
from RR = 0.00 (the pre-fix flat-baseline failure noted in the README §9
"Known limitations") to RR = 1.00. `TABLE_SPLIT` and `hr_1_COLUMN_MERGE`
also improved as a side effect of the enriched fallback prompt, though
they were not the primary target.

## Cost

Not separately metered by the harness; consistent with the "< USD 2"
estimate in the task brief for a full 10-config, ≤3-call-per-query sweep at
Haiku 4.5 list pricing (152 pooled queries × up to 4 configurations each).

## Files

- Raw per-config summaries and CSVs from this run: `results/archive/2026-07-03_G2_rerun/`
- Canonical accepted-version files restored from: `results/archive/2026-07-03_pre_RC1_hardening_reruns/`
