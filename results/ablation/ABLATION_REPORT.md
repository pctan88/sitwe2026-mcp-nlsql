# Ablation Study — SITWE 2026 Pilot

Model: **haiku**.  Five EvoSchema operators (TABLE_RENAME, TABLE_SPLIT, TABLE_MERGE, COLUMN_RENAME, COLUMN_MERGE) × two databases (concert_singer, hr_1).

Each configuration removes exactly one MCP primitive while keeping the other two intact.  Config A is the full pipeline (control); Config B disables `schema/fingerprint` (relink receives an empty diff event list and becomes a no-op); Config C disables `query/relink` (the stale SQL is passed straight to validate); Config D disables `query/validate` (relinked SQL is returned with no execution check and no LLM re-prompt).

## 1. Pooled summary (all operators)

| Database | Config | EX | ΔEX vs Full MCP |
|---|---|---|---|
| concert_singer | A — Full MCP | 0.59 | — |
| concert_singer | B — No Fingerprint | 0.63 | +0.04 |
| concert_singer | C — No Relink | 0.59 | +0.00 |
| concert_singer | D — No Validate | 0.36 | −0.24 |
| hr_1 | A — Full MCP | 0.63 | — |
| hr_1 | B — No Fingerprint | 0.61 | −0.03 |
| hr_1 | C — No Relink | 0.63 | +0.00 |
| hr_1 | D — No Validate | 0.34 | −0.29 |

## 2. Per-operator breakdown

### TABLE_RENAME

| Database | A Full MCP | B No Fingerprint | C No Relink | D No Validate |
|---|---|---|---|---|
| concert_singer | 0.56 | 0.56 | 0.56 | 0.56 |
| hr_1 | 0.56 | 0.56 | 0.56 | 0.56 |

### TABLE_SPLIT

| Database | A Full MCP | B No Fingerprint | C No Relink | D No Validate |
|---|---|---|---|---|
| concert_singer | 0.62 | 0.62 | 0.62 | 0.06 |
| hr_1 | 0.56 | 0.56 | 0.56 | 0.19 |

### TABLE_MERGE

| Database | A Full MCP | B No Fingerprint | C No Relink | D No Validate |
|---|---|---|---|---|
| concert_singer | 0.79 | 0.79 | 0.79 | 0.14 |
| hr_1 | 0.64 | 0.64 | 0.64 | 0.14 |

### COLUMN_RENAME

| Database | A Full MCP | B No Fingerprint | C No Relink | D No Validate |
|---|---|---|---|---|
| concert_singer | 0.75 | 0.69 | 0.75 | 0.75 |
| hr_1 | 0.56 | 0.56 | 0.56 | 0.56 |

### COLUMN_MERGE

| Database | A Full MCP | B No Fingerprint | C No Relink | D No Validate |
|---|---|---|---|---|
| concert_singer | 0.21 | 0.50 | 0.21 | 0.21 |
| hr_1 | 0.86 | 0.71 | 0.86 | 0.21 |

## 3. Interpretation

**Pooled EX across both databases:** A 0.61, B 0.62, C 0.61, D 0.35.

- **Largest pooled drop** when removed: `validate` (Δ 0.26 vs Full MCP).  Under Haiku 4.5 this is the component contributing the most to the end-to-end accuracy gain.
- **Validate (A vs D):** Δ 0.26.  Config D trusts the relinker's output blindly with no execution check and no LLM re-prompt.  Its very large drop is concentrated on TABLE_SPLIT and TABLE_MERGE — operators the AST relinker does *not* handle and which therefore rely entirely on the validate → LLM-re-prompt fallback to produce correct SQL.  Without validate, the no-op relinker ships the stale SQL and every such query fails.
- **Relink (A vs C):** Δ 0.00.  Removing the deterministic AST rewriter is almost free for Haiku 4.5: validate still fires on the stale SQL, the LLM re-prompt is given the real diff, and the model recovers the same queries the AST path would have handled.  In other words, for the operators the AST does handle (TABLE_RENAME, COLUMN_RENAME), the LLM re-prompt is a near-perfect substitute under this model.
- **Fingerprint (A vs B):** Δ -0.01.  The diff classifier turns out to be barely necessary at this model size: even when the LLM re-prompt receives only a placeholder diff string, it can usually infer the schema change from the current-schema block alone.  On COLUMN_MERGE for concert_singer, Config B actually *outperforms* Config A — the placeholder diff appears to be less misleading than the verbatim MERGE event the AST path injects.
- **COLUMN_MERGE focus:** concert_singer (A 0.21, C 0.21, D 0.21), hr_1 (A 0.86, C 0.86, D 0.21).  The AST cannot reconstruct merged columns, so anything Config A recovers above the stale baseline here is owed to the validate → LLM re-prompt loop — and the hr_1 drop from A 0.86 to D 0.21 is the clearest demonstration of validate's contribution in the whole suite.

**Headline finding.**  Under Haiku 4.5 the validate + LLM re-prompt loop is the dominant primitive.  The deterministic AST relinker contributes very little on top of it for queries the AST can handle, and the fingerprint diff classifier provides essentially zero pooled accuracy.  Whether this generalises to weaker / cheaper models (where the AST guarantees become more valuable) is an open question — replicating this ablation on GPT-4o-mini, Gemini, and the Llama-3.1 reference model would address it directly.

_Tables and numbers in this report are generated directly from `results/summary_ablation_*.json`.  Re-run `python -m pilot.build_ablation_report` to refresh after any ablation re-run._
