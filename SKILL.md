---
name: meiyijia-product-sync
description: Automate the daily Meiyijia product sync workflow from Pospal beta product management to the MYJ admin product importer. Use when Codex needs to export all products from https://beta9.pospal.cn/Product/Manage, import the downloaded product file into https://admin.myj.info/products, configure the operator-computer-time 12:00 schedule, inspect logs/screenshots, or safely dry-run/debug this merchant product synchronization.
---

# Meiyijia Product Sync

## Purpose

Production scheduling currently runs through GitHub Actions at **12:00 Asia/Kuala_Lumpur / Asia/Shanghai**. Follow [.github/CLOUD_SYNC.md](.github/CLOUD_SYNC.md) for the active cloud executor, safe recovery, and Chinese Issue reporting. The local launchd importer is disabled. Keep one real importer: do not reinstall the local schedule while cloud imports are enabled. A Codex cloud task may maintain or inspect this setup; it must not start a second importer. Local completion markers do not establish the result of a GitHub Actions run.

Use this skill to operate and maintain the Meiyijia product synchronization:

1. Log in to Pospal beta product management.
2. Export all product data from product records.
3. Log in to the MYJ admin product page.
4. Open product batch import to verify the importer UI is available.
5. Import the exported file through the MYJ `/api/admin/products/import` endpoint using the logged-in admin token.
6. Keep troubleshooting artifacts only when the run fails or is unclear. Successful real imports clean up current-run artifacts after the completion marker is written.
7. Keep a Codex-visible daily execution record in the left sidebar by running a separate report-only Codex automation after the launchd job. The visible record must summarize the Beijing-date completion marker, success summary, latest failure report if any, and next action. It must not trigger a second import.

Never hard-code usernames, passwords, tokens, or private URLs into code or this skill. Read credentials from `.env` or the process environment.

## Project Files

- `scripts/sync_products.py`: Playwright automation for dry-run and real import.
- `scripts/install_launchd.py`: macOS launchd installer for operator-computer-time 12:00 scheduling.
- `scripts/check_skill.py`: local safety checks for syntax, tests, CLI help, and config templates.
- `tests/`: unittest coverage for export validation, import result assessment, and stale lock cleanup.
- `references/operation.md`: operational checklist, troubleshooting, and selector notes.
- `.env.example`: required environment variable names with placeholder values.
- `requirements.txt`: Python dependencies.

## Execution Rules

- Default to dry-run unless the user explicitly asks for a real import.
- Use `--dry-run` to log in, export the Pospal file, and stop before MYJ upload.
- Use `--run` only when the user clearly authorizes importing into MYJ admin.
- Real imports use the MYJ backend API after browser login because the web UI has a short frontend timeout and can falsely show a file-format error while the backend import succeeds.
- Export files must pass structural validation before import. XLSX/CSV files are checked for row count and required Pospal headers.
- Real import results must have zero failed rows and a sane success count. High pack/spec warning counts generate review reports instead of silently passing.
- Successful real imports delete current-run downloads, screenshots, logs, reports, warning files, and launchd stdout/stderr logs after writing the Beijing-date completion marker.
- Failed or unclear runs keep all current-run artifacts for troubleshooting.
- Every scheduled day must also have a Codex-visible report-only run after the 12:00 PM America/Los_Angeles launchd update window. This is the operator-facing conversation record in the Codex left sidebar, and it must actively notify the user with the execution result instead of only writing local logs.
- The Codex-visible report automation is read-only: inspect `.schedule-state/`, `logs/`, `downloads/`, and launchd status only. Do not run `--run`, do not upload files, and do not delete artifacts from the report automation.
- A stale scheduler lock is cleared only when the original process is gone or the lock is older than the safety window.
- Before editing automation selectors, inspect the current site behavior and keep changes minimal.
- After script changes, run `python scripts/check_skill.py`.
- If Playwright browsers are missing, run `python3 -m playwright install chromium`.

## Setup

From the skill directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

Fill `.env` locally with real credentials. Do not commit `.env`.

Optional safety thresholds can be tuned in `.env` after checking normal daily volumes:

- `POSPAL_EXPORT_MIN_ROWS`
- `MYJ_IMPORT_MIN_SUCCESS_COUNT`
- `MYJ_IMPORT_WARNING_REVIEW_THRESHOLD`
- `CLEANUP_SUCCESS_ARTIFACTS` defaults to `true`; set to `false` only when you intentionally want to inspect success artifacts.

## Manual Runs

Dry-run:

```bash
. .venv/bin/activate
python scripts/sync_products.py --dry-run
```

MYJ importer check without uploading:

```bash
. .venv/bin/activate
python scripts/sync_products.py --check-myj
```

Real import:

```bash
. .venv/bin/activate
python scripts/sync_products.py --run
```

Visible browser for debugging:

```bash
. .venv/bin/activate
python scripts/sync_products.py --dry-run --headed --slow-mo 300
```

Local checks:

```bash
. .venv/bin/activate
python scripts/check_skill.py
```

## Operator-Computer-Time Schedule

Install a macOS launchd job that checks every 10 minutes and runs only once per business marker date after the operator's computer reaches 12:00 PM America/Los_Angeles:

```bash
. .venv/bin/activate
python scripts/install_runtime_launchd.py --install
```

The runtime installer copies the skill to `~/.codex/automations/meiyijia-product-sync` before installing launchd. This avoids macOS Desktop privacy restrictions that can block LaunchAgents from reading files under `~/Desktop`.

Unload it:

```bash
python scripts/install_runtime_launchd.py --uninstall
```

The installer now uses the script's `--scheduled --schedule-timezone America/Los_Angeles --schedule-hour 12 --run` gate. The trigger time is the user's computer time. The completion marker and success summary still use `TIMEZONE` from `.env` (default `Asia/Shanghai`) so the business date remains stable.

## Codex Visible Record

The launchd job is the real executor, but it runs in the macOS background and does not create a Codex chat record by itself. To keep an operator-visible record in the Codex left sidebar, configure a separate Codex automation to run after the local 12:00 PM update window. For the current America/Los_Angeles summer offset, use UTC 19:30, which is 12:30 PM PDT and 03:30 Beijing time the next day.

That Codex automation must be report-only:

- Read `.schedule-state/last-success-summary.md` and the latest `completed-YYYY-MM-DD.txt`.
- If no success marker exists for the Beijing date, inspect the newest `logs/daily-report-*-failed.md`, newest `sync-*.log`, and launchd status.
- Report the result in Simplified Chinese with status, counts, warnings, file references, risk, and next action.
- Always notify the user in the final response after the automatic execution window, whether the result is success, failure, skipped, or unclear.
- Do not run `python scripts/sync_products.py --run`.
- Do not repeat a real import if launchd already completed or if the result is unclear.

## Failure Handling

When a run fails:

1. Read the newest file in `logs/`.
2. Inspect matching screenshots in `screenshots/`.
3. Check whether the browser hit captcha, MFA, password expiry, or changed button text.
4. Run a headed dry-run.
5. Update selectors in `scripts/sync_products.py` only after confirming the live UI.

Do not retry real imports repeatedly if the first import may have partially succeeded. Inspect MYJ admin import history or product count first.

After successful real imports, current-run artifacts are intentionally removed. If files remain in `logs/`, `downloads/`, or `screenshots/` after a real run, treat that as a failure or unclear run and inspect them before retrying.
