from __future__ import annotations

import html
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import sync_products  # noqa: E402


REQUIRED_HEADERS = ["名称（必填）", "分类（必填）", "条码", "进货价（必填）", "销售价（必填）", "商品状态"]


def write_xlsx(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    all_rows = [headers, *rows]
    row_xml = []
    for row_index, row in enumerate(all_rows, start=1):
        cells = []
        for column_index, value in enumerate(row):
            column = column_name(column_index)
            escaped = html.escape(value)
            cells.append(
                f'<c r="{column}{row_index}" t="inlineStr"><is><t>{escaped}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )
    with ZipFile(path, "w") as workbook:
        workbook.writestr("xl/worksheets/sheet1.xml", sheet)
        workbook.writestr("xl/filler.txt", "x" * 2048)


def column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


class ExportValidationTests(unittest.TestCase):
    def test_validate_export_file_accepts_expected_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "products.xlsx"
            rows = [[f"商品{i}", "饮料", f"69000000000{i}", "1.00", "2.00", "启用"] for i in range(3)]
            write_xlsx(path, REQUIRED_HEADERS, rows)

            sync_products.validate_export_file(path, min_data_rows=3)

    def test_validate_export_file_rejects_missing_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "products.xlsx"
            write_xlsx(path, ["名称（必填）", "分类（必填）"], [["商品", "饮料"] for _ in range(3)])

            with self.assertRaises(sync_products.BrowserAutomationError):
                sync_products.validate_export_file(path, min_data_rows=3)

    def test_validate_export_file_rejects_low_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "products.xlsx"
            write_xlsx(path, REQUIRED_HEADERS, [["商品", "饮料", "6900", "1.00", "2.00", "启用"]])

            with self.assertRaises(sync_products.BrowserAutomationError):
                sync_products.validate_export_file(path, min_data_rows=2)


class ImportAssessmentTests(unittest.TestCase):
    def test_assess_import_result_accepts_success_with_review_warnings(self) -> None:
        result = {
            "success": True,
            "message": "ok",
            "data": {
                "successCount": 12,
                "failCount": 0,
                "packWarnings": [{"master_code": "1", "reason": "needs review", "sample": "sample"}],
            },
        }

        data = sync_products.assess_import_result(result, min_success_count=10, warning_review_threshold=1)

        self.assertEqual(data["successCount"], 12)

    def test_assess_import_result_rejects_failed_rows(self) -> None:
        result = {"success": True, "message": "failed rows", "data": {"successCount": 12, "failCount": 1}}

        with self.assertRaises(sync_products.BrowserAutomationError):
            sync_products.assess_import_result(result, min_success_count=10)

    def test_assess_import_result_rejects_low_success_count(self) -> None:
        result = {"success": True, "message": "too low", "data": {"successCount": 9, "failCount": 0}}

        with self.assertRaises(sync_products.BrowserAutomationError):
            sync_products.assess_import_result(result, min_success_count=10)


class SuccessSummaryTests(unittest.TestCase):
    def test_write_success_summary_keeps_small_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_state_dir = sync_products.STATE_DIR
            root = Path(temp)
            try:
                sync_products.STATE_DIR = root / ".schedule-state"
                export_file = root / "downloads" / "products.xlsx"
                import_result_path = root / "logs" / "import-result.json"
                report_path = root / "logs" / "daily-report.md"
                warnings_csv_path = root / "logs" / "warnings.csv"
                warnings_json_path = root / "logs" / "warnings.json"
                for path in (export_file, import_result_path, report_path, warnings_csv_path, warnings_json_path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("x", encoding="utf-8")
                result = {
                    "success": True,
                    "data": {
                        "insertCount": 1,
                        "updateCount": 2,
                        "skipCount": 3,
                        "successCount": 6,
                        "failCount": 0,
                        "packWarnings": [{"master_code": "1", "reason": "review", "sample": "sample"}],
                    },
                }

                last_path = sync_products.write_success_summary(
                    result=result,
                    export_file=export_file,
                    import_result_path=import_result_path,
                    report_path=report_path,
                    warnings_json_path=warnings_json_path,
                    warnings_csv_path=warnings_csv_path,
                    tz_name="Asia/Shanghai",
                )

                self.assertEqual(last_path, sync_products.STATE_DIR / "last-success-summary.md")
                content = last_path.read_text(encoding="utf-8")
                self.assertIn("状态：成功", content)
                self.assertIn("成功：6", content)
                self.assertIn("warnings.csv", content)
                self.assertEqual(len(list(sync_products.STATE_DIR.glob("success-summary-*.md"))), 1)
            finally:
                sync_products.STATE_DIR = original_state_dir


class RunLockTests(unittest.TestCase):
    def test_run_lock_clears_stale_dead_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "sync.lock"
            lock_path.write_text("pid=99999999 started=2000-01-01T00:00:00\n", encoding="utf-8")

            with sync_products.RunLock(lock_path, stale_seconds=1):
                self.assertTrue(lock_path.exists())
                self.assertIn("pid=", lock_path.read_text(encoding="utf-8"))

            self.assertFalse(lock_path.exists())


class SuccessCleanupTests(unittest.TestCase):
    def test_cleanup_success_artifacts_deletes_only_tracked_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_root = sync_products.ROOT
            original_download_dir = sync_products.DOWNLOAD_DIR
            original_log_dir = sync_products.LOG_DIR
            original_screenshot_dir = sync_products.SCREENSHOT_DIR
            original_artifacts = set(sync_products.RUN_ARTIFACTS)
            root = Path(temp)
            try:
                sync_products.ROOT = root
                sync_products.DOWNLOAD_DIR = root / "downloads"
                sync_products.LOG_DIR = root / "logs"
                sync_products.SCREENSHOT_DIR = root / "screenshots"
                for directory in (sync_products.DOWNLOAD_DIR, sync_products.LOG_DIR, sync_products.SCREENSHOT_DIR):
                    directory.mkdir()

                download = sync_products.DOWNLOAD_DIR / "products.xlsx"
                log = sync_products.LOG_DIR / "sync.log"
                screenshot = sync_products.SCREENSHOT_DIR / "screen.png"
                state_marker = root / ".schedule-state" / "completed-2026-06-13.txt"
                state_marker.parent.mkdir()
                source_file = root / "scripts" / "sync_products.py"
                source_file.parent.mkdir()
                for path in (download, log, screenshot, state_marker, source_file):
                    path.write_text("x", encoding="utf-8")

                sync_products.RUN_ARTIFACTS.clear()
                for path in (download, log, screenshot, state_marker, source_file):
                    sync_products.track_artifact(path)

                deleted = sync_products.cleanup_success_artifacts(shutdown_logging=False)

                self.assertEqual(set(deleted), {download.resolve(), log.resolve(), screenshot.resolve()})
                self.assertFalse(download.exists())
                self.assertFalse(log.exists())
                self.assertFalse(screenshot.exists())
                self.assertTrue(state_marker.exists())
                self.assertTrue(source_file.exists())
            finally:
                sync_products.ROOT = original_root
                sync_products.DOWNLOAD_DIR = original_download_dir
                sync_products.LOG_DIR = original_log_dir
                sync_products.SCREENSHOT_DIR = original_screenshot_dir
                sync_products.RUN_ARTIFACTS.clear()
                sync_products.RUN_ARTIFACTS.update(original_artifacts)


if __name__ == "__main__":
    unittest.main()
