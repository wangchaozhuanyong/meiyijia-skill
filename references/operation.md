# Operation Notes

## Required confirmation before real imports

Only run `scripts/sync_products.py --run` after the user explicitly authorizes a real MYJ batch import. Use `--dry-run` for Pospal export checks and `--check-myj` for MYJ importer checks without uploading.

## Current known URLs

- Pospal product management: `https://beta9.pospal.cn/Product/Manage`
- MYJ products admin: `https://admin.myj.info/products`

Credentials must come from `.env` or environment variables:

- `POSPAL_USERNAME`
- `POSPAL_PASSWORD`
- `MYJ_ADMIN_USERNAME`
- `MYJ_ADMIN_PASSWORD`

## Selector strategy

The two web apps may change markup. Prefer robust locators in this order:

1. Accessible labels and roles.
2. Exact visible text for stable Chinese labels such as login, product data, export, product management, batch import, upload, confirm, import.
3. Scoped CSS selectors only after inspecting the current page.

When a selector fails, run:

```bash
python scripts/sync_products.py --dry-run --headed --slow-mo 300
```

Then inspect `screenshots/` and update the smallest necessary part of `scripts/sync_products.py`.

## Beijing-time scheduling behavior

The launchd job runs every 10 minutes, but the Python script checks `Asia/Shanghai` time. It only executes once per Beijing date when the Beijing hour is `03`.

This avoids a common bug: a Mac in America/Los_Angeles cannot use a fixed local `StartCalendarInterval` and still stay aligned to Beijing 03:00 across daylight saving time changes.

Install launchd through `scripts/install_runtime_launchd.py`, not `scripts/install_launchd.py` directly from Desktop. The runtime installer copies the skill to `~/.codex/automations/meiyijia-product-sync` first because macOS LaunchAgents may not have Desktop folder permission.

## Risk controls

- Dry-run exports only and never uploads to MYJ.
- A successful real import writes the current Beijing date marker, so a later scheduled check on the same Beijing date will skip.
- If an old lock file remains after a crash, the script clears it only when the recorded process is gone or the lock is older than the stale-lock window.
- Export files are rejected before upload when they are too small, malformed, have too few product rows, or are missing required columns such as 名称、分类、条码、进货价、销售价、商品状态.
- Import results are rejected when MYJ reports failed rows or an unexpectedly low success count.
- Screenshots are taken around important steps.
- Logs are timestamped and do not print passwords.
- Successful real imports write `daily-report-*.md`; high pack/spec warning counts also write `warnings-*.json` and `warnings-*.csv` for review.
- Real import posts to `/api/admin/products/import` with a 300-second timeout after browser login. The MYJ web UI can time out after about 15 seconds and show a misleading file-format error even when the backend import succeeds.
- Do not loop real imports after an unclear failure; check MYJ admin first.

## Local checks

Run this after script or rule changes:

```bash
python scripts/check_skill.py
```

The check script runs Python compilation, unit tests, CLI help, and basic template checks.

## Daily report review

After a scheduled run, inspect the newest `logs/daily-report-*.md`.

Treat these as review triggers:

- `失败` is non-zero.
- `成功` is much lower than the normal daily product volume.
- `拆包/规格警告` reaches `MYJ_IMPORT_WARNING_REVIEW_THRESHOLD`.
- The same `master_code` appears repeatedly in warning CSV files.
