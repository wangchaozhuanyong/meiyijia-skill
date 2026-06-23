#!/usr/bin/env python3
"""Export Pospal products and optionally import them into MYJ admin."""

from __future__ import annotations

import argparse
import asyncio
import csv
import contextlib
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_DIR = ROOT / "downloads"
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
STATE_DIR = ROOT / ".schedule-state"
LOCK_PATH = STATE_DIR / "sync.lock"
DEFAULT_LOCK_STALE_SECONDS = 2 * 60 * 60
DEFAULT_EXPORT_MIN_DATA_ROWS = 1000
DEFAULT_IMPORT_MIN_SUCCESS_COUNT = 1000
DEFAULT_IMPORT_WARNING_REVIEW_THRESHOLD = 20
XLSX_NS = {"xlsx": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RUN_ARTIFACTS: set[Path] = set()

EXPORT_REQUIRED_HEADER_GROUPS = (
    ("名称", ("名称", "商品名称")),
    ("分类", ("分类", "商品分类")),
    ("条码", ("条码", "商品条码")),
    ("进货价", ("进货价", "采购价")),
    ("销售价", ("销售价", "零售价")),
    ("商品状态", ("商品状态", "状态")),
)


class BrowserAutomationError(RuntimeError):
    """Raised when browser automation cannot complete safely."""


@dataclass(frozen=True)
class ExportInspection:
    path: Path
    file_type: str
    sheet_name: str
    headers: list[str]
    data_row_count: int


class RunLock:
    def __init__(self, path: Path, stale_seconds: int = DEFAULT_LOCK_STALE_SECONDS) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.fd: int | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            if self._clear_stale_lock():
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise RuntimeError(f"Another sync run is already active: {self.path}") from exc
        os.write(self.fd, f"pid={os.getpid()} started={datetime.now().isoformat()}\n".encode("utf-8"))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.fd is not None:
            os.close(self.fd)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _clear_stale_lock(self) -> bool:
        info = read_lock_info(self.path)
        pid = info.get("pid")
        started = info.get("started")
        pid_dead = pid is not None and not process_exists(pid)
        now = datetime.now(started.tzinfo) if isinstance(started, datetime) and started.tzinfo else datetime.now()
        too_old = started is not None and (now - started).total_seconds() > self.stale_seconds
        unknown_but_old = not pid and not started and lock_mtime_age_seconds(self.path) > self.stale_seconds
        if not (pid_dead or too_old or unknown_but_old):
            return False
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        logging.warning(
            "Cleared stale lock: %s pid=%s started=%s pid_dead=%s too_old=%s",
            self.path,
            pid,
            started.isoformat() if started else None,
            pid_dead,
            too_old,
        )
        return True


@dataclass(frozen=True)
class Config:
    pospal_username: str
    pospal_password: str
    myj_username: str
    myj_password: str
    pospal_url: str
    myj_url: str
    timezone: str


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("your-"):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def bool_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "y", "on"}:
        return True
    if raw_value in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"Environment variable {name} must be true or false.")


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    return Config(
        pospal_username=require_env("POSPAL_USERNAME"),
        pospal_password=require_env("POSPAL_PASSWORD"),
        myj_username=require_env("MYJ_ADMIN_USERNAME"),
        myj_password=require_env("MYJ_ADMIN_PASSWORD"),
        pospal_url=os.environ.get("POSPAL_PRODUCT_URL", "https://beta9.pospal.cn/Product/Manage"),
        myj_url=os.environ.get("MYJ_PRODUCTS_URL", "https://admin.myj.info/products"),
        timezone=os.environ.get("TIMEZONE", "Asia/Shanghai"),
    )


def read_lock_info(path: Path) -> dict[str, int | datetime | None]:
    info: dict[str, int | datetime | None] = {"pid": None, "started": None}
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return info
    for part in content.replace("\n", " ").split():
        if part.startswith("pid="):
            with contextlib.suppress(ValueError):
                info["pid"] = int(part.removeprefix("pid="))
        elif part.startswith("started="):
            with contextlib.suppress(ValueError):
                info["started"] = datetime.fromisoformat(part.removeprefix("started="))
    return info


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_mtime_age_seconds(path: Path) -> float:
    try:
        return datetime.now().timestamp() - path.stat().st_mtime
    except FileNotFoundError:
        return 0


def track_artifact(path: Path) -> Path:
    RUN_ARTIFACTS.add(path.resolve())
    return path


def cleanup_success_artifacts(shutdown_logging: bool = True) -> list[Path]:
    if shutdown_logging:
        logging.shutdown()
    deleted: list[Path] = []
    cleanup_targets = set(RUN_ARTIFACTS)
    cleanup_targets.update({LOG_DIR / "launchd.out.log", LOG_DIR / "launchd.err.log"})
    for path in sorted((item.resolve() for item in cleanup_targets), key=lambda item: str(item)):
        if not is_safe_runtime_artifact(path):
            continue
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted.append(path)
        except OSError:
            # Cleanup is intentionally best-effort after success. A failure here should not
            # make the completed import look failed or trigger a repeat import.
            continue
    RUN_ARTIFACTS.clear()
    return deleted


def is_safe_runtime_artifact(path: Path) -> bool:
    root = ROOT.resolve()
    allowed_dirs = (DOWNLOAD_DIR.resolve(), LOG_DIR.resolve(), SCREENSHOT_DIR.resolve())
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return any(path == directory or directory in path.parents for directory in allowed_dirs)


def setup_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"sync-{now}.log"
    track_artifact(log_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return log_path


def ensure_dirs() -> None:
    for path in (DOWNLOAD_DIR, LOG_DIR, SCREENSHOT_DIR, STATE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def zoned_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def beijing_now(tz_name: str) -> datetime:
    return zoned_now(tz_name)


def should_run_scheduled(tz_name: str, scheduled_hour: int, marker_tz_name: str | None = None) -> tuple[bool, str]:
    now = zoned_now(tz_name)
    marker_now = zoned_now(marker_tz_name or tz_name)
    date_key = marker_now.strftime("%Y-%m-%d")
    marker = STATE_DIR / f"completed-{date_key}.txt"
    if now.hour != scheduled_hour:
        return False, f"Scheduled time in {tz_name} is {now:%Y-%m-%d %H:%M:%S}; outside hour {scheduled_hour:02d}."
    if marker.exists():
        return False, f"Already completed for marker date {date_key} in {marker_tz_name or tz_name}."
    return True, date_key


def mark_completed(tz_name: str, details: str) -> None:
    now = beijing_now(tz_name)
    marker = STATE_DIR / f"completed-{now:%Y-%m-%d}.txt"
    marker.write_text(f"{now.isoformat()}\n{details}\n", encoding="utf-8")


def write_success_summary(
    result: dict,
    export_file: Path,
    import_result_path: Path,
    report_path: Path,
    warnings_json_path: Path | None,
    warnings_csv_path: Path | None,
    tz_name: str = "Asia/Shanghai",
) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = beijing_now(tz_name)
    data = result.get("data") or {}
    warnings = data.get("packWarnings") or []
    threshold = int_env("MYJ_IMPORT_WARNING_REVIEW_THRESHOLD", DEFAULT_IMPORT_WARNING_REVIEW_THRESHOLD)
    review_status = "需要人工复核" if len(warnings) >= threshold else "正常"
    lines = [
        "# 美宜佳商品同步最后成功摘要",
        "",
        f"- 北京时间：{now:%Y-%m-%d %H:%M:%S}",
        f"- 状态：成功（{review_status}）",
        f"- 导出文件：`{export_file.name}`",
        f"- 新增：{safe_int(data.get('insertCount'))}",
        f"- 更新：{safe_int(data.get('updateCount'))}",
        f"- 跳过：{safe_int(data.get('skipCount'))}",
        f"- 成功：{safe_int(data.get('successCount'))}",
        f"- 失败：{safe_int(data.get('failCount'))}",
        f"- 拆包/规格警告：{len(warnings)}",
        f"- 导入结果文件：`{import_result_path.name}`",
        f"- 日报文件：`{report_path.name}`",
    ]
    if warnings_json_path:
        lines.append(f"- 警告 JSON：`{warnings_json_path.name}`")
    if warnings_csv_path:
        lines.append(f"- 警告 CSV：`{warnings_csv_path.name}`")
    lines.extend(
        [
            "",
            "说明：成功运行后的下载、截图、详细日志和日报可能会按清理规则删除；此摘要会保留用于确认当天是否成功。",
        ]
    )
    content = "\n".join(lines) + "\n"
    dated_path = STATE_DIR / f"success-summary-{now:%Y-%m-%d}.md"
    last_path = STATE_DIR / "last-success-summary.md"
    dated_path.write_text(content, encoding="utf-8")
    last_path.write_text(content, encoding="utf-8")
    return last_path


async def screenshot(page: "Page", label: str) -> None:
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    path = SCREENSHOT_DIR / f"{stamp}-{label}.png"
    with contextlib.suppress(Exception):
        await page.screenshot(path=str(path), full_page=True)
        track_artifact(path)
        logging.info("Saved screenshot: %s", path)


async def click_first(page: "Page", names: list[str], timeout: int = 8000) -> None:
    last_error: Exception | None = None
    for name in names:
        locators = [
            page.get_by_role("button", name=name),
            page.get_by_role("link", name=name),
            page.get_by_text(name, exact=True),
            page.get_by_text(name),
        ]
        for locator in locators:
            try:
                await locator.first.click(timeout=timeout)
                logging.info("Clicked: %s", name)
                return
            except Exception as exc:
                last_error = exc
    raise BrowserAutomationError(f"Could not click any of: {names}") from last_error


async def fill_login_form(page: "Page", username: str, password: str) -> None:
    user_selectors = [
        "input[name='username']",
        "input[name='userName']",
        "input[name='account']",
        "input[type='text']",
    ]
    password_selectors = [
        "input[name='password']",
        "input[type='password']",
    ]
    await fill_first_selector(page, user_selectors, username)
    await fill_first_selector(page, password_selectors, password)


async def fill_first_selector(page: "Page", selectors: list[str], value: str) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            await page.locator(selector).first.fill(value, timeout=5000)
            return
        except Exception as exc:
            last_error = exc
    raise BrowserAutomationError(f"Could not fill selectors: {selectors}") from last_error


async def login_pospal(page: "Page", config: Config) -> None:
    logging.info("Opening Pospal product page")
    await page.goto(config.pospal_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)
    await screenshot(page, "pospal-opened")
    if await page.locator("input[type='password']").count():
        logging.info("Logging in to Pospal")
        await fill_login_form(page, config.pospal_username, config.pospal_password)
        await click_login_submit(page)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        await screenshot(page, "pospal-after-login")
        await assert_logged_in(page, "Pospal")


async def export_pospal_products(page: "Page") -> Path:
    logging.info("Navigating to Pospal product records")
    await open_pospal_product_records(page)
    await screenshot(page, "pospal-products")

    logging.info("Starting Pospal export")
    async with page.expect_download(timeout=180000) as download_info:
        export_button = page.locator(".btnExport")
        if await export_button.count():
            await export_button.first.click(timeout=10000)
            logging.info("Clicked Pospal .btnExport")
        else:
            await click_first(page, ["导出", "Export"], timeout=8000)
    download = await download_info.value
    suggested = download.suggested_filename or f"pospal-products-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    target = DOWNLOAD_DIR / suggested
    if target.exists():
        target = DOWNLOAD_DIR / f"{target.stem}-{datetime.now():%H%M%S}{target.suffix}"
    await download.save_as(str(target))
    track_artifact(target)
    logging.info("Downloaded Pospal export: %s (%s bytes)", target, target.stat().st_size)
    validate_export_file(target)
    return target


async def open_pospal_product_records(page: "Page") -> None:
    if await page.locator(".btnExport").count() and await page.get_by_text("商品资料", exact=True).count():
        await page.wait_for_timeout(800)
        return
    await click_first(page, ["商品"], timeout=6000)
    await page.wait_for_timeout(800)
    product_record = page.locator(".yb-tmenu-item .lnk_menu").filter(has_text="商品资料")
    if await product_record.count():
        await product_record.first.click(timeout=8000)
    else:
        await click_first(page, ["商品资料", "商品档案", "Product", "Products"], timeout=6000)
    await page.wait_for_load_state("networkidle", timeout=30000)
    await page.wait_for_timeout(1200)


def validate_export_file(path: Path, min_data_rows: int | None = None) -> None:
    allowed_suffixes = {".xlsx", ".csv"}
    if path.suffix.lower() not in allowed_suffixes:
        raise BrowserAutomationError(f"Unexpected export file type: {path.name}")
    if path.stat().st_size < 1024:
        raise BrowserAutomationError(f"Export file is too small to trust: {path}")
    inspection = inspect_export_file(path)
    min_rows = min_data_rows if min_data_rows is not None else int_env(
        "POSPAL_EXPORT_MIN_ROWS",
        DEFAULT_EXPORT_MIN_DATA_ROWS,
    )
    if inspection.data_row_count < min_rows:
        raise BrowserAutomationError(
            f"Export file has too few product rows: {inspection.data_row_count}; expected at least {min_rows}."
        )
    missing_headers = missing_required_headers(inspection.headers)
    if missing_headers:
        raise BrowserAutomationError(
            "Export file is missing required columns: "
            + ", ".join(missing_headers)
            + f"; detected headers: {', '.join(inspection.headers[:20])}"
        )
    logging.info(
        "Validated export file: type=%s sheet=%s rows=%s headers=%s",
        inspection.file_type,
        inspection.sheet_name,
        inspection.data_row_count,
        ", ".join(inspection.headers[:10]),
    )


def inspect_export_file(path: Path) -> ExportInspection:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return inspect_xlsx_export(path)
    if suffix == ".csv":
        return inspect_csv_export(path)
    raise BrowserAutomationError(f"Unsupported export file type for validation: {path.name}")


def inspect_xlsx_export(path: Path) -> ExportInspection:
    try:
        with ZipFile(path) as workbook:
            sheet_paths = sorted(
                name for name in workbook.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )
            if not sheet_paths:
                raise BrowserAutomationError(f"XLSX file has no worksheets: {path}")
            shared_strings = read_xlsx_shared_strings(workbook)
            sheet_path = sheet_paths[0]
            sheet_root = ElementTree.fromstring(workbook.read(sheet_path))
    except BadZipFile as exc:
        raise BrowserAutomationError(f"Export file is not a valid XLSX archive: {path}") from exc
    except ElementTree.ParseError as exc:
        raise BrowserAutomationError(f"Export file contains invalid XLSX XML: {path}") from exc

    rows = read_xlsx_rows(sheet_root, shared_strings)
    if not rows:
        raise BrowserAutomationError(f"XLSX file has no non-empty rows: {path}")
    return ExportInspection(
        path=path,
        file_type="xlsx",
        sheet_name=Path(sheet_path).stem,
        headers=[value.strip() for value in rows[0]],
        data_row_count=max(len(rows) - 1, 0),
    )


def read_xlsx_shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("xlsx:si", XLSX_NS):
        strings.append("".join(text.text or "" for text in item.findall(".//xlsx:t", XLSX_NS)))
    return strings


def read_xlsx_rows(sheet_root: ElementTree.Element, shared_strings: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in sheet_root.findall(".//xlsx:sheetData/xlsx:row", XLSX_NS):
        values: list[str] = []
        for cell in row.findall("xlsx:c", XLSX_NS):
            index = xlsx_cell_column_index(cell.attrib.get("r", ""))
            while len(values) <= index:
                values.append("")
            values[index] = xlsx_cell_text(cell, shared_strings)
        trimmed = [value.strip() for value in values]
        if any(trimmed):
            rows.append(trimmed)
    return rows


def xlsx_cell_column_index(reference: str) -> int:
    match = re.match(r"([A-Z]+)", reference.upper())
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def xlsx_cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//xlsx:t", XLSX_NS))
    value = cell.find("xlsx:v", XLSX_NS)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        with contextlib.suppress(ValueError, IndexError):
            return shared_strings[int(value.text)]
    return value.text


def inspect_csv_export(path: Path) -> ExportInspection:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file)
            rows = [[value.strip() for value in row] for row in reader if any(value.strip() for value in row)]
    except UnicodeDecodeError as exc:
        raise BrowserAutomationError(f"CSV export must be UTF-8 encoded: {path}") from exc
    if not rows:
        raise BrowserAutomationError(f"CSV file has no non-empty rows: {path}")
    return ExportInspection(
        path=path,
        file_type="csv",
        sheet_name="csv",
        headers=rows[0],
        data_row_count=max(len(rows) - 1, 0),
    )


def missing_required_headers(headers: list[str]) -> list[str]:
    normalized_headers = {normalize_header(header) for header in headers if header.strip()}
    missing = []
    for label, aliases in EXPORT_REQUIRED_HEADER_GROUPS:
        if not any(normalize_header(alias) in normalized_headers for alias in aliases):
            missing.append(label)
    return missing


def normalize_header(value: str) -> str:
    normalized = re.sub(r"[\s\u3000]+", "", value)
    normalized = re.sub(r"[（(]必填[）)]", "", normalized)
    return normalized.lower()


async def login_myj(page: "Page", config: Config) -> None:
    logging.info("Opening MYJ products page")
    await page.goto(config.myj_url, wait_until="domcontentloaded")
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=30000)
    await wait_for_myj_login_or_app(page)
    await screenshot(page, "myj-opened")
    if await page.locator("input[type='password']").count():
        logging.info("Logging in to MYJ admin")
        await fill_login_form(page, config.myj_username, config.myj_password)
        await click_login_submit(page)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=30000)
        try:
            await wait_for_myj_login_or_app(page, allow_login=False, timeout_ms=60000)
        finally:
            await screenshot(page, "myj-after-login")


async def wait_for_myj_login_or_app(page: "Page", allow_login: bool = True, timeout_ms: int = 30000) -> None:
    deadline = datetime.now().timestamp() + timeout_ms / 1000
    while datetime.now().timestamp() < deadline:
        if allow_login and await page.locator("input[type='password']:visible").count():
            return
        if await page.locator(".admin-layout").count():
            return
        if await page.get_by_text("商品管理", exact=True).count():
            return
        await page.wait_for_timeout(500)
    raise BrowserAutomationError("MYJ page did not load login form or admin app in time.")


async def click_login_submit(page: "Page") -> None:
    login_text = re.compile(r"登\s*录|登录|log\s*in|sign\s*in", re.I)
    locators = [
        page.locator("#submitLoginBtn"),
        page.locator(".submitLoginBtn"),
        page.locator("button[type='submit']"),
        page.locator("input[type='submit']"),
        page.locator("button").filter(has_text=login_text),
        page.get_by_role("button", name=login_text),
        page.locator(".loginBtn, .login-btn, .btn-login, .ant-btn-primary, .el-button--primary").filter(
            has_text=login_text
        ),
    ]
    last_error: Exception | None = None
    for locator in locators:
        try:
            count = await locator.count()
            if count == 0:
                continue
            await locator.last.click(timeout=8000)
            logging.info("Clicked login submit button")
            return
        except Exception as exc:
            last_error = exc
    raise BrowserAutomationError("Could not click login submit button.") from last_error


async def assert_logged_in(page: "Page", system_name: str) -> None:
    password_input = page.locator("input[type='password']:visible")
    for _ in range(15):
        if await password_input.count() == 0:
            return
        await page.wait_for_timeout(1000)
    raise BrowserAutomationError(f"{system_name} login did not complete; password field is still visible.")


async def import_to_myj(page: "Page", export_file: Path) -> None:
    await open_myj_products_page(page)
    await click_first(page, ["批量导入", "批量上传", "Import"], timeout=8000)
    await page.locator("input[type='file']").first.wait_for(state="attached", timeout=15000)
    await screenshot(page, "myj-import-ready")

    token = await page.evaluate("() => localStorage.getItem('admin_token')")
    if not token:
        raise BrowserAutomationError("MYJ admin token was not found after login.")
    result = await asyncio.to_thread(import_to_myj_api, page.url, token, export_file)
    import_result_path = write_import_result(result)
    warnings_json_path, warnings_csv_path = write_import_warnings(result)
    data = assess_import_result(result)
    report_path = write_import_report(
        result=result,
        export_file=export_file,
        import_result_path=import_result_path,
        warnings_json_path=warnings_json_path,
        warnings_csv_path=warnings_csv_path,
    )
    summary_path = write_success_summary(
        result=result,
        export_file=export_file,
        import_result_path=import_result_path,
        report_path=report_path,
        warnings_json_path=warnings_json_path,
        warnings_csv_path=warnings_csv_path,
    )
    logging.info(
        "MYJ API import complete: %s; insert=%s update=%s skip=%s warnings=%s",
        result.get("message"),
        data.get("insertCount"),
        data.get("updateCount"),
        data.get("skipCount"),
        len(data.get("packWarnings") or []),
    )
    logging.info("Saved daily import report: %s", report_path)
    logging.info("Saved success summary: %s", summary_path)


async def open_myj_products_page(page: "Page") -> None:
    logging.info("Opening MYJ products management")
    await wait_for_myj_login_or_app(page, allow_login=False)
    if await page.get_by_text("批量导入", exact=True).count() == 0:
        sidebar_product = page.locator(".admin-sidebar").get_by_text("商品管理", exact=True)
        if await sidebar_product.count():
            await sidebar_product.first.click(timeout=10000)
            logging.info("Clicked MYJ sidebar product management")
        else:
            await click_first(page, ["商品管理", "Products", "商品"], timeout=10000)
    await page.get_by_text("批量导入", exact=True).wait_for(state="visible", timeout=15000)
    await page.wait_for_timeout(800)
    await screenshot(page, "myj-products")


async def check_myj_batch_import(page: "Page") -> None:
    await open_myj_products_page(page)
    await click_first(page, ["批量导入", "批量上传", "Import"], timeout=8000)
    await page.locator("input[type='file']").first.wait_for(state="attached", timeout=15000)
    logging.info("MYJ batch import page is ready. No file was uploaded.")
    await screenshot(page, "myj-import-ready")


async def assert_no_visible_import_failure(page: "Page") -> None:
    failure_text = page.get_by_text("失败").or_(page.get_by_text("错误")).or_(page.get_by_text("异常"))
    if await failure_text.count():
        visible = await failure_text.first.is_visible()
        if visible:
            raise BrowserAutomationError("MYJ import page shows failure/error text after submission.")


async def wait_for_myj_import_result(page: "Page", timeout_ms: int = 180000) -> None:
    deadline = datetime.now().timestamp() + timeout_ms / 1000
    while datetime.now().timestamp() < deadline:
        body_text = ""
        with contextlib.suppress(Exception):
            body_text = await page.locator("body").inner_text(timeout=1000)
        if any(term in body_text for term in ("导入失败", "失败", "错误", "异常")):
            await screenshot(page, "myj-import-failed")
            raise BrowserAutomationError("MYJ import page shows failure/error text after submission.")
        if any(term in body_text for term in ("导入成功", "成功导入", "导入完成", "处理完成")):
            await screenshot(page, "myj-import-success")
            logging.info("MYJ import success text detected.")
            return
        loading_count = await page.locator(".van-button--loading, .van-loading, .van-toast--loading").count()
        if loading_count == 0 and await page.get_by_text("开始导入", exact=False).count():
            await page.wait_for_timeout(3000)
            await assert_no_visible_import_failure(page)
            await screenshot(page, "myj-import-submitted")
            logging.info("MYJ import loading finished without visible failure text.")
            return
        await page.wait_for_timeout(1000)
    await screenshot(page, "myj-import-timeout")
    raise BrowserAutomationError("Timed out waiting for MYJ import result.")


def import_to_myj_api(current_url: str, token: str, export_file: Path) -> dict:
    parsed = urlparse(current_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = f"{origin}/api/admin/products/import"
    boundary = "----codex" + uuid.uuid4().hex
    payload = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{export_file.name}"\r\n'.encode("utf-8"),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            export_file.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise BrowserAutomationError(f"MYJ API import HTTP {exc.code}: {body[:500]}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise BrowserAutomationError(f"MYJ API import returned non-JSON response: {body[:500]}") from exc


def write_import_result(result: dict) -> Path:
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"import-result-{now}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    track_artifact(path)
    logging.info("Saved MYJ import result: %s", path)
    return path


def assess_import_result(
    result: dict,
    min_success_count: int | None = None,
    warning_review_threshold: int | None = None,
) -> dict:
    data = result.get("data") or {}
    fail_count = safe_int(data.get("failCount"))
    success_count = safe_int(data.get("successCount"))
    warnings = data.get("packWarnings") or []
    if not result.get("success"):
        raise BrowserAutomationError(f"MYJ API import failed: {result.get('message')}")
    if fail_count:
        raise BrowserAutomationError(f"MYJ API import has failed rows: {fail_count}; {result.get('message')}")
    min_count = min_success_count if min_success_count is not None else int_env(
        "MYJ_IMPORT_MIN_SUCCESS_COUNT",
        DEFAULT_IMPORT_MIN_SUCCESS_COUNT,
    )
    if success_count < min_count:
        raise BrowserAutomationError(
            f"MYJ API import success count is unexpectedly low: {success_count}; expected at least {min_count}."
        )
    threshold = warning_review_threshold if warning_review_threshold is not None else int_env(
        "MYJ_IMPORT_WARNING_REVIEW_THRESHOLD",
        DEFAULT_IMPORT_WARNING_REVIEW_THRESHOLD,
    )
    if len(warnings) >= threshold:
        logging.warning(
            "MYJ import completed but needs review: %s pack/spec warnings reached threshold %s.",
            len(warnings),
            threshold,
        )
    return data


def safe_int(value: object) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(value)  # type: ignore[arg-type]
    return 0


def write_import_warnings(result: dict) -> tuple[Path | None, Path | None]:
    warnings = ((result.get("data") or {}).get("packWarnings") or [])
    if not warnings:
        return None, None
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    json_path = LOG_DIR / f"warnings-{now}.json"
    csv_path = LOG_DIR / f"warnings-{now}.csv"
    json_path.write_text(json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")
    track_artifact(json_path)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["master_code", "reason", "sample"])
        writer.writeheader()
        for item in warnings:
            writer.writerow(
                {
                    "master_code": item.get("master_code", ""),
                    "reason": item.get("reason", ""),
                    "sample": item.get("sample", ""),
                }
            )
    track_artifact(csv_path)
    logging.info("Saved MYJ warning files: %s, %s", json_path, csv_path)
    return json_path, csv_path


def write_import_report(
    result: dict,
    export_file: Path,
    import_result_path: Path,
    warnings_json_path: Path | None,
    warnings_csv_path: Path | None,
) -> Path:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    stamp = now.strftime("%Y%m%d-%H%M%S")
    data = result.get("data") or {}
    warnings = data.get("packWarnings") or []
    threshold = int_env("MYJ_IMPORT_WARNING_REVIEW_THRESHOLD", DEFAULT_IMPORT_WARNING_REVIEW_THRESHOLD)
    review_status = "需要人工复核" if len(warnings) >= threshold else "正常"
    lines = [
        f"# 美宜佳商品同步日报 {now:%Y-%m-%d %H:%M:%S}",
        "",
        f"- 状态：成功（{review_status}）",
        f"- 导出文件：`{export_file}`",
        f"- 导出文件大小：{export_file.stat().st_size} bytes",
        f"- 导入结果：`{import_result_path}`",
        f"- 新增：{safe_int(data.get('insertCount'))}",
        f"- 更新：{safe_int(data.get('updateCount'))}",
        f"- 跳过：{safe_int(data.get('skipCount'))}",
        f"- 成功：{safe_int(data.get('successCount'))}",
        f"- 失败：{safe_int(data.get('failCount'))}",
        f"- 拆包/规格警告：{len(warnings)}",
    ]
    if warnings_json_path:
        lines.append(f"- 警告 JSON：`{warnings_json_path}`")
    if warnings_csv_path:
        lines.append(f"- 警告 CSV：`{warnings_csv_path}`")
    if warnings:
        lines.extend(["", "## 需要人工复核的前 20 条", ""])
        for index, item in enumerate(warnings[:20], start=1):
            reason = item.get("reason", "")
            master_code = item.get("master_code", "")
            sample = item.get("sample", "")
            lines.append(f"{index}. `{master_code}`：{reason}；样例：{sample}")
    path = LOG_DIR / f"daily-report-{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    track_artifact(path)
    return path


def write_failure_report(exc: Exception, log_path: Path) -> Path:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    stamp = now.strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"daily-report-{stamp}-failed.md"
    lines = [
        f"# 美宜佳商品同步失败报告 {now:%Y-%m-%d %H:%M:%S}",
        "",
        "- 状态：失败",
        f"- 错误类型：`{type(exc).__name__}`",
        f"- 错误信息：{exc}",
        f"- 日志文件：`{log_path}`",
        "",
        "## 建议处理",
        "",
        "1. 先查看上面的日志文件。",
        "2. 再查看同一时间附近的 `screenshots/` 截图。",
        "3. 如果真实导入状态不明确，先在 MYJ 后台确认导入记录，不要重复执行真实导入。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    track_artifact(path)
    logging.info("Saved failure report: %s", path)
    return path


async def click_optional(page: "Page", names: list[str], timeout: int = 3000) -> bool:
    try:
        await click_first(page, names, timeout=timeout)
        return True
    except BrowserAutomationError:
        return False


async def run_browser(args: argparse.Namespace, config: Config) -> Path:
    ensure_dirs()
    try:
        from playwright.async_api import Browser, BrowserContext, async_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: run `pip install -r requirements.txt` first.") from exc

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
        context: BrowserContext = await browser.new_context(accept_downloads=True, locale="zh-CN")
        page = await context.new_page()
        try:
            if args.check_myj:
                await login_myj(page, config)
                await check_myj_batch_import(page)
                logging.info("MYJ check complete. No file was uploaded.")
                return Path()
            await login_pospal(page, config)
            export_file = await export_pospal_products(page)
            if args.dry_run:
                logging.info("Dry-run complete. MYJ import was not executed.")
                return export_file
            await login_myj(page, config)
            await import_to_myj(page, export_file)
            logging.info("Real import completed.")
            return export_file
        finally:
            await context.close()
            await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Pospal products into MYJ admin.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Export only; do not upload to MYJ.")
    mode.add_argument("--run", action="store_true", help="Export and import into MYJ.")
    mode.add_argument("--check-myj", action="store_true", help="Login to MYJ and open the batch import UI without uploading.")
    parser.add_argument("--scheduled", action="store_true", help="Run only during the configured scheduled hour and once per scheduled date.")
    parser.add_argument("--schedule-timezone", help="IANA timezone for the scheduled gate, for example America/Los_Angeles.")
    parser.add_argument("--schedule-hour", type=int, help="Scheduled hour in --schedule-timezone. Use 12 for noon.")
    parser.add_argument("--beijing-hour", type=int, default=3, help="Backward-compatible scheduled Beijing hour, default 3.")
    parser.add_argument("--headed", action="store_true", help="Show browser window.")
    parser.add_argument("--slow-mo", type=int, default=0, help="Playwright slow motion in milliseconds.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    RUN_ARTIFACTS.clear()
    ensure_dirs()
    log_path = setup_logging()
    logging.info("Log file: %s", log_path)
    try:
        config = load_config()
        if args.scheduled:
            schedule_timezone = args.schedule_timezone or config.timezone
            schedule_hour = args.schedule_hour if args.schedule_hour is not None else args.beijing_hour
            allowed, reason = should_run_scheduled(schedule_timezone, schedule_hour, config.timezone)
            if not allowed:
                logging.info("Scheduled run skipped: %s", reason)
                if bool_env("CLEANUP_SUCCESS_ARTIFACTS", True):
                    cleanup_success_artifacts()
                return 0
        with RunLock(LOCK_PATH):
            export_file = await run_browser(args, config)
            if args.run:
                mark_completed(config.timezone, f"imported={export_file}")
                if bool_env("CLEANUP_SUCCESS_ARTIFACTS", True):
                    cleanup_success_artifacts()
        return 0
    except Exception as exc:
        logging.exception("Sync failed: %s", exc)
        write_failure_report(exc, log_path)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
