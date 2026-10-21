#!/usr/bin/env python3
"""Coordinate daily imports, durable recovery, and Chinese reports."""

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
sys.path.insert(0, str(ROOT))
from scripts.import_diagnostic import (  # noqa: E402
    BrowserAutomationError, DIAGNOSTIC_ERRORS, DIAGNOSTIC_OUTCOMES,
    DIAGNOSTIC_REQUEST_STATES, DIAGNOSTIC_STAGES, import_count_semantics, strict_import_counts,
)

TITLE = "美宜佳每日商品同步报告"
STATE_PREFIX = "<!-- meiyijia-cloud-state:"
REQUIRED = ("POSPAL_USERNAME", "POSPAL_PASSWORD", "MYJ_ADMIN_USERNAME", "MYJ_ADMIN_PASSWORD")
STATUSES = {"idle", "running", "succeeded", "needs_review", "reconciled"}
RECONCILIATION_PREFIX = "<!-- meiyijia-reconciliation:"


class SafeError(RuntimeError):
    """A fixed coordinator message that is safe for the public report."""


def safe_diagnostic(value, run_key):
    """Treat child files as untrusted; copy only validated, public-safe fields."""
    if not isinstance(run_key, str) or not re.fullmatch(r"\d+:\d+", run_key):
        return None
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or value.get("run_key") != run_key:
        return None
    fields = {"stage": DIAGNOSTIC_STAGES, "request_state": DIAGNOSTIC_REQUEST_STATES,
              "outcome": DIAGNOSTIC_OUTCOMES, "error": DIAGNOSTIC_ERRORS}
    if any(not isinstance(value.get(key), str) or value[key] not in allowed for key, allowed in fields.items()):
        return None
    safe = {"version": 1, "run_key": run_key, **{key: value[key] for key in fields}}
    for key in ("export_rows", "warning_details", "warning_total", "category_checked", "category_created"):
        if key in value:
            if type(value[key]) is not int or value[key] < 0:
                return None
            safe[key] = value[key]
    if safe.get("warning_total", safe.get("warning_details", 0)) < safe.get("warning_details", 0):
        return None
    if safe.get("category_created", 0) > safe.get("category_checked", 0):
        return None
    if "http_status" in value:
        if type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599:
            return None
        safe["http_status"] = value["http_status"]
    if "counts" in value:
        try:
            safe["counts"] = strict_import_counts(
                {"success": True, "data": value["counts"]}, value.get("export_rows"),
            )
            safe["count_semantics"] = import_count_semantics(safe["counts"])
        except BrowserAutomationError:
            return None
    if safe["outcome"] in {"succeeded", "partial", "rejected"}:
        counts = safe.get("counts")
        if not counts or safe["request_state"] != "response_received":
            return None
        if safe["outcome"] == "succeeded" and counts["failCount"]:
            return None
        if safe["outcome"] == "partial" and not (counts["successCount"] and counts["failCount"]):
            return None
    if safe["request_state"] != "not_started" and safe["outcome"] == "not_imported":
        return None
    return safe


def completed_today(state, business_date):
    return (state["status"] == "succeeded" and state.get("business_date") == business_date) or (
        state["status"] == "reconciled" and state.get("reconciliation_date") == business_date
    )


def validated_reconciliation(comment, repository, issue, run_key):
    """Accept a linked, trusted catalog readback record, never arbitrary prose."""
    if not isinstance(comment, dict) or comment.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"} or comment.get("issue_url") != f"https://api.github.com/repos/{repository}/issues/{issue}":
        raise SafeError("对账证据必须来自本仓库报告 Issue 的维护者评论。")
    matches = re.findall(re.escape(RECONCILIATION_PREFIX) + r"(.*?)-->", comment.get("body", ""), re.DOTALL)
    try:
        if len(matches) != 1 or len(matches[0]) > 4096:
            raise ValueError
        proof = json.loads(matches[0])
    except (TypeError, ValueError):
        raise SafeError("对账证据缺少有效的结构化读回记录。") from None
    return validated_reconciliation_record(proof, run_key)


def validated_reconciliation_record(proof, run_key):
    try:
        if not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1 or proof.get("run_key") != run_key:
            raise ValueError
        numbers = ("source_rows", "target_rows", "target_before", "added_rows", "failed_rows", "source_missing", "removed_rows", "stock_precision_rows", "cost_precision_rows")
        if any(type(proof.get(key)) is not int or proof[key] < 0 for key in numbers):
            raise ValueError
        if not proof["source_rows"] or proof["target_rows"] < proof["source_rows"] or proof["target_before"] + proof["added_rows"] != proof["target_rows"] or any(proof[key] for key in ("failed_rows", "source_missing", "removed_rows")):
            raise ValueError
        if any(proof[key] > proof["added_rows"] for key in ("stock_precision_rows", "cost_precision_rows")):
            raise ValueError
        if not isinstance(proof.get("verified_date"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", proof["verified_date"]):
            raise ValueError
        datetime.strptime(proof["verified_date"], "%Y-%m-%d")
        for key in ("source_sha256", "readback_sha256"):
            if not isinstance(proof.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", proof[key]):
                raise ValueError
    except (TypeError, ValueError, KeyError):
        raise SafeError("对账证据缺少绑定执行的完整源表、导入回执和零缺口读回记录。") from None
    return {key: proof[key] for key in ("version", "run_key", "verified_date", "source_sha256", "readback_sha256", *numbers)}


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
            raise SafeError(f"GitHub API HTTP {exc.code}") from None

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
            raise SafeError("发现多个同名报告 Issue，请先合并，避免分裂同步状态。")
        if matches:
            return matches[0]
        return self.request("POST", "issues", {"title": TITLE, "body": issue_body({"status": "idle"})})

    def state(self, number):
        issue = self.request("GET", f"issues/{number}")
        body = issue.get("body") or ""
        match = re.search(re.escape(STATE_PREFIX) + r"(.*?)-->", body, re.S)
        if not match:
            raise SafeError("报告 Issue 的同步状态缺失，请先核实历史导入；不会自动重建。")
        state = json.loads(match.group(1))
        if state.get("status") not in STATUSES:
            raise SafeError("报告 Issue 的同步状态无效。")
        if state["status"] != "idle":
            datetime.strptime(state["business_date"], "%Y-%m-%d")
            if not re.fullmatch(r"\d+:\d+", state.get("run_key", "")):
                raise SafeError("报告 Issue 的执行标识无效。")
        if state["status"] == "reconciled":
            validated_reconciliation_record(state.get("reconciliation"), state["run_key"])
            if state.get("review_result") != "reconciled" or not re.fullmatch(
                r"https://github\.com/" + re.escape(self.repository) + rf"/issues/{number}#issuecomment-\d+", state.get("reconciliation_url", "")
            ):
                raise SafeError("报告 Issue 的补齐恢复证据关联无效。")
            datetime.strptime(state["reconciliation_date"], "%Y-%m-%d")
            if not state["business_date"] <= state["reconciliation"]["verified_date"] <= state["reconciliation_date"]:
                raise SafeError("报告 Issue 的补齐恢复日期不一致。")
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

    def read_diagnostic(self, run_key):
        path = self.directory / "import-diagnostic.json"
        try:
            if path.stat().st_size > 65536:
                return None
            return safe_diagnostic(json.loads(path.read_text(encoding="utf-8")), run_key)
        except (OSError, ValueError, TypeError):
            return None

    def collect_diagnostic(self, context):
        diagnostic = self.read_diagnostic(context["run_key"])
        if diagnostic:
            context["diagnostic"] = diagnostic
            counts = diagnostic.get("counts", {})
            labels = {"insertCount": "新增", "updateCount": "更新", "skipCount": "跳过",
                      "successCount": "成功", "failCount": "失败"}
            context["counts"] = [f"- {label}：{counts[key]}" for key, label in labels.items() if key in counts]
            if "warning_details" in diagnostic:
                context["counts"].append(f"- 已返回拆包/规格警告明细：{diagnostic['warning_details']}")
                context["counts"].append(f"- 后台拆包异常总数：{diagnostic.get('warning_total', '未知')}")
        return diagnostic

    def output(self, value):
        path = self.environ.get("GITHUB_OUTPUT")
        if path:
            with open(path, "a", encoding="utf-8") as file:
                file.write(f"should_run={'true' if value else 'false'}\n")

    def manual_reconciled_today(self, state, context):
        key = self.environ.get("MANUAL_RECONCILED_RUN_KEY", "")
        if not key:
            return False
        if (
            self.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or context["mode"] != "run"
            or not re.fullmatch(r"\d+:\d+", key)
            or state["status"] != "reconciled"
            or state.get("run_key") != key
            or state.get("reconciliation_date") != context["business_date"]
        ):
            raise SafeError("补齐当日手动同步必须明确绑定当前已核实的原执行标识；不能重复执行或重试待核实导入。")
        validated_reconciliation_record(state.get("reconciliation"), key)
        return True

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
                raise SafeError("无效的同步模式。")
            if mode != "run" and env.get("MANUAL_RECONCILED_RUN_KEY"):
                raise SafeError("补齐当日手动同步参数只能用于明确授权的 run 模式。")
            if mode == "run" and env.get("MEIYIJIA_CLOUD_SYNC_ENABLED") != "true":
                context.update(status="disabled", detail="云端同步尚未启用。请先接入凭据并停用本机 launchd。")
                self.write(context)
                return 0
            issue = self.api.issue()
            context["issue"] = issue["number"]
            self.write(context)
            state = self.api.state(context["issue"])
            if mode == "run":
                manual = self.manual_reconciled_today(state, context)
                key = env.get("RESOLVE_RUN_KEY", "")
                result = env.get("PREVIOUS_RESULT", "unverified")
                if state["status"] in {"running", "needs_review"}:
                    if key != state["run_key"] or result not in {"not-imported", "completed", "partial", "reconciled"}:
                        raise SafeError(f"上次导入结果待核实（{state['run_key']}），已暂停自动导入。")
                    known = safe_diagnostic(state.get("diagnostic"), key)
                    if result not in {"partial", "reconciled"} and (
                        state.get("review_result") == "partial" or
                        (known and known["outcome"] in {"partial", "rejected"})
                    ):
                        raise SafeError("已记录部分成功或失败行，不能标记为全部完成或未导入；请先核实并补齐。")
                    if result == "not-imported" and known and known.get("counts", {}).get("successCount", 0):
                        raise SafeError("已有成功行回执，不能标记为未导入；请先核实实际结果。")
                    if result == "reconciled":
                        url = env.get("RECONCILIATION_EVIDENCE_URL", "")
                        match = re.fullmatch(r"https://github\.com/" + re.escape(env["GITHUB_REPOSITORY"]) + rf"/issues/{context['issue']}#issuecomment-(\d+)", url)
                        if not match:
                            raise SafeError("补齐后恢复必须提供本仓库报告 Issue 的对账证据评论链接。")
                        proof = validated_reconciliation(self.api.request("GET", f"issues/comments/{match.group(1)}"), env["GITHUB_REPOSITORY"], context["issue"], key)
                        if not state["business_date"] <= proof["verified_date"] <= context["business_date"]:
                            raise SafeError("对账证据日期必须位于原执行日期与本次核实日期之间。")
                        state.update(reconciliation=proof, reconciliation_url=url, reconciliation_date=context["business_date"])
                    state["status"] = {"completed": "succeeded", "not-imported": "idle", "partial": "needs_review", "reconciled": "reconciled"}[result]
                    state["review_result"] = result
                    self.api.save_state(context["issue"], state)
                    labels = {"completed": "确认已完成", "not-imported": "确认未导入", "partial": "存在部分成功或失败行，继续暂停", "reconciled": "当前条码缺口已补齐并读回，原执行终态不标记为全部成功；后续日期允许同步"}
                    self.api.comment(context["issue"], f"已人工核实 {key}：{labels[result]}。本次仅记录核实，不上传商品。")
                    context.update(status="review_recorded", detail=f"{labels[result]}。本次仅记录核实，不上传商品。")
                    self.write(context)
                    return 0
                if key or result != "unverified" or env.get("RECONCILIATION_EVIDENCE_URL"):
                    raise SafeError("当前没有与核实参数匹配的待核实运行；本次不会导入。")
                if completed_today(state, context["business_date"]) and not manual:
                    context.update(status="skipped", detail="当天已成功同步或完成补齐核实，跳过重复执行。")
                    self.write(context)
                    return 0
                if manual:
                    context["manual_reconciled_run_key"] = state["run_key"]
            missing = [name for name in REQUIRED if not env.get(name, "").strip() or env[name].startswith("your-")]
            if missing:
                raise SafeError("缺少凭据：" + "、".join(missing))
            context.update(status="ready", detail="前置检查通过，等待安装和执行。")
            self.write(context)
            self.output(True)
            return 0
        except Exception as exc:
            context.update(status="blocked", detail=str(exc) if isinstance(exc, SafeError) else "协调步骤失败，未执行同步。")
            self.write(context)
            return 1

    def execute(self):
        context = self.read()
        if context["status"] != "ready":
            raise SafeError("前置检查未通过，不能执行同步。")
        real = context["mode"] == "run"
        try:
            if real:
                state = self.api.state(context["issue"])
                manual = self.manual_reconciled_today(state, context)
                if state["status"] in {"running", "needs_review"} or (
                    completed_today(state, context["business_date"]) and not manual
                ):
                    raise SafeError("执行前同步状态已变化，已阻止重复导入。")
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
                    [sys.executable, "scripts/sync_products.py", "--run-key", context["run_key"], f"--{context['mode']}"],
                    cwd=self.root, check=False, stdout=console, stderr=subprocess.STDOUT,
                )
            diagnostic = self.collect_diagnostic(context)
            if result.returncode:
                raise SafeError(f"同步程序退出码 {result.returncode}。请先核实本次安全诊断和后台导入记录。")
            if real:
                marker = self.directory / f"completed-{context['business_date']}.txt"
                summary = self.directory / "last-success-summary.md"
                if not marker.is_file() or not summary.is_file():
                    raise SafeError("程序退出成功，但缺少本次完成标记或成功摘要。")
                if not diagnostic or diagnostic["outcome"] != "succeeded" or diagnostic["stage"] != "completed" or "export_rows" not in diagnostic:
                    raise SafeError("程序退出成功，但缺少绑定本次运行的完整成功回执；结果仍待核实。")
                summary_text = summary.read_text(encoding="utf-8")
                context["warning_review"] = "需要人工复核" in summary_text
                self.api.save_state(context["issue"], {
                    "status": "succeeded", "business_date": context["business_date"],
                    "run_key": context["run_key"], "run_url": context["run_url"],
                    "diagnostic": diagnostic,
                })
            context.update(status="succeeded", detail="商品同步成功。" if real else "检查成功，未上传商品。")
            if context.get("warning_review"):
                context["detail"] = "商品同步成功；拆包／规格警告需要人工复核。"
            self.write(context)
            return 0
        except Exception as exc:
            started = context["status"] == "running"
            if started:
                self.collect_diagnostic(context)
            context.update(
                status="needs_review" if real and started else "failed",
                detail=str(exc) if isinstance(exc, SafeError) else "协调步骤失败，结果需核实。",
            )
            self.write(context)
            if real and started:
                # If this write fails, the durable 'running' state still blocks retries.
                self.api.save_state(context["issue"], {
                    "status": "needs_review", "business_date": context["business_date"],
                    "run_key": context["run_key"], "run_url": context["run_url"],
                    **({"diagnostic": context["diagnostic"]} if "diagnostic" in context else {}),
                })
            return 1

    def report(self):
        context = self.read() if self.context_path.exists() else {"status": "failed", "detail": "工作流未完成环境准备。"}
        labels = {
            "succeeded": "成功", "skipped": "跳过", "disabled": "未启用", "blocked": "被阻止",
            "ready": "环境准备失败，未执行同步", "preparing": "前置检查中断",
            "running": "执行中断，结果待核实", "needs_review": "失败或结果不明，需核实", "failed": "失败",
            "review_recorded": "已记录人工核实，本次未导入",
        }
        lines = [
            f"# 美宜佳商品同步日报 {context.get('business_date', self.now().strftime('%Y-%m-%d'))}", "",
            "- 计划：北京时间／吉隆坡时间每天 12:00",
            f"- 状态：{labels[context['status']]}",
            f"- 模式：{context.get('mode', '未执行')}",
            f"- 说明：{context.get('detail', '')}",
            f"- 执行标识：{context.get('run_key', '未开始')}",
        ]
        if re.fullmatch(r"\d+:\d+", context.get("manual_reconciled_run_key", "")):
            lines.append(f"- 执行授权：补齐核实当天明确要求手动同步一次；关联原执行 {context['manual_reconciled_run_key']}。")
        lines.extend(context.get("counts", []))
        diagnostic = safe_diagnostic(context.get("diagnostic"), context.get("run_key"))
        if diagnostic:
            stages = {
                "starting": "启动", "configuration": "读取配置", "browser": "启动浏览器",
                "pospal_login": "银豹登录", "export": "商品导出与校验", "myj_login": "美宜佳登录",
                "import_ready": "准备导入入口", "import_request": "导入请求", "import_receipt": "收到导入回执",
                "category_preflight": "导入前检查并补齐分类",
                "receipt_saved": "保存回执", "summary_saved": "保存摘要", "closing_browser": "关闭浏览器",
                "completion_marker": "保存完成标记", "completed": "本次完成",
            }
            requests = {"not_started": "尚未开始提交", "started": "已开始提交，未收到完整回执",
                        "response_received": "已收到服务端响应"}
            outcomes = {"not_imported": "脚本未开始提交", "unknown": "结果不明", "partial": "部分成功",
                        "succeeded": "回执计数全部成功", "rejected": "回执未确认成功行"}
            errors = {"none": "无", "stage_failed": "阶段执行失败", "transport_unknown": "请求中断，写入结果不明",
                      "http_error": "服务端 HTTP 错误", "invalid_json": "响应不是有效 JSON",
                      "invalid_receipt": "回执字段或计数不完整", "failed_rows": "回执存在失败行",
                      "backend_conflict_constraint": "后台数据库冲突约束不匹配",
                      "category_preflight_failed": "分类检查或补齐失败，尚未提交商品"}
            lines.extend([
                f"- 最后阶段：{stages[diagnostic['stage']]}",
                f"- 请求边界：{requests[diagnostic['request_state']]}",
                f"- 回执结论：{outcomes[diagnostic['outcome']]}",
                f"- 原因分类：{errors[diagnostic['error']]}",
            ])
            if "export_rows" in diagnostic:
                lines.append(f"- 已校验导出行数：{diagnostic['export_rows']}")
            if "http_status" in diagnostic:
                lines.append(f"- 导入 HTTP 状态码：{diagnostic['http_status']}")
            if "category_checked" in diagnostic:
                lines.append(f"- 导入前已核实分类：{diagnostic['category_checked']}；新增分类：{diagnostic.get('category_created', 0)}")
            if "count_semantics" in diagnostic:
                semantics = {"no_skips": "本次无跳过", "skips_in_success": "跳过计入成功", "skips_separate": "跳过单列"}
                lines.append(f"- 回执计数口径：{semantics[diagnostic['count_semantics']]}")
        elif context["status"] in {"running", "needs_review"}:
            lines.append("- 安全诊断：缺失或无效；不能据此断定未导入。")
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
