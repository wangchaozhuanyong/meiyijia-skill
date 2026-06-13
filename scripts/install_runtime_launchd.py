#!/usr/bin/env python3
"""Install the launchd job from a non-Desktop runtime copy."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path.home() / ".codex" / "automations" / "meiyijia-product-sync"
IGNORED_NAMES = {
    ".git",
    ".DS_Store",
    ".venv",
    "downloads",
    "logs",
    "screenshots",
    ".schedule-state",
    "tmp",
    "__pycache__",
}


def ignore_runtime_artifacts(dir_path: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in IGNORED_NAMES or name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def sync_runtime() -> None:
    RUNTIME_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_ROOT, RUNTIME_ROOT, ignore=ignore_runtime_artifacts, dirs_exist_ok=True)


def install() -> None:
    sync_runtime()
    run([sys.executable, "-m", "venv", ".venv"], RUNTIME_ROOT)
    python = RUNTIME_ROOT / ".venv" / "bin" / "python"
    pip = RUNTIME_ROOT / ".venv" / "bin" / "pip"
    run([str(pip), "install", "-r", "requirements.txt"], RUNTIME_ROOT)
    run([str(python), "-m", "playwright", "install", "chromium"], RUNTIME_ROOT)
    run([str(python), "scripts/install_launchd.py", "--install"], RUNTIME_ROOT)


def status() -> None:
    python = RUNTIME_ROOT / ".venv" / "bin" / "python"
    run([str(python), "scripts/install_launchd.py", "--status"], RUNTIME_ROOT)


def uninstall() -> None:
    python = RUNTIME_ROOT / ".venv" / "bin" / "python"
    if python.exists():
        run([str(python), "scripts/install_launchd.py", "--uninstall"], RUNTIME_ROOT)
    else:
        run([sys.executable, str(SOURCE_ROOT / "scripts/install_launchd.py"), "--uninstall"], SOURCE_ROOT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Meiyijia product sync launchd from a safe runtime path.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install:
        install()
    elif args.status:
        status()
    else:
        uninstall()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
