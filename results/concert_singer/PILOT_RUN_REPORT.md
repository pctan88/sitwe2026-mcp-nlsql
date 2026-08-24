# Pilot Run Report

Generated from results in `/Users/pctan/Documents/Claude/Projects/Master Research/Pilot_Study_SITWE2026/results`.

## Model: haiku

### Database: concert_singer

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.1053 |
| Refreshed | 0.7105 |
| Error-Feedback | 0.1053 |
| MCP | 0.5921 |

Recovery Rate (RR): 0.8043

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.0625 | 0.7500 | 0.0625 | 0.5625 |
| TABLE_SPLIT | 0.0625 | 0.7500 | 0.0625 | 0.6250 |
| TABLE_MERGE | 0.1429 | 0.7143 | 0.1429 | 0.7857 |
| COLUMN_RENAME | 0.0625 | 0.8125 | 0.0625 | 0.7500 |
| COLUMN_MERGE | 0.2143 | 0.5000 | 0.2143 | 0.2143 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 1

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0002 |
| ast_relink | 0.0009 | 0.0009 |

### Database: hr_1

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.2105 |
| Refreshed | 0.6711 |
| Error-Feedback | 0.2105 |
| MCP | 0.6184 |

Recovery Rate (RR): 0.8857

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1875 | 0.5625 | 0.1875 | 0.5625 |
| TABLE_SPLIT | 0.1875 | 0.5625 | 0.1875 | 0.5625 |
| TABLE_MERGE | 0.1429 | 0.7143 | 0.1429 | 0.6429 |
| COLUMN_RENAME | 0.3125 | 0.5625 | 0.3125 | 0.5625 |
| COLUMN_MERGE | 0.2143 | 1.0000 | 0.2143 | 0.7857 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0001 |
| ast_relink | 0.0007 | 0.0005 |


## Model: gpt-4o

### Database: concert_singer

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.1579 |
| Refreshed | 0.8158 |
| Error-Feedback | 0.1579 |
| MCP | 0.7895 |

Recovery Rate (RR): 0.9600

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1250 | 0.8750 | 0.1250 | 0.8750 |
| TABLE_SPLIT | 0.1250 | 0.9375 | 0.1250 | 0.8750 |
| TABLE_MERGE | 0.1429 | 0.8571 | 0.1429 | 0.8571 |
| COLUMN_RENAME | 0.1250 | 1.0000 | 0.1250 | 1.0000 |
| COLUMN_MERGE | 0.2857 | 0.3571 | 0.2857 | 0.2857 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0001 |
| ast_relink | 0.0007 | 0.0003 |

### Database: hr_1

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.2105 |
| Refreshed | 0.6842 |
| Error-Feedback | 0.2105 |
| MCP | 0.6579 |

Recovery Rate (RR): 0.9445

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1875 | 0.5625 | 0.1875 | 0.5625 |
| TABLE_SPLIT | 0.1875 | 0.5625 | 0.1875 | 0.5625 |
| TABLE_MERGE | 0.1429 | 0.7857 | 0.1429 | 0.7143 |
| COLUMN_RENAME | 0.3125 | 0.5625 | 0.3125 | 0.5625 |
| COLUMN_MERGE | 0.2143 | 1.0000 | 0.2143 | 0.9286 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0001 |
| ast_relink | 0.0007 | 0.0004 |


## Model: gemini

### Database: concert_singer

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.1579 |
| Refreshed | 0.8816 |
| Error-Feedback | 0.1579 |
| MCP | 0.8289 |

Recovery Rate (RR): 0.9273

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1250 | 0.7500 | 0.1250 | 0.9375 |
| TABLE_SPLIT | 0.1250 | 1.0000 | 0.1250 | 1.0000 |
| TABLE_MERGE | 0.1429 | 0.8571 | 0.1429 | 0.7857 |
| COLUMN_RENAME | 0.1250 | 1.0000 | 0.1250 | 1.0000 |
| COLUMN_MERGE | 0.2857 | 0.7857 | 0.2857 | 0.3571 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0001 |
| ast_relink | 0.0009 | 0.0004 |

### Database: hr_1

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.2105 |
| Refreshed | 0.6842 |
| Error-Feedback | 0.2237 |
| MCP | 0.6579 |

Recovery Rate (RR): 0.9444

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1875 | 0.6250 | 0.1875 | 0.5625 |
| TABLE_SPLIT | 0.1875 | 0.6250 | 0.1875 | 0.5625 |
| TABLE_MERGE | 0.1429 | 0.7143 | 0.1429 | 0.7143 |
| COLUMN_RENAME | 0.3125 | 0.5625 | 0.3750 | 0.5625 |
| COLUMN_MERGE | 0.2143 | 0.9286 | 0.2143 | 0.9286 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0003 | 0.0004 |
| ast_relink | 0.0007 | 0.0004 |


## Model: llama31

### Database: concert_singer

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.1579 |
| Refreshed | 0.8553 |
| Error-Feedback | 0.1579 |
| MCP | 0.8026 |

Recovery Rate (RR): 0.9245

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1250 | 0.8125 | 0.1250 | 0.7500 |
| TABLE_SPLIT | 0.1250 | 0.9375 | 0.1250 | 0.8750 |
| TABLE_MERGE | 0.1429 | 0.8571 | 0.1429 | 0.9286 |
| COLUMN_RENAME | 0.1250 | 1.0000 | 0.1250 | 0.9375 |
| COLUMN_MERGE | 0.2857 | 0.6429 | 0.2857 | 0.5000 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0001 |
| ast_relink | 0.0007 | 0.0004 |

### Database: hr_1

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | 0.2237 |
| Refreshed | 0.7237 |
| Error-Feedback | 0.2237 |
| MCP | 0.7105 |

Recovery Rate (RR): 0.9737

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | 0 |
| MCP vs Error-Feedback | 0 |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | 0.1875 | 0.6875 | 0.1875 | 0.6250 |
| TABLE_SPLIT | 0.1875 | 0.6250 | 0.1875 | 0.6250 |
| TABLE_MERGE | 0.1429 | 0.7857 | 0.1429 | 0.7857 |
| COLUMN_RENAME | 0.3750 | 0.6250 | 0.3750 | 0.6250 |
| COLUMN_MERGE | 0.2143 | 0.9286 | 0.2143 | 0.9286 |

**Expected Failures**

Unanswerable queries: 4 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | 0.0002 | 0.0001 |
| ast_relink | 0.0006 | 0.0003 |


## Model: grok

### Database: concert_singer

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | n/a |
| Refreshed | n/a |
| Error-Feedback | n/a |
| MCP | n/a |

Recovery Rate (RR): n/a

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | n/a |
| MCP vs Error-Feedback | n/a |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | n/a | n/a | n/a | n/a |
| TABLE_SPLIT | n/a | n/a | n/a | n/a |
| TABLE_MERGE | n/a | n/a | n/a | n/a |
| COLUMN_RENAME | n/a | n/a | n/a | n/a |
| COLUMN_MERGE | n/a | n/a | n/a | n/a |

**Expected Failures**

Unanswerable queries: 0 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | n/a | n/a |
| ast_relink | n/a | n/a |

### Database: hr_1

**EX / RR**

| Path | EX |
| --- | --- |
| Stale | n/a |
| Refreshed | n/a |
| Error-Feedback | n/a |
| MCP | n/a |

Recovery Rate (RR): n/a

**McNemar p-values**

| Comparison | p-value |
| --- | --- |
| MCP vs Stale | n/a |
| MCP vs Error-Feedback | n/a |

**Per-Operator EX**

| Operator | Stale | Refreshed | Error-Feedback | MCP |
| --- | --- | --- | --- | --- |
| TABLE_RENAME | n/a | n/a | n/a | n/a |
| TABLE_SPLIT | n/a | n/a | n/a | n/a |
| TABLE_MERGE | n/a | n/a | n/a | n/a |
| COLUMN_RENAME | n/a | n/a | n/a | n/a |
| COLUMN_MERGE | n/a | n/a | n/a | n/a |

**Expected Failures**

Unanswerable queries: 0 | Path D flagged (non-valid verdicts): 0

**Latency (median / IQR, seconds)**

| Metric | Median | IQR |
| --- | --- | --- |
| fingerprint_diff | n/a | n/a |
| ast_relink | n/a | n/a |


## Scalability Benchmarks

| Tables | Fingerprint Median (s) | Fingerprint IQR (s) | Relink Median (s) | Relink IQR (s) |
| --- | --- | --- | --- | --- |
| 10 | 0.0001 | 0.0000 | 0.0007 | 0.0225 |
| 100 | 0.0011 | 0.0002 | 0.0017 | 0.0001 |
| 1000 | 0.0094 | 0.0005 | 0.0114 | 0.0008 |
