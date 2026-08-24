# Multi-seed stability across clients (scale-up Module 4)

Extends the RC1 three-seed evidence to **5 seeds × both DBs (Haiku 4.5)** and **3 seeds (GPT-4o)**, pooled query set, all five operators, temperature 0, strict mode, embedding validate. A query is *stable* for an arm when every seed produced the same binary EX outcome.

## haiku (5 seeds)

Pooled per-query agreement over 152 scored queries: pre 0.993 · stale 1.000 · refreshed 0.993 · error-feedback 1.000 · MCP 1.000

| Config | n | stable stale | stable MCP | EX stale mean [min,max] | EX MCP mean [min,max] |
|---|---|---|---|---|---|
| concert_singer_TABLE_RENAME | 16 | 1.000 | 1.000 | 0.0625 [0.0625, 0.0625] | 0.5625 [0.5625, 0.5625] |
| concert_singer_TABLE_SPLIT | 16 | 1.000 | 1.000 | 0.0625 [0.0625, 0.0625] | 0.625 [0.625, 0.625] |
| concert_singer_TABLE_MERGE | 14 | 1.000 | 1.000 | 0.1429 [0.1429, 0.1429] | 0.7857 [0.7857, 0.7857] |
| concert_singer_COLUMN_RENAME | 16 | 1.000 | 1.000 | 0.0625 [0.0625, 0.0625] | 0.75 [0.75, 0.75] |
| concert_singer_COLUMN_MERGE | 14 | 1.000 | 1.000 | 0.2143 [0.2143, 0.2143] | 0.5 [0.5, 0.5] |
| hr_1_TABLE_RENAME | 16 | 1.000 | 1.000 | 0.1875 [0.1875, 0.1875] | 0.5625 [0.5625, 0.5625] |
| hr_1_TABLE_SPLIT | 16 | 1.000 | 1.000 | 0.1875 [0.1875, 0.1875] | 0.5625 [0.5625, 0.5625] |
| hr_1_TABLE_MERGE | 14 | 1.000 | 1.000 | 0.1429 [0.1429, 0.1429] | 0.6429 [0.6429, 0.6429] |
| hr_1_COLUMN_RENAME | 16 | 1.000 | 1.000 | 0.3125 [0.3125, 0.3125] | 0.5625 [0.5625, 0.5625] |
| hr_1_COLUMN_MERGE | 14 | 1.000 | 1.000 | 0.2143 [0.2143, 0.2143] | 0.9286 [0.9286, 0.9286] |

### Disagreeing queries (2)

| Config | Query | Flipping arms | Per-seed outcomes |
|---|---|---|---|
| concert_singer_TABLE_SPLIT | cs_ts_09 | refreshed_schema_ok | `{"refreshed_schema_ok": [0, 0, 0, 1, 0]}` |
| hr_1_COLUMN_MERGE | hr_cm_05 | pre_ok | `{"pre_ok": [0, 1, 1, 1, 1]}` |

Cause note: at temperature 0 the remaining variance comes from provider-side nondeterminism (batching / MoE routing) surfacing as different-but-tied SQL formulations; flips concentrate in queries whose gold answer admits several near-equivalent SQL shapes.

## gpt-4o (3 seeds)

Pooled per-query agreement over 152 scored queries: pre 0.987 · stale 1.000 · refreshed 1.000 · error-feedback 1.000 · MCP 0.967

| Config | n | stable stale | stable MCP | EX stale mean [min,max] | EX MCP mean [min,max] |
|---|---|---|---|---|---|
| concert_singer_TABLE_RENAME | 16 | 1.000 | 0.938 | 0.125 [0.125, 0.125] | 0.8542 [0.8125, 0.875] |
| concert_singer_TABLE_SPLIT | 16 | 1.000 | 0.938 | 0.125 [0.125, 0.125] | 0.9167 [0.875, 0.9375] |
| concert_singer_TABLE_MERGE | 14 | 1.000 | 1.000 | 0.1429 [0.1429, 0.1429] | 0.8571 [0.8571, 0.8571] |
| concert_singer_COLUMN_RENAME | 16 | 1.000 | 1.000 | 0.125 [0.125, 0.125] | 1.0 [1.0, 1.0] |
| concert_singer_COLUMN_MERGE | 14 | 1.000 | 0.857 | 0.2857 [0.2857, 0.2857] | 0.3571 [0.3571, 0.3571] |
| hr_1_TABLE_RENAME | 16 | 1.000 | 1.000 | 0.1875 [0.1875, 0.1875] | 0.5625 [0.5625, 0.5625] |
| hr_1_TABLE_SPLIT | 16 | 1.000 | 1.000 | 0.1875 [0.1875, 0.1875] | 0.5625 [0.5625, 0.5625] |
| hr_1_TABLE_MERGE | 14 | 1.000 | 0.929 | 0.1429 [0.1429, 0.1429] | 0.6667 [0.6429, 0.7143] |
| hr_1_COLUMN_RENAME | 16 | 1.000 | 1.000 | 0.3125 [0.3125, 0.3125] | 0.5625 [0.5625, 0.5625] |
| hr_1_COLUMN_MERGE | 14 | 1.000 | 1.000 | 0.2143 [0.2143, 0.2143] | 1.0 [1.0, 1.0] |

### Disagreeing queries (5)

| Config | Query | Flipping arms | Per-seed outcomes |
|---|---|---|---|
| concert_singer_TABLE_RENAME | cs_tr_09 | pre_ok, mcp_ok | `{"pre_ok": [0, 1, 1], "mcp_ok": [1, 1, 0]}` |
| concert_singer_TABLE_SPLIT | cs_ts_13 | pre_ok, mcp_ok | `{"pre_ok": [0, 1, 1], "mcp_ok": [1, 1, 0]}` |
| concert_singer_COLUMN_MERGE | cs_cm_05 | mcp_ok | `{"mcp_ok": [0, 1, 1]}` |
| concert_singer_COLUMN_MERGE | cs_cm_09 | mcp_ok | `{"mcp_ok": [1, 0, 0]}` |
| hr_1_TABLE_MERGE | hr_tm_04 | mcp_ok | `{"mcp_ok": [0, 1, 0]}` |

Cause note: at temperature 0 the remaining variance comes from provider-side nondeterminism (batching / MoE routing) surfacing as different-but-tied SQL formulations; flips concentrate in queries whose gold answer admits several near-equivalent SQL shapes.
