from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import unittest
import uuid

from monitor_lab.evidence import decide
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.stores import BACKENDS
from monitor_lab.tests.test_events import add_registry, publication, registry_for
from monitor_lab.tests.test_history import NOW as PAGE_NOW, add_history, past_row
from monitor_lab.tests.test_lab import NOW, offer
from monitor_lab.tests.test_points import points_inputs


def stock_inputs(root, *, observed_at=PAGE_NOW):
    offers = points_inputs(root, candidate=0, comparison=0)
    add_history(root, [past_row(offers['koubou-b550'])])
    manifest = read(root / 'manifest.json')
    row = next(p for p in manifest['pages'] if p['name'] == 'tsukumo-b550')
    path = root / row['path']
    body = path.read_bytes().replace(b'https://schema.org/InStock', b'https://schema.org/OutOfStock')
    path.write_bytes(body)
    row.update(body_sha256=hashlib.sha256(body).hexdigest(), observed_at=observed_at)
    manifest.pop('input_hash')
    manifest['input_hash'] = digest(manifest)
    write(root / 'manifest.json', manifest)
    return offers


class ComparatorStockTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('comparator-stock-' + uuid.uuid4().hex)

    def comparison(self, *, history=False):
        candidate = offer('koubou', 10000)
        first, second, unavailable = offer('ark', 13000), offer('dospara', 14000), offer('tsukumo', 9000)
        unavailable.stock = 'out_of_stock'
        for row in (candidate, first, second, unavailable):
            row.points_yen = 0
        if history:
            old = deepcopy(candidate)
            old.price_yen, old.observed_at, old.observed_run_id = 13000, '2026-09-01T20:05:00Z', 'old-run'
            return candidate, [first, unavailable], [{'offer': old.to_dict()}]
        return candidate, [first, second, unavailable], []

    def test_stale_future_missing_and_invalid_stock_observations_hold_both_rules_and_bases(self):
        for historical in (False, True):
            for points in (False, True):
                for date in (None, 'not-a-date', (NOW - timedelta(hours=12, microseconds=1)).isoformat(),
                             (NOW + timedelta(microseconds=1)).isoformat()):
                    with self.subTest(history=historical, points=points, date=date):
                        candidate, others, history = self.comparison(history=historical)
                        others[-1].observed_at = date
                        result = decide(candidate, others, history, NOW, 'lab-run', points=points)
                        self.assertEqual('insufficient', result['status'])
                        self.assertIsNone(result['rule'])
                        self.assertIn('comparator_stale_observation', result['reasons'])

    def test_unverified_conflicting_and_stale_source_stock_does_not_release_comparison(self):
        for changes, reason in (({'verified': False}, 'unverified_product'),
                                ({'evidence': []}, 'unverified_product'),
                                ({'issues': ['stock_conflict_review_needed']}, 'stock_conflict_review_needed'),
                                ({'source_updated_at': '2026-09-01T20:05:00Z'}, 'stale_source'),
                                ({'shipping_yen': None}, 'shipping_unknown')):
            with self.subTest(reason=reason, changes=changes):
                candidate, others, history = self.comparison()
                for key, value in changes.items():
                    setattr(others[-1], key, value)
                result = decide(candidate, others, history, NOW, 'lab-run')
                self.assertEqual('insufficient', result['status'])
                self.assertIn('comparator_' + reason, result['reasons'])
                self.assertNotIn('comparator_stock_out_of_stock', result['reasons'])

    def test_current_verified_out_of_stock_is_excluded_without_using_its_price_or_points(self):
        for historical in (False, True):
            for points in (False, True):
                candidate, others, history = self.comparison(history=historical)
                others[-1].observed_at = (NOW - timedelta(hours=12)).isoformat()
                others[-1].points_yen = None
                result = decide(candidate, others, history, NOW, 'lab-run', points=points)
                self.assertEqual('accepted', result['status'])
                self.assertEqual('B_observed_year_low' if historical else 'A', result['rule'])
                self.assertNotIn(others[-1].url, [r['url'] for r in result['comparisons']])

    def test_wrong_run_unavailable_comparator_stays_held_without_mutating_inputs(self):
        candidate, others, history = self.comparison()
        others[-1].observed_run_id = 'previous'
        before = deepcopy([candidate, others, history])
        result = decide(candidate, others, history, NOW, 'lab-run')
        self.assertEqual('insufficient', result['status'])
        self.assertIn('known_comparator_not_verified_this_run', result['reasons'])
        self.assertEqual(before, [candidate, others, history])

    def test_stale_stock_pipeline_holds_events_and_resumes_without_fetch_on_all_backends(self):
        inputs = self.root / 'inputs'
        offers = stock_inputs(inputs, observed_at='2026-09-30T20:00:00Z')
        registry = registry_for(offers['koubou-b550'])
        add_registry(inputs, registry)
        input_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs.rglob('*') if p.is_file()}
        for backend, cls in BACKENDS.items():
            output = self.root / backend
            result = run(inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            bases = next(d for d in result['basis_decisions'] if d['offer_key'] == offers['koubou-b550'].key)
            for basis in ('payment', 'points'):
                self.assertEqual('insufficient', bases[basis]['status'])
                self.assertIn('comparator_stale_observation', bases[basis]['reasons'])
            self.assertEqual(6, result['observations'])
            self.assertEqual(0, result['http_navigation_attempts'])
            before = read(output / 'state-export.json')
            self.assertEqual(registry['events'], before['records']['events'])
            self.assertEqual(registry['states'][offers['koubou-b550'].key],
                             before['records']['event_state'][offers['koubou-b550'].key])
            self.assertEqual([], publication(output)['notifications'])
            resumed = run(inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            self.assertEqual(0, resumed['replayed_pages'])
            with cls(output / 'store') as store:
                self.assertEqual(before, store.snapshot())
        self.assertTrue(all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h
                            for p, h in input_hashes.items()))

    def test_verified_stock_pipeline_keeps_B_eligibility_for_both_bases(self):
        inputs, output = self.root / 'inputs', self.root / 'output'
        offers = stock_inputs(inputs)
        result = run(inputs, 'transport_failure', 'B', 'replay', output)
        bases = next(d for d in result['basis_decisions'] if d['offer_key'] == offers['koubou-b550'].key)
        for basis in ('payment', 'points'):
            self.assertEqual('accepted', bases[basis]['status'])
            self.assertEqual('B_observed_year_low', bases[basis]['rule'])
            self.assertEqual([offers['ark-b550'].url], [c['url'] for c in bases[basis]['comparisons']])
        self.assertEqual(1, len(publication(output)['notifications']))

    def test_explicitly_different_product_configuration_is_not_a_missing_comparator(self):
        for attribute, candidate_value, other_value in (('variant', 'single', 'two modules'),
                                                       ('condition', 'new', 'used'),
                                                       ('warranty', 'one year', '90 days')):
            candidate, others, history = self.comparison()
            for row in (candidate, *others[:-1]):
                setattr(row, attribute, candidate_value)
            setattr(others[-1], attribute, other_value)
            others[-1].observed_at = '2026-09-01T20:05:00Z'
            self.assertEqual('accepted', decide(candidate, others, history, NOW, 'lab-run')['status'])

    def test_unavailable_candidate_is_never_accepted(self):
        candidate, others, history = self.comparison()
        candidate.stock = 'out_of_stock'
        result = decide(candidate, others, history, NOW, 'lab-run')
        self.assertEqual('insufficient', result['status'])
        self.assertIn('stock_out_of_stock', result['reasons'])
