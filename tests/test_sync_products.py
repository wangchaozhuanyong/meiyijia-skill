from __future__ import annotations

import html
import asyncio
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo


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
    def test_both_closed_skip_count_conventions_account_for_source_rows(self) -> None:
        for success_count in (12, 15):
            result = {"success": True, "data": {
                "insertCount": 2, "updateCount": 10, "skipCount": 3, "successCount": success_count, "failCount": 1,
            }}
            with self.subTest(success_count=success_count):
                counts = sync_products.strict_import_counts(result, expected_rows=16)
                self.assertEqual(counts["successCount"], success_count)
                with self.assertRaises(sync_products.BrowserAutomationError):
                    sync_products.strict_import_counts(result, expected_rows=15)

    def test_assess_import_result_accepts_success_with_review_warnings(self) -> None:
        result = {
            "success": True,
            "message": "ok",
            "data": {
                "insertCount": 0, "updateCount": 12, "skipCount": 0,
                "successCount": 12,
                "failCount": 0,
                "packWarnings": [{"master_code": "1", "reason": "needs review", "sample": "sample"}],
            },
        }

        data = sync_products.assess_import_result(result, min_success_count=10, warning_review_threshold=1)

        self.assertEqual(data["successCount"], 12)

    def test_assess_import_result_rejects_failed_rows(self) -> None:
        result = {"success": True, "message": "failed rows", "data": {
            "insertCount": 0, "updateCount": 12, "skipCount": 0, "successCount": 12, "failCount": 1,
        }}

        with self.assertRaises(sync_products.BrowserAutomationError):
            sync_products.assess_import_result(result, min_success_count=10)

    def test_assess_import_result_rejects_low_success_count(self) -> None:
        result = {"success": True, "message": "too low", "data": {
            "insertCount": 0, "updateCount": 9, "skipCount": 0, "successCount": 9, "failCount": 0,
        }}

        with self.assertRaises(sync_products.BrowserAutomationError):
            sync_products.assess_import_result(result, min_success_count=10)

    def test_missing_malformed_and_inconsistent_counts_are_never_assumed_zero(self) -> None:
        valid = {"insertCount": 2, "updateCount": 10, "skipCount": 0, "successCount": 12, "failCount": 0}
        variants = [
            {key: value for key, value in valid.items() if key != "failCount"},
            {**valid, "failCount": "0"}, {**valid, "failCount": False},
            {**valid, "failCount": -1}, {**valid, "insertCount": 3},
        ]
        for data in variants:
            with self.subTest(data=data), self.assertRaises(sync_products.BrowserAutomationError):
                sync_products.assess_import_result({"success": True, "data": data}, min_success_count=10)
        with self.assertRaises(sync_products.BrowserAutomationError):
            sync_products.assess_import_result({"success": True, "data": valid}, min_success_count=10, expected_rows=13)


class SafeDiagnosticTests(unittest.TestCase):
    def test_receipt_is_preserved_before_local_raw_result_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            diagnostic = sync_products.ImportDiagnostic("123:1", root)
            diagnostic.update(export_rows=1200)
            result = {"success": True, "data": {
                "insertCount": 10, "updateCount": 1190, "skipCount": 0, "successCount": 1200, "failCount": 0,
            }}
            class Locator:
                first = None
                async def wait_for(self, **kwargs):
                    pass
            locator = Locator()
            locator.first = locator
            class Page:
                url = "https://example.invalid/products"
                evaluate = AsyncMock(return_value="fixture")
                def locator(self, selector):
                    return locator
            diagnostic.update(request_state="response_received")
            with patch.object(sync_products, "open_myj_products_page", AsyncMock()), \
                    patch.object(sync_products, "click_first", AsyncMock()), \
                    patch.object(sync_products, "screenshot", AsyncMock()), \
                    patch.object(sync_products, "ensure_myj_categories", return_value={}), \
                    patch.object(sync_products, "import_to_myj_api", return_value=result) as send, \
                    patch.object(sync_products, "write_import_result", side_effect=OSError("private fixture")):
                with self.assertRaises(OSError):
                    asyncio.run(sync_products.import_to_myj(Page(), root / "products.xlsx", diagnostic))
                self.assertEqual(send.call_count, 1)
            self.assertEqual(diagnostic.value["stage"], "import_receipt")
            self.assertEqual(diagnostic.value["outcome"], "succeeded")
            self.assertEqual(diagnostic.value["counts"]["successCount"], 1200)
            self.assertNotIn("private fixture", diagnostic.path.read_text())

    def test_warning_total_is_distinct_from_truncated_details_and_raw_text(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostic = sync_products.ImportDiagnostic("123:1", Path(temp))
            diagnostic.update(export_rows=1)
            result = {"success":True,"message":"导入完成，拆包关系 0 组（0 个子品），异常 174 组", "data":{
                "insertCount":1,"updateCount":0,"skipCount":0,"successCount":1,"failCount":0,"packWarnings":[{}]*50}}
            sync_products.diagnose_import_receipt(result, diagnostic)
            self.assertEqual(diagnostic.value['warning_total'],174)
            self.assertEqual(diagnostic.value['warning_details'],50)
            self.assertNotIn('导入完成',diagnostic.path.read_text())
            result['message']='unrecognized private fixture'
            self.assertIsNone(sync_products.pack_warning_total(result))
            result['message']='拆包关系 0 组（0 个子品），异常 2 组'
            self.assertIsNone(sync_products.pack_warning_total(result))

    def test_partial_receipt_preserves_counts_and_only_fixed_error_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            diagnostic = sync_products.ImportDiagnostic("123:1", Path(temp))
            diagnostic.update(stage="import_receipt", request_state="response_received", export_rows=13)
            result = {"success": True, "message": "private fixture", "data": {
                "insertCount": 2, "updateCount": 10, "skipCount": 0, "successCount": 12, "failCount": 1,
                "errors": ["private product: no unique or exclusion constraint matching the ON CONFLICT specification"],
                "packWarnings": [{"sample": "private barcode"}],
            }}
            sync_products.diagnose_import_receipt(result, diagnostic)
            content = diagnostic.path.read_text()
            self.assertEqual(diagnostic.value["outcome"], "partial")
            self.assertEqual(diagnostic.value["counts"]["failCount"], 1)
            self.assertIn("backend_conflict_constraint", content)
            self.assertNotIn("private", content)
            self.assertNotIn("barcode", content)

    def test_request_failure_marks_unknown_and_does_not_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = root / "products.xlsx"
            export.write_bytes(b"fixture")
            diagnostic = sync_products.ImportDiagnostic("123:1", root)
            def interrupted(*args, **kwargs):
                self.assertEqual(diagnostic.value["request_state"], "started")
                raise TimeoutError("private fixture")
            with patch.object(sync_products.request, "urlopen", side_effect=interrupted) as send:
                with self.assertRaises(TimeoutError):
                    sync_products.import_to_myj_api("https://example.invalid/products", "fixture", export, diagnostic)
                self.assertEqual(send.call_count, 1)
            self.assertEqual(diagnostic.value["outcome"], "unknown")
            self.assertEqual(diagnostic.value["error"], "transport_unknown")
            self.assertNotIn("private", diagnostic.path.read_text())

    def test_invalid_receipt_never_creates_success_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            diagnostic = sync_products.ImportDiagnostic("123:1", Path(temp))
            diagnostic.update(request_state="response_received")
            sync_products.diagnose_import_receipt({"success": True, "data": {"successCount": 1200}}, diagnostic)
            self.assertEqual(diagnostic.value["outcome"], "unknown")
            self.assertEqual(diagnostic.value["error"], "invalid_receipt")
            self.assertNotIn("counts", diagnostic.value)


class CategoryPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.xlsx'
        write_xlsx(self.source, REQUIRED_HEADERS, [
            ['商品','水果','6900','1','2','启用'],['停用商品','不创建','6901','1','2','停用'],
            ['商品2','水果','6902','1','2','启用']])
        self.diagnostic = sync_products.ImportDiagnostic('123:1',self.root)

    def response(self,data,success=True):
        return io.BytesIO(json.dumps({'success':success,'data':data}).encode())

    def run_preflight(self):
        return sync_products.ensure_myj_categories('https://example.invalid/products/import','fixture',self.source,self.diagnostic)

    def test_existing_categories_use_only_read_and_preserve_original_file(self):
        original = self.source.read_bytes()
        with patch.object(sync_products.request,'urlopen',return_value=self.response([{'name':'水果','sort_order':130}])) as send:
            self.assertEqual(self.run_preflight(),{'category_checked':1,'category_created':0})
        self.assertEqual(send.call_count,1)
        self.assertEqual(send.call_args.args[0].method,'GET')
        self.assertEqual(original,self.source.read_bytes())

    def test_missing_category_uses_observed_form_fields_and_requires_readback(self):
        with patch.object(sync_products.request,'urlopen',side_effect=[
            self.response([{'name':'饮料','sort_order':120}]),self.response({'id':13}),
            self.response([{'name':'饮料','sort_order':120},{'name':'水果','sort_order':130}])]) as send:
            self.assertEqual(self.run_preflight()['category_created'],1)
        requests=[call.args[0] for call in send.call_args_list]
        self.assertEqual([req.method for req in requests],['GET','POST','GET'])
        self.assertTrue(all(req.full_url.endswith('/api/admin/categories') for req in requests))
        self.assertEqual(json.loads(requests[1].data),{'name':'水果','name_en':'','parent_id':0,'sort_order':'130'})
        self.assertEqual(self.diagnostic.value['request_state'],'not_started')

    def test_ambiguous_category_write_does_not_retry_or_submit_products(self):
        with patch.object(sync_products.request,'urlopen',side_effect=[self.response([]),TimeoutError('private fixture')]) as send:
            with self.assertRaisesRegex(sync_products.BrowserAutomationError,'before product import'):
                self.run_preflight()
        self.assertEqual(send.call_count,2)
        self.assertEqual(self.diagnostic.value['error'],'category_preflight_failed')
        self.assertEqual(self.diagnostic.value['outcome'],'not_imported')
        self.assertNotIn('private fixture',self.diagnostic.path.read_text())

    def test_unverified_creation_is_not_accepted(self):
        with patch.object(sync_products.request,'urlopen',side_effect=[self.response([]),self.response({'id':13}),self.response([])]):
            with self.assertRaises(sync_products.BrowserAutomationError):
                self.run_preflight()

    def test_invalid_category_list_stops_before_any_write(self):
        for value in [None,{},[{'name':False}]]:
            with self.subTest(value=value),patch.object(sync_products.request,'urlopen',return_value=self.response(value)) as send:
                with self.assertRaises(sync_products.BrowserAutomationError):
                    self.run_preflight()
                self.assertEqual(send.call_count,1)

    def test_csv_uses_header_aliases_and_disabled_category_is_ignored(self):
        source=self.root/'source.csv'
        source.write_text('商品名称,商品分类,状态\n商品,水果,启用\n停用商品,不创建,停用\n',encoding='utf-8-sig')
        self.assertEqual(sync_products.export_category_names(source),{'水果'})

    def test_missing_enabled_category_fails_before_request(self):
        write_xlsx(self.source,REQUIRED_HEADERS,[['商品','','6900','1','2','启用']])
        with patch.object(sync_products.request,'urlopen') as send:
            with self.assertRaises(sync_products.BrowserAutomationError):
                self.run_preflight()
        send.assert_not_called()


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


class ScheduleGateTests(unittest.TestCase):
    def test_should_run_scheduled_uses_configured_timezone_and_hour(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_state_dir = sync_products.STATE_DIR
            original_zoned_now = sync_products.zoned_now
            try:
                sync_products.STATE_DIR = Path(temp) / ".schedule-state"
                def fake_zoned_now(tz_name: str) -> datetime:
                    if tz_name == "America/Los_Angeles":
                        return datetime(2026, 6, 23, 12, 5, tzinfo=ZoneInfo(tz_name))
                    if tz_name == "Asia/Shanghai":
                        return datetime(2026, 6, 24, 3, 5, tzinfo=ZoneInfo(tz_name))
                    raise AssertionError(tz_name)

                sync_products.zoned_now = fake_zoned_now

                allowed, reason = sync_products.should_run_scheduled("America/Los_Angeles", 12, "Asia/Shanghai")
                self.assertTrue(allowed)
                self.assertEqual(reason, "2026-06-24")

                marker = sync_products.STATE_DIR / "completed-2026-06-24.txt"
                marker.parent.mkdir(parents=True)
                marker.write_text("done", encoding="utf-8")

                allowed, reason = sync_products.should_run_scheduled("America/Los_Angeles", 12, "Asia/Shanghai")
                self.assertFalse(allowed)
                self.assertIn("Already completed", reason)
            finally:
                sync_products.STATE_DIR = original_state_dir
                sync_products.zoned_now = original_zoned_now


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
