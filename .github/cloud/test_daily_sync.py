import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import sys
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
        self.evidence = None

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

    def request(self, method, path, payload=None):
        if method == 'GET' and path == 'issues/comments/42':
            return copy.deepcopy(self.evidence)
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
            (directory / f'completed-{self.day:%Y-%m-%d}.txt').write_text('completed')
            (directory / 'last-success-summary.md').write_text(
                '- 成功：1200\n- 失败：0\n- 新增：10\n- 更新：1190\n- 拆包/规格警告：2\n'
                '- 导出文件：do-not-publish-products.xlsx\n', encoding='utf-8')
            self.diagnostic()
        return SimpleNamespace(returncode=0)

    def diagnostic(self, **updates):
        value = {
            'version': 1, 'run_key': '123:1', 'stage': 'completed',
            'request_state': 'response_received', 'outcome': 'succeeded', 'error': 'none',
            'export_rows': 1200, 'http_status': 200, 'warning_details': 2,
            'counts': {'insertCount': 10, 'updateCount': 1190, 'skipCount': 0, 'successCount': 1200, 'failCount': 0},
            **updates,
        }
        path = self.root / '.schedule-state/import-diagnostic.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return value

    def prior(self, status='needs_review', date='2026-10-18'):
        self.api.saved = {'status': status, 'business_date': date, 'run_key': '122:1', 'run_url': 'https://example.invalid'}

    def report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.coordinator.report()

    def reconciliation(self, **updates):
        proof={'version':1,'run_key':'122:1','verified_date':'2026-10-18',
            'source_rows':1200,'target_rows':1210,'target_before':1200,'added_rows':10,
            'failed_rows':0,'source_missing':0,'removed_rows':0,'stock_precision_rows':1,'cost_precision_rows':1,
            'source_sha256':'a'*64,'readback_sha256':'b'*64,**updates}
        self.api.evidence={'author_association':'OWNER',
            'issue_url':f'https://api.github.com/repos/{self.env["GITHUB_REPOSITORY"]}/issues/17',
            'body':daily.RECONCILIATION_PREFIX+json.dumps(proof)+' -->'}
        self.env.update(RESOLVE_RUN_KEY='122:1',PREVIOUS_RESULT='reconciled',
            RECONCILIATION_EVIDENCE_URL=f'https://github.com/{self.env["GITHUB_REPOSITORY"]}/issues/17#issuecomment-42')
        return proof

    def manual_reconciled(self):
        self.prior(date='2026-10-17')
        proof = self.reconciliation()
        self.api.saved.update(
            status='reconciled', review_result='reconciled',
            reconciliation=proof, reconciliation_date='2026-10-18',
            reconciliation_url=self.env['RECONCILIATION_EVIDENCE_URL'],
        )
        self.env.update(
            GITHUB_EVENT_NAME='workflow_dispatch', SYNC_MODE='run',
            RESOLVE_RUN_KEY='', PREVIOUS_RESULT='unverified', RECONCILIATION_EVIDENCE_URL='',
            MANUAL_RECONCILED_RUN_KEY='122:1',
        )

    def test_explicit_manual_reconciled_same_day_runs_once_and_reports_authorization(self):
        self.manual_reconciled()
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.api.saved['status'], 'reconciled')
        self.assertEqual(self.coordinator.execute(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.api.saved['status'], 'succeeded')
        self.assertEqual(self.report(), 0)
        self.assertIn('关联原执行 122:1', self.api.comments[-1])
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.api.saved['status'], 'succeeded')
        self.assertEqual(len(self.calls), 1)

    def test_manual_reconciled_requires_exact_current_proof_and_manual_run(self):
        cases = ('wrong_key', 'wrong_event', 'wrong_mode', 'older_date', 'missing_proof',
                 'wrong_proof_key', 'already_succeeded', 'needs_review', 'running')
        for case in cases:
            with self.subTest(case=case):
                self.manual_reconciled()
                if case == 'wrong_key': self.env['MANUAL_RECONCILED_RUN_KEY'] = '121:1'
                if case == 'wrong_event': self.env['GITHUB_EVENT_NAME'] = 'schedule'
                if case == 'wrong_mode': self.env['SYNC_MODE'] = 'dry-run'
                if case == 'older_date': self.api.saved['reconciliation_date'] = '2026-10-17'
                if case == 'missing_proof': self.api.saved.pop('reconciliation')
                if case == 'wrong_proof_key': self.api.saved['reconciliation']['run_key'] = '121:1'
                if case == 'already_succeeded': self.api.saved['status'] = 'succeeded'
                if case in ('needs_review', 'running'): self.api.saved['status'] = case
                before = copy.deepcopy(self.api.saved)
                self.assertEqual(self.coordinator.prepare(), 1)
                self.assertEqual(self.api.saved, before)
                self.assertEqual(self.calls, [])

    def test_manual_reconciled_rechecks_state_before_submitting(self):
        for status in ('succeeded', 'running', 'needs_review'):
            with self.subTest(status=status):
                self.manual_reconciled()
                self.assertEqual(self.coordinator.prepare(), 0)
                self.api.saved['status'] = status
                before = copy.deepcopy(self.api.saved)
                self.assertEqual(self.coordinator.execute(), 1)
                self.assertEqual(self.api.saved, before)
                self.assertEqual(self.calls, [])

    def test_failed_manual_reconciled_import_stays_paused(self):
        self.manual_reconciled()
        def failed(command, **kwargs):
            self.calls.append(command)
            return SimpleNamespace(returncode=1)
        self.coordinator.run = failed
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(len(self.calls), 1)

    def test_manual_reconciled_cannot_enable_disabled_sync(self):
        self.manual_reconciled()
        self.env['MEIYIJIA_CLOUD_SYNC_ENABLED'] = 'false'
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.read()['status'], 'disabled')
        self.assertEqual(self.api.saved['status'], 'reconciled')
        self.assertEqual(self.calls, [])

    def test_reconciled_coverage_preserves_original_identity_and_never_imports_same_day(self):
        self.prior(date='2026-10-17')
        self.reconciliation()
        self.assertEqual(self.coordinator.prepare(),0)
        self.assertEqual(self.api.saved['status'],'reconciled')
        self.assertEqual(self.api.saved['business_date'],'2026-10-17')
        self.assertEqual(self.api.saved['run_key'],'122:1')
        self.assertEqual(self.api.saved['reconciliation_date'],'2026-10-18')
        self.assertEqual(self.calls,[])
        self.assertNotIn('should_run=true',Path(self.env['GITHUB_OUTPUT']).read_text())
        self.env.update(RESOLVE_RUN_KEY='',PREVIOUS_RESULT='unverified',RECONCILIATION_EVIDENCE_URL='')
        self.assertEqual(self.coordinator.prepare(),0)
        self.assertEqual(self.coordinator.read()['status'],'skipped')
        self.assertEqual(self.calls,[])
        self.day=self.day.replace(day=19)
        self.assertEqual(self.coordinator.prepare(),0)
        self.assertEqual(self.coordinator.execute(),0)
        self.assertEqual(len(self.calls),1)

    def test_reconciliation_retains_known_partial_receipt_instead_of_claiming_original_success(self):
        self.prior()
        diagnostic=self.diagnostic(run_key='122:1',outcome='partial',error='failed_rows',
            counts={'insertCount':10,'updateCount':1189,'skipCount':0,'successCount':1199,'failCount':1})
        self.api.saved.update(review_result='partial',diagnostic=diagnostic)
        self.reconciliation()
        self.assertEqual(self.coordinator.prepare(),0)
        self.assertEqual(self.api.saved['status'],'reconciled')
        self.assertEqual(self.api.saved['diagnostic'],diagnostic)
        self.assertEqual(self.calls,[])

    def test_incomplete_or_inconsistent_reconciliation_never_unpauses(self):
        for update in [{'source_missing':1},{'failed_rows':1},{'removed_rows':1},{'source_rows':True},
                       {'target_before':1199},{'run_key':'121:1'},{'readback_sha256':'invalid'},
                       {'stock_precision_rows':11},{'verified_date':'2026-10-19'},{'verified_date':'2026-10-17'}]:
            with self.subTest(update=update):
                self.prior()
                self.reconciliation(**update)
                self.assertEqual(self.coordinator.prepare(),1)
                self.assertEqual(self.api.saved['status'],'needs_review')
                self.assertEqual(self.calls,[])

    def test_reconciliation_requires_this_issue_and_trusted_author(self):
        for invalid in ['url','author','issue','absent']:
            with self.subTest(invalid=invalid):
                self.prior()
                self.reconciliation()
                if invalid=='url': self.env['RECONCILIATION_EVIDENCE_URL']='https://github.com/other/repo/issues/17#issuecomment-42'
                if invalid=='author': self.api.evidence['author_association']='NONE'
                if invalid=='issue': self.api.evidence['issue_url']='https://api.github.com/repos/other/repo/issues/17'
                if invalid=='absent': self.api.evidence['body']='No structured readback evidence'
                self.assertEqual(self.coordinator.prepare(),1)
                self.assertEqual(self.api.saved['status'],'needs_review')

    def test_reconciliation_state_change_after_prepare_blocks_import(self):
        self.assertEqual(self.coordinator.prepare(),0)
        self.api.saved={'status':'reconciled','reconciliation_date':'2026-10-18'}
        self.assertEqual(self.coordinator.execute(),1)
        self.assertEqual(self.calls,[])

    def test_warning_total_and_details_are_reported_separately(self):
        def with_total(*args,**kwargs):
            value=self.success(*args,**kwargs)
            self.diagnostic(warning_details=50,warning_total=174)
            return value
        self.coordinator.run=with_total
        self.assertEqual(self.coordinator.prepare(),0)
        self.assertEqual(self.coordinator.execute(),0)
        self.assertEqual(self.report(),0)
        self.assertIn('警告明细：50',self.api.comments[-1])
        self.assertIn('异常总数：174',self.api.comments[-1])

    def test_invalid_warning_or_category_counts_are_rejected(self):
        for updates in [{'warning_total':1},{'warning_total':True},{'category_created':2,'category_checked':1},
                        {'category_checked':-1},{'category_checked':'private fixture'}]:
            with self.subTest(updates=updates):
                self.assertIsNone(daily.safe_diagnostic(self.diagnostic(**updates),'123:1'))

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
        self.assertEqual(self.coordinator.read()['status'], 'review_recorded')
        self.assertEqual(self.api.saved['status'], 'succeeded')
        self.assertEqual(self.calls, [])

    def test_confirmed_not_imported_records_only_and_requires_separate_attempt(self):
        self.prior()
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='not-imported')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.read()['status'], 'review_recorded')
        self.assertEqual(self.api.saved['status'], 'idle')
        self.assertEqual(len(self.calls), 0)
        self.env.update(RESOLVE_RUN_KEY='', PREVIOUS_RESULT='unverified')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_old_date_completed_resolution_cannot_upload_today(self):
        self.prior(date='2026-10-17')
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='completed')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.read()['status'], 'review_recorded')
        self.assertEqual(self.api.saved['business_date'], '2026-10-17')
        self.assertEqual(self.calls, [])
        self.assertNotIn('should_run=true', Path(self.env['GITHUB_OUTPUT']).read_text())
        with self.assertRaises(daily.SafeError):
            self.coordinator.execute()

    def test_partial_resolution_keeps_pause_and_cannot_be_relabelled_complete(self):
        self.prior()
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='partial')
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.api.saved['status'], 'needs_review')
        self.env['PREVIOUS_RESULT'] = 'completed'
        self.assertEqual(self.coordinator.prepare(), 1)
        self.env['PREVIOUS_RESULT'] = 'not-imported'
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.calls, [])

    def test_known_partial_counts_cannot_be_relabelled_complete(self):
        self.prior()
        self.api.saved['diagnostic'] = self.diagnostic(
            run_key='122:1', stage='receipt_saved', outcome='partial', error='failed_rows',
            counts={'insertCount': 10, 'updateCount': 1189, 'skipCount': 0, 'successCount': 1199, 'failCount': 1},
        )
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='completed')
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')

    def test_received_success_cannot_be_relabelled_not_imported(self):
        self.prior()
        self.api.saved['diagnostic'] = self.diagnostic(run_key='122:1', stage='closing_browser', error='stage_failed')
        self.env.update(RESOLVE_RUN_KEY='122:1', PREVIOUS_RESULT='not-imported')
        self.assertEqual(self.coordinator.prepare(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')

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

    def test_success_files_without_current_valid_receipt_cannot_unlock(self):
        original = self.coordinator.run
        for updates in ({'run_key': '122:1'}, {'counts': {'successCount': 1200}}, {'export_rows': 1201}):
            with self.subTest(updates=updates):
                self.api.saved = {'status': 'idle'}
                def invalid_receipt(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.diagnostic(**updates)
                    return result
                self.coordinator.run = invalid_receipt
                self.assertEqual(self.coordinator.prepare(), 0)
                self.assertEqual(self.coordinator.execute(), 1)
                self.assertEqual(self.api.saved['status'], 'needs_review')

    def test_failed_rows_report_valid_counts_without_publishing_raw_error(self):
        def partial(*args, **kwargs):
            self.diagnostic(
                stage='receipt_saved', outcome='partial', error='backend_conflict_constraint',
                counts={'insertCount': 10, 'updateCount': 1189, 'skipCount': 0, 'successCount': 1199, 'failCount': 1},
                response='private fixture', message=self.env['MYJ_ADMIN_PASSWORD'],
            )
            return SimpleNamespace(returncode=1)
        self.coordinator.run = partial
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.report(), 0)
        self.assertIn('失败：1', self.api.comments[-1])
        self.assertIn('后台数据库冲突约束不匹配', self.api.comments[-1])
        self.assertNotIn('private fixture', json.dumps(self.api.saved))
        self.assertNotIn(self.env['MYJ_ADMIN_PASSWORD'], self.api.comments[-1])
        self.assertEqual(self.coordinator.prepare(), 1)

    def test_cleanup_failure_retains_received_success_but_does_not_auto_unlock(self):
        def failed_cleanup(*args, **kwargs):
            self.diagnostic(stage='closing_browser', error='stage_failed')
            return SimpleNamespace(returncode=1)
        self.coordinator.run = failed_cleanup
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.api.saved['status'], 'needs_review')
        self.assertEqual(self.api.saved['diagnostic']['outcome'], 'succeeded')
        self.assertEqual(self.report(), 0)
        self.assertIn('回执计数全部成功', self.api.comments[-1])
        self.assertEqual(self.coordinator.prepare(), 1)

    def test_unexpected_exception_text_never_enters_public_report(self):
        def private_failure(*args, **kwargs):
            raise RuntimeError(self.env['POSPAL_PASSWORD'])
        self.coordinator.run = private_failure
        self.assertEqual(self.coordinator.prepare(), 0)
        self.assertEqual(self.coordinator.execute(), 1)
        self.assertEqual(self.report(), 0)
        self.assertNotIn(self.env['POSPAL_PASSWORD'], self.api.comments[-1])

    def test_raw_child_output_stays_off_public_logs_and_issue(self):
        def failed_child(command, **kwargs):
            self.assertEqual(kwargs['stderr'], daily.subprocess.STDOUT)
            kwargs['stdout'].write('private fixture: ' + self.env['POSPAL_PASSWORD'])
            return SimpleNamespace(returncode=1)
        self.coordinator.run = failed_child
        self.env.update(GITHUB_EVENT_NAME='workflow_dispatch', SYNC_MODE='dry-run')
        self.assertEqual(self.coordinator.prepare(), 0)
        public_output = io.StringIO()
        with contextlib.redirect_stdout(public_output):
            self.assertEqual(self.coordinator.execute(), 1)
            self.assertEqual(self.coordinator.report(), 0)
        private_log = (self.root / 'logs/cloud-sync-console.log').read_text()
        self.assertIn(self.env['POSPAL_PASSWORD'], private_log)
        self.assertNotIn(self.env['POSPAL_PASSWORD'], public_output.getvalue())
        self.assertNotIn(self.env['POSPAL_PASSWORD'], self.api.comments[-1])

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
    def test_prepare_report_import_and_direct_script_work_without_site_packages(self):
        loaded = daily.subprocess.run(
            [sys.executable, '-S', '-c',
             'import runpy; module = runpy.run_path(' + repr(str(MODULE)) + '); '
             'assert callable(module["Coordinator"].prepare); assert callable(module["Coordinator"].report)'],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        direct = daily.subprocess.run(
            [sys.executable, '-S', str(daily.ROOT / 'scripts/sync_products.py'), '--help'],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)

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

    def test_reconciled_state_requires_bound_complete_proof(self):
        proof={'version':1,'run_key':'123:1','verified_date':'2026-10-18','source_rows':1200,
            'target_rows':1210,'target_before':1200,'added_rows':10,'failed_rows':0,'source_missing':0,
            'removed_rows':0,'stock_precision_rows':1,'cost_precision_rows':1,
            'source_sha256':'a'*64,'readback_sha256':'b'*64}
        state={'status':'reconciled','business_date':'2026-10-17','run_key':'123:1',
            'reconciliation_date':'2026-10-18','review_result':'reconciled','reconciliation':proof,
            'reconciliation_url':'https://github.com/a/b/issues/17#issuecomment-42'}
        api=daily.GitHub({'GITHUB_REPOSITORY':'a/b','GH_TOKEN':'fixture'})
        api.request=lambda *args:{'body':daily.issue_body(state)}
        self.assertEqual(api.state(17),state)
        for field in ['reconciliation','reconciliation_url','reconciliation_date','review_result']:
            bad=copy.deepcopy(state)
            bad.pop(field)
            api.request=lambda *args:{'body':daily.issue_body(bad)}
            with self.assertRaises((daily.SafeError,KeyError)):
                api.state(17)


if __name__ == '__main__':
    unittest.main()
