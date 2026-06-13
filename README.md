# Meiyijia Product Sync

Automates the daily product synchronization from Pospal beta product management to the MYJ admin product importer.

## Current Stack

- Python
- Playwright
- macOS launchd
- `pip` with `requirements.txt`

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

Fill `.env` with local credentials. Never commit `.env`.

## Commands

```bash
python scripts/sync_products.py --dry-run
python scripts/sync_products.py --check-myj
python scripts/sync_products.py --run
python scripts/check_skill.py
python scripts/install_runtime_launchd.py --status
```

Real imports require explicit operator approval.

## Safety Features

- Dry-run is the default operational mode.
- Export files are validated for type, size, row count, and required columns before import.
- MYJ API import results are checked for failed rows and abnormal success counts.
- Pack/spec warnings are written to JSON, CSV, and the Markdown daily report.
- Scheduled runs use Beijing time and write a once-per-date completion marker.
- Stale lock files are cleared only when the recorded process is gone or the lock is old enough.

## Git Notes

This project should track source files, tests, docs, and templates only.

Ignored local runtime artifacts include `.env`, `.venv/`, `downloads/`, `logs/`, `screenshots/`, `.schedule-state/`, and `tmp/`.
