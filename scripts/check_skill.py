#!/usr/bin/env python3
"""Run local safety checks for the Meiyijia product sync skill."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def assert_file_contains(path: Path, required_text: list[str]) -> None:
    content = path.read_text(encoding="utf-8")
    missing = [item for item in required_text if item not in content]
    if missing:
        raise SystemExit(f"{path} is missing required entries: {', '.join(missing)}")


def main() -> int:
    run(
        [
            sys.executable,
            "-m",
            "py_compile",
            "scripts/sync_products.py",
            "scripts/install_launchd.py",
            "scripts/install_runtime_launchd.py",
            "scripts/check_skill.py",
        ]
    )
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    run([sys.executable, "scripts/sync_products.py", "--help"])
    assert_file_contains(
        ROOT / ".env.example",
        [
            "POSPAL_USERNAME=",
            "POSPAL_PASSWORD=",
            "MYJ_ADMIN_USERNAME=",
            "MYJ_ADMIN_PASSWORD=",
            "POSPAL_EXPORT_MIN_ROWS=",
            "MYJ_IMPORT_MIN_SUCCESS_COUNT=",
            "MYJ_IMPORT_WARNING_REVIEW_THRESHOLD=",
            "CLEANUP_SUCCESS_ARTIFACTS=",
        ],
    )
    assert_file_contains(
        ROOT / ".gitignore",
        [
            ".env",
            "downloads/",
            "logs/",
            "screenshots/",
            ".schedule-state/",
            "tmp/",
        ],
    )
    assert_file_contains(
        ROOT / "scripts" / "install_launchd.py",
        [
            'SCHEDULE_TIMEZONE = "America/Los_Angeles"',
            "SCHEDULE_HOUR = 12",
            "--schedule-timezone",
            "--schedule-hour",
        ],
    )
    assert_file_contains(
        ROOT / "SKILL.md",
        [
            "America/Los_Angeles",
            "12:00 PM",
            "UTC 19:30",
            "actively notify the user",
        ],
    )
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
