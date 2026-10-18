import contextlib
import copy
import importlib.util
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


MODULE = Path(__file__).with_name('daily_sync.py')
spec = importlib.util.spec_from_file_location('daily_sync', MODULE)
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)


class FakeGitHub:
    def __init__(self, state=None):
        self.saved = state or {'status': 'idle'}
        self.comments = []
        self.fail_write = False
        self.fail_success_write = False

    def issue(self):
        return {'number': 17}

    def state(self, number):
        return copy.deepcopy(self.saved)

    def save_state(self, number, state):
        if self.fail_write or (self.fail_success_write and state['status'] == 'succeeded'):
            raise RuntimeError('GitHub API HTTP 503')
        self.saved = copy.deepcopy(state)

    def comment(self, number, body):
        self.comments.append(body)

    def request(self, method, path, payload):
        if method != 'PATCH':
            raise AssertionError(method)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            'GITHUB_REPOSITORY': 'wangchaozhuanyong/meiyijia-skill',
            'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
            'MEIYIJIA_CLOUD_SYNC_ENABLED': 'true',
            'MEIYIJIA_REPORT_MENTION': 'wangchaozhuanyong',
            'GITHUB_STEP_SUMMARY': str(self.root / 'summary.md'),
            'GITHUB_OUTPUT': str(self.root / 'outputs'),
            **{key: 'fixture-do-not-report-' + key for key in daily.REQUIRED},
        }
        self.api = FakeGitHub()
        self.calls = []
        self.day = datetime(2026, 10, 18, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.coordinator = daily.Coordinator(self.root, self.env, self.api, lambda: self.day, self.success)

    def success(self, command, **kwargs):
        self.calls.append(command)
        if command[-1] == '--run':
            directory = self.root / '.schedule-state'
            (directory / 'completed-2026-10-18.txt').write_text('completed')
            (directory / 'last-success-summary.md').write_text(
                '- 成功：1200\n- 失败：0\n- 新增：10\n- 更新：1190\n- 拆包/规格警告：2\n'
                '- 导出文件：do-not-publish-products.xlsx\n', encoding='utf-8')
        return SimpleNamespace(returncode=0)

    def prior(self, status='needs_review', date='2026-10-18'):
        self.api.saved = {'status': status, 'business_date': date, 'run_key': '122:1', 'run_url': 'https://example.invalid'}

    def report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.coordinator.report()

    def test_schedule_executes_one_real_import_and_reports_counts_without_credentials(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 0)
        self.assertEqual(self.calls[0][-1], '--run')
        self.assertEqual(self.api.saved['status'], 'succeeded')
        self.assertEqual(self.report(), 0)
        body = self.api.comments[-1]
        self.assertIn('@wangchaozhuanyong', body)
        self.assertIn('成功：1200', body)
        self.assertNotIn('do-not-publish-products', body)
        for key in daily.REQUIRED:
            self.assertNotIn(self.env[key], body)

    def test_same_day_success_skips_duplicate(self):
        self.prior('succeeded')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.read()['status'], 'skipped')
        self.assertEqual(self.calls, [])

    def test_unresolved_import_blocks_even_on_next_day(self):
        self.prior('running', '2026-10-17')
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.api.saved['status'], 'running')

    def test_resolution_requires_exact_run_identifier(self):
        self.prior()
        self.env.update(RESOLVE_RUN_KEY='121:1', PREVIOUS_RESULT='not-imported')
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')

    def test_confirmed_completed_does_not_reimport_same_day(self):
        self.prior()
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='completed')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.read()['status'], 'skipped')
        self.assertEqual(self.api.saved['status'], 'succeeded')
        self.assertEqual(self.calls, [])

    def test_confirmed_not_imported_allows_one_new_attempt(self):
        self.prior()
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='not-imported')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_failed_import_persists_stop_and_next_attempt_cannot_run(self):
        self.coordinator.run = lambda *args, **kwargs: SimpleNamespace(returncode=1)
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')
        self.env['GITHUB_RUN_ATTEMPT'] = '2'
        self.assertEqual(self.coordinator.prepare(), 1)

    def test_zero_exit_without_success_files_is_not_claimed_success(self):
        self.coordinator.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')

    def test_failed_durable_start_prevents_import(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        self.api.fail_write = True
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.calls, [])

    def test_failed_success_record_keeps_retry_blocked(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        self.api.fail_success_write = True
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')
        self.assertEqual(self.coordinator.prepare(), 1)

    def test_dependency_failure_before_execute_reports_without_marking_partial_import(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.report(), 0)
        self.assertIn('环境准备失败，未执行同步', self.api.comments[-1])
        self.assertEqual(self.api.saved['status'], 'idle')

    def test_missing_credentials_blocks_and_reports_names_only(self):
        del self.env['POSPAL_PASSWORD']
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.report(), 0)
        self.assertIn('POSPAL_PASSWORD', self.api.comments[-1])
        self.assertEqual(self.calls, [])

    def test_disabled_scheduler_does_not_import_or_notify_daily(self):
        self.env['MEIYIJIA_CLOUD_SYNC_ENABLED'] = 'false'
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.report(), 0)
        self.assertEqual(self.api.comments, [])

    def test_dry_run_can_diagnose_sites_while_real_import_is_paused(self):
        self.prior()
        self.env.update(GITHUB_EVENT_NAME='workflow_dispatch', SYNC_MODE='dry-run')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 0)
        self.assertEqual(self.calls[0][-1], '--dry-run')
        self.assertEqual(self.api.saved['status'], 'needs_review')

    def test_report_after_interruption_does_not_clear_durable_lock(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        context = self.coordinator.read()
        context['status'] = 'running'
        self.coordinator.write(context)
        self.prior('running')
        self.assertEqual(self.report(), 0)
        self.assertIn('执行中断，结果待核实', self.api.comments[-1])
        self.assertEqual(self.api.saved['status'], 'running')

    def test_state_change_between_prepare_and_execute_prevents_import(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        self.prior('running')
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.api.saved['status'], 'running')

    def test_notification_failure_does_not_allow_repeat_import(self):
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 0)
        def denied(*args):
            raise RuntimeError('GitHub API HTTP 403')
        self.api.comment = denied
        self.assertEqual(self.report(), 1)
        self.assertEqual(self.api.saved['status'], 'succeeded')
        self.assertIn('成功：1200', Path(self.env['GITHUB_STEP_SUMMARY']).read_text())
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.read()['status'], 'skipped')
        self.assertEqual(len(self.calls), 1)

    def test_successful_import_with_warnings_reports_review_without_reimport(self):
        success = self.coordinator.run
        def with_warnings(*args, **kwargs):
            result = success(*args, **kwargs)
            summary = self.root / '.schedule-state/last-success-summary.md'
            with summary.open('a', encoding='utf-8') as file:
                file.write('- 状态：成功（需要人工复核）\n')
            return result
        self.coordinator.run = with_warnings
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 0)
        self.assertEqual(self.report(), 0)
        self.assertIn('需要人工复核', self.api.comments[-1])
        self.assertEqual(self.api.saved['status'], 'succeeded')


class APIStateTests(unittest.TestCase):
    def test_missing_or_malformed_durable_state_is_rejected(self):
        api = daily.GitHub({'GITHUB_REPOSITORY': 'a/b', 'GH_TOKEN': 'fixture'})
        for body in ('', daily.STATE_PREFIX + '{"status":"unknown"} -->'):
            api.request = lambda *args: {'body': body}
            with self.assertRaises(RuntimeError):
                api.state(17)

    def test_persisted_state_round_trips_through_issue_body(self):
        state = {'status': 'succeeded', 'business_date': '2026-10-18', 'run_key': '123:1'}
        api = daily.GitHub({'GITHUB_REPOSITORY': 'a/b', 'GH_TOKEN': 'fixture'})
        api.request = lambda *args: {'body': daily.issue_body(state)}
        self.assertEqual(api.state(17), state)


if __name__ == '__main__':
    unittest.main()
