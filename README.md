# SITWE 2026 — Pilot Study

**Paper:** *A Schema-Aware Model Context Protocol Server for Natural Language to
SQL Translation under Schema Evolution*

Pilot codebase backing the SITWE 2026 paper. Implements the three RC1 MCP
primitives (`schema/fingerprint`, `query/relink`, `query/validate`) and a
76-query evaluation harness covering all five EvoSchema operator classes across
two Spider sub-domains (`concert_singer`, `hr_1`) with four pre-trained LLM
clients (Claude Haiku 4.5, GPT-4o, Gemini 2.5 Flash, Llama 3.3 70B).

---

## 1. Project layout

```
Pilot_Study_SITWE2026/
├── README.md
├── requirements.txt
├── conftest.py                   ← pytest root config
│
├── data/
│   ├── build_dbs.py              ← rebuild pre/post SQLite DBs
│   ├── build_fixtures.py         ← generate per-operator fixture dirs
│   ├── build_queries.py          ← generate query sets
│   ├── perturbations.py          ← five EvoSchema operator definitions
│   ├── database_config.py        ← DB path helpers
│   ├── validate_queries.py       ← gold-SQL validation harness
│   ├── verify_fixtures.py        ← integrity checks
│   ├── concert_singer/           ← primary domain (TABLE_RENAME sub-study)
│   │   ├── concert_singer_pre.sqlite
│   │   ├── concert_singer_post.sqlite
│   │   ├── pilot_queries.json    ← 20-query rename sub-study (legacy)
│   │   └── config.json
│   ├── concert_singer_TABLE_RENAME/   ─┐
│   ├── concert_singer_TABLE_SPLIT/    │  5-operator per-domain dirs
│   ├── concert_singer_TABLE_MERGE/    │  each contains:
│   ├── concert_singer_COLUMN_RENAME/  │    pre.sqlite, post.sqlite,
│   ├── concert_singer_COLUMN_MERGE/   │    queries.json, config.json
│   ├── hr_1/                          │
│   ├── hr_1_TABLE_RENAME/             │
│   ├── hr_1_TABLE_SPLIT/              │
│   ├── hr_1_TABLE_MERGE/              │
│   ├── hr_1_COLUMN_RENAME/            │
│   └── hr_1_COLUMN_MERGE/            ─┘
│
├── mcp_server/
│   ├── fingerprint.py            ← BLAKE3 canonical hash + diff classifier
│   ├── relink.py                 ← sqlglot AST rewriter + LLM fallback hook
│   ├── validate.py               ← exec + arity + SQL2Text back-translation
│   └── server.py                 ← FastMCP server exposing the three primitives
│
├── pilot/
│   ├── llm_client.py             ← multi-backend LLM client (Anthropic / OpenAI-compat / mock)
│   ├── metrics.py                ← EX, RR, Wilson CI, bootstrap CI formulas
│   ├── run_pilot.py              ← main harness — 4-config evaluation per DB/operator
│   ├── run_ablation.py           ← ablation sweep (disable one primitive at a time)
│   ├── run_scalability.py        ← fingerprint + relink timing at 10/100/1000 tables
│   └── build_ablation_report.py  ← aggregate ablation JSONs into summary
│
├── tests/
│   ├── conftest.py
│   ├── test_diff_classifier.py
│   ├── test_relink_complex.py
│   ├── test_perturbations.py
│   ├── test_baselines.py
│   ├── test_llm_backends.py
│   ├── test_metrics.py
│   └── test_scalability.py
│
├── tools/
│   ├── build_pilot_report.py     ← aggregate per-operator results into paper tables
│   └── verify_api_keys.py        ← check API key validity before a long run
│
├── results/
│   ├── summary.json              ← canonical pooled aggregate (paper-driving)
│   ├── pilot_results_contaminated_2026-05-22.csv  ← audit evidence (see §3.1)
│   ├── concert_singer/           ← 76-query Haiku 4.5 run, five operators
│   │                                (per-op CSVs + summaries, Tables I–II)
│   ├── hr_1/                     ← secondary-domain 76-query run
│   ├── cross_model/              ← GPT-4o, Gemini 2.5 Flash, Llama 3.3 70B
│   │                                raw runs on both domains (Table VIII)
│   ├── ablation/                 ← configurations A–D (Table VI)
│   ├── vendor_native/            ← vendor function-calling + diff-in-prompt
│   │                                arms, 152 queries/model (Table VII)
│   ├── evoschema_subset/         ← 199-item EvoSchema/BIRD-dev run incl.
│   │                                the 24-item gate-exclusion list
│   ├── silent_failure_set/       ← 60-case labelled-set detector results,
│   │                                v1 + v2 backends (Table III); labels in
│   │                                data/sfd_labels.json
│   ├── seed_stability/           ← 5-seed (Haiku) + 3-seed (GPT-4o) sweeps
│   │                                with per-seed disagreement itemisation
│   └── hardening/                ← post-submission COLUMN_MERGE hardening
│                                    re-run (G2 report + per-op CSVs)
│
└── paper/
    ├── sitwe2026_paper.tex       ← IEEEtran source
    ├── build_docx.py             ← generates sitwe2026_paper.docx from results
    ├── build_figure.py           ← generates fig_arch.png
    ├── submitted/                ← the submitted revision (clean copy)
    └── figures/
        ├── fig_arch_cycle.png    ← Figure 1: MCP-mediated query cycle (tracked)
        ├── fig_ex_bar.png        ← Figure 2: EX bar chart with CI (tracked)
        └── fig_crossmodel_bars.png  ← Figure 3: cross-model comparison (tracked)
                                  (*.pdf and *.docx are gitignored — regenerate locally)
```

---

## 2. Quick start (no API key — uses deterministic mock LLM)

```bash
cd Pilot_Study_SITWE2026
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Rebuild the SQLite databases for a specific domain + operator.
python data/build_dbs.py

# Run the harness with the deterministic mock LLM (zero API cost).
# Evaluates all four configurations (stale / refreshed / error-feedback / MCP)
# on the concert_singer TABLE_RENAME operator.
python -m pilot.run_pilot --llm mock --db concert_singer --op TABLE_RENAME
```

Mock-mode outputs land in `results/pilot_results_mock.csv` and
`results/summary_mock.json`. The canonical per-domain files under
`results/concert_singer/` and `results/hr_1/`
(`pilot_results.csv` / `summary.json`) are the **paper-driving** outputs
and should only be produced by a clean live API run (see §3).

Source of truth for all paper numbers is `results/summary.json` —
`build_docx.py` reads it at build time.

---

## 3. Running with real LLMs (paper-driving run)

The cost for 76 queries × ≤ 3 LLM calls per query per operator is approximately
**USD 0.20–0.80** total at Haiku 4.5 list pricing for one full domain sweep.

```bash
# Store API keys in .env (gitignored). NEVER paste keys into chat.
cp .env.example .env   # then fill in your keys

# Strict mode: refuse to fall back to mock on transient API failures.
python -m pilot.run_pilot --llm anthropic --strict \
    --db concert_singer --op TABLE_RENAME

# Run all five operators on concert_singer with Haiku 4.5:
for op in TABLE_RENAME TABLE_SPLIT TABLE_MERGE COLUMN_RENAME COLUMN_MERGE; do
    python -m pilot.run_pilot --llm anthropic --strict --db concert_singer --op $op
done
```

Supported `--llm` values: `anthropic`, `openai` (GPT-4o via OpenAI API),
`gemini` (Gemini 2.5 Flash via Google GenAI SDK),
`llama` (Llama 3.3 70B via Together AI OpenAI-compat endpoint), `mock`.

The `summary.json` will report `"backend": "anthropic"`,
`"strict_mode": true`, and `"contaminated_queries": 0`.

### 3.1. Why strict mode matters — May 2026 audit

The first live Haiku sweep silently fell back to the mock LLM on
**9 of 20 queries (45 %)** after Anthropic's API returned `OverloadedError`.
The pre-fix harness caught the exception and substituted mock-generated SQL
without surfacing the failure. The contaminated CSV is preserved as
`results/pilot_results_contaminated_2026-05-22.csv` for the record.

`llm_client._call_anthropic` now retries on transient errors with
exponential backoff (1 s / 2 s / 4 s / 8 s / 16 s). The `--strict` flag
re-raises after the retry budget is exhausted instead of falling back.

If `build_docx.py` detects any `mock-after-*` rows in `pilot_results.csv`,
the produced `.docx` carries a bold red **"DRAFT — NOT FOR SUBMISSION"**
banner above the title.

### 3.2. Multi-seed stability check (`--seeds N`)

```bash
# 3 independent T=0 completions per query (≈ USD 0.60–2.40 for a full
# concert_singer 5-operator sweep × 3 seeds at Haiku 4.5 pricing).
python -m pilot.run_pilot --llm anthropic --strict --seeds 3 \
    --db concert_singer --op TABLE_RENAME
```

When `N > 1`, the harness writes `results/pilot_results_seeds_{suffix}.csv`
and adds three stability fields to `summary.json`:

| Field | Meaning |
|---|---|
| `seed_stability_pre`      | Fraction of queries where all N seeds agree on `pre_ok`. |
| `seed_stability_baseline` | Same for `baseline_ok`. |
| `seed_stability_mcp`      | Same for `mcp_ok`. Lists disagreements in `seed_disagreements`. |

---

## 4. Running the ablation and scalability studies

### 4.1. Ablation (Table IV in the paper)

Disables one primitive at a time to attribute recovery to individual components.

```bash
# Run ablation for one domain/operator combination:
python -m pilot.run_ablation --llm anthropic --db concert_singer --op TABLE_RENAME

# Build the aggregate ablation summary after running all combinations:
python -m pilot.build_ablation_report
```

Results land in `results/ablation_results_{db}_{op}.csv` and
`results/summary_ablation_{db}_{op}.json`.

### 4.2. Scalability (Table VI in the paper)

Times `schema/fingerprint` diff and `query/relink` AST pass on synthetic
schemas of 10, 100, and 1 000 tables.

```bash
python -m pilot.run_scalability
```

---

## 5. Pointing the harness at the real Spider dataset

The bundled SQLite files match Spider's schemas so the pilot runs without
downloading Spider. For the full evaluation you will need Spider in full.

### 5.1. Get Spider

```bash
# Option A — HuggingFace Datasets mirror (recommended).
pip install datasets
python - <<'PY'
from datasets import load_dataset
ds = load_dataset("xlangai/spider")   # ~95 MB
print(ds)
PY

# Option B — direct from Yale Lily release page.
# Visit https://yale-lily.github.io/spider and click "Spider dataset".
# SQLite files live in spider/database/<db_id>/*.sqlite
```

### 5.2. Get EvoSchema (for the full evaluation, not the pilot)

EvoSchema (Zhang et al., 2025; arXiv:2603.10697) is the primary measurement
substrate for the full RC2 evaluation. Contact the corresponding author
before redistribution. For SITWE 2026, the pilot's self-generated
perturbations are sufficient.

---

## 6. Running the FastMCP server standalone

The pilot harness invokes the three primitives in-process (no JSON-RPC
round-trip). You can also run the MCP server as a normal STDIO server:

```bash
fastmcp run mcp_server/server.py
# Or:
python -m mcp_server.server --db-dir data/concert_singer/
```

Connect with any MCP-compatible client (Claude Desktop, Anthropic Inspector,
etc.). The server exposes:

| Primitive | Kind | Address |
|---|---|---|
| `schema://current/{database_id}` | Resource | URI |
| `query/relink`                   | Tool     | name |
| `query/validate`                 | Tool     | name |

---

## 7. Reproducing the SITWE paper

```bash
# 1. Run the canonical paper-driving harness (Haiku 4.5, all 5 operators).
for op in TABLE_RENAME TABLE_SPLIT TABLE_MERGE COLUMN_RENAME COLUMN_MERGE; do
    python -m pilot.run_pilot --llm anthropic --strict \
        --db concert_singer --op $op
done

# 2. Build the aggregate results (populates per-operator summary JSONs).
python tools/build_pilot_report.py

# 3. Build the IEEE-style Word document.
#    Tables I–VI and the abstract pull live from results/summary*.json.
python paper/build_docx.py             # → paper/sitwe2026_paper.docx

# 4. Build the PDF. Run from the PROJECT ROOT (not from paper/).
#    Run twice for cross-references.
TEXINPUTS=/usr/share/texlive/texmf-dist/tex//:/tmp/texlocal//: \
    pdflatex -interaction=nonstopmode \
    -output-directory=paper paper/sitwe2026_paper.tex

# (One-time: download IEEEtran.cls if not in your TeX distribution)
# mkdir -p /tmp/texlocal
# curl -fsSL https://mirrors.ctan.org/macros/latex/contrib/IEEEtran/IEEEtran.cls \
#      -o /tmp/texlocal/IEEEtran.cls

# 5. Regenerate figures if needed.
python paper/build_figure.py                     # → paper/fig_arch.png
python paper/figures/gen_bar_chart.py            # → paper/figures/fig_ex_bar.*
python paper/figures/gen_fig3_crossmodel.py      # → paper/figures/fig_crossmodel_bars.*
```

See `paper/PAPER_UPDATE_GUIDE.md` for how to keep `.tex` and `build_docx.py`
in sync when numbers change.

---

## 8. Running the test suite

```bash
pip install pytest
pytest tests/ -v
# Expected: 69 passed, 0 failed, 0 skipped.
# Zero real API calls — all LLM calls are socket-patched in CI.
```

---

## 9. Known limitations (carry to full evaluation)

- The token-overlap back-translator in `validate.py` (v1) is a feasibility
  stub. On the original 5-case TABLE_RENAME subset it had zero
  precision/recall; on the 60-case labelled SFD set (`data/sfd_labels.json`,
  built 2026-07-03 by `data/build_sfd_labels.py`) it scores precision 0.92 /
  recall 0.30 — high precision, but it misses 70% of real silent failures.
  **validate v2** (2026-07-03) adds a sentence-embedding cosine backend
  (`validate_v2`, local MiniLM via sentence-transformers): select with
  `--validate embedding` on `run_pilot`, evaluate with
  `python -m pilot.eval_sfd --v2`. Gate G1 target: SFD precision > 0.5 with
  improved recall. **Gate G1 ran 2026-07-09 (PASS)** — threshold sweep on the
  60-case set: 0.35 → precision 1.00 / recall 0.25; 0.40 → 1.00 / 0.325;
  0.45 → 1.00 / 0.375. The default `EMBEDDING_THRESHOLD` was **locked to
  0.45** on 2026-07-10 (best recall at zero false positives; beats v1 on
  both precision and recall). Sweep table preserved in
  `results/sfd_eval_v2.json`.
- The column-merge (`COLUMN_MERGE`) operator is outside the deterministic AST
  rewriter's scope: when two columns are merged through an expression, the
  original components cannot be recovered from the post-schema. Pilot EX on
  the MCP path stayed near the stale baseline across all four models.
  **Remediation (2026-07-03):** the LLM fallback prompt is now enriched with
  the original question plus deterministic per-operator rewrite rules
  (`mcp_server.relink.build_llm_guidance` — e.g. equality-filter → LIKE over
  the merge separator), and the harness retries once more with an explicit
  cue when an affirmative question still returns an empty result. Gate G2
  re-run (all five operators, live API) pending.
- The canonical paper run uses a single completion per query at temperature 0.
  The `--seeds N` flag is implemented but the camera-ready 3-seed stability
  sweep is pending. Until that runs, `seed_stability_*` fields are absent from
  `summary.json`.
- The deterministic mock LLM has limited NL-to-SQL competence on joins and
  `NOT IN` subqueries. Use it for harness unit testing only; canonical paper
  numbers come from live API runs.

---

## 10. Citation

If you use this code or the perturbed fixtures, please cite the SITWE 2026 paper:

```bibtex
@inproceedings{tan2026mcp,
  title     = {A Schema-Aware Model Context Protocol Server for Natural
               Language to SQL Translation under Schema Evolution},
  author    = {Tan, Poi-Cheong and Haw, Su-Cheng and Goh, Hui-Ngo and J, Jayapradha},
  booktitle = {Proc. SITWE 2026},
  year      = {2026},
  publisher = {Multimedia University Press},
}
```

Page numbers, DOI, and final volume metadata will be updated here once the
proceedings are published. Please re-pull this README before citing.
