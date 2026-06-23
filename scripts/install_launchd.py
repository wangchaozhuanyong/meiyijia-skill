#!/usr/bin/env python3
"""Install or remove the macOS launchd job for Meiyijia product sync."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.meiyijia.product-sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = ROOT / "logs"
SCHEDULE_TIMEZONE = "America/Los_Angeles"
SCHEDULE_HOUR = 12


def venv_python() -> Path:
    candidate = ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def assert_install_ready() -> None:
    env_path = ROOT / ".env"
    python_path = ROOT / ".venv" / "bin" / "python"
    if not env_path.exists():
        raise SystemExit(f"Missing {env_path}. Create it from .env.example and fill credentials first.")
    if not python_path.exists():
        raise SystemExit("Missing .venv. Run: python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt")
    result = subprocess.run(
        [str(python_path), "-c", "import playwright"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit("Playwright is not installed in .venv. Run: . .venv/bin/activate && pip install -r requirements.txt")


def build_plist() -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(venv_python()),
            str(ROOT / "scripts" / "sync_products.py"),
            "--scheduled",
            "--schedule-timezone",
            SCHEDULE_TIMEZONE,
            "--schedule-hour",
            str(SCHEDULE_HOUR),
            "--run",
        ],
        "WorkingDirectory": str(ROOT),
        "StartInterval": 600,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / "launchd.out.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"),
        },
    }


def run_launchctl(args: list[str]) -> None:
    subprocess.run(["launchctl", *args], check=False)


def install() -> None:
    assert_install_ready()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PLIST_PATH.exists():
        uninstall()
    with PLIST_PATH.open("wb") as file:
        plistlib.dump(build_plist(), file, sort_keys=False)
    run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)])
    run_launchctl(["enable", f"gui/{os.getuid()}/{LABEL}"])
    print(f"Installed launchd job: {PLIST_PATH}")
    print(f"It checks every 10 minutes and runs once per scheduled date during {SCHEDULE_HOUR:02d}:00-"
          f"{SCHEDULE_HOUR:02d}:59 in {SCHEDULE_TIMEZONE}.")


def uninstall() -> None:
    run_launchctl(["bootout", f"gui/{os.getuid()}", str(PLIST_PATH)])
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    print(f"Removed launchd job: {PLIST_PATH}")


def print_status() -> None:
    print(f"Plist: {PLIST_PATH}")
    print(f"Exists: {PLIST_PATH.exists()}")
    run_launchctl(["print", f"gui/{os.getuid()}/{LABEL}"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the Meiyijia product sync launchd job.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install:
        install()
    elif args.uninstall:
        uninstall()
    else:
        print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
