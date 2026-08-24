# Query-complexity stratification (scale-up Module 3)

Buckets from the post-perturbation gold SQL AST (sqlglot): **easy** = single table, no join/aggregation/subquery; **medium** = exactly 1 join OR aggregation/GROUP BY; **hard** = ≥ 2 joins, subquery, or HAVING. All scored queries of the 10 db×operator pilot configs; expected-failure probes excluded. RR uses the canonical formula per bucket (refreshed ceiling within the same bucket).

Bucket sizes (per model): easy = 86, medium = 66, hard = 0 (of 152 scored queries).

**Finding:** the pilot per-operator query sets contain NO hard queries — no gold SQL has ≥ 2 joins, a subquery, or HAVING (verified against the AST classifier, which does detect these constructs on synthetic examples). The stratification below is therefore easy-vs-medium only; hard-query coverage is a real gap of the pilot sets, addressed by the query-set expansion module (≥ 10% subquery/HAVING mix) and by the EvoSchema subset.

## Per-model EX/RR by complexity bucket

| Model | Bucket | n | EX stale | EX refreshed | EX err-fb | EX MCP | RR MCP | RR err-fb |
|---|---|---|---|---|---|---|---|---|
| haiku | easy | 86 | 0.093 | 0.616 | 0.093 | 0.546 | 0.867 | 0.000 |
| haiku | medium | 66 | 0.242 | 0.788 | 0.242 | 0.682 | 0.805 | 0.000 |
| gpt-4o | easy | 86 | 0.140 | 0.709 | 0.140 | 0.686 | 0.959 | 0.000 |
| gpt-4o | medium | 66 | 0.242 | 0.803 | 0.242 | 0.773 | 0.946 | 0.000 |
| gemini | easy | 86 | 0.140 | 0.756 | 0.151 | 0.698 | 0.906 | 0.019 |
| gemini | medium | 66 | 0.242 | 0.818 | 0.242 | 0.803 | 0.974 | 0.000 |
| llama31 | easy | 86 | 0.140 | 0.756 | 0.140 | 0.709 | 0.924 | 0.000 |
| llama31 | medium | 66 | 0.258 | 0.833 | 0.258 | 0.818 | 0.974 | 0.000 |
| qwen-coder | easy | 86 | 0.093 | 0.628 | 0.093 | 0.616 | 0.978 | 0.000 |
| qwen-coder | medium | 66 | 0.258 | 0.803 | 0.258 | 0.803 | 1.000 | 0.000 |
| qwen | easy | 86 | 0.093 | 0.360 | 0.093 | 0.314 | 0.826 | 0.000 |
| qwen | medium | 66 | 0.182 | 0.273 | 0.182 | 0.288 | 1.167 | 0.000 |
| qwen-small | easy | 86 | 0.093 | 0.523 | 0.093 | 0.616 | 1.216 | 0.000 |
| qwen-small | medium | 66 | 0.242 | 0.515 | 0.242 | 0.636 | 1.444 | 0.000 |

## Pooled across all models

| Bucket | n | EX stale | EX refreshed | EX err-fb | EX MCP | RR MCP | MCP Wilson 95% CI |
|---|---|---|---|---|---|---|---|
| easy | 602 | 0.113 | 0.621 | 0.115 | 0.598 | 0.954 | [0.558, 0.636] |
| medium | 462 | 0.238 | 0.691 | 0.238 | 0.686 | 0.990 | [0.642, 0.727] |

## Vendor-native arms by bucket (Brief 1 results, bonus)

| Model | Bucket | n | EX diff-in-prompt | EX vendor-native |
|---|---|---|---|---|
| haiku | easy | 86 | 0.593 | 0.605 |
| haiku | medium | 66 | 0.758 | 0.727 |
| gpt-4o | easy | 86 | 0.686 | 0.674 |
| gpt-4o | medium | 66 | 0.773 | 0.818 |

## LaTeX table (pooled, paper-ready)

```latex
\begin{tabular}{lrrrrrr}
\toprule
Bucket & $n$ & EX$_{\text{stale}}$ & EX$_{\text{refreshed}}$ & EX$_{\text{err-fb}}$ & EX$_{\text{MCP}}$ & RR$_{\text{MCP}}$ \\
\midrule
Easy & 602 & 0.113 & 0.621 & 0.115 & 0.598 & 0.954 \\
Medium & 462 & 0.238 & 0.691 & 0.238 & 0.686 & 0.990 \\
\bottomrule
\end{tabular}
```

Scope note: buckets are computed over the 10 pilot db×operator configs (152 scored queries per model, 7 models). EvoSchema-subset rows are NOT included here; see the EvoSchema report if present.
