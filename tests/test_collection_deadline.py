"""Real subprocess deadlines, durable export and continuation after a hang."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from sale_monitor.collection_deadline import main, mark_interrupted, supervise
from sale_monitor.models import Offer
from sale_monitor.reporting import aggregate, checkpoint, health
from sale_monitor.runner import Collector
from sale_monitor.storage import atomic_json, read_json

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
RUN = 'deadline-1'


class CollectionDeadline(unittest.TestCase):
    def setUp(self):
        parent = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='deadline-test-', dir=parent)
        self.base = Path(self.temp.name).resolve()
        self.assertEqual(self.base.parent, parent)
        self.assertTrue(self.base.name.startswith('deadline-test-'))
        self.addCleanup(self.temp.cleanup)
        self.root = self.base / 'state-repo/state'
        self.path = self.root / 'stores/tsukumo.json'
        offer = Offer('tsukumo', 'fixture', 'https://example.invalid/item',
                      observed_at=NOW.isoformat(), observed_run_id=RUN)
        receipt = {'observation_id': 'receipt', 'run_id': RUN, 'offer': offer.to_dict()}
        self.state = {'store': 'tsukumo', 'run_id': RUN, 'status': 'running',
                      'cycle_complete': False, 'checkpoint_at': NOW.isoformat(),
                      'queue': {'old': {'type': 'product', 'url': offer.url, 'kind': 'sale',
                                        'created_at': '2026-09-01T00:00:00+00:00', 'attempts': 17,
                                        'last_error': 'RemoteDisconnected'}},
                      'offers': {offer.key: offer.to_dict()}, 'journal': [receipt],
                      'done': ['completed'], 'request_count': 3,
                      'custom': {'preserve': ['unknown', 1]}}
        self.env = os.environ.copy()
        self.env['PYTHONPATH'] = str(REPO)
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'

    def worker(self, code):
        return [sys.executable, '-B', '-c', code]

    def supervise(self, command, timeout=.5):
        return supervise(command, self.root, 'tsukumo', RUN, timeout, stop_grace=.2)

    def test_normal_exit_and_failure_exit_do_not_rewrite_checkpoint(self):
        atomic_json(self.path, self.state)
        before = self.path.read_bytes()
        for expected in [0, 7, 124]:
            with self.subTest(exit=expected):
                code, record = self.supervise(self.worker('raise SystemExit(' + str(expected) + ')'), 5)
                self.assertEqual(code, expected)
                self.assertIsNone(record)
                self.assertEqual(self.path.read_bytes(), before)

    def test_hung_worker_checkpoint_is_exported_aggregated_and_resumable(self):
        seed = self.base / 'seed.json'
        atomic_json(seed, self.state)
        command = self.worker('import time,json; from pathlib import Path; '
                              'from sale_monitor.storage import atomic_json; '
                              'atomic_json(Path(' + repr(str(self.path)) + '),json.loads(Path(' + repr(str(seed)) + ').read_text())); '
                              'time.sleep(60)')
        started = time.monotonic()
        code, record = self.supervise(command, 1)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(record['checkpoint_status'], 'preserved')
        after = read_json(self.path, {})
        self.assertEqual({k: v for k, v in after.items() if k not in ('status', 'collection_deadline')},
                         {k: v for k, v in self.state.items() if k != 'status'})
        self.assertEqual(after['status'], 'interrupted')
        exported = subprocess.run([sys.executable, '-B', str(REPO / 'scripts/export_store.py'), '--store', 'tsukumo'],
                                  cwd=self.base, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        published_state = self.base / 'published-state'
        summary = checkpoint(published_state, self.base / 'outgoing', RUN)
        self.assertEqual(summary['accepted_stores'], ['tsukumo'])
        self.assertEqual(read_json(published_state / 'stores/tsukumo.json', {}), after)
        public = self.base / 'public'
        index = aggregate(published_state, public, RUN, NOW)
        self.assertEqual(len(list(public.glob('*.json'))), 9)
        self.assertEqual(index['stores']['tsukumo']['status'], 'interrupted')
        self.assertEqual(index['stores']['tsukumo']['request_count'], 3)
        self.assertIn('final_request_total_unknown', index['stores']['tsukumo']['request_count_scope'])
        self.assertEqual(index['stores']['tsukumo']['collection_deadline'], record)
        self.assertEqual(index['stores']['tsukumo']['pending_over_24h'], 1)
        self.assertFalse(read_json(public / 'validation.json', {})['cutover_ready'])
        self.assertEqual(read_json(published_state / 'history/tsukumo/2026-11-10.json', []), self.state['journal'])
        collector = Collector(published_state, 'tsukumo', {'stores': {'tsukumo': {'adapter': 'html', 'seed_urls': []}}},
                              'next-run', client=SimpleNamespace(count=0))
        collector.seed()
        self.assertEqual(collector.state['queue']['old'], self.state['queue']['old'])
        self.assertIsNone(health(collector.state, NOW)['collection_deadline'])

    def test_missing_or_stale_checkpoint_is_not_invented(self):
        for value in [None, {**self.state, 'run_id': 'old-run'}, {**self.state, 'store': 'ark'}]:
            with self.subTest(value=value is None):
                if value is not None:
                    atomic_json(self.path, value)
                before = self.path.read_bytes() if self.path.exists() else None
                code, record = self.supervise(self.worker('import time; time.sleep(60)'), .2)
                self.assertEqual(code, 124)
                self.assertEqual(record['checkpoint_status'], 'no_matching_current_checkpoint')
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_unreadable_and_invalid_checkpoint_are_not_replaced(self):
        self.path.parent.mkdir(parents=True)
        for value in ['{"store":', json.dumps({**self.state, 'journal': {}})]:
            self.path.write_text(value)
            result = mark_interrupted(self.root, 'tsukumo', RUN, 1, 1, -15)
            self.assertIn(result['checkpoint_status'], ('unreadable', 'invalid_containers'))
            self.assertEqual(self.path.read_text(), value)

    def test_finished_collection_stalled_in_diagnostic_keeps_completion_receipts(self):
        state = {**self.state, 'status': 'complete', 'cycle_complete': True,
                 'queue': {}, 'transport_diagnostic': {'run_id': RUN, 'status': 'started', 'diagnostic_request_count': None}}
        atomic_json(self.path, state)
        record = mark_interrupted(self.root, 'tsukumo', RUN, 2, 2.1, -15)
        saved = read_json(self.path, {})
        self.assertEqual(record['prior_status'], 'complete')
        self.assertTrue(saved['cycle_complete'])
        self.assertEqual(saved['done'], state['done'])
        self.assertEqual(saved['transport_diagnostic'], state['transport_diagnostic'])
        self.assertEqual(health(saved, NOW)['status'], 'interrupted')
        self.assertIsNone(health({**saved, 'status': 'job_missing'}, NOW)['collection_deadline'])
        self.assertIsNone(health({**saved, 'status': 'complete'}, NOW)['collection_deadline'])

    def test_invalid_deadline_does_not_start_worker(self):
        with self.assertRaises(SystemExit) as error:
            main(['--store', 'tsukumo', '--state', str(self.root), '--run-id', RUN,
                  '--seconds', '2100', '--deadline-seconds', '2100'])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.root.exists())

    def test_workflow_saves_state_after_a_failed_run_and_migrates_first(self):
        workflow = (REPO / '.github/workflows/monitor.yml').read_text()
        self.assertIn("cron: '17 */4 * * *'", workflow)
        self.assertIn('group: sale-monitor-state\n  cancel-in-progress: false', workflow)
        # One job runs every stage; there is no store matrix or artifact hand-off.
        self.assertNotIn('matrix:', workflow)
        migrate = workflow.index('python -m sale_monitor.cli migrate --state state-repo/state')
        saved = workflow.index('python scripts/state_branch.py publish --state-only')
        run = workflow.index('python -m sale_monitor.cli run --state state-repo/state')
        self.assertLess(migrate, saved)
        self.assertLess(saved, run)
        after_run = workflow[run:]
        self.assertIn('        if: always()\n        run: python scripts/state_branch.py publish\n', after_run)
        self.assertIn('      - uses: actions/upload-artifact@v4\n        if: always()', after_run)

    @unittest.skipUnless(os.name == 'posix', 'CI Linux process-group cleanup')
    def test_descendant_ignoring_term_is_stopped_with_owned_group(self):
        heartbeat = self.base / 'child-heartbeat'
        child = ('import signal,time; from pathlib import Path; '
                 'signal.signal(signal.SIGTERM,signal.SIG_IGN); p=Path(' + repr(str(heartbeat)) + '); '
                 '\nwhile True:\n p.write_text(str(time.monotonic()))\n time.sleep(.02)\n')
        parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(60)'
        code, record = self.supervise(self.worker(parent), 1)
        self.assertEqual(code, 124)
        self.assertTrue(heartbeat.exists())
        time.sleep(.1)
        before = heartbeat.read_bytes()
        time.sleep(.2)
        self.assertEqual(heartbeat.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
