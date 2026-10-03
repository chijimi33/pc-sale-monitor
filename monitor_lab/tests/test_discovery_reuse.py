from copy import deepcopy
from dataclasses import asdict
import hashlib
import unittest
import uuid

from monitor_lab.capture import verify_capture
from monitor_lab.pipeline import make_task
from monitor_lab.queue_study import CapturedClient
from monitor_lab.safety import allowed_root, write
from monitor_lab.scheduler import collect
from monitor_lab.stores import SQLite
from monitor_lab.study import study
from monitor_lab.tests.test_request_plan_capture import fake_study, make_plan, no_network
from monitor_lab.tests.test_scheduler import PlannedStop

HOME = 'https://www.ark-pc.co.jp/'
PRODUCT = HOME + 'i/12201487/'
RUN = 'lab-reuse-test'


def task(kind='search', store='ark'):
    value = make_task(store, HOME, '2000-01-01T00:00:00Z', original={'attempts': 7})
    value['lab_kind'] = kind
    return value


class DiscoveryReuseTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('discovery-reuse-' + uuid.uuid4().hex)
        self.capture = self.root / 'capture'
        self.enterContext(no_network())
        write(self.root / 'plan.json', make_plan([
            {'store': 'ark', 'url': HOME, 'kind': 'home'},
            {'store': 'ark', 'url': PRODUCT, 'kind': 'product'}]))
        with fake_study({'urllib': {HOME: (200, {}, b'<html>Saved discovery</html>'),
                                   PRODUCT: (200, {}, b'<html>Saved product</html>')}}):
            study(self.capture, methods=['urllib'], plan=self.root / 'plan.json', budget=120)
        self.verified = verify_capture(self.capture)

    def client(self, enabled=True, verified=None):
        return CapturedClient(self.capture, verified or self.verified, 'urllib', discovery_reuse=enabled)

    def source(self, client, url=HOME):
        _, receipt = client.fetch(url)
        return {'dispatches': {RUN + ':1': {'state': 'committed', 'slot': 1, 'url': url,
                    'task_ids': ['root'], 'receipt': asdict(receipt), 'processing_error': None,
                    'started_at_epoch': client.start, 'finished_at_epoch': client.clock()}}}

    def test_new_discovery_parsing_keeps_original_bytes_time_and_source_without_attempt(self):
        client = self.client()
        records = self.source(client)
        before = deepcopy(records)
        clock, used = client.clock(), set(client.used)
        for key in ('search-jan1', 'search-jan2'):
            page, receipt = client.reuse_discovery(HOME, {key: task()}, records, RUN)
            source = records['dispatches'][RUN + ':1']['receipt']
            self.assertEqual(source['body_sha256'], hashlib.sha256(page.body).hexdigest())
            self.assertEqual(source['observed_at'], page.observed_at)
            self.assertEqual(source['observed_at'], receipt.observed_at)
            self.assertEqual(source['method'], receipt.method)
            self.assertEqual(source['source_capture_manifest_sha256'], receipt.source_capture_manifest_sha256)
            self.assertEqual(source['source_receipt_index'], receipt.source_receipt_index)
            self.assertEqual(RUN + ':1', receipt.derived_from_dispatch)
            self.assertTrue(receipt.analysis_reuse)
            self.assertEqual(([], [], 0), (receipt.attempts, receipt.waits, receipt.elapsed_seconds))
        self.assertEqual((clock, used, before, 0), (client.clock(), client.used, records, client.requests))
        records['dispatches'][RUN + ':2'] = {'state': 'committed', 'slot': 2, 'url': HOME,
            'task_ids': ['search-jan2'], 'receipt': asdict(receipt)}
        self.assertIsNone(client.reuse_discovery(HOME, {'search-jan2': task()}, records, RUN))
        self.assertIsNotNone(client.reuse_discovery(HOME, {'search-jan3': task()}, records, RUN))
        self.assertEqual('evidence_exhausted', client.fetch(HOME)[1].error)

    def test_only_new_discovery_tasks_and_same_run_committed_sources_are_eligible(self):
        client = self.client()
        records = self.source(client)
        for selected in ({'root': task()}, {'p': task('product')}, {'a': task(), 'b': task('product')},
                         {'x': task(store='sofmap')}, {}):
            with self.subTest(selected=selected):
                self.assertIsNone(client.reuse_discovery(HOME, selected, records, RUN))
        self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, records, 'lab-another-run'))
        for state in ('reserved', 'interrupted_unknown'):
            changed = deepcopy(records)
            changed['dispatches'][RUN + ':1']['state'] = state
            self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, changed, RUN))
        changed = deepcopy(records)
        changed['dispatches'][RUN + ':1']['processing_error'] = 'ValueError'
        self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, changed, RUN))
        disabled = self.client(False)
        self.assertIsNone(disabled.reuse_discovery(HOME, {'x': task()}, self.source(disabled), RUN))
        product = self.source(client, PRODUCT)
        self.assertIsNone(client.reuse_discovery(PRODUCT, {'x': task()}, product, RUN))

    def test_changed_committed_reference_or_body_is_rejected(self):
        client = self.client()
        records = self.source(client)
        changed = deepcopy(records)
        changed['dispatches'][RUN + ':1']['receipt']['observed_at'] = '2020-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'differs from captured source'):
            client.reuse_discovery(HOME, {'x': task()}, changed, RUN)
        source = records['dispatches'][RUN + ':1']['receipt']
        (self.capture / source['body_file']).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'Capture changed'):
            client.reuse_discovery(HOME, {'x': task()}, records, RUN)

    def test_failed_incomplete_or_later_unknown_source_cannot_become_reuse_success(self):
        for change in ({'error': 'transport:TimeoutError'}, {'status': 403}, {'body_incomplete': True},
                       {'body_unavailable': 'not_recorded'}):
            verified = deepcopy(self.verified)
            verified['receipts'][0].update(change)
            client = self.client(verified=verified)
            # Build the prior committed record without asking invalid bytes to parse.
            client.used.add(0)
            records = {'dispatches': {RUN + ':1': {'state': 'committed', 'slot': 1, 'url': HOME,
                'task_ids': ['root'], 'receipt': asdict(client.trace_receipt(0, verified['receipts'][0]))}}}
            with self.subTest(change=change):
                self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, records, RUN))
        client = self.client()
        records = self.source(client)
        records['dispatches'][RUN + ':9'] = {**records['dispatches'].pop(RUN + ':1'), 'slot': 9}
        records['dispatches'][RUN + ':10'] = {'state': 'interrupted_unknown', 'slot': 10, 'url': HOME, 'task_ids': ['retry']}
        restored = self.client()
        restored.used = set(client.used)
        self.assertIsNone(restored.reuse_discovery(HOME, {'x': task()}, records, RUN))
        self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, records, RUN))
        verified = deepcopy(self.verified)
        verified['receipts'].append(deepcopy(verified['receipts'][0]))
        verified['receipts'][-1].update(status=503, error='server_error')
        client = self.client(verified=verified)
        records = self.source(client)
        self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, records, RUN))
        _, failed = client.fetch(HOME)
        records['dispatches'][RUN + ':2'] = {**records['dispatches'][RUN + ':1'], 'slot': 2, 'receipt': asdict(failed)}
        self.assertIsNone(client.reuse_discovery(HOME, {'x': task()}, records, RUN))

    def test_scheduler_commits_reuse_separately_and_resume_preserves_limits(self):
        def expand(current, page, receipt, tasks, cycle, records):
            return [('tasks', 'later', task())] if current['lab_kind'] == 'list' else []

        def stop(stage):
            if stage == 'after_checkpoint':
                raise PlannedStop()

        with SQLite(self.root / 'store') as store:
            store.commit('initial', [('tasks', 'root', task('list'))])
            with self.assertRaises(PlannedStop):
                collect(store, self.client(), RUN, {HOME}, expand, architecture='B', cycles=1,
                        max_tasks=2, budget=120, hook=stop)
            before = store.snapshot()['records']
            client = self.client()
            client.restore(before)
            result = collect(store, client, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120)
            after = store.snapshot()
            records = after['records']
            self.assertEqual('complete', records['tasks']['later']['lab_status'])
            self.assertEqual([], records['tasks']['later']['lab_attempts'])
            self.assertEqual(1, len(records['tasks']['later']['lab_analysis_reuses']))
            self.assertEqual(7, records['tasks']['later']['attempts'])
            self.assertEqual('2000-01-01T00:00:00Z', records['tasks']['later']['created_at'])
            self.assertEqual(before['scheduler']['collection']['deadline_epoch'], result['scheduling']['control']['deadline_epoch'])
            self.assertEqual(2, result['scheduling']['control']['reserved_resources'])
            self.assertEqual(0, result['scheduling']['control']['confirmed_http_requests'])
            restarted = self.client(); restarted.restore(records)
            final = collect(store, restarted, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120)
            self.assertEqual([], final['receipts'])
            self.assertEqual(after, store.snapshot())

    def test_existing_host_gate_is_not_bypassed_for_parsing_reuse(self):
        with SQLite(self.root / 'store') as store:
            store.commit('initial', [('tasks', 'root', task('list'))])

            def expand(current, page, receipt, tasks, cycle, records):
                client.hosts['www.ark-pc.co.jp'] = {'blocked': True, 'reason': 'fixture_hold'}
                return [('tasks', 'later', task())]

            client = self.client()
            result = collect(store, client, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120)
            self.assertEqual('blocked_hosts', result['scheduling']['stop_reason'])
            self.assertEqual(1, len(result['receipts']))
            self.assertEqual('pending', store.snapshot()['records']['tasks']['later']['lab_status'])

    def test_reuse_reservation_crash_and_deadline_do_not_reset_work_or_claim_success(self):
        for stop_stage in ('after_reservation', 'after_fetch', 'after_checkpoint', 'deadline'):
            with self.subTest(stop_stage=stop_stage), SQLite(self.root / stop_stage) as store:
                store.commit('initial', [('tasks', 'root', task('list'))])
                client = self.client()

                def expand(current, page, receipt, tasks, cycle, records):
                    return [('tasks', 'later', task())] if current['lab_kind'] == 'list' else []

                def stop(stage):
                    if stage == 'after_checkpoint' and store.snapshot()['records']['scheduler']['collection']['reserved_resources'] == 1:
                        raise PlannedStop()

                with self.assertRaises(PlannedStop):
                    collect(store, client, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120, hook=stop)
                original_deadline = store.snapshot()['records']['scheduler']['collection']['deadline_epoch']
                if stop_stage == 'deadline':
                    client.sleep(121)
                    result = collect(store, client, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120)
                    self.assertEqual('budget_exhausted', result['scheduling']['stop_reason'])
                else:
                    def interrupt(stage):
                        if stage == stop_stage:
                            raise PlannedStop()
                    with self.assertRaises(PlannedStop):
                        collect(store, client, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120, hook=interrupt)
                    resumed = self.client(); resumed.restore(store.snapshot()['records'])
                    result = collect(store, resumed, RUN, {HOME}, expand, architecture='B', cycles=1, max_tasks=2, budget=120)
                records = store.snapshot()['records']
                later = records['tasks']['later']
                self.assertEqual(original_deadline, result['scheduling']['control']['deadline_epoch'])
                self.assertEqual(0, result['scheduling']['control']['confirmed_http_requests'])
                self.assertEqual(7, later['attempts'])
                self.assertEqual(int(stop_stage == 'after_checkpoint'), len(later.get('lab_analysis_reuses', [])))
                self.assertEqual(stop_stage == 'after_checkpoint', later['lab_status'] == 'complete')


if __name__ == '__main__':
    unittest.main()
