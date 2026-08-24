# Camera-ready seeds-3 stability run — 2026-07-03

Command: `python -m pilot.run_pilot --llm anthropic --strict --seeds 3`
(default `--database concert_singer` = legacy 20-query TABLE_RENAME sub-study,
matching the ~USD 0.15 cost estimate in run_pilot.py's --seeds help text).

Backend: claude-haiku-4-5-20251001, strict_mode=true, contaminated_queries=0.

Results: N=20, ex_post_baseline=0.0, ex_post_refreshed_schema=0.9,
ex_post_mcp=0.85, recovery_rate=0.9444 (matches the known single-op
RR=0.9444 figure from the 2026-05-22 changelog entry).

seed_stability_pre=1.0, seed_stability_baseline=1.0, seed_stability_mcp=0.95
(1 disagreement: Q05, mcp_ok flips between seeds).

This run wrote to the canonical `results/summary.json` /
`results/pilot_results.csv` (single-config default). Those canonical files
have been restored to the pooled N=76 accepted-version numbers (see
`results/archive/2026-07-03_pre_RC1_hardening_reruns/`) so
`paper/build_docx.py` continues to read the locked pooled figures. This
seeds-3 run's output is preserved here for the record.
