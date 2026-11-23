"""Offline comparison refresh integration tests using isolated captured products.

Every study uses fake_study; all tests and fixture creation prohibit networking.
Only new UUID directories below allowed_root()/tests are written.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.models import Offer, timestamp
from monitor_lab.capture import verify_capture
from monitor_lab.comparison import load_comparison, prepare_comparison
from monitor_lab.comparison_apply import apply_comparison
from monitor_lab.comparison_provenance import retain_comparison, validate_comparison
from monitor_lab.evidence import decide, normalize
from monitor_lab.pipeline import run
from monitor_lab.queue_study import CapturedClient
from monitor_lab.safety import allowed_root, atomic_bytes, digest, read, write
from monitor_lab.study import study
from monitor_lab.tests import test_followup as fixture
from monitor_lab.tests.test_capture_pipeline import BODY
from monitor_lab.tests.test_followup import CREATED, PRODUCT
from monitor_lab.tests.test_request_plan_capture import (
    NOW, bundle_hashes, copy_published_capture, fake_study, make_plan,
    no_network, rehash_manifest,
)


COMPARATOR = 'https://www.pc-koubou.jp/products/detail.php?product_id=1051336'
NEW_PRICES = {PRODUCT: 6100, COMPARATOR: 9400}


def product_body(url, price=7777, jan='0195553309745'):
    body = BODY.replace(b'7777', str(price).encode()).replace(b'0195553309745', jan.encode())
    if url == COMPARATOR:
        # Koubou's primary price input is required by its production parser.
        body = body.replace(b'</html>', f'<input id="priceIncTax" value="{price}"></html>'.encode())
    return body


class ComparisonRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with no_network():
            # Reuse the small pinned backlog, including an unrelated old task.
            fixture.FollowupTest.setUpClass.__func__(cls)
            cls.root = allowed_root() / 'tests' / ('cmp-' + uuid.uuid4().hex)
            cls.capture, cls.experiment = cls.root / 'source-capture', cls.root / 'source'
            plan = make_plan([{'store': 'ark', 'url': PRODUCT, 'kind': 'product'},
                              {'store': 'koubou', 'url': COMPARATOR, 'kind': 'product'}])
            write(cls.root / 'plan.json', plan)
            with fake_study({'urllib': {url: (200, {}, product_body(url)) for url in (PRODUCT, COMPARATOR)}}):
                study(cls.capture, methods=['urllib'], plan=cls.root / 'plan.json', budget=120)
                run(cls.inputs, 'transport_failure', 'B', 'replay', cls.experiment,
                    capture_input=cls.capture, candidate_urls=[PRODUCT], budget=120)
            cls.state = read(cls.experiment / 'state-export.json')
            cls.meta = read(cls.experiment / 'experiment.json')
            cls.verified = verify_capture(cls.capture)
            cls.config = cls.verified['settings']['stores']
            tasks = cls.state['records']['tasks']
            cls.candidate_id = next(k for k, t in tasks.items() if t.get('url') == PRODUCT)
            cls.comparator_id = next(k for k, t in tasks.items() if t.get('url') == COMPARATOR)
            if any(tasks[k]['lab_status'] != 'complete' for k in (cls.candidate_id, cls.comparator_id)):
                raise AssertionError('Both source product responses must be captured and normalized')
            if {row['offer']['price_yen'] for row in cls.state['records']['observations'].values()} != {7777}:
                raise AssertionError('Both source products must have the old fixture price')
            cls.source_paths = (cls.inputs, cls.capture, cls.experiment)
            cls.source_hashes = [bundle_hashes(p) for p in cls.source_paths]

    def setUp(self):
        self.enterContext(no_network())
        # Leave room for capture body hashes on Windows without long-path opt-in.
        self.case = self.root / uuid.uuid4().hex[:12]
        self.addCleanup(self.assert_sources_unchanged)

    def assert_sources_unchanged(self):
        self.assertEqual(self.source_hashes, [bundle_hashes(p) for p in self.source_paths])

    def copy_source(self):
        experiment, capture = self.case / 'source', self.case / 'source-capture'
        for name in ('experiment.json', 'state-export.json'):
            atomic_bytes(experiment / name, (self.experiment / name).read_bytes())
        copy_published_capture(self.capture, capture)
        return experiment, capture

    def prepare(self, *, experiment=None, capture=None, output=None, ids=None):
        return prepare_comparison(experiment or self.experiment, capture or self.capture,
                                  output or self.case / 'intent',
                                  [self.candidate_id] if ids is None else ids, created_at=CREATED)

    def captured(self, responses=None, *, experiment=None, source_capture=None):
        intent, capture = self.case / 'intent', self.case / 'capture'
        bundle = self.prepare(experiment=experiment, capture=source_capture)
        responses = responses if responses is not None else {
            url: (200, {}, product_body(url, price)) for url, price in NEW_PRICES.items()}
        with patch('monitor_lab.tests.test_request_plan_capture.NOW', NOW + 600), \
                fake_study({'urllib': responses}) as recording:
            study(capture, methods=['urllib'], comparison=intent, budget=120)
        self.assertEqual({('urllib', url) for url in responses}, set(recording.calls))
        self.assertTrue(all(t.closed and not t.pending for t in recording.transports))
        return intent, capture, bundle

    def assert_lab_only(self, result):
        for field in ('http_requests', 'production_prices_added', 'formal_audits_added',
                      'scheduled_stability_samples', 'notifications'):
            self.assertEqual(0, result[field], field)
        self.assertEqual('not_sent_lab_only', result['decisions'][0]['notification_status'])

    def test_prepare_derives_candidate_and_every_known_product_without_activating_discovery(self):
        bundle = self.prepare()
        self.assertEqual({self.candidate_id, self.comparator_id}, {r['task_id'] for r in bundle['tasks']})
        candidate = bundle['candidates'][0]
        self.assertEqual([self.comparator_id], candidate['product_task_ids'])
        self.assertEqual(self.state['records']['tasks'][self.candidate_id]['lab_identity'], candidate['expected_identity'])
        unresolved = {k: t for k, t in self.state['records']['tasks'].items()
                      if PRODUCT in t.get('lab_dependencies', []) and t['lab_kind'] in {'search', 'list'}
                      and t['lab_status'] != 'complete'}
        self.assertTrue(unresolved)
        self.assertEqual(unresolved, candidate['unresolved_discovery'])
        self.assertEqual({('ark', PRODUCT, 'product'), ('koubou', COMPARATOR, 'product')},
                         {(r['store'], r['url'], r['kind']) for r in bundle['request_plan']['resources']})
        for row in bundle['tasks']:
            self.assertEqual(self.state['records']['tasks'][row['task_id']], row['task'])
            proof = row['product_source']
            self.assertEqual(self.meta['experiment_id'], proof['experiment_id'])
            self.assertEqual(self.state['records']['dispatches'][proof['dispatch_id']], proof['dispatch'])
            self.assertEqual(self.verified['receipts'][proof['dispatch']['receipt']['source_receipt_index']], proof['receipt'])
        for name, field in (('experiment.json', 'experiment_sha256'), ('state-export.json', 'state_export_sha256')):
            self.assertEqual(hashlib.sha256((self.experiment / name).read_bytes()).hexdigest(), bundle['source'][field])
        self.assertEqual(bundle, load_comparison(self.case / 'intent', self.config))

    def test_catalog_also_selects_a_product_without_an_explicit_dependency_edge(self):
        experiment, capture = self.copy_source()
        state = deepcopy(self.state)
        state['records']['tasks'][self.comparator_id]['lab_dependencies'] = []
        state['records'].setdefault('identity_catalog', {})['known'] = {
            'identity': state['records']['tasks'][self.candidate_id]['lab_identity'],
            'store': 'koubou', 'url': COMPARATOR}
        write(experiment / 'state-export.json', state)
        bundle = self.prepare(experiment=experiment, capture=capture)
        self.assertEqual([self.comparator_id], bundle['candidates'][0]['product_task_ids'])
        self.assertEqual(state['records']['identity_catalog'], bundle['candidates'][0]['known_catalog'])

    def test_load_rechecks_source_bytes_and_rehashed_intent_semantics(self):
        experiment, capture = self.copy_source()
        bundle = self.prepare(experiment=experiment, capture=capture)
        for path in (experiment / 'experiment.json', experiment / 'state-export.json', capture / 'capture-manifest.json'):
            before = path.read_bytes()
            with self.subTest(source=path.name):
                try:
                    atomic_bytes(path, before + b'\n')
                    with self.assertRaises(ValueError):
                        load_comparison(self.case / 'intent', self.config)
                finally:
                    atomic_bytes(path, before)
        for field in ('candidates', 'tasks', 'request_plan'):
            changed = deepcopy(bundle)
            if field == 'candidates':
                changed[field][0]['unresolved_discovery'] = {}
            elif field == 'tasks':
                changed[field][0]['task']['lab_attempts'] = []
            else:
                changed[field]['resources'].pop()
                changed[field]['plan_hash'] = digest({k: v for k, v in changed[field].items() if k != 'plan_hash'})
            changed['bundle_hash'] = digest({k: v for k, v in changed.items() if k != 'bundle_hash'})
            write(self.case / 'intent/comparison.json', changed)
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_comparison(self.case / 'intent', self.config)

    def test_changed_capture_body_or_authenticated_dispatch_is_rejected_before_output(self):
        experiment, capture = self.copy_source()
        path = capture / self.verified['receipts'][0]['body_file']
        body = path.read_bytes()
        atomic_bytes(path, b'invented response')
        with self.assertRaises(ValueError):
            self.prepare(experiment=experiment, capture=capture)
        self.assertFalse((self.case / 'intent').exists())
        atomic_bytes(path, body)
        state = deepcopy(self.state)
        dispatch = next(d for d in state['records']['dispatches'].values() if self.candidate_id in d['task_ids'])
        dispatch['receipt']['source_receipt_index'] = 1
        write(experiment / 'state-export.json', state)
        with self.assertRaises(ValueError):
            self.prepare(experiment=experiment, capture=capture)
        self.assertFalse((self.case / 'intent').exists())

    def test_fresh_products_keep_incomplete_search_held_on_both_price_bases(self):
        intent, capture, bundle = self.captured()
        result = apply_comparison(intent, capture, self.case / 'apply')
        self.assertEqual(2, result['new_phase_observations'])
        self.assertEqual(2, result['replayed_actual_attempts'])
        decision = result['decisions'][0]
        self.assertEqual([], decision['missing_product_task_ids'])
        self.assertEqual({}, decision['missing_catalog'])
        self.assertEqual(bundle['candidates'][0]['unresolved_discovery'], decision['unresolved_discovery'])
        for basis in ('payment', 'points', 'selected_decision'):
            self.assertEqual('insufficient', decision[basis]['status'])
            self.assertIsNone(decision[basis]['rule'])
            self.assertIn('comparison_discovery_incomplete', decision[basis]['reasons'])
        self.assert_lab_only(result)

    def test_only_current_capture_bytes_are_normalized_with_the_new_run_identity(self):
        intent, capture, bundle = self.captured()
        verified = verify_capture(capture)
        by_url = {r['url']: (i, r) for i, r in enumerate(verified['receipts'])}
        checksum = hashlib.sha256((capture / 'capture-manifest.json').read_bytes()).hexdigest()
        seen, decisions = [], []

        def checked_normalize(store, page, config, run_id, receipt):
            index, source = by_url[page.url]
            self.assertEqual((capture / source['body_file']).read_bytes(), page.body)
            self.assertEqual(source['observed_at'], page.observed_at)
            self.assertEqual(source['body_sha256'], receipt['body_sha256'])
            self.assertEqual(checksum, receipt['source_capture_manifest_sha256'])
            self.assertEqual(index, receipt['source_receipt_index'])
            self.assertEqual('product', source['resource_kind'])
            seen.append((page.url, run_id))
            return normalize(store, page, config, run_id, receipt)

        def checked_decide(candidate, comparators, history, now, run_id, *, points=False):
            self.assertEqual(NEW_PRICES[PRODUCT], candidate.price_yen)
            self.assertEqual([(COMPARATOR, NEW_PRICES[COMPARATOR], run_id)],
                             [(o.url, o.price_yen, o.observed_run_id) for o in comparators])
            self.assertEqual(run_id, candidate.observed_run_id)
            decisions.append(points)
            return decide(candidate, comparators, history, now, run_id, points=points)

        with patch('monitor_lab.comparison_apply.normalize', side_effect=checked_normalize), \
                patch('monitor_lab.comparison_apply.decide', side_effect=checked_decide):
            result = apply_comparison(intent, capture, self.case / 'apply')
        run_id = result['experiment_id']
        self.assertEqual({(PRODUCT, run_id), (COMPARATOR, run_id)}, set(seen))
        self.assertEqual(2, len(seen))
        self.assertEqual([False, True], decisions)
        self.assertNotEqual(self.meta['experiment_id'], run_id)
        retained = verified['metadata']['comparison']
        self.assertEqual(retain_comparison(bundle), retained)
        self.assertNotIn('experiment_path', retained['intent']['source'])
        self.assertNotIn('capture_path', retained['intent']['source'])
        self.assertEqual(retained, validate_comparison(retained, self.config, bundle['request_plan']))

    def test_failed_candidate_or_comparator_never_falls_back_to_old_phase_observations(self):
        for failed_id, failed_url in ((self.candidate_id, PRODUCT), (self.comparator_id, COMPARATOR)):
            with self.subTest(failed=failed_url):
                self.case = self.root / uuid.uuid4().hex[:12]
                experiment, source_capture = self.copy_source()
                state = deepcopy(self.state)
                old_keys = set()
                for key in (self.candidate_id, self.comparator_id):
                    task = state['records']['tasks'][key]
                    offer_key, observation = next((k, v) for k, v in state['records']['observations'].items()
                                                  if v['offer']['url'] == task['url'])
                    old_key = self.meta['experiment_id'] + ':' + offer_key
                    state['records'].setdefault('phase_observations', {})[old_key] = deepcopy(observation)
                    task['lab_phase_observation'] = old_key
                    old_keys.add(old_key)
                write(experiment / 'state-export.json', state)
                responses = {url: (200, {}, product_body(url, price)) for url, price in NEW_PRICES.items()}
                responses[failed_url] = (404, {}, b'Current product unavailable')
                intent, capture, _ = self.captured(responses, experiment=experiment, source_capture=source_capture)
                with patch('monitor_lab.comparison_apply.decide', wraps=decide) as evaluated:
                    result = apply_comparison(intent, capture, self.case / 'apply')
                decision = result['decisions'][0]
                self.assertEqual(1, result['new_phase_observations'])
                self.assertEqual('waiting', result['selected_statuses'][failed_id])
                self.assertEqual(1, len(decision['observation_keys']))
                self.assertTrue(all(k.startswith(result['experiment_id'] + ':') for k in decision['observation_keys']))
                self.assertFalse(old_keys & set(decision['observation_keys']))
                for basis in ('payment', 'points'):
                    value = decision[basis]
                    self.assertEqual('insufficient', value['status'])
                    if failed_url == PRODUCT:
                        self.assertIsNone(value['offer_key'])
                        self.assertIn('candidate_not_observed_in_run', value['reasons'])
                        self.assertEqual([], value['history_evidence']['sources'])
                    else:
                        self.assertIn('known_comparator_not_verified_this_run', value['reasons'])
                if failed_url == PRODUCT:
                    evaluated.assert_not_called()
                else:
                    self.assertEqual([self.comparator_id], decision['missing_product_task_ids'])
                    self.assertEqual(2, evaluated.call_count)
                    self.assertTrue(all(call.args[1] == [] for call in evaluated.call_args_list))
                final = read(self.case / 'apply/state-export.json')['records']
                self.assertEqual(state['records']['observations'], final['observations'])
                self.assertEqual(state['records']['phase_observations'],
                                 {k: final['phase_observations'][k] for k in old_keys})
                self.assert_lab_only(result)

    def test_inherited_records_tasks_transactions_and_attempts_are_preserved(self):
        intent, capture, bundle = self.captured()
        before = [bundle_hashes(p) for p in (intent, capture)]
        result = apply_comparison(intent, capture, self.case / 'apply')
        state = read(self.case / 'apply/state-export.json')
        selected = set(result['selected_task_ids'])
        self.assertEqual(set(self.state['records']['tasks']), set(state['records']['tasks']))
        for namespace, rows in self.state['records'].items():
            for key, value in rows.items():
                if namespace == 'tasks' and key in selected:
                    task = state['records']['tasks'][key]
                    for field in ('created_at', 'attempts', 'lab_dependencies'):
                        self.assertEqual(value.get(field), task.get(field), (key, field))
                    self.assertEqual(value['lab_attempts'], task['lab_attempts'][:-1])
                    self.assertEqual('complete', task['lab_status'])
                else:
                    self.assertEqual(value, state['records'][namespace][key], (namespace, key))
        self.assertEqual(self.state['transactions'], {k: state['transactions'][k] for k in self.state['transactions']})
        phase = state['records']['comparison_phases'][result['experiment_id']]
        self.assertEqual(bundle, phase['source_bundle'])
        self.assertEqual(before, [bundle_hashes(p) for p in (intent, capture)])

    def test_source_alias_uses_authenticated_resource_without_claiming_prior_completion(self):
        experiment, source_capture = self.copy_source()
        state = deepcopy(self.state)
        alias_id = 'koubou:old-alias'
        alias = deepcopy(state['records']['tasks'][self.comparator_id])
        alias.update(created_at='2001-01-01T00:00:00Z', attempts=13,
                     lab_status='pending', lab_selected=False, lab_attempts=[])
        alias.pop('lab_observation', None)
        alias.pop('lab_phase_observation', None)
        state['records']['tasks'][alias_id] = alias
        write(experiment / 'state-export.json', state)
        intent, capture, bundle = self.captured(experiment=experiment, source_capture=source_capture)
        self.assertEqual({self.candidate_id, self.comparator_id, alias_id}, {r['task_id'] for r in bundle['tasks']})
        self.assertEqual({self.comparator_id, alias_id}, set(bundle['candidates'][0]['product_task_ids']))
        self.assertEqual(2, len(bundle['request_plan']['resources']))
        row = next(r for r in bundle['tasks'] if r['task_id'] == alias_id)
        self.assertEqual(alias, row['task'])
        proof = row['product_source']
        self.assertEqual(self.comparator_id, proof['source_task_id'])
        self.assertEqual('same_product_resource_only_not_alias_completion', proof['scope'])
        self.assertNotIn(alias_id, proof['dispatch']['task_ids'])
        self.assertEqual(state['records']['tasks'][self.comparator_id], proof['source_task'])
        result = apply_comparison(intent, capture, self.case / 'apply', max_tasks=2)
        final = read(self.case / 'apply/state-export.json')['records']
        self.assertEqual(2, result['replayed_actual_attempts'])
        self.assertEqual(2, result['collection']['control']['reserved_resources'])
        self.assertEqual(2, result['new_phase_observations'])
        dispatches = [d for k, d in final['dispatches'].items() if k.startswith(result['experiment_id'] + ':')]
        shared = next(d for d in dispatches if d['url'] == COMPARATOR)
        self.assertEqual({self.comparator_id, alias_id}, set(shared['task_ids']))
        self.assertEqual([], result['decisions'][0]['missing_product_task_ids'])
        after_alias = final['tasks'][alias_id]
        self.assertEqual('complete', after_alias['lab_status'])
        self.assertEqual(13, after_alias['attempts'])
        self.assertEqual(alias['created_at'], after_alias['created_at'])
        self.assertEqual([shared['receipt']], after_alias['lab_attempts'])
        self.assertEqual(alias, read(experiment / 'state-export.json')['records']['tasks'][alias_id])

    def test_changed_fresh_candidate_or_comparator_identity_is_explicitly_held(self):
        for url, reason in ((PRODUCT, 'candidate_identity_changed_or_unverified'),
                            (COMPARATOR, 'known_comparator_identity_changed_or_unverified')):
            with self.subTest(changed=url):
                self.case = self.root / uuid.uuid4().hex[:12]
                responses = {address: (200, {}, product_body(address)) for address in NEW_PRICES}
                responses[url] = (200, {}, product_body(url, jan='4711289500124'))
                intent, capture, _ = self.captured(responses)
                result = apply_comparison(intent, capture, self.case / 'apply')
                self.assertEqual(2, result['new_phase_observations'])
                decision = result['decisions'][0]
                self.assertEqual([], decision['missing_product_task_ids'])
                for basis in ('payment', 'points', 'selected_decision'):
                    self.assertEqual('insufficient', decision[basis]['status'])
                    self.assertIn(reason, decision[basis]['reasons'])
                    self.assertIsNone(decision[basis]['rule'])
                if url == COMPARATOR:
                    self.assertEqual([self.comparator_id], decision['identity_unverified_task_ids'])

    def test_shared_comparator_expiry_survives_both_alias_orders_and_resume(self):
        tsukumo = 'https://shop.tsukumo.co.jp/goods/0195553309745/'
        sofmap = 'https://www.sofmap.com/product_detail.aspx?sku=999'
        prices = {PRODUCT: 10000, COMPARATOR: 12000, tsukumo: 13000, sofmap: 14000}
        stores = {PRODUCT: 'ark', COMPARATOR: 'koubou', tsukumo: 'tsukumo', sofmap: 'sofmap'}
        resources = [{'store': stores[url], 'url': url, 'kind': 'product'} for url in prices]
        plan_path = self.case / 'plan.json'
        write(plan_path, make_plan(resources))
        source_capture, source = self.case / 'seed-capture', self.case / 'seed'
        responses = {url: (200, {}, product_body(url, price)) for url, price in prices.items()}
        with fake_study({'urllib': responses}):
            study(source_capture, methods=['urllib'], plan=plan_path, budget=120)
            run(self.inputs, 'transport_failure', 'B', 'replay', source,
                capture_input=source_capture, candidate_urls=list(prices), budget=120)
        seed = read(source / 'state-export.json')
        ids = {url: next(k for k, task in seed['records']['tasks'].items() if task.get('url') == url)
               for url in prices}
        self.assertEqual({'complete'}, {seed['records']['tasks'][key]['lab_status'] for key in ids.values()})
        now = timestamp('2026-10-04T12:00:00+09:00')
        evidence = {'url': 'https://www.pc-koubou.jp/goods/parts_goods_tokusen.php',
                    'observed_at': '2026-10-01T12:00:00+09:00', 'http_status': 200,
                    'body_sha256': 'e' * 64, 'body_kind': 'http_response',
                    'evidence_mode': 'fixture_replay',
                    'discovered': {'expires_at': '2026-10-02', 'kind': 'comparison',
                                   'title': 'Synthetic expired comparison listing', 'sale_page': False}}

        def valid_product(store, page, config, run_id, receipt):
            # Isolate expiry and A-rule maths from unrelated store-specific gaps.
            # Acquisition, durable collection, alias callbacks and reconciliation stay real.
            observation = normalize(store, page, config, run_id, receipt)
            offer = observation.offer
            for field, value in dict(jan='0195553309745', seller_id=store, condition='new',
                                     variant=None, warranty=None, price_yen=prices[page.url],
                                     shipping_yen=0, discount_yen=0, points_yen=0,
                                     stock='in_stock', verified=True, expires_at=None, issues=[]).items():
                setattr(offer, field, value)
            observation.conflicts = []
            observation.fields['expires_at'].update(value=None, selected_value=None, sources=[])
            self.assertEqual([], offer.errors(timestamp(page.observed_at)))
            return observation

        before_seed = [bundle_hashes(p) for p in (source, source_capture)]
        shared_outcomes = []
        for expired_first in (True, False):
            with self.subTest(expired_alias_first=expired_first):
                case = self.case / ('first' if expired_first else 'last')
                experiment, intent, capture, output = [case / name for name in ('source', 'intent', 'capture', 'apply')]
                atomic_bytes(experiment / 'experiment.json', (source / 'experiment.json').read_bytes())
                state = deepcopy(seed)
                # Locally seed completed discovery and inherited listing evidence;
                # no unrelated discovery hold may conceal an alias overwrite.
                for task in state['records']['tasks'].values():
                    if task.get('lab_kind') in {'search', 'list'}:
                        task['lab_status'] = 'complete'
                for url, key in ids.items():
                    state['records']['tasks'][key].update(
                        lab_role='candidate' if url == PRODUCT else 'comparison',
                        lab_dependencies=[] if url == PRODUCT else [PRODUCT], lab_discovery_evidence=[])
                alias_id = ('0' if expired_first else 'z') + ':expired-alias'
                alias = deepcopy(state['records']['tasks'][ids[COMPARATOR]])
                alias.update(lab_status='pending', lab_selected=False, lab_attempts=[],
                             lab_discovery_evidence=[deepcopy(evidence), deepcopy(evidence)])
                state['records']['tasks'][alias_id] = alias
                write(experiment / 'state-export.json', state)
                bundle = prepare_comparison(experiment, source_capture, intent, [ids[PRODUCT]], created_at=CREATED)
                self.assertEqual(5, len(bundle['tasks']))
                self.assertEqual(4, len(bundle['request_plan']['resources']))
                self.assertEqual({}, bundle['candidates'][0]['unresolved_discovery'])
                with patch('monitor_lab.tests.test_request_plan_capture.NOW', now.timestamp()), \
                        fake_study({'urllib': responses}) as recording:
                    study(capture, methods=['urllib'], comparison=intent, budget=120)
                self.assertEqual(4, len(recording.calls))
                before = [bundle_hashes(p) for p in (experiment, intent, capture)]
                with patch('monitor_lab.comparison_apply.normalize', side_effect=valid_product) as parsed:
                    result = apply_comparison(intent, capture, output, max_tasks=4)
                self.assertEqual(5, parsed.call_count)
                final = read(output / 'state-export.json')
                records = final['records']
                run_id = result['experiment_id']
                self.assertEqual('2026-10-04', result['decision_time'][:10])
                self.assertEqual(4, result['new_phase_observations'])
                self.assertEqual(4, result['replayed_actual_attempts'])
                self.assertEqual({'complete'}, set(result['selected_statuses'].values()))
                shared_dispatch = next(d for k, d in records['dispatches'].items()
                                       if k.startswith(run_id + ':') and d['url'] == COMPARATOR)
                expected_order = [alias_id, ids[COMPARATOR]] if expired_first else [ids[COMPARATOR], alias_id]
                self.assertEqual(expected_order, shared_dispatch['task_ids'])
                shared_key = records['tasks'][alias_id]['lab_phase_observation']
                self.assertEqual(shared_key, records['tasks'][ids[COMPARATOR]]['lab_phase_observation'])
                observation = records['phase_observations'][shared_key]
                self.assertEqual([evidence], observation['discovery_sources'])
                self.assertEqual(timestamp('2026-10-02T23:59:59+09:00'),
                                 timestamp(observation['offer']['expires_at']))
                self.assertEqual(observation['offer']['expires_at'], observation['fields']['expires_at']['selected_value'])
                self.assertEqual(['expired'], Offer.from_dict(observation['offer']).errors(timestamp(result['decision_time'])))
                shared_outcomes.append((observation['offer']['expires_at'], observation['discovery_sources']))
                decision = result['decisions'][0]
                for field in ('missing_product_task_ids', 'identity_unverified_task_ids'):
                    self.assertEqual([], decision[field])
                for field in ('missing_catalog', 'unresolved_discovery'):
                    self.assertEqual({}, decision[field])
                for basis in ('payment', 'points', 'selected_decision'):
                    self.assertEqual('insufficient', decision[basis]['status'])
                    self.assertIsNone(decision[basis]['rule'])
                    self.assertEqual(['comparator_expired'], decision[basis]['reasons'])
                offers = {row['offer']['url']: Offer.from_dict(row['offer'])
                          for key, row in records['phase_observations'].items() if key.startswith(run_id + ':')}
                for points in (False, True):
                    # Without the known expired comparator the two healthy sellers
                    # would accept A: this is the false positive the regression blocks.
                    counterfactual = decide(offers[PRODUCT], [offers[tsukumo], offers[sofmap]], [],
                                            timestamp(result['decision_time']), run_id, points=points)
                    self.assertEqual(('accepted', 'A'), (counterfactual['status'], counterfactual['rule']))
                for key in set(ids.values()) | {alias_id}:
                    original, updated = state['records']['tasks'][key], records['tasks'][key]
                    self.assertEqual(original['lab_discovery_evidence'], updated['lab_discovery_evidence'])
                    self.assertEqual(original['lab_attempts'], updated['lab_attempts'][:-1])
                self.assertEqual(state['transactions'], {k: final['transactions'][k] for k in state['transactions']})
                with patch.object(CapturedClient, 'fetch', side_effect=AssertionError('Resume must not acquire')), \
                        patch('monitor_lab.comparison_apply.normalize', side_effect=AssertionError('Resume must not normalize')):
                    resumed = apply_comparison(intent, capture, output, max_tasks=4)
                self.assertEqual(result['decisions'], resumed['decisions'])
                self.assertEqual(final, read(output / 'state-export.json'))
                self.assertEqual(before, [bundle_hashes(p) for p in (experiment, intent, capture)])
                self.assert_lab_only(result)
        if len(shared_outcomes) == 2:
            self.assertEqual(shared_outcomes[0], shared_outcomes[1])
        self.assertEqual(before_seed, [bundle_hashes(p) for p in (source, source_capture)])

    def test_resource_and_time_budgets_survive_resume_and_reject_changed_conditions(self):
        intent, capture, _ = self.captured()
        for name, limits, reason in (('resources', {'max_tasks': 1}, 'resource_limit'),
                                     ('time', {'budget': .01}, 'budget_exhausted')):
            with self.subTest(limit=name):
                output = self.case / name
                result = apply_comparison(intent, capture, output, **limits)
                self.assertEqual(1, result['replayed_actual_attempts'])
                self.assertEqual(reason, result['collection']['stop_reason'])
                control = result['collection']['control']
                self.assertEqual(1, control['reserved_resources'])
                self.assertAlmostEqual(limits.get('budget', 120), control['deadline_epoch'] - control['started_at_epoch'], places=5)
                state = read(output / 'state-export.json')
                with patch.object(CapturedClient, 'fetch', side_effect=AssertionError('Resume must not acquire again')):
                    resumed = apply_comparison(intent, capture, output, **limits)
                self.assertEqual(state, read(output / 'state-export.json'))
                self.assertEqual(control, resumed['collection']['control'])
                before = bundle_hashes(output)
                for changed in ({**limits, 'budget': 121}, {**limits, 'max_tasks': 2}, {**limits, 'method': 'pooled'}):
                    with self.subTest(changed=changed), self.assertRaises(ValueError):
                        apply_comparison(intent, capture, output, **changed)
                    self.assertEqual(before, bundle_hashes(output))

    def test_interrupted_reservation_is_not_replayed_or_given_a_new_budget(self):
        intent, capture, _ = self.captured()
        output = self.case / 'apply'
        with patch.object(CapturedClient, 'fetch', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            apply_comparison(intent, capture, output, max_tasks=1)
        state = read(output / 'state-export.json')
        run_id = read(output / 'experiment.json')['experiment_id']
        control = state['records']['scheduler']['comparison:' + run_id]
        self.assertEqual('reserved', state['records']['dispatches'][run_id + ':1']['state'])
        with patch.object(CapturedClient, 'fetch', side_effect=AssertionError('Reserved work must not be retried')):
            result = apply_comparison(intent, capture, output, max_tasks=1)
        final = read(output / 'state-export.json')
        self.assertEqual('interrupted_unknown', final['records']['dispatches'][run_id + ':1']['state'])
        self.assertEqual(0, result['replayed_actual_attempts'])
        self.assertEqual(0, result['new_phase_observations'])
        self.assertEqual(control['deadline_epoch'], result['collection']['control']['deadline_epoch'])
        self.assertEqual(1, result['collection']['control']['reserved_resources'])

    def test_output_guard_and_held_products_are_rejected_without_writes(self):
        for target in (allowed_root(), Path(__file__).resolve().parent / 'forbidden-comparison',
                       self.experiment / 'nested', self.capture / 'nested', self.case / 'monitor-data/intent'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.prepare(output=target)
        experiment, capture = self.copy_source()
        for key in (self.candidate_id, self.comparator_id):
            for status in ('waiting', 'external_wait'):
                state = deepcopy(self.state)
                state['records']['tasks'][key]['lab_status'] = status
                write(experiment / 'state-export.json', state)
                with self.subTest(key=key, status=status), self.assertRaisesRegex(ValueError, 'held'):
                    self.prepare(experiment=experiment, capture=capture)
                self.assertFalse((self.case / 'intent').exists())

    def test_new_local_host_gates_are_not_erased_by_preparation(self):
        experiment, capture = self.copy_source()
        for gate in ({'blocked': True, 'reason': 'local hold'}, {'until': NOW + 3600, 'reason': 'local wait'}):
            state = deepcopy(self.state)
            state['records'].setdefault('hosts', {})['www.ark-pc.co.jp'] = gate
            write(experiment / 'state-export.json', state)
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                self.prepare(experiment=experiment, capture=capture)
            self.assertFalse((self.case / 'intent').exists())

    def test_mismatched_capture_intent_and_mixed_study_modes_fail_before_output(self):
        intent, capture, _ = self.captured()
        other = self.case / 'other-intent'
        prepare_comparison(self.experiment, self.capture, other, [self.candidate_id],
                           created_at='2026-10-04T12:35:56+09:00')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            apply_comparison(other, capture, self.case / 'rejected')
        self.assertFalse((self.case / 'rejected').exists())
        for mixed in ({'plan': self.root / 'plan.json'}, {'followup': intent}):
            with self.subTest(mixed=mixed), fake_study({'urllib': {}}) as recording:
                with self.assertRaisesRegex(ValueError, 'not both'):
                    study(self.case / 'mixed', methods=['urllib'], comparison=intent, **mixed)
                self.assertEqual([], recording.calls)
            self.assertFalse((self.case / 'mixed').exists())

    def test_rehashed_malformed_or_mismatched_retained_provenance_is_rejected(self):
        intent, capture, bundle = self.captured()
        retained = retain_comparison(bundle)
        mutations = {
            'duplicate_task': lambda v: v['intent']['tasks'].append(deepcopy(v['intent']['tasks'][0])),
            'duplicate_candidate': lambda v: v['intent']['candidates'].append(deepcopy(v['intent']['candidates'][0])),
            'missing_dependency': lambda v: v['intent']['candidates'][0].update(product_task_ids=[]),
            'wrong_identity': lambda v: v['intent']['candidates'][0].update(expected_identity='invented'),
            'wrong_resource': lambda v: v['intent']['tasks'][0]['task'].update(url=PRODUCT + '?invented'),
            'malformed_candidates': lambda v: v['intent'].update(candidates=None),
            'malformed_catalog': lambda v: v['intent']['candidates'][0].update(known_catalog={'bad': None}),
            'malformed_discovery': lambda v: v['intent']['candidates'][0].update(unresolved_discovery={'bad': None}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                value = deepcopy(retained)
                mutate(value)
                value['provenance_hash'] = digest({k: v for k, v in value.items() if k != 'provenance_hash'})
                # Recompute outer capture checksums too: semantic validation must reject it.
                target = self.case / label
                copy_published_capture(capture, target)
                metadata = read(target / 'study.json')
                metadata['comparison'] = value
                write(target / 'study.json', metadata)
                rehash_manifest(target)
                for check in ('retained_provenance', 'capture'):
                    with self.subTest(check=check), self.assertRaises(ValueError):
                        if check == 'retained_provenance':
                            validate_comparison(value, self.config, bundle['request_plan'])
                        else:
                            verify_capture(target)


if __name__ == '__main__':
    unittest.main()
