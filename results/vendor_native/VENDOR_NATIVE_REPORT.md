# Vendor-Native Function-Calling Report (SITWE 2026 revision)

Reviewer comments R1-2 / R2-2: direct comparison between the MCP-mediated pipeline and vendor-native function calling (Anthropic tool use for Haiku 4.5, OpenAI function calling for GPT-4o), plus a diff-in-prompt arm that isolates *information* from *protocol*.

Notes on method: the stale baseline was re-run as a sanity check (agreement with the canonical baseline reported below); the **refreshed ceiling is reused from the canonical `results/summary_{config}.json` files and was not re-run** (saves ~30% of the API cost). RR uses the locked canonical formula with the canonical EX_stale floor and canonical EX_refreshed ceiling. All live runs used `--strict` (zero mock contamination) at temperature 0.

## 1. Headline results (pooled: 2 DBs × 5 operators)

| Model | N | EX stale (canon) | EX stale (sanity) | Agreement | EX refreshed (canon) | EX diff-in-prompt (D) | EX vendor-native (F) | EX MCP (canon) | RR D | RR F | RR MCP |
|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku | 152 | 0.158 | 0.158 | 1.000 | 0.691 | 0.664 | 0.658 | 0.605 | 0.951 | 0.938 | 0.840 |
| gpt-4o | 152 | 0.184 | 0.184 | 1.000 | 0.750 | 0.724 | 0.737 | 0.724 | 0.953 | 0.977 | 0.953 |

Wilson 95% CIs:

| Model | stale (sanity) | diff-in-prompt | vendor-native | MCP (canon) |
|---|---|---|---|---|
| haiku | [0.108, 0.224] | [0.586, 0.735] | [0.579, 0.729] | [0.526, 0.679] |
| gpt-4o | [0.131, 0.253] | [0.648, 0.789] | [0.662, 0.800] | [0.648, 0.789] |

## 2. McNemar tests (paired by query id, pooled)

`b` = first arm wrong & second arm right; `c` = first arm right & second arm wrong; exact two-sided p, continuity-corrected χ².

**haiku**

| Comparison | b | c | χ² | p |
|---|---|---|---|---|
| F (vendor-native) vs MCP | 17 | 9 | 1.885 | 0.1686 |
| D (diff-in-prompt) vs MCP | 11 | 2 | 4.923 | 0.0225 |
| F vs D | 11 | 12 | 0.000 | 1.0000 |

**gpt-4o**

| Comparison | b | c | χ² | p |
|---|---|---|---|---|
| F (vendor-native) vs MCP | 7 | 5 | 0.083 | 0.7744 |
| D (diff-in-prompt) vs MCP | 3 | 3 | 0.167 | 1.0000 |
| F vs D | 7 | 5 | 0.083 | 0.7744 |

## 3. Per-operator recovery rates (pooled over both DBs)

**haiku**

| Operator | n | EX stale | EX refreshed | RR D | RR F | RR MCP |
|---|---|---|---|---|---|---|
| TABLE_RENAME | 32 | 0.125 | 0.656 | 0.941 | 1.000 | 0.824 |
| TABLE_SPLIT | 32 | 0.125 | 0.656 | 0.882 | 0.824 | 0.882 |
| TABLE_MERGE | 28 | 0.143 | 0.714 | 1.125 | 1.125 | 1.000 |
| COLUMN_RENAME | 32 | 0.188 | 0.688 | 1.062 | 0.938 | 0.938 |
| COLUMN_MERGE | 28 | 0.214 | 0.750 | 0.733 | 0.800 | 0.533 |

**gpt-4o**

| Operator | n | EX stale | EX refreshed | RR D | RR F | RR MCP |
|---|---|---|---|---|---|---|
| TABLE_RENAME | 32 | 0.156 | 0.719 | 1.000 | 0.889 | 1.000 |
| TABLE_SPLIT | 32 | 0.156 | 0.750 | 0.947 | 1.000 | 0.947 |
| TABLE_MERGE | 28 | 0.143 | 0.821 | 0.842 | 1.053 | 0.947 |
| COLUMN_RENAME | 32 | 0.219 | 0.781 | 1.056 | 0.944 | 1.000 |
| COLUMN_MERGE | 28 | 0.250 | 0.679 | 0.917 | 1.000 | 0.833 |

## 4. Cost & latency per arm

Prices (USD per MTok, list, Aug 2026): haiku: in $1.00 / out $5.00, gpt-4o: in $2.50 / out $10.00

| Model | Arm | mean tok in | mean tok out | USD/query | mean lat (s) | median lat (s) |
|---|---|---|---|---|---|---|
| haiku | sanity | 240.2 | 30.2 | 0.000391 | 1.269 | 1.135 |
| haiku | diff_in_prompt | 336.4 | 32.8 | 0.000500 | 1.257 | 1.122 |
| haiku | vendor_native | 4837.5 | 256.5 | 0.006120 | 5.374 | 4.550 |
| haiku | MCP (canonical, reference) | — | — | — | 0.710 | 0.868 |
| gpt-4o | sanity | 203.3 | 22.7 | 0.000735 | 1.103 | 1.000 |
| gpt-4o | diff_in_prompt | 290.2 | 22.0 | 0.000945 | 1.289 | 1.067 |
| gpt-4o | vendor_native | 2506.5 | 92.6 | 0.007193 | 4.471 | 4.229 |
| gpt-4o | MCP (canonical, reference) | — | — | — | 0.602 | 0.684 |

## 5. Tool-loop behaviour (Arm F)

| Model | mean API calls | mean tool calls | % get_schema_diff | % relink_sql | % validate_sql | % no tools | % capped |
|---|---|---|---|---|---|---|---|
| haiku | 3.401 | 2.428 | 99.3% | 11.2% | 99.3% | 0.7% | 2.6% |
| gpt-4o | 3.717 | 3.0 | 98.0% | 86.2% | 88.8% | 2.0% | 0.0% |

## Integrity checks

- **haiku**: 10/10 configs present; contaminated_queries = 0; strict mode in all runs = True; configs with sanity agreement < 0.9: none.
- **gpt-4o**: 10/10 configs present; contaminated_queries = 0; strict mode in all runs = True; configs with sanity agreement < 0.9: none.

## 6. Interpretation

For **haiku**, vendor-native function calling reached EX = 0.658 — numerically higher than the MCP arm's canonical EX = 0.605 — and is statistically indistinguishable from the MCP arm under McNemar's exact test (b = 17, c = 9, p = 0.1686). The diff-in-prompt arm reached EX = 0.664 (vs MCP: p = 0.0225; vs vendor-native: p = 1.0000), showing how much of the recovery is attributable to the *information* (the classified diff) rather than the delivery *protocol*.

For **gpt-4o**, vendor-native function calling reached EX = 0.737 — numerically higher than the MCP arm's canonical EX = 0.724 — and is statistically indistinguishable from the MCP arm under McNemar's exact test (b = 7, c = 5, p = 0.7744). The diff-in-prompt arm reached EX = 0.724 (vs MCP: p = 1.0000; vs vendor-native: p = 0.7744), showing how much of the recovery is attributable to the *information* (the classified diff) rather than the delivery *protocol*.

These results are reported as measured, whichever direction they fall; the paper's §VI prediction is that vendor-native accuracy is statistically indistinguishable from the MCP-mediated pipeline, with MCP's contribution being standardised discovery/transport rather than accuracy.
