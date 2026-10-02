from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
import uuid

from sale_monitor.http import Page
from monitor_lab.evidence import normalize
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.tests.test_history import NOW, add_history, history_inputs, past_row
from monitor_lab.stores import BACKENDS
from monitor_lab.tests.test_events import add_registry, publication, registry_for


def points_inputs(root, *, candidate=2000, comparison=0, price_gap=False, overrides=None,
                  candidate_price=10000, extra_markup=''):
    history_inputs(root, price_gap=price_gap)
    manifest = read(root / 'manifest.json')
    config = read(Path(__file__).resolve().parents[2] / 'config/sources.json')['stores']
    offers = {}
    for row in manifest['pages']:
        points = (overrides or {}).get(row['name'], candidate if row['name'] == 'koubou-b550' else comparison)
        value = (str(points) + 'ポイント') if type(points) is int else points
        path = root / row['path']
        body = path.read_bytes()
        if row['name'] == 'koubou-b550':
            body = body.replace(b'"price": 10000', ('"price": ' + str(candidate_price)).encode())
            body = body.replace(b'value="10000"', ('value="' + str(candidate_price) + '"').encode())
            body += extra_markup.encode()
        if value is not None:
            body += ('<table><tr><th>ポイント</th><td>' + value + '</td></tr></table>').encode()
        path.write_bytes(body)
        row['body_sha256'] = hashlib.sha256(body).hexdigest()
        offers[row['name']] = normalize(row['store'], Page(row['url'], body, NOW), config[row['store']],
                                       'lab-fixture', {'body_sha256': row['body_sha256']}).offer
    manifest.pop('input_hash')
    manifest['input_hash'] = digest(manifest)
    write(root / 'manifest.json', manifest)
    return offers


class PointsTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('points-' + uuid.uuid4().hex)
        self.inputs, self.output = self.root / 'inputs', self.root / 'output'

    def test_points_only_A_qualification_reaches_event_and_current_evidence(self):
        offers = points_inputs(self.inputs)
        self.assertEqual(2000, offers['koubou-b550'].points_yen)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        selected = next(d for d in result['decisions'] if d['offer_key'] == offers['koubou-b550'].key)
        self.assertEqual('accepted', selected['status'])
        self.assertEqual('points', selected['basis'])
        self.assertEqual('A', selected['rule'])
        notice = publication(self.output)['notifications'][0]
        self.assertEqual('insufficient', notice['current_evidence']['payment']['status'])
        self.assertEqual('accepted', notice['current_evidence']['points']['status'])
        self.assertEqual(10000, notice['current_evidence']['offer']['price_yen'])

    def test_points_B_uses_historical_effective_prices_not_payment_prices(self):
        offers = points_inputs(self.inputs, candidate=1000, comparison=1000)
        old = past_row(offers['koubou-b550'], price=10000)
        old['offer']['points_yen'] = 0
        add_history(self.inputs, [old])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        selected = next(d for d in result['decisions'] if d['offer_key'] == offers['koubou-b550'].key)
        self.assertEqual('B_observed_year_low', selected['rule'])
        self.assertEqual('points', selected['basis'])
        self.assertEqual(10000, selected['history']['minimum_yen'])
        self.assertEqual({9000}, {c['price_yen'] for c in selected['comparisons']})

    def bases(self, result, offers):
        return next(r for r in result['basis_decisions'] if r['offer_key'] == offers['koubou-b550'].key)

    def test_unknown_candidate_points_do_not_become_zero_or_block_payment(self):
        offers = points_inputs(self.inputs, candidate=None, price_gap=True)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual('accepted', bases['payment']['status'])
        self.assertEqual('insufficient', bases['points']['status'])
        self.assertIn('points_unknown', bases['points']['reasons'])
        self.assertIsNone(publication(self.output)['notifications'][0]['current_evidence']['offer']['points_yen'])

    def test_unknown_current_comparator_points_hold_B_without_substituting_history(self):
        offers = points_inputs(self.inputs, candidate=1000, overrides={'ark-b550': None})
        old = past_row(offers['ark-b550'], price=10000)
        old['offer']['points_yen'] = 0
        add_history(self.inputs, [old])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        points = self.bases(result, offers)['points']
        self.assertEqual('insufficient', points['status'])
        self.assertIn('comparator_points_unknown', points['reasons'])
        self.assertTrue(all(c['url'] != offers['ark-b550'].url for c in points['comparisons']))
        self.assertEqual(1, points['history_evidence']['eligible_rows'])
        self.assertEqual([], publication(self.output)['notifications'])

    def test_payment_preferred_when_accepted_even_if_points_find_a_cheaper_competitor(self):
        offers = points_inputs(self.inputs, candidate=0, comparison=5000, price_gap=True)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual('accepted', bases['payment']['status'])
        self.assertEqual('rejected', bases['points']['status'])
        self.assertEqual(['cheaper_current_offer'], bases['points']['reasons'])
        self.assertEqual('payment', bases['selected_basis'])
        evidence = publication(self.output)['notifications'][0]['current_evidence']
        self.assertEqual(bases['payment'], evidence['payment'])
        self.assertEqual(bases['points'], evidence['points'])

    def test_conditional_points_are_preserved_without_becoming_a_discount(self):
        offers = points_inputs(self.inputs, candidate='2,000ポイント（有料会員限定）', price_gap=True)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual('accepted', bases['payment']['status'])
        self.assertEqual('insufficient', bases['points']['status'])
        observations = read(self.output / 'state-export.json')['records']['observations']
        row = observations[offers['koubou-b550'].key]
        self.assertIsNone(row['offer']['points_yen'])
        self.assertIn('有料会員限定', row['offer']['conditional_points'][0]['text'])
        self.assertEqual('conditions_unverified', row['fields']['points_yen']['status'])
        self.assertEqual(row['receipt']['body_sha256'], row['fields']['points_yen']['sources'][0]['body_sha256'])
        self.assertEqual(10000, bases['payment']['reference_yen'] - bases['payment']['difference_yen'])

    def test_conflicting_point_sources_do_not_invalidate_independent_payment(self):
        markup = ('<table><tr><th>型番</th><td>SYNTHETIC-1</td></tr></table><script>'
                  'eccube.classCategories = {"1":{"1":{"stock_find":true,"product_code":"SYNTHETIC-1",'
                  '"price02":"10000","point":"2000"}}};</script>')
        offers = points_inputs(self.inputs, candidate=1000, price_gap=True, extra_markup=markup)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual('accepted', bases['payment']['status'])
        self.assertEqual('insufficient', bases['points']['status'])
        row = read(self.output / 'state-export.json')['records']['observations'][offers['koubou-b550'].key]
        self.assertEqual(2000, row['fields']['points_yen']['value'])
        self.assertIsNone(row['fields']['points_yen']['selected_value'])
        self.assertEqual('conflict', row['fields']['points_yen']['status'])
        self.assertIn('points_conflict_review_needed', row['conflicts'])
        self.assertEqual([], row['offer']['issues'])

    def test_unknown_historical_points_have_separate_eligibility_and_citations(self):
        offers = points_inputs(self.inputs, candidate=1000)
        old = past_row(offers['koubou-b550'], price=10000)
        old['offer']['points_yen'] = None
        source = add_history(self.inputs, [old])
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual(1, bases['payment']['history_evidence']['eligible_rows'])
        self.assertEqual(0, bases['points']['history_evidence']['eligible_rows'])
        ref = bases['points']['history_evidence']['sources'][0]
        self.assertIn('historical_points_unknown', ref['reasons'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), ref['source_sha256'])
        self.assertEqual(old['observation_id'], ref['observation_id'])

    def test_matching_variants_with_different_points_remain_unconfirmed(self):
        markup = ('<table><tr><th>型番</th><td>SYNTHETIC-1</td></tr></table><script>'
                  'eccube.classCategories = {"1":{"1":{"stock_find":true,"product_code":"SYNTHETIC-1",'
                  '"price02":"10000","point":"2000"},"2":{"stock_find":true,"product_code":"SYNTHETIC-1",'
                  '"price02":"10000","point":"0"}}};</script>')
        offers = points_inputs(self.inputs, candidate=None, price_gap=True, extra_markup=markup)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual('accepted', bases['payment']['status'])
        self.assertEqual('insufficient', bases['points']['status'])
        row = read(self.output / 'state-export.json')['records']['observations'][offers['koubou-b550'].key]
        self.assertEqual(2000, row['fields']['points_yen']['value'])
        self.assertIsNone(row['offer']['points_yen'])
        self.assertEqual('conflict', row['fields']['points_yen']['status'])
        source = next(s for s in row['fields']['points_yen']['sources'] if s['kind'] == 'matching_product_variants')
        self.assertEqual(['2000', '0'], source['points'])

    def test_points_median_uses_effective_daily_minima_with_original_span_rule(self):
        offers = points_inputs(self.inputs, candidate=1000, comparison=1000)
        rows = []
        for day, points in [('2026-09-01', 0), ('2026-09-15', 0), ('2026-09-30', 1000)]:
            row = past_row(offers['koubou-b550'], date=day + 'T20:00:00Z', price=10000)
            row['offer']['points_yen'] = points
            rows.append(row)
        add_history(self.inputs, rows)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertNotEqual('accepted', bases['payment']['status'])
        self.assertEqual('B_recent_median', bases['points']['rule'])
        self.assertEqual(10000, bases['points']['history']['median_yen'])
        self.assertEqual(9000, bases['points']['history']['minimum_yen'])
        self.assertEqual(3, bases['points']['history']['days'])

    def test_points_only_improvement_preserves_delivered_event_and_emits_new_id(self):
        offers = points_inputs(self.inputs)
        old = deepcopy(offers['koubou-b550'])
        old.points_yen = 1000
        registry = registry_for(old, status='delivered')
        registry['states'][old.key]['facts']['basis'] = 'points'
        next(iter(registry['events'].values()))['decision']['basis'] = 'points'
        add_registry(self.inputs, registry)
        run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        records = read(self.output / 'state-export.json')['records']
        for key, value in registry['events'].items():
            self.assertEqual(value, records['events'][key])
        notices = publication(self.output)['notifications']
        self.assertEqual(['points_improved'], [n['kind'] for n in notices])
        self.assertEqual('points', notices[0]['decision']['basis'])
        self.assertEqual('accepted', notices[0]['current_evidence']['points']['status'])

    def test_lost_points_evidence_does_not_reset_prior_qualification_when_payment_rejected(self):
        offers = points_inputs(self.inputs, candidate=None, candidate_price=11000)
        old = deepcopy(offers['koubou-b550'])
        old.points_yen = 2000
        registry = registry_for(old, price=11000, status='delivered')
        registry['states'][old.key]['facts']['basis'] = 'points'
        next(iter(registry['events'].values()))['decision']['basis'] = 'points'
        add_registry(self.inputs, registry)
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.output)
        bases = self.bases(result, offers)
        self.assertEqual('rejected', bases['payment']['status'])
        self.assertEqual('insufficient', bases['points']['status'])
        records = read(self.output / 'state-export.json')['records']
        self.assertEqual(registry['states'][old.key], records['event_state'][old.key])
        self.assertEqual(registry['events'], records['events'])
        self.assertEqual([], publication(self.output)['notifications'])
        recovered_inputs = self.root / 'recovered-inputs'
        points_inputs(recovered_inputs, candidate=2000, candidate_price=11000)
        add_registry(recovered_inputs, {'states': records['event_state'], 'events': records['events']})
        recovered_output = self.root / 'recovered-output'
        result = run(recovered_inputs, 'transport_failure', 'B', 'replay', recovered_output)
        self.assertEqual('accepted', self.bases(result, offers)['points']['status'])
        self.assertEqual(registry['events'], read(recovered_output / 'state-export.json')['records']['events'])
        self.assertEqual([], publication(recovered_output)['notifications'])

    def test_points_A_and_B_boundaries_still_use_existing_thresholds(self):
        for points, history, expected in [(999, False, None), (1000, False, 'A'),
                                           (499, True, None), (500, True, 'B_observed_year_low')]:
            folder = self.root / str(points)
            offers = points_inputs(folder / 'inputs', candidate=points)
            if history:
                row = past_row(offers['koubou-b550'], price=10000)
                row['offer']['points_yen'] = 0
                add_history(folder / 'inputs', [row])
            result = run(folder / 'inputs', 'transport_failure', 'B', 'replay', folder / 'output')
            self.assertEqual(expected, self.bases(result, offers)['points']['rule'])

    def test_points_acceptance_and_both_bases_survive_resume_on_every_backend(self):
        points_inputs(self.inputs)
        for backend, cls in BACKENDS.items():
            output = self.root / backend
            run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            before = read(output / 'state-export.json')
            notices = publication(output)
            result = run(self.inputs, 'transport_failure', 'B', 'replay', output, backend=backend)
            self.assertEqual(0, result['replayed_pages'])
            self.assertEqual(before, read(output / 'state-export.json'))
            self.assertEqual(notices, publication(output))
            self.assertEqual(1, len(before['records']['events']))
            self.assertEqual('points', next(iter(before['records']['events'].values()))['decision']['basis'])
