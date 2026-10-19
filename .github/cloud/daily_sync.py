#!/usr/bin/env python3
"""Coordinate daily imports and Chinese reports without changing the importer."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
TITLE = "美宜佳每日商品同步报告"
STATE_PREFIX = "<!-- meiyijia-cloud-state:"
REQUIRED = ("POSPAL_USERNAME", "POSPAL_PASSWORD", "MYJ_ADMIN_USERNAME", "MYJ_ADMIN_PASSWORD")
STATUSES = {"idle", "running", "succeeded", "needs_review"}


class GitHub:
    def __init__(self, environ):
        self.repository = environ["GITHUB_REPOSITORY"]
        self.token = environ["GH_TOKEN"]

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "meiyijia-daily-sync",
            },
        )
        try:
            with urlopen(req, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            # Response bodies and request headers must not enter public reports.
            raise RuntimeError(f"GitHub API HTTP {exc.code}") from None

    def issue(self):
        matches = []
        page = 1
        while True:
            issues = self.request("GET", "issues?" + urlencode({"state": "all", "per_page": 100, "page": page}))
            matches.extend(item for item in issues if "pull_request" not in item and item["title"] == TITLE)
            if len(issues) < 100:
                break
            page += 1
        if len(matches) > 1:
            raise RuntimeError("发现多个同名报告 Issue，请先合并，避免分裂同步状态。")
        if matches:
            return matches[0]
        return self.request("POST", "issues", {"title": TITLE, "body": issue_body({"status": "idle"})})

    def state(self, number):
        issue = self.request("GET", f"issues/{number}")
        body = issue.get("body") or ""
        match = re.search(re.escape(STATE_PREFIX) + r"(.*?)-->", body, re.S)
        if not match:
            raise RuntimeError("报告 Issue 的同步状态缺失，请先核实历史导入；不会自动重建。")
        state = json.loads(match.group(1))
        if state.get("status") not in STATUSES:
            raise RuntimeError("报告 Issue 的同步状态无效。")
        if state["status"] != "idle":
            datetime.strptime(state["business_date"], "%Y-%m-%d")
            if not re.fullmatch(r"\d+:\d+", state.get("run_key", "")):
                raise RuntimeError("报告 Issue 的执行标识无效。")
        return state

    def save_state(self, number, state):
        self.request("PATCH", f"issues/{number}", {"body": issue_body(state)})

    def comment(self, number, body):
        self.request("POST", f"issues/{number}/comments", {"body": body})


def issue_body(state, report=""):
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
    return (
        "每天北京时间／吉隆坡时间 12:00 执行商品同步；GitHub 调度可能延迟。\n\n"
        "每次执行结果见评论。请勿删除下方机器状态，它用于防止重复导入。\n\n"
        + report + "\n\n" + STATE_PREFIX + encoded + " -->\n"
    )


class Coordinator:
    def __init__(self, root=ROOT, environ=None, api=None, now=None, run=None):
        self.root = root
        self.environ = os.environ if environ is None else environ
        self.api = api if api is not None else GitHub(self.environ)
        self.now = now or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self.run = run or subprocess.run
        self.directory = root / ".schedule-state"
        self.context_path = self.directory / "cloud-context.json"

    def write(self, context):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    def read(self):
        return json.loads(self.context_path.read_text(encoding="utf-8"))

    def output(self, value):
        path = self.environ.get("GITHUB_OUTPUT")
        if path:
            with open(path, "a", encoding="utf-8") as file:
                file.write(f"should_run={'true' if value else 'false'}\n")

    def prepare(self):
        env = self.environ
        mode = "run" if env.get("GITHUB_EVENT_NAME") == "schedule" else env.get("SYNC_MODE", "dry-run")
        context = {
            "mode": mode,
            "business_date": self.now().strftime("%Y-%m-%d"),
            "run_key": f"{env['GITHUB_RUN_ID']}:{env.get('GITHUB_RUN_ATTEMPT', '1')}",
            "run_url": f"https://github.com/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}",
            "status": "preparing",
        }
        self.write(context)
        self.output(False)
        try:
            if mode not in {"run", "dry-run", "check-myj"}:
                raise RuntimeError("无效的同步模式。")
            if mode == "run" and env.get("MEIYIJIA_CLOUD_SYNC_ENABLED") != "true":
                context.update(status="disabled", detail="云端同步尚未启用。请先接入凭据并停用本机 launchd。")
                self.write(context)
                return 0
            issue = self.api.issue()
            context["issue"] = issue["number"]
            self.write(context)
            missing = [name for name in REQUIRED if not env.get(name, "").strip() or env[name].startswith("your-")]
            if missing:
                raise RuntimeError("缺少凭据：" + "、".join(missing))
            state = self.api.state(context["issue"])
            if mode == "run":
                if state["status"] in {"running", "needs_review"}:
                    key = env.get("RESOLVE_RUN_KEY", "")
                    result = env.get("PREVIOUS_RESULT", "unverified")
                    if key != state["run_key"] or result not in {"not-imported", "completed"}:
                        raise RuntimeError(f"上次导入结果待核实（{state['run_key']}），已暂停自动导入。")
                    state["status"] = "succeeded" if result == "completed" else "idle"
                    self.api.save_state(context["issue"], state)
                    self.api.comment(context["issue"], f"已人工核实 {key}：{'确认已完成' if result == 'completed' else '确认未导入'}。")
                if state["status"] == "succeeded" and state["business_date"] == context["business_date"]:
                    context.update(status="skipped", detail="当天已成功同步，跳过重复执行。")
                    self.write(context)
                    return 0
            context.update(status="ready", detail="前置检查通过，等待安装和执行。")
            self.write(context)
            self.output(True)
            return 0
        except Exception as exc:
            context.update(status="blocked", detail=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
            self.write(context)
            return 1

    def execute(self):
        context = self.read()
        if context["status"] != "ready":
            raise RuntimeError("前置检查未通过，不能执行同步。")
        real = context["mode"] == "run"
        try:
            if real:
                state = self.api.state(context["issue"])
                if state["status"] in {"running", "needs_review"} or (
                    state["status"] == "succeeded" and state["business_date"] == context["business_date"]
                ):
                    raise RuntimeError("执行前同步状态已变化，已阻止重复导入。")
                self.api.save_state(context["issue"], {
                    "status": "running", "business_date": context["business_date"],
                    "run_key": context["run_key"], "run_url": context["run_url"],
                })
            context.update(status="running", detail="同步开始。")
            self.write(context)
            # Playwright exception chains can include filled values. Keep raw output
            # on the ephemeral runner, outside public Actions logs and artifacts.
            log_directory = self.root / "logs"
            log_directory.mkdir(parents=True, exist_ok=True)
            with (log_directory / "cloud-sync-console.log").open("w", encoding="utf-8") as console:
                result = self.run(
                    [sys.executable, "scripts/sync_products.py", f"--{context['mode']}"],
                    cwd=self.root, check=False, stdout=console, stderr=subprocess.STDOUT,
                )
            if result.returncode:
                raise RuntimeError(f"同步程序退出码 {result.returncode}。请检查运行日志。")
            if real:
                marker = self.directory / f"completed-{context['business_date']}.txt"
                summary = self.directory / "last-success-summary.md"
                if not marker.is_file() or not summary.is_file():
                    raise RuntimeError("程序退出成功，但缺少本次完成标记或成功摘要。")
                summary_text = summary.read_text(encoding="utf-8")
                context["counts"] = [
                    line for line in summary_text.splitlines()
                    if re.fullmatch(r"- (?:新增|更新|跳过|成功|失败|拆包/规格警告)：\d+", line)
                ]
                context["warning_review"] = "需要人工复核" in summary_text
                self.api.save_state(context["issue"], {
                    "status": "succeeded", "business_date": context["business_date"],
                    "run_key": context["run_key"], "run_url": context["run_url"],
                })
            context.update(status="succeeded", detail="商品同步成功。" if real else "检查成功，未上传商品。")
            if context.get("warning_review"):
                context["detail"] = "商品同步成功；拆包／规格警告需要人工复核。"
            self.write(context)
            return 0
        except Exception as exc:
            started = context["status"] == "running"
            context.update(
                status="needs_review" if real and started else "failed",
                detail=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
            )
            self.write(context)
            if real and started:
                # If this write fails, the durable 'running' state still blocks retries.
                self.api.save_state(context["issue"], {
                    "status": "needs_review", "business_date": context["business_date"],
                    "run_key": context["run_key"], "run_url": context["run_url"],
                })
            return 1

    def report(self):
        context = self.read() if self.context_path.exists() else {"status": "failed", "detail": "工作流未完成环境准备。"}
        labels = {
            "succeeded": "成功", "skipped": "跳过", "disabled": "未启用", "blocked": "被阻止",
            "ready": "环境准备失败，未执行同步", "preparing": "前置检查中断",
            "running": "执行中断，结果待核实", "needs_review": "失败或结果不明，需核实", "failed": "失败",
        }
        lines = [
            f"# 美宜佳商品同步日报 {context.get('business_date', self.now().strftime('%Y-%m-%d'))}", "",
            "- 计划：北京时间／吉隆坡时间每天 12:00",
            f"- 状态：{labels[context['status']]}",
            f"- 模式：{context.get('mode', '未执行')}",
            f"- 说明：{context.get('detail', '')}",
            f"- 执行标识：{context.get('run_key', '未开始')}",
        ]
        lines.extend(context.get("counts", []))
        if context.get("run_url"):
            lines.append(f"- 详细日志：[GitHub Actions]({context['run_url']})")
        if context["status"] in {"running", "needs_review"}:
            lines.append("- 下一步：先核对 MYJ 导入记录，再使用手动执行中的核实结果恢复；不要直接重复导入。")
        report = "\n".join(lines) + "\n"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "cloud-report.md").write_text(report, encoding="utf-8")
        if self.environ.get("GITHUB_STEP_SUMMARY"):
            with open(self.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
                file.write(report)
        print(report)
        if context["status"] == "disabled":
            return 0
        try:
            number = context.get("issue") or self.api.issue()["number"]
            mention = self.environ.get("MEIYIJIA_REPORT_MENTION", "")
            prefix = f"@{mention}\n\n" if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", mention) else ""
            self.api.comment(number, prefix + report)
            state = self.api.state(number)
            self.api.request("PATCH", f"issues/{number}", {"body": issue_body(state, report)})
            return 0
        except Exception as exc:
            print(f"::error::GitHub Issue 汇报失败（{type(exc).__name__}）；日报已写入 Actions Summary。")
            return 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "execute", "report"}:
        raise SystemExit("Usage: daily_sync.py prepare|execute|report")
    raise SystemExit(getattr(Coordinator(), sys.argv[1])())
