from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.http import Page
from sale_monitor.models import Offer, timestamp
from monitor_lab.evidence import normalize
from monitor_lab.history import HistoryIndex
from monitor_lab.inputs import import_state
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.stores import BACKENDS
from monitor_lab.tests.test_pipeline_scheduling import make_inputs

NOW = '2026-10-01T20:00:00+00:00'


def history_inputs(root, *, price_gap=False):
    pages = make_inputs(root)
    config = read(Path(__file__).resolve().parents[2] / 'config/sources.json')['stores']
    observations = {}
    for row in pages:
        price = 13000 if price_gap and row['group'] == 'b550' and row['store'] != 'koubou' else 10000
        schema = {'@context': 'https://schema.org', '@type': 'Product', 'name': 'Synthetic ' + row['group'],
                  'gtin13': '0195553309745' if row['group'] == 'b550' else '4711289500124',
                  'offers': {'price': price, 'priceCurrency': 'JPY', 'availability': 'https://schema.org/InStock',
                             'itemCondition': 'https://schema.org/NewCondition',
                             'shippingDetails': {'shippingRate': {'value': 0, 'currency': 'JPY'}}}}
        body = ('<h1>Synthetic</h1><script type="application/ld+json">' + json.dumps(schema) + '</script>'
                '<input id="priceIncTax" value="' + str(price) + '"><table><tr><th>送料</th><td>無料</td></tr></table>').encode()
        (root / row['path']).write_bytes(body)
        row['body_sha256'] = hashlib.sha256(body).hexdigest()
        observations[row['name']] = normalize(row['store'], Page(row['url'], body, NOW), config[row['store']],
                                              'lab-fixture', {'body_sha256': row['body_sha256']}).offer
        assert not observations[row['name']].errors(timestamp(NOW))
    manifest = read(root / 'manifest.json')
    manifest['pages'] = pages
    manifest.pop('input_hash')
    manifest['input_hash'] = digest(manifest)
    write(root / 'manifest.json', manifest)
    return observations


def add_history(root, rows, name='state/history/koubou/2026-09-01.json'):
    manifest = read(root / 'manifest.json')
    path = root / 'transport_failure' / name
    write(path, rows)
    body = path.read_bytes()
    relative = 'transport_failure/' + name
    files = manifest['snapshots']['transport_failure']['files']
    files[:] = [r for r in files if r['path'] != relative]
    files.append({'path': relative, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
    manifest.pop('input_hash')
    manifest['input_hash'] = digest(manifest)
    write(root / 'manifest.json', manifest)
    return path


def past_row(offer, *, date='2026-09-01T20:00:00+00:00', price=13000, identity=None):
    offer = deepcopy(offer)
    offer.observed_at, offer.observed_run_id, offer.price_yen = date, 'prior-real-or-fixture-run', price
    row = {'run_id': offer.observed_run_id, 'offer': offer.to_dict()}
    row['observation_id'] = identity or digest(row)
    return row


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('history-' + uuid.uuid4().hex)
        self.inputs = self.root / 'inputs'

    def test_pipeline_uses_preserved_history_for_B_without_old_current_comparisons(self):
        offers = history_inputs(self.inputs)
        add_history(self.inputs, [past_row(offers['koubou-b550'])])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output')
        decision = next(d for d in result['decisions'] if d['candidate_url'] == offers['koubou-b550'].url)
        self.assertEqual('accepted', decision['status'])
        self.assertEqual('B_observed_year_low', decision['rule'])
        self.assertEqual(13000, decision['history']['minimum_yen'])
        self.assertTrue(all(c['observed_at'] == NOW and c['price_yen'] == 10000 for c in decision['comparisons']))
        self.assertEqual(0, result['http_navigation_attempts'])
        source = decision['history_evidence']['sources'][0]
        original = self.inputs / 'transport_failure' / source['source_file']
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), source['source_sha256'])
        self.assertEqual(read(original)[source['row_index']]['observation_id'], source['observation_id'])

    def test_history_only_known_competitor_requires_current_observation(self):
        offers = history_inputs(self.inputs, price_gap=True)
        old = deepcopy(offers['koubou-b550'])
        old.store = old.seller_id = 'sofmap'
        old.product_id = '999'
        old.url = 'https://www.sofmap.com/product_detail.aspx?sku=999'
        add_history(self.inputs, [past_row(old, price=9000)], 'state/history/sofmap/2026-09-01.json')
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output')
        decision = next(d for d in result['decisions'] if d['candidate_url'] == offers['koubou-b550'].url)
        self.assertEqual('insufficient', decision['status'])
        self.assertIn('known_comparator_not_verified_this_run', decision['reasons'])
        self.assertFalse(any(c['url'] == old.url for c in decision['comparisons']))

    def decision(self, result, offers):
        return next(d for d in result['decisions'] if d['candidate_url'] == offers['koubou-b550'].url)

    def test_A_first_cycle_does_not_substitute_history_for_missing_current_comparisons(self):
        offers = history_inputs(self.inputs)
        add_history(self.inputs, [past_row(offers['ark-b550'])])
        result = run(self.inputs, 'transport_failure', 'A', 'replay', self.root / 'output')
        decision = self.decision(result, offers)
        self.assertEqual('insufficient', decision['status'])
        self.assertEqual([], decision['comparisons'])
        self.assertIn('B_current_comparison_missing', decision['reasons'])
        self.assertEqual(1, decision['history_evidence']['eligible_rows'])

    def test_future_equal_time_and_invalid_past_rows_are_explained_and_not_used(self):
        offers = history_inputs(self.inputs)
        invalid = past_row(offers['koubou-b550'])
        invalid['offer']['shipping_yen'] = None
        add_history(self.inputs, [invalid, past_row(offers['koubou-b550'], date=NOW),
                                 past_row(offers['koubou-b550'], date='2026-10-02T00:00:00+00:00')])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output')
        decision = self.decision(result, offers)
        self.assertEqual('insufficient', decision['status'])
        evidence = decision['history_evidence']
        self.assertEqual(0, evidence['eligible_rows'])
        self.assertEqual(3, evidence['excluded_rows'])
        self.assertEqual(2, evidence['exclusion_reasons']['not_before_decision_time'])
        self.assertEqual(1, evidence['exclusion_reasons']['shipping_unknown'])

    def test_past_expiry_is_checked_at_its_observation_time(self):
        offers = history_inputs(self.inputs)
        old = past_row(offers['koubou-b550'])
        old['offer']['expires_at'] = '2026-09-02T00:00:00+00:00'
        add_history(self.inputs, [old])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output')
        self.assertEqual('B_observed_year_low', self.decision(result, offers)['rule'])
        self.assertEqual(1, self.decision(result, offers)['history_evidence']['eligible_rows'])

    def test_configuration_difference_does_not_create_a_false_comparator_hold(self):
        offers = history_inputs(self.inputs, price_gap=True)
        old = deepcopy(offers['koubou-b550'])
        old.store = old.seller_id = 'sofmap'
        old.url = 'https://www.sofmap.com/product_detail.aspx?sku=999'
        old.variant = 'two-module bundle'
        add_history(self.inputs, [past_row(old, price=9000)])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output')
        decision = self.decision(result, offers)
        self.assertEqual('A', decision['rule'])
        self.assertEqual({'product_configuration_mismatch': 1}, decision['history_evidence']['exclusion_reasons'])
        tasks = read(self.root / 'output/state-export.json')['records']['tasks'].values()
        self.assertFalse(any(t.get('url') == old.url for t in tasks))

    def test_recent_median_uses_distinct_days_and_the_existing_span_rule(self):
        for name, dates, rule in [
            ('three-days', ['2026-09-01', '2026-09-15', '2026-09-30'], 'B_recent_median'),
            ('repeated-day', ['2026-09-01', '2026-09-01', '2026-09-30'], None),
            ('too-short', ['2026-09-10', '2026-09-15', '2026-09-30'], None),
        ]:
            with self.subTest(name=name):
                inputs = self.root / name / 'inputs'
                offers = history_inputs(inputs)
                rows = [past_row(offers['koubou-b550'], date=date + f'T{18 + i}:00:00+00:00', price=10000 if i == 2 else 13000)
                        for i, date in enumerate(dates)]
                add_history(inputs, rows)
                result = run(inputs, 'transport_failure', 'B', 'replay', self.root / name / 'output')
                decision = self.decision(result, offers)
                self.assertEqual(rule, decision['rule'])
                if rule:
                    self.assertEqual(3, decision['history']['days'])
                    self.assertEqual(13000, decision['history']['median_yen'])
                else:
                    self.assertIn('B_history_insufficient', decision['reasons'])

    def test_same_observation_in_two_files_counts_once_and_retains_both_sources(self):
        offers = history_inputs(self.inputs)
        row = past_row(offers['koubou-b550'])
        one = add_history(self.inputs, [row])
        two = add_history(self.inputs, [row], 'state/history/koubou/2026-09-02.json')
        before = {p: p.read_bytes() for p in (one, two)}
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output')
        self.assertEqual(1, self.decision(result, offers)['history']['samples'])
        self.assertEqual(2, result['history_input']['counts']['rows'])
        self.assertEqual(1, result['history_input']['counts']['duplicate_rows'])
        self.assertEqual(1, len(result['history_input']['duplicates']))
        self.assertEqual(before, {p: p.read_bytes() for p in (one, two)})

    def test_conflicting_observation_identity_fails_before_collection(self):
        offers = history_inputs(self.inputs)
        row = past_row(offers['koubou-b550'])
        conflict = deepcopy(row)
        conflict['offer']['price_yen'] = 999
        add_history(self.inputs, [row, conflict])
        output = self.root / 'refused'
        with self.assertRaisesRegex(ValueError, 'Conflicting historical observation identity'):
            run(self.inputs, 'transport_failure', 'B', 'replay', output)
        self.assertFalse(output.exists())

    def test_bad_history_container_does_not_become_an_empty_history(self):
        history_inputs(self.inputs)
        add_history(self.inputs, {'corrupt_shape': []})
        output = self.root / 'refused'
        with self.assertRaisesRegex(ValueError, 'observation list'):
            run(self.inputs, 'transport_failure', 'B', 'replay', output)
        self.assertFalse(output.exists())

    def test_source_is_rechecked_when_matching_rows_are_loaded(self):
        offers = history_inputs(self.inputs)
        source = add_history(self.inputs, [past_row(offers['koubou-b550'])])
        files, _, _ = import_state(self.inputs, 'transport_failure')
        index = HistoryIndex(files, timestamp(NOW))
        self.assertEqual(1, len(index.select(offers['koubou-b550'], timestamp(NOW), 'lab-run')[0]))
        source.write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'Historical source changed'):
            index.select(offers['koubou-b550'], timestamp(NOW), 'lab-run')

    def test_current_run_observation_is_not_a_historical_sample(self):
        offers = history_inputs(self.inputs)
        row = past_row(offers['koubou-b550'])
        row['run_id'] = 'lab-same'
        add_history(self.inputs, [row])
        files, _, _ = import_state(self.inputs, 'transport_failure')
        index = HistoryIndex(files, timestamp(NOW))
        rows, evidence = index.select(offers['koubou-b550'], timestamp(NOW), 'lab-same')
        self.assertEqual([], rows)
        self.assertEqual(1, evidence['exclusion_reasons']['current_run_not_history'])

    def test_B_acceptance_resume_is_idempotent_on_all_storage_backends(self):
        offers = history_inputs(self.inputs)
        source = add_history(self.inputs, [past_row(offers['koubou-b550'])])
        original = source.read_bytes()
        for backend, cls in BACKENDS.items():
            output = self.root / backend
            first = run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            self.assertEqual('B_observed_year_low', self.decision(first, offers)['rule'])
            with cls(output / 'store') as store:
                before = store.snapshot()
            resumed = run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            with cls(output / 'store') as store:
                self.assertEqual(before, store.snapshot())
            self.assertEqual(0, resumed['replayed_pages'])
            self.assertEqual(1, len(before['records']['events']))
            self.assertEqual(original, source.read_bytes())

    def test_history_preparation_consumes_the_shared_collection_budget(self):
        offers = history_inputs(self.inputs)
        add_history(self.inputs, [past_row(offers['koubou-b550'])])
        def slow_history(*args):
            time.sleep(0.08)
            return HistoryIndex(*args)
        with patch('monitor_lab.pipeline.HistoryIndex', side_effect=slow_history):
            result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'output', budget=0.05)
        self.assertEqual(0, result['replayed_pages'])
        self.assertEqual(0, result['observations'])
        self.assertEqual('budget_exhausted', result['scheduling']['stop_reason'])
        self.assertGreaterEqual(result['history_index_seconds'], 0.08)

    def test_history_source_identity_is_portable_across_input_directories(self):
        offers = history_inputs(self.inputs)
        add_history(self.inputs, [past_row(offers['koubou-b550'])])
        elsewhere = self.root / 'relocated-inputs'
        shutil.copytree(self.inputs, elsewhere)
        one = HistoryIndex(import_state(self.inputs, 'transport_failure')[0], timestamp(NOW))
        two = HistoryIndex(import_state(elsewhere, 'transport_failure')[0], timestamp(NOW))
        self.assertEqual(one.summary()['source_set_hash'], two.summary()['source_set_hash'])
        self.assertEqual(one.select(offers['koubou-b550'], timestamp(NOW), 'lab-run'),
                         two.select(offers['koubou-b550'], timestamp(NOW), 'lab-run'))


if __name__ == '__main__':
    unittest.main()
