import unittest
from unittest.mock import patch
import uuid

from monitor_lab.capture import verify_capture
from monitor_lab.followup import prepare_followup
from monitor_lab.followup_apply import apply_followup
from monitor_lab.pipeline import run
from monitor_lab.queue_study import CapturedClient
from monitor_lab.safety import read, write
from monitor_lab.study import study
from monitor_lab.tests.test_capture_pipeline import BODY
from monitor_lab.tests import test_followup as fixture
from monitor_lab.tests.test_followup import HOME, PRODUCT, SEARCH, OUTSIDE, JAN, CREATED
from monitor_lab.tests.test_request_plan_capture import bundle_hashes, fake_study, make_plan, no_network, NOW


class FollowupApplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FollowupTest.setUpClass.__func__(cls)

    def setUp(self):
        self.enterContext(no_network())
        self.case = self.root / uuid.uuid4().hex

    def captured(self, ids=None, responses=None):
        intent = self.case / 'intent'
        bundle = prepare_followup(self.experiment, self.capture, intent,
                                  ids or [self.search_id], created_at=CREATED)
        capture = self.case / 'capture'
        responses = responses or {SEARCH: (200, {}, f'<li><a href="{OUTSIDE}">Ordinary motherboard</a></li>'.encode())}
        with fake_study({'urllib': responses}):
            study(capture, methods=['urllib'], followup=intent, budget=120)
        return intent, capture, bundle

    def test_selected_original_query_runs_with_prior_receipt_indexes_and_all_history_preserved(self):
        intent, capture, bundle = self.captured()
        before = [bundle_hashes(p) for p in (self.experiment, self.capture, intent, capture)]
        output = self.case / 'apply'
        result = apply_followup(intent, capture, output)
        state = read(output / 'state-export.json')
        records = state['records']
        self.assertEqual(1, result['replayed_actual_attempts'])
        self.assertEqual({self.search_id: 'complete'}, result['selected_statuses'])
        self.assertEqual(0, result['http_requests'])
        self.assertEqual(JAN, records['tasks'][self.search_id]['query'])
        self.assertEqual('comparison', records['tasks'][self.search_id]['kind'])
        self.assertEqual(self.state['records']['tasks'][self.search_id]['created_at'], records['tasks'][self.search_id]['created_at'])
        self.assertEqual(self.original, {k: records['tasks'][self.product_id][k] for k in self.original})
        self.assertEqual(self.state['records']['tasks'][self.product_id]['lab_role'], records['tasks'][self.product_id]['lab_role'])
        self.assertEqual('evidence_wait', records['tasks'][self.product_id]['lab_status'])
        self.assertIn(self.product_id, records['tasks'][self.search_id]['lab_child_keys'])
        # Same-URL sale parsing is not silently activated by comparison scope.
        self.assertEqual(self.state['records']['tasks'][self.campaign_id], records['tasks'][self.campaign_id])
        self.assertEqual(set(self.state['records']['tasks']), set(records['tasks']))
        for namespace in ('observations', 'decisions', 'events', 'event_state', 'source_files', 'hosts'):
            self.assertEqual(self.state['records'].get(namespace), records.get(namespace))
        self.assertEqual(self.state['records']['scheduler']['collection'], records['scheduler']['collection'])
        self.assertEqual(self.state['transactions'], {k: state['transactions'][k] for k in self.state['transactions']})
        resumed = apply_followup(intent, capture, output)
        self.assertEqual(1, resumed['replayed_actual_attempts'])
        self.assertEqual(state, read(output / 'state-export.json'))
        self.assertEqual(before, [bundle_hashes(p) for p in (self.experiment, self.capture, intent, capture)])

    def test_existing_product_keeps_authenticated_discoveries_from_both_phases(self):
        intent, capture, _ = self.captured()
        output = self.case / 'apply'
        apply_followup(intent, capture, output)
        value = prepare_followup(output, capture, self.case / 'next', [self.product_id], created_at=CREATED)
        selected = value['tasks'][0]
        self.assertEqual(self.original, {k: selected['task'][k] for k in self.original})
        proofs = selected['source_records']
        self.assertTrue(any('source_context' in proof for proof in proofs))
        self.assertTrue(any('source_context' not in proof for proof in proofs))
        self.assertEqual(set(selected['task']['lab_parent_keys']),
                         set().union(*(set(proof['parent_keys']) for proof in proofs)))
        state = read(output / 'state-export.json')
        phase = read(output / 'experiment.json')['experiment_id']
        state['records']['followup_phases'][phase]['source_bundle']['source']['state_export_sha256'] = '0' * 64
        write(output / 'state-export.json', state)
        with self.assertRaisesRegex(ValueError, 'ancestry'):
            prepare_followup(output, capture, self.case / 'tampered', [self.product_id], created_at=CREATED)
        self.assertFalse((self.case / 'tampered').exists())

    def test_expired_translated_wait_is_authenticated_through_the_ancestor(self):
        failed = HOME + '&fixture=failed'
        plan = make_plan([{'store': 'sofmap', 'url': HOME, 'kind': 'home'},
                          {'store': 'sofmap', 'url': failed, 'kind': 'list'},
                          {'store': 'ark', 'url': PRODUCT, 'kind': 'product'}])
        plan_path, old_capture, old_run = [self.case / name for name in ('plan.json', 'old-capture', 'old-run')]
        write(plan_path, plan)
        home_body = (f'<li>SALE SSD<a href="{OUTSIDE}">SALE motherboard</a></li>'
                     '<form action="/contents/" method="get"><input name="keyword"></form>').encode()
        with fake_study({'urllib': {HOME: (200, {}, home_body),
                                   failed: (503, {}, b'temporary'), PRODUCT: (200, {}, BODY)}}):
            study(old_capture, methods=['urllib'], plan=plan_path, budget=120)
            run(self.inputs, 'transport_failure', 'B', 'replay', old_run,
                capture_input=old_capture, candidate_urls=[PRODUCT], budget=120)
        state = read(old_run / 'state-export.json')
        task_id = next(k for k, t in state['records']['tasks'].items() if t.get('url') == SEARCH)
        gate = state['records']['hosts']['www.sofmap.com']
        self.assertTrue(gate['simulation_translation'])
        intent, capture, output = [self.case / name for name in ('intent', 'capture', 'apply')]
        prepare_followup(old_run, old_capture, intent, [task_id], created_at=CREATED)
        with patch('monitor_lab.tests.test_request_plan_capture.NOW', NOW + 600), fake_study({'urllib': {
                SEARCH: (200, {}, f'<a href="{OUTSIDE}">Ordinary motherboard</a>'.encode())}}):
            study(capture, methods=['urllib'], followup=intent, budget=120)
        apply_followup(intent, capture, output)
        self.assertEqual(gate, read(output / 'state-export.json')['records']['hosts']['www.sofmap.com'])
        followup = prepare_followup(output, capture, self.case / 'next', [self.product_id], created_at=CREATED)
        self.assertEqual(verify_capture(old_capture)['hosts']['www.sofmap.com'], followup['inherited_host_gates']['www.sofmap.com'])

    def test_products_keep_old_observations_and_use_a_separate_phase(self):
        intent, capture, _ = self.captured([self.product_id], {OUTSIDE: (200, {}, BODY)})
        result = apply_followup(intent, capture, self.case / 'apply')
        records = read(self.case / 'apply/state-export.json')['records']
        self.assertEqual(1, result['new_phase_observations'])
        self.assertEqual(self.state['records'].get('observations'), records.get('observations'))
        self.assertEqual(self.original, {k: records['tasks'][self.product_id][k] for k in self.original})
        key = records['tasks'][self.product_id]['lab_phase_observation']
        observation = records['phase_observations'][key]
        self.assertEqual(7777, observation['offer']['price_yen'])
        self.assertTrue(key.startswith(result['experiment_id'] + ':'))

    def test_continued_queue_can_prepare_and_apply_its_next_verified_dependency(self):
        next_url = SEARCH + '&page=2'
        intent, capture, _ = self.captured(responses={SEARCH: (200, {}, f'<a rel="next" href="{next_url}">Next</a>'.encode())})
        first = self.case / 'first'
        result = apply_followup(intent, capture, first)
        self.assertEqual(1, len(result['new_task_ids']))
        child_id = result['new_task_ids'][0]
        next_intent, next_capture = self.case / 'next-intent', self.case / 'next-capture'
        value = prepare_followup(first, capture, next_intent, [child_id], created_at=CREATED)
        self.assertEqual(JAN, value['tasks'][0]['task']['query'])
        self.assertEqual([self.search_id], value['tasks'][0]['task']['lab_parent_keys'])
        self.assertEqual([{'store': 'sofmap', 'url': next_url, 'kind': 'list'}], value['request_plan']['resources'])
        new_product = OUTSIDE + '0'
        with fake_study({'urllib': {next_url: (200, {}, f'<a href="{new_product}">Ordinary motherboard</a>'.encode())}}):
            study(next_capture, methods=['urllib'], followup=next_intent, budget=120)
        second = self.case / 'second'
        applied = apply_followup(next_intent, next_capture, second)
        records = read(second / 'state-export.json')['records']
        self.assertEqual('complete', records['tasks'][child_id]['lab_status'])
        new_id = applied['new_task_ids'][0]
        self.assertEqual(new_product, records['tasks'][new_id]['url'])
        self.assertEqual('comparison', records['tasks'][new_id]['lab_role'])
        self.assertEqual('evidence_wait', records['tasks'][new_id]['lab_status'])
        self.assertEqual(self.state['records'].get('observations'), records.get('observations'))
        self.assertEqual(read(first / 'state-export.json')['records']['scheduler'],
                         {k: records['scheduler'][k] for k in read(first / 'state-export.json')['records']['scheduler']})
        # A query edited only on a list child cannot acquire unrelated results.
        changed = read(first / 'state-export.json')
        changed['records']['tasks'][child_id]['query'] = 'wrong-query'
        write(first / 'state-export.json', changed)
        with self.assertRaisesRegex(ValueError, 'not derived'):
            prepare_followup(first, capture, self.case / 'bad-query', [child_id], created_at=CREATED)

    def test_failed_http_and_parser_errors_never_complete_selected_task(self):
        for name, response in [('denied', (403, {}, b'access denied')), ('unparsed', (200, {}, b'unknown layout'))]:
            with self.subTest(name=name):
                self.case = self.root / uuid.uuid4().hex
                intent, capture, _ = self.captured(responses={SEARCH: response})
                if name == 'unparsed':
                    with self.assertRaises(ValueError):
                        apply_followup(intent, capture, self.case / 'apply')
                else:
                    result = apply_followup(intent, capture, self.case / 'apply')
                    self.assertEqual('waiting', result['selected_statuses'][self.search_id])
                records = read(self.case / 'apply/state-export.json')['records']
                self.assertEqual('waiting', records['tasks'][self.search_id]['lab_status'])
                self.assertEqual(self.state['records'].get('observations'), records.get('observations'))
                if name == 'denied':
                    self.assertTrue(records['hosts']['www.sofmap.com']['blocked'])

    def test_different_capture_intent_and_changed_sources_rejected_before_output(self):
        intent, capture, _ = self.captured()
        other = self.case / 'other'
        prepare_followup(self.experiment, self.capture, other, [self.campaign_id], created_at=CREATED)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            apply_followup(other, capture, self.case / 'rejected')
        self.assertFalse((self.case / 'rejected').exists())
        value = read(intent / 'followup.json')
        value['tasks'][0]['task']['query'] = 'changed'
        write(intent / 'followup.json', value)
        with self.assertRaises(ValueError):
            apply_followup(intent, capture, self.case / 'changed')
        self.assertFalse((self.case / 'changed').exists())

    def test_resource_cap_and_resume_do_not_reset_prior_or_phase_budgets(self):
        intent, capture, _ = self.captured([self.search_id, self.product_id], {
            SEARCH: (200, {}, f'<li><a href="{OUTSIDE}">Ordinary motherboard</a></li>'.encode()), OUTSIDE: (200, {}, BODY)})
        output = self.case / 'apply'
        result = apply_followup(intent, capture, output, max_tasks=1)
        self.assertEqual(1, result['replayed_actual_attempts'])
        self.assertEqual('resource_limit', result['collection']['stop_reason'])
        self.assertEqual(1, result['collection']['control']['reserved_resources'])
        before = read(output / 'state-export.json')
        apply_followup(intent, capture, output, max_tasks=1)
        self.assertEqual(before, read(output / 'state-export.json'))
        with self.assertRaisesRegex(ValueError, 'changed follow-up conditions'):
            apply_followup(intent, capture, output, max_tasks=2)

    def test_crash_after_reservation_remains_unknown_without_consuming_old_capture(self):
        intent, capture, _ = self.captured()
        output = self.case / 'apply'
        with patch.object(CapturedClient, 'fetch', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                apply_followup(intent, capture, output, max_tasks=1)
        state = read(output / 'state-export.json')
        phase = read(output / 'experiment.json')['experiment_id']
        self.assertEqual('reserved', state['records']['dispatches'][phase + ':1']['state'])
        result = apply_followup(intent, capture, output, max_tasks=1)
        self.assertEqual(0, result['replayed_actual_attempts'])
        state = read(output / 'state-export.json')
        self.assertEqual('interrupted_unknown', state['records']['dispatches'][phase + ':1']['state'])
        self.assertEqual('waiting', state['records']['tasks'][self.search_id]['lab_status'])

    def test_restore_scopes_receipt_indexes_clock_and_waits_to_the_phase(self):
        _, capture, _ = self.captured()
        client = CapturedClient(capture, verify_capture(capture), 'urllib')
        start = client.now
        records = {'dispatches': {
            'old:1': {'receipt': {'source_capture_manifest_sha256': 'f'*64, 'source_receipt_index': 0},
                      'started_at_epoch': start + 999, 'finished_at_epoch': start + 1000},
            'new:1': {'receipt': {'source_capture_manifest_sha256': client.checksum, 'source_receipt_index': 0},
                      'started_at_epoch': start + 1, 'finished_at_epoch': start + 2}},
            'scheduler_waits': {'old:1': {'started_at_epoch': start + 2000}, 'new:1': {'started_at_epoch': start + 3}}}
        client.restore(records, run_id='new')
        self.assertEqual({0}, client.used)
        self.assertEqual(start + 3, client.now)


if __name__ == '__main__':
    unittest.main()
