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
- Successful real imports delete the current run's downloads, screenshots, logs, reports, and warning files after the completion marker is written.
- Failed or unclear runs keep downloads, screenshots, logs, reports, and warning files for troubleshooting.
- Scheduled runs default to the operator's computer time: `America/Los_Angeles` at 12:00 PM, then write the business completion marker by `TIMEZONE` (default `Asia/Shanghai`).
- A report-only Codex automation runs after the local-noon update window so the Codex left sidebar keeps a visible daily execution record.
- The visible Codex record must not run a second import; it only reads `.schedule-state/`, failure logs, and launchd status.
- Stale lock files are cleared only when the recorded process is gone or the lock is old enough.

## Visible Execution Record

The macOS launchd job performs the real background sync. Because launchd does not create a Codex conversation by itself, keep a separate Codex automation scheduled after the local 12:00 PM update window. For the current America/Los_Angeles summer offset, use UTC 19:30, which is 12:30 PM PDT and 03:30 Beijing time the next day.

That automation is report-only. It should summarize `last-success-summary.md`, the Beijing-date completion marker, any latest failure report, warnings, and next action in Simplified Chinese. It must not run `python scripts/sync_products.py --run`, upload products, delete artifacts, or retry an unclear import.

## Git Notes

This project should track source files, tests, docs, and templates only.

Ignored local runtime artifacts include `.env`, `.venv/`, `downloads/`, `logs/`, `screenshots/`, `.schedule-state/`, and `tmp/`.
