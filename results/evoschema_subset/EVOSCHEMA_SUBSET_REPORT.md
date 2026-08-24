# EvoSchema subset evaluation (scale-up Module 1, R1-1/R2-1)

Real-benchmark run on a deterministic stratified subset of **EvoSchema** (BIRD-dev substrate, 11 databases). Sampling rule (seed-free): per operator: first 4 items per db_id sorted by train_idx (COLUMN_MERGE: all merge-subset items); seed-free and deterministic. Sampled 223 items; **24 excluded by the gold-SQL gate** (both golds must execute with non-empty results and match pre/post, row-count equality for COLUMN_MERGE); **199 items evaluated** across 11 databases. The five in-scope operator files total 5768 items — this is a subset evaluation; the full benchmark remains future work.

Method notes: 4-configuration harness (stale / refreshed / error-feedback / MCP) exactly as the pilot; temperature 0, strict mode (zero mock contamination); embedding validate backend; the pre-EX column reuses the stale generation's SQL scored on the pre-DB (no separate call at T=0). The brief's mention of Spider databases was corrected against the benchmark inventory — EvoSchema perturbs BIRD-dev.

## 1. Headline (pooled over all operators and databases)

| Model | n | EX pre (sanity) | EX stale | EX refreshed | EX err-fb | EX MCP | RR MCP | RR err-fb |
|---|---|---|---|---|---|---|---|---|
| haiku | 199 | 0.322 | 0.000 | 0.296 | 0.010 | 0.322 | 1.085 | 0.034 |
| gpt_4o | 199 | 0.367 | 0.020 | 0.362 | 0.020 | 0.412 | 1.147 | 0.000 |

Wilson 95% CIs:

| Model | stale | refreshed | error-feedback | MCP |
|---|---|---|---|---|
| haiku | [0.000, 0.019] | [0.237, 0.363] | [0.003, 0.036] | [0.261, 0.389] |
| gpt_4o | [0.008, 0.051] | [0.298, 0.431] | [0.008, 0.051] | [0.346, 0.481] |

## 2. McNemar (paired, exact two-sided)

| Model | Comparison | b | c | χ² | p |
|---|---|---|---|---|---|
| haiku | MCP vs stale | 64 | 0 | 62.016 | 0.000000 |
| haiku | refreshed vs stale | 59 | 0 | 57.017 | 0.000000 |
| haiku | error-feedback vs stale | 2 | 0 | 0.500 | 0.500000 |
| haiku | MCP vs error-feedback | 2 | 64 | 56.379 | 0.000000 |
| gpt_4o | MCP vs stale | 78 | 0 | 76.013 | 0.000000 |
| gpt_4o | refreshed vs stale | 68 | 0 | 66.015 | 0.000000 |
| gpt_4o | error-feedback vs stale | 0 | 0 | — | 1.000000 |
| gpt_4o | MCP vs error-feedback | 0 | 78 | 76.013 | 0.000000 |

## 3. Per-operator breakdown

**haiku**

| Operator | n | EX stale | EX refreshed | EX err-fb | EX MCP | RR MCP | MCP Wilson CI |
|---|---|---|---|---|---|---|---|
| COLUMN_MERGE | 43 | 0.000 | 0.395 | 0.000 | 0.465 | 1.177 | [0.325, 0.611] |
| COLUMN_RENAME | 40 | 0.000 | 0.250 | 0.050 | 0.250 | 1.000 | [0.142, 0.402] |
| TABLE_MERGE | 36 | 0.000 | 0.194 | 0.000 | 0.222 | 1.143 | [0.117, 0.381] |
| TABLE_RENAME | 44 | 0.000 | 0.341 | 0.000 | 0.341 | 1.000 | [0.219, 0.489] |
| TABLE_SPLIT | 36 | 0.000 | 0.278 | 0.000 | 0.306 | 1.100 | [0.180, 0.469] |

**gpt_4o**

| Operator | n | EX stale | EX refreshed | EX err-fb | EX MCP | RR MCP | MCP Wilson CI |
|---|---|---|---|---|---|---|---|
| COLUMN_MERGE | 43 | 0.000 | 0.535 | 0.000 | 0.581 | 1.087 | [0.433, 0.716] |
| COLUMN_RENAME | 40 | 0.100 | 0.375 | 0.100 | 0.375 | 1.000 | [0.242, 0.530] |
| TABLE_MERGE | 36 | 0.000 | 0.194 | 0.000 | 0.333 | 1.714 | [0.202, 0.497] |
| TABLE_RENAME | 44 | 0.000 | 0.364 | 0.000 | 0.386 | 1.062 | [0.257, 0.534] |
| TABLE_SPLIT | 36 | 0.000 | 0.306 | 0.000 | 0.361 | 1.182 | [0.225, 0.524] |

## 4. Per-database breakdown

**haiku**

| Database | n | EX stale | EX refreshed | EX MCP | RR MCP |
|---|---|---|---|---|---|
| california_schools | 18 | 0.000 | 0.278 | 0.222 | 0.800 |
| card_games | 14 | 0.000 | 0.000 | 0.000 | — |
| codebase_community | 16 | 0.000 | 0.125 | 0.188 | 1.500 |
| debit_card_specializing | 14 | 0.000 | 0.214 | 0.214 | 1.000 |
| european_football_2 | 15 | 0.000 | 0.400 | 0.467 | 1.167 |
| financial | 15 | 0.000 | 0.133 | 0.333 | 2.500 |
| formula_1 | 12 | 0.000 | 0.333 | 0.333 | 1.000 |
| student_club | 55 | 0.000 | 0.473 | 0.491 | 1.038 |
| superhero | 11 | 0.000 | 0.727 | 0.727 | 1.000 |
| thrombosis_prediction | 15 | 0.000 | 0.067 | 0.200 | 3.000 |
| toxicology | 14 | 0.000 | 0.143 | 0.000 | 0.000 |

**gpt_4o**

| Database | n | EX stale | EX refreshed | EX MCP | RR MCP |
|---|---|---|---|---|---|
| california_schools | 18 | 0.056 | 0.333 | 0.389 | 1.200 |
| card_games | 14 | 0.000 | 0.000 | 0.000 | — |
| codebase_community | 16 | 0.062 | 0.250 | 0.562 | 2.667 |
| debit_card_specializing | 14 | 0.000 | 0.429 | 0.500 | 1.167 |
| european_football_2 | 15 | 0.000 | 0.200 | 0.200 | 1.000 |
| financial | 15 | 0.067 | 0.200 | 0.200 | 1.000 |
| formula_1 | 12 | 0.000 | 0.333 | 0.333 | 1.000 |
| student_club | 55 | 0.000 | 0.618 | 0.654 | 1.059 |
| superhero | 11 | 0.000 | 0.727 | 0.727 | 1.000 |
| thrombosis_prediction | 15 | 0.000 | 0.067 | 0.133 | 2.000 |
| toxicology | 14 | 0.071 | 0.214 | 0.214 | 1.000 |

## 5. Excluded items (gold-SQL gate)

| Reason (category) | n |
|---|---|
| materialize | 9 |
| gold_post execution error | 8 |
| result-set mismatch pre vs post | 6 |
| gold_post empty result | 1 |

<details><summary>Full exclusion list</summary>

| id | op | db | reason |
|---|---|---|---|
| evo_cm_01347 | COLUMN_MERGE | student_club | gold_post execution error: near "Gallagher": syntax error |
| evo_cm_01381 | COLUMN_MERGE | student_club | materialize: MaterializeError: malformed COLUMN_MERGE spec (no table dicts) |
| evo_cm_01384 | COLUMN_MERGE | student_club | materialize: MaterializeError: malformed COLUMN_MERGE spec (no table dicts) |
| evo_cm_01385 | COLUMN_MERGE | student_club | materialize: MaterializeError: malformed COLUMN_MERGE spec (no table dicts) |
| evo_cr_00002 | COLUMN_RENAME | california_schools | gold_post empty result |
| evo_cr_00848 | COLUMN_RENAME | formula_1 | gold_post execution error: no such column: T2.circuitID |
| evo_cr_00849 | COLUMN_RENAME | formula_1 | gold_post execution error: no such column: T2.circuitID |
| evo_cr_01151 | COLUMN_RENAME | thrombosis_prediction | gold_post execution error: near "of": syntax error |
| evo_tm_00347 | TABLE_MERGE | card_games | materialize: MaterializeError: merged column 'rulings_id' not found in cards or rulings |
| evo_tm_01025 | TABLE_MERGE | european_football_2 | materialize: MaterializeError: merged column 'league_name' not found in match or league |
| evo_tm_00090 | TABLE_MERGE | financial | materialize: MaterializeError: cannot determine join key for district x loan |
| evo_tm_00849 | TABLE_MERGE | formula_1 | materialize: MaterializeError: merged column 'races_name' not found in races or circuits |
| evo_tm_00850 | TABLE_MERGE | formula_1 | result-set mismatch pre vs post |
| evo_tm_00717 | TABLE_MERGE | superhero | materialize: MaterializeError: merged column 'power_id' not found in superhero or superpow |
| evo_tm_00719 | TABLE_MERGE | superhero | result-set mismatch pre vs post |
| evo_tm_00720 | TABLE_MERGE | superhero | gold_post execution error: no such column: super_hero_powerfull_name |
| evo_ts_00341 | TABLE_SPLIT | card_games | gold_post execution error: no such column: cardIdentifiers.cardKingdomId |
| evo_ts_01472 | TABLE_SPLIT | debit_card_specializing | result-set mismatch pre vs post |
| evo_ts_01473 | TABLE_SPLIT | debit_card_specializing | result-set mismatch pre vs post |
| evo_ts_01315 | TABLE_SPLIT | student_club | materialize: MaterializeError: split table EventLink wants columns not in attendance: ['ev |
| evo_ts_00718 | TABLE_SPLIT | superhero | result-set mismatch pre vs post |
| evo_ts_00719 | TABLE_SPLIT | superhero | gold_post execution error: no such column: T3.power_name |
| evo_ts_00197 | TABLE_SPLIT | toxicology | result-set mismatch pre vs post |
| evo_ts_00198 | TABLE_SPLIT | toxicology | gold_post execution error: no such column: T3.label |

</details>

## 6. Cost

| Model | tokens in | tokens out | USD | contaminated |
|---|---|---|---|---|
| haiku | 418102 | 59493 | 0.7156 | 0 |
| gpt_4o | 340965 | 39406 | 1.2465 | 0 |

## LaTeX table (paper-ready, pooled)

```latex
\begin{tabular}{lrrrrrr}
\toprule
Model & $n$ & EX$_{\text{stale}}$ & EX$_{\text{refr}}$ & EX$_{\text{err-fb}}$ & EX$_{\text{MCP}}$ & RR \\
\midrule
haiku & 199 & 0.000 & 0.296 & 0.010 & 0.322 & 1.085 \\
gpt_4o & 199 & 0.020 & 0.362 & 0.020 & 0.412 & 1.147 \\
\bottomrule
\end{tabular}
```
