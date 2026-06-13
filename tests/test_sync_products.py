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


class RunLockTests(unittest.TestCase):
    def test_run_lock_clears_stale_dead_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "sync.lock"
            lock_path.write_text("pid=99999999 started=2000-01-01T00:00:00\n", encoding="utf-8")

            with sync_products.RunLock(lock_path, stale_seconds=1):
                self.assertTrue(lock_path.exists())
                self.assertIn("pid=", lock_path.read_text(encoding="utf-8"))

            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
