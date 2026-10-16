from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import unittest
import uuid

from monitor_lab.acquire import Receipt
from monitor_lab.capture import Capture, verify_capture
from monitor_lab.queue_study import CapturedClient, queue_study
from monitor_lab.safety import allowed_root, read
from monitor_lab.stores import SQLite
from monitor_lab.tests.test_pipeline_scheduling import make_inputs

NOW = 1791000000.0


class QueueStudyTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('queue-study-' + uuid.uuid4().hex)
        self.inputs, self.capture = self.root / 'inputs', self.root / 'capture'
        self.pages = make_inputs(self.inputs)
        self.failed = next(p for p in self.pages if p['name'] == 'tsukumo-b550')
        self.skipped = next(p for p in self.pages if p['name'] == 'tsukumo-capture')
        self.success = next(p for p in self.pages if p['name'] == 'ark-b550')
        settings = read(Path(__file__).resolve().parents[2] / 'config/sources.json')
        capture = Capture(self.capture, {'experiment_id': 'lab-synthetic-trace', 'mode': 'fixture'}, settings)
        gate = {'until': NOW + 61, 'reason': 'transport:RemoteDisconnected'}
        for kind, source in [('failure', self.failed), ('skipped', self.skipped), ('success', self.success)]:
            body = (self.inputs / source['path']).read_bytes() if kind == 'success' else None
            receipt = Receipt(source['url'], 200 if body else None,
                              datetime.fromtimestamp(NOW + (2 if body else 1), timezone.utc).isoformat(),
                              hashlib.sha256(body).hexdigest() if body else None, 'urllib', {'fixture': True},
                              attempts=[] if kind == 'skipped' else [{'started_at': NOW, 'sequence': 1}],
                              error=None if body else ('shared_host_wait' if kind == 'skipped' else 'transport:RemoteDisconnected'),
                              elapsed_seconds=0 if kind == 'skipped' else 1, evidence_mode='fixture',
                              body_bytes=len(body) if body else None, waits=[gate] if kind == 'skipped' else [])
            if body:
                receipt.body_file = capture.body(receipt, body)
            capture.append({'store': source['store'], **asdict(receipt)}, {'shop.tsukumo.co.jp': gate})
        capture.checkpoint(result={'experiment_id': 'lab-synthetic-trace', 'mode': 'fixture', 'receipts': capture.receipts})

    def test_skipped_source_and_unknown_retry_are_not_http_outcomes(self):
        verified = verify_capture(self.capture)
        client = CapturedClient(self.capture, verified, 'urllib')
        page, receipt = client.fetch(self.skipped['url'])
        self.assertIsNone(page)
        self.assertEqual('evidence_exhausted', receipt.error)
        self.assertEqual([], receipt.attempts)
        self.assertIsNone(receipt.source_receipt_index)
        self.assertEqual({}, client.hosts)
        _, failure = client.fetch(self.failed['url'])
        self.assertEqual(verified['receipts'][0]['attempts'], failure.attempts)
        self.assertEqual(NOW + 61, client.hosts['shop.tsukumo.co.jp']['until'])
        client.sleep(65)
        _, gap = client.fetch(self.failed['url'])
        self.assertEqual('evidence_exhausted', gap.error)
        self.assertEqual(0, client.requests)

    def test_simulation_keeps_source_timestamp_bytes_and_request_record(self):
        verified = verify_capture(self.capture)
        client = CapturedClient(self.capture, verified, 'urllib')
        client.sleep(40)
        page, receipt = client.fetch(self.success['url'])
        original = verified['receipts'][2]
        self.assertEqual(original['observed_at'], page.observed_at)
        self.assertEqual(original['attempts'], receipt.attempts)
        self.assertEqual(original['environment'], receipt.environment)
        self.assertEqual(original['body_sha256'], hashlib.sha256(page.body).hexdigest())
        self.assertEqual('fixture', receipt.source_evidence_mode)
        self.assertEqual('captured_queue_simulation', receipt.evidence_mode)
        self.assertEqual(NOW + 41, client.clock())

    def test_integrated_study_and_resume_do_not_invent_missing_successes(self):
        before = {str(p.relative_to(self.capture)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.capture.rglob('*') if p.is_file()}
        output = self.root / 'study'
        result = queue_study(self.inputs, 'transport_failure', self.capture, output)
        self.assertEqual(0, result['http_requests'])
        self.assertEqual(2, result['replayed_actual_attempts'])
        self.assertEqual(1, result['replayed_failed_requests'])
        self.assertEqual(1, result['observations'])
        self.assertEqual(5, result['evidence_gaps'])
        with SQLite(output / 'store') as store:
            first_state = store.snapshot()
        resumed = queue_study(self.inputs, 'transport_failure', self.capture, output)
        with SQLite(output / 'store') as store:
            self.assertEqual(first_state, store.snapshot())
        self.assertEqual([], resumed['scheduling']['dispatches_this_invocation'])
        self.assertEqual(before, {str(p.relative_to(self.capture)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in self.capture.rglob('*') if p.is_file()})
