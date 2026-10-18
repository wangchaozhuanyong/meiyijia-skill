#!/usr/bin/env python3
"""Run on the original computer to bind existing credentials without displaying them."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


REPOSITORY = "wangchaozhuanyong/meiyijia-skill"
NAMES = ("POSPAL_USERNAME", "POSPAL_PASSWORD", "MYJ_ADMIN_USERNAME", "MYJ_ADMIN_PASSWORD")


def read_credentials(path: Path) -> dict[str, str]:
    credentials = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name in NAMES and value and not value.startswith("your-"):
            credentials[name] = value
    missing = [name for name in NAMES if name not in credentials]
    if missing:
        raise RuntimeError("本机配置缺少：" + "、".join(missing))
    return credentials


def discover(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError("指定的本机 .env 文件不存在。")
        return path
    candidates = {
        path.resolve() for path in (
            Path.cwd() / ".env",
            Path(__file__).resolve().parents[2] / ".env",
            Path.home() / ".codex/automations/meiyijia-product-sync/.env",
        ) if path.is_file()
    }
    if not candidates:
        raise RuntimeError("未找到本机现有 .env；请在原电脑运行，并用 --env-file 指定配置。")
    if len(candidates) != 1:
        raise RuntimeError("发现多个本机 .env；请用 --env-file 指定当前实际使用的配置。")
    return candidates.pop()


def list_names(run=subprocess.run) -> set[str]:
    result = run(
        ["gh", "secret", "list", "--repo", REPOSITORY, "--json", "name"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError("无法读取 GitHub Secrets 名称；请检查本机现有 GitHub 认证与仓库权限。")
    return {item["name"] for item in json.loads(result.stdout)}


def bind(credentials: dict[str, str], run=subprocess.run) -> tuple[int, int]:
    existing = list_names(run)
    added = 0
    for name in NAMES:
        if name in existing:
            print(f"{name}: 已有绑定，保留现有值")
            continue
        # gh encrypts the secret for GitHub. Values go only through stdin, never argv or logs.
        result = run(
            ["gh", "secret", "set", name, "--repo", REPOSITORY],
            input=credentials[name].encode("utf-8"),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"{name} 写入失败；已完成的绑定会保留，脚本可安全重跑。")
        added += 1
        print(f"{name}: 已安全写入")
    if not set(NAMES).issubset(list_names(run)):
        raise RuntimeError("写入后尚未确认全部绑定，请检查权限或稍后重跑；不会显示密钥值。")
    return added, len(set(NAMES) & existing)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="本机当前实际使用的 .env 文件")
    parser.add_argument("--check-only", action="store_true", help="仅检查本机来源，不访问或修改 GitHub")
    args = parser.parse_args()
    try:
        credentials = read_credentials(discover(args.env_file))
        if args.check_only:
            print("四项本机凭据均已找到；值未显示，尚未写入 GitHub。")
            return 0
        if shutil.which("gh") is None:
            raise RuntimeError("本机未找到 GitHub CLI（gh）；需要先安装并使用本机现有账号认证。")
        added, reused = bind(credentials)
        print(f"已确认四项 GitHub Actions Secrets：新增 {added} 项，复用 {reused} 项。")
        print("这不等于已写入 Codex 环境密钥，也不表示每日任务已经启用。")
        return 0
    except Exception as exc:
        # Do not print subprocess output, HTTP bodies, or exception data containing credentials.
        print(str(exc) if isinstance(exc, RuntimeError) else f"绑定失败（{type(exc).__name__}）；密钥值未显示。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
