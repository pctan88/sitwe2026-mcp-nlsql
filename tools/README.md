# Tools

## verify_api_keys.py

Checks that expected API keys are present, and optionally performs lightweight
HTTP checks against each provider.

### Usage

```bash
python3 tools/verify_api_keys.py
python3 tools/verify_api_keys.py --live
python3 tools/verify_api_keys.py --live --insecure
```

Notes:
- `--live` makes network calls to verify keys. It does **not** print secrets.
- The script loads `.env` if present, without overwriting existing env vars.
- If you see SSL certificate errors, install certs or use `--insecure` as a
  temporary local-only workaround.

## build_pilot_report.py

Aggregates per-model outputs into `results/PILOT_RUN_REPORT.md`.

### Usage

```bash
python3 tools/build_pilot_report.py
```
