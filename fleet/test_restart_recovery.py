import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from .app import Console


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('restart_case_runner', source)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.BASE = Path(self.temp.name)
        self.m.RUNS = self.m.BASE / 'jobs'
        self.m.RUNS.mkdir()
        self.m.CONFIG = {'service': {'host': '192.0.2.5', 'port': 8000}}
        self.record = {'id': 'real-restart', 'status': 'interrupted', 'pid': 218,
                       'fleet_request_id': 'f' * 32, 'config': {'host': '192.0.2.5', 'port': 8000},
                       'metrics': {'accuracy': 25}}
        self.write(self.record)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, row):
        self.m.save(self.m.RUNS / row['id'] / 'record.json', row)

    def test_live_then_unknown_then_exited_recovers_without_signals_or_success_fabrication(self):
        with patch.object(self.m, 'restarted_job_stopped', return_value=False), patch.object(self.m.os, 'kill', create=True) as kill:
            self.m.restore_restarted_tasks()
            self.assertFalse(self.m.SAFE_IDLE)
            self.assertTrue(self.m.LOCK.locked())
            self.assertEqual(self.m.FLEET_REQUESTS['f' * 32]['state'], 'recovering')
            self.assertFalse(self.m.reserve_operation('checking'))
            self.assertEqual(self.m.FLEET_REQUESTS['f' * 32]['reason'], 'old_process_alive')
            kill.assert_not_called()
        with patch.object(self.m, 'restarted_job_stopped', return_value=None):
            self.m.reconcile_finished_task()
            self.assertTrue(self.m.LOCK.locked())
        with patch.object(self.m, 'restarted_job_stopped', return_value=True), patch.object(self.m.os, 'kill', create=True) as kill:
            self.m.reconcile_finished_task()
            kill.assert_not_called()
        row = json.loads((self.m.RUNS / self.record['id'] / 'record.json').read_text())
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['metrics'], self.record['metrics'])
        self.assertTrue(row['completed_at'])
        self.assertTrue(row['process_tree_confirmed_stopped'])
        self.assertTrue(self.m.SAFE_IDLE)
        self.assertFalse(self.m.LOCK.locked())
        self.assertEqual(self.m.FLEET_REQUESTS['f' * 32]['state'], 'finished')
        self.m.FLEET_REQUESTS.clear()
        self.m.restore_finished_requests()
        self.assertTrue(self.m.FLEET_REQUESTS['f' * 32]['restored_after_restart'])

    def test_multiple_orphans_keep_gate_until_all_have_exit_proof(self):
        self.write({**self.record, 'id': 'real-other', 'fleet_request_id': 'a' * 32})
        proof = lambda row: row['id'] == 'real-restart'
        with patch.object(self.m, 'restarted_job_stopped', side_effect=proof):
            self.m.restore_restarted_tasks()
        self.assertTrue(self.m.LOCK.locked())
        self.assertEqual(self.m.ACTIVE_JOB['id'], 'real-other')
        self.assertEqual(set(self.m.RECOVERING_JOBS), {'real-other'})

    def test_saved_terminal_result_is_kept_after_delayed_child_exit(self):
        self.write({**self.record, 'status': 'unknown', 'terminal_status': 'failed', 'completed_at': '2026-11-08T01:00:00Z'})
        with patch.object(self.m, 'restarted_job_stopped', return_value=True):
            self.m.restore_restarted_tasks()
        row = json.loads((self.m.RUNS / self.record['id'] / 'record.json').read_text())
        self.assertEqual(row['status'], 'failed')

    def test_only_zombies_are_absent_but_escaped_tagged_child_blocks(self):
        proc = self.m.BASE / 'proc'
        member = proc / '12345'
        member.mkdir(parents=True)
        (member / 'stat').write_text('12345 (child) Z 1 999 999 0')
        with patch.object(self.m, 'process_group_running', return_value=False):
            self.assertTrue(self.m.restarted_job_stopped(self.record, proc))
            (member / 'stat').write_text('12345 (child) S 1 999 999 0')
            (member / 'cmdline').write_bytes(b'worker\0')
            (member / 'environ').write_bytes(b'ASCEND_BENCH_JOB_ID=real-restart\0')
            self.assertFalse(self.m.restarted_job_stopped(self.record, proc))
            (member / 'environ').unlink()
            with patch.object(Path, 'read_bytes', side_effect=PermissionError()):
                self.assertIsNone(self.m.restarted_job_stopped(self.record, proc))

    def test_reused_pid_in_new_namespace_does_not_block_but_tag_still_checked(self):
        proc = self.m.BASE / 'empty-proc'
        proc.mkdir()
        record = {**self.record, 'recovery_identity': {'boot_id': 'old', 'pid_namespace': 'old'}}
        with patch.object(self.m, 'recovery_identity', return_value={'boot_id': 'new', 'pid_namespace': 'new'}), patch.object(self.m, 'process_group_running') as groups:
            self.assertTrue(self.m.restarted_job_stopped(record, proc))
            groups.assert_not_called()
        record['recovery_identity'] = {'boot_id': 'same', 'pid_namespace': 'same', 'start_ticks': '100'}
        with patch.object(self.m, 'recovery_identity', return_value={'boot_id': 'same', 'pid_namespace': 'same', 'start_ticks': '200'}), patch.object(self.m, 'process_group_running') as groups:
            self.assertTrue(self.m.restarted_job_stopped(record, proc))
            groups.assert_not_called()
        with patch.object(self.m, 'process_group_running', return_value=None):
            self.assertIsNone(self.m.restarted_job_stopped(self.record, proc))
        self.assertIsNone(self.m.restarted_job_stopped({**self.record, 'pid': None}, proc))


class CenterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = Console(root / 'absent.json', root / 'data', demo=True)
        self.row = self.app.store.list('runner')[0]
        self.row['status'].update(instance_id='old', activity='idle')
        self.app.store.save('runner', self.row)
        self.request = 'f' * 32
        self.app.store.claim(self.row['id'], '192.0.2.5:8000', self.request)

    def tearDown(self):
        self.app.reconcile_stop.set()
        if self.app.reconcile_thread:self.app.reconcile_thread.join(2)
        self.app.executor.shutdown(wait=True)
        self.app.store.db.close()
        self.temp.cleanup()

    def test_claim_is_visible_and_cannot_look_idle_in_user_or_admin_view(self):
        for row in (self.app.runners()[0], self.app.user_view()['runners'][0]):
            self.assertEqual(row['status']['activity'], 'recovering')
            self.assertEqual(row['occupancy']['target'], '192.0.2.5:8000')
            self.assertEqual(row['occupancy']['request'], self.request[:12])
            self.assertNotIn('token', row)
        self.assertEqual(self.app.store.get('runner', self.row['id'])['status']['activity'], 'idle')

    def test_changed_runner_instance_releases_only_proven_interrupted_receipt(self):
        self.app.demo = False
        status = {**self.row['status'], 'instance_id': 'new', 'process_tree_confirmed_stopped': True,
                  'fleet_requests': {self.request: {'state': 'finished', 'job_id': 'real-restart',
                                                   'restored_after_restart': True, 'terminal_status': 'interrupted',
                                                   'completed_at': '2026-11-08T01:00:00Z', 'process_tree_confirmed_stopped': True}}}
        with patch.object(self.app.transport, 'status', return_value={'token': '', 'status': status}):
            self.assertEqual(self.app.refresh_once(self.row['id'])['status']['activity'], 'idle')
        self.assertIsNone(self.app.store.request_for(self.row['id']))

    def test_missing_or_unverified_receipt_stays_claimed(self):
        self.app.demo = False
        status = {**self.row['status'], 'instance_id': 'new', 'process_tree_confirmed_stopped': True, 'fleet_requests': {}}
        with patch.object(self.app.transport, 'status', return_value={'token': '', 'status': status}):
            self.assertEqual(self.app.refresh_once(self.row['id'])['status']['activity'], 'recovering')
        self.assertEqual(self.app.store.request_for(self.row['id']), self.request)

    def test_background_reconciliation_runs_without_browser_refresh(self):
        self.app.demo = False
        called = threading.Event()
        with patch.object(self.app, 'refresh', side_effect=lambda *args: called.set()):
            self.app.start_reconciliation()
            self.assertTrue(called.wait(2))
            self.app.reconcile_stop.set()
            self.app.reconcile_thread.join(2)

    def test_maintenance_blocks_new_work_but_does_not_block_existing_task_stop(self):
        self.app.demo = False
        self.app.store.release(self.row['id'], self.request)
        self.app.store.claim(self.row['id'], 'maintenance-restart-recovery:fixture', 'maintenance')
        self.app.transport.profiles[self.row['spec']['profile_id']]['mode'] = 'session'
        with patch.object(self.app.transport, 'request', return_value=(200, 'application/json', b'{}')) as remote:
            for path in ('/api/jobs', '/api/datasets/generate', '/api/check-service'):
                with self.assertRaisesRegex(ValueError, '正在安全更新'):
                    self.app._proxy_once(self.row['id'], 'POST', path, {})
            remote.assert_not_called()
            self.app._proxy_once(self.row['id'], 'POST', '/api/jobs/real-fixture/stop', {})
            self.assertEqual(remote.call_count, 1)
