from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from monitor_lab.capture import Capture, verify_capture
from monitor_lab.followup_provenance import retain_followup, validate_provenance, verify_inherited_gates
from monitor_lab.safety import allowed_root, digest, read
from monitor_lab.study import study
from monitor_lab.tests.test_request_plan_capture import fake_study, make_plan, no_network, NOW

HOME = 'https://www.ark-pc.co.jp/'
LIST = HOME + 'search/?keyword=fixture'
OTHER = 'https://shop.tsukumo.co.jp/special/fixture/'
CONFIG = read(Path(__file__).resolve().parents[2] / 'config/sources.json')['stores']


def bundle(gates=None):
    plan = make_plan([{'store': 'ark', 'url': LIST, 'kind': 'list'},
                      {'store': 'tsukumo', 'url': OTHER, 'kind': 'list'}])
    value = {'format': 'pc-sale-monitor-followup-v1', 'created_at': plan['created_at'],
        'source': {'experiment_id': 'lab-source', 'experiment_path': 'local/source', 'capture_path': 'local/capture',
                   'experiment_sha256': '1'*64, 'state_export_sha256': '2'*64, 'capture_manifest_sha256': '3'*64,
                   'backlog_data_sha': '4'*40, 'capture_source_method': 'urllib'},
        'request_plan': plan, 'tasks': [{'task_id': 'fixture:query1'}, {'task_id': 'fixture:query2'}],
        'inherited_host_gates': gates or {}}
    return {**value, 'bundle_hash': digest(value)}


class FollowupStudyTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('followup-study-' + uuid.uuid4().hex)
        self.enterContext(no_network())

    def test_followup_has_separate_current_receipts_and_portable_original_provenance(self):
        intent = bundle()
        before = deepcopy(intent)
        with patch('monitor_lab.followup.load_followup', return_value=intent), fake_study({'urllib': {
                LIST: (200, {}, b'<html>New search</html>'), OTHER: (200, {}, b'<html>New list</html>')}}):
            result = study(self.root / 'new-capture', methods=['urllib'], followup=self.root / 'prepared', budget=120)
        verified = verify_capture(self.root / 'new-capture')
        provenance = verified['metadata']['followup']
        self.assertEqual(retain_followup(intent), provenance)
        self.assertNotIn('experiment_path', provenance['intent']['source'])
        self.assertNotIn('capture_path', provenance['intent']['source'])
        self.assertEqual(2, result['http_navigation_attempts'])
        self.assertEqual(0, result['scope']['stores']['ark']['successful_product_responses'])
        self.assertNotEqual('lab-source', result['experiment_id'])
        self.assertTrue(all(row['evidence_mode'] == 'live' and not row.get('analysis_reuse') for row in verified['receipts']))
        self.assertEqual(before, intent)

    def test_new_study_preserves_original_block_or_future_wait_before_any_transport_call(self):
        for name, gate in (('blocked', {'blocked': True, 'reason': 'authentication_or_challenge', 'status': 403}),
                           ('wait', {'until': NOW + 600, 'reason': 'retry_after', 'status': 429})):
            intent = bundle({'www.ark-pc.co.jp': gate})
            with self.subTest(name=name), patch('monitor_lab.followup.load_followup', return_value=intent), fake_study({'urllib': {
                    OTHER: (200, {}, b'<html>Other store</html>')}}) as fixture:
                result = study(self.root / name, methods=['urllib'], followup=self.root / 'prepared', budget=120)
            first = result['receipts'][0]
            self.assertEqual('shared_host_wait', first['error'])
            self.assertEqual([], first['attempts'])
            self.assertEqual([gate], first['waits'])
            self.assertEqual(1, result['http_navigation_attempts'])
            self.assertEqual(gate, verify_capture(self.root / name)['hosts']['www.ark-pc.co.jp'])

    def test_expired_original_wait_allows_new_request_without_resetting_record(self):
        gate = {'until': NOW - 1, 'reason': 'transport:TimeoutError'}
        with patch('monitor_lab.followup.load_followup', return_value=bundle({'www.ark-pc.co.jp': gate})), fake_study({'urllib': {
                LIST: (200, {}, b'<html>New query</html>'), OTHER: (200, {}, b'<html>Other query</html>')}}):
            result = study(self.root / 'expired', methods=['urllib'], followup=self.root / 'prepared', budget=120)
        self.assertEqual(2, result['http_navigation_attempts'])
        self.assertEqual(gate, verify_capture(self.root / 'expired')['metadata']['followup']['intent']['inherited_host_gates']['www.ark-pc.co.jp'])

    def test_invalid_followup_or_mixed_plan_is_rejected_before_output_and_transport(self):
        with patch('monitor_lab.followup.load_followup', side_effect=ValueError('Source changed')), \
                patch('monitor_lab.study.TRANSPORTS', {'urllib': unittest.mock.Mock(side_effect=AssertionError('No transport'))}):
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                study(self.root / 'bad', methods=['urllib'], followup=self.root / 'prepared')
        with self.assertRaisesRegex(ValueError, 'not both'):
            study(self.root / 'mixed', methods=['urllib'], plan=self.root / 'plan', followup=self.root / 'prepared')
        self.assertFalse((self.root / 'bad').exists())
        self.assertFalse((self.root / 'mixed').exists())

    def test_portable_scope_hash_and_original_host_gate_validation_fail_closed(self):
        intent = bundle({'www.ark-pc.co.jp': {'blocked': True, 'reason': 'fixture'}})
        retained = retain_followup(intent)
        self.assertEqual(retained, validate_provenance(retained, CONFIG, intent['request_plan']))
        changed = deepcopy(retained); changed['intent']['tasks'].append({'task_id': 'invented'})
        with self.assertRaisesRegex(ValueError, 'checksum'):
            validate_provenance(changed, CONFIG, intent['request_plan'])
        for gate in ({'until': float('inf'), 'reason': 'fixture'}, {'until': True, 'reason': 'fixture'},
                     {'blocked': True, 'reason': 'fixture', 'simulation_translation': True}):
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                changed_intent = bundle({'www.ark-pc.co.jp': gate})
                validate_provenance(retain_followup(changed_intent), CONFIG, changed_intent['request_plan'])
        with self.assertRaisesRegex(ValueError, 'inside an inherited host wait'):
            verify_inherited_gates(retained, [{'url': LIST, 'attempts': [{'started_at': NOW}]}])
        changed = deepcopy(intent['request_plan'])
        changed['resources'][0]['url'] += '2'
        changed.pop('plan_hash'); changed['plan_hash'] = digest(changed)
        with self.assertRaisesRegex(ValueError, 'scope differs'):
            validate_provenance(retained, CONFIG, changed)


if __name__ == '__main__':
    unittest.main()
