# Version Status

## 2026-06-13 Reliability Upgrade

Status: local implementation ready for review.

Remote repository: `https://github.com/wangchaozhuanyong/meiyijia-skill.git`

## Confirmed Capabilities

- Logs in to Pospal beta product management.
- Exports product records.
- Logs in to MYJ admin.
- Imports through the MYJ backend API after browser login.
- Runs under launchd with an operator-computer-time scheduled gate.
- Avoids repeated scheduled imports with a business-date completion marker.
- Keeps runtime artifacts out of Git through `.gitignore`.

## Added Reliability Controls

- Stronger XLSX/CSV export validation.
- MYJ import result threshold checks.
- Warning JSON/CSV and Markdown daily reports for failed or unclear runs.
- Successful real-run artifact cleanup after the Beijing-date completion marker is written.
- Codex left-sidebar visibility rule: keep a separate report-only Codex automation after the local 12:00 PM launchd update window.
- Failure Markdown reports.
- Stale lock cleanup.
- Unit tests and `scripts/check_skill.py`.

## Remaining Risks

- Pospal or MYJ login pages can still change selectors or introduce captcha/MFA.
- The pack/spec warning list still needs human business review.
- The success-count thresholds should be adjusted if the normal product volume changes materially.
- The Codex-visible record is a read-only report of the launchd result; it is not the real importer and must not be converted into a duplicate `--run` path unless launchd is first removed or disabled.
- No remote commit or push has been performed unless the operator explicitly requests it.

## 2026-06-23 Operator-Time Schedule Refactor

Status: local implementation ready for review.

## Confirmed Changes

- Scheduled real sync defaults to the operator computer timezone `America/Los_Angeles` at 12:00 PM.
- Completion markers and success summaries still use `.env` `TIMEZONE` (default `Asia/Shanghai`) so business-date de-duplication remains stable.
- The Codex visible report should run after the real update window. For the current PDT offset, use UTC 19:30, equal to 12:30 PM PDT and Beijing 03:30 the next day.
- The old `--beijing-hour` CLI remains backward-compatible for existing manual commands.

## Remaining Risks

- The UTC visible-report time must be rechecked when America/Los_Angeles leaves daylight saving time, unless the Codex scheduler is confirmed to honor America/Los_Angeles wall-clock time correctly.
- Existing installed launchd jobs must be reinstalled or updated before they pick up the new operator-time launch arguments. The current runtime was refreshed on 2026-06-23.
