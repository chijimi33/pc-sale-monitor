"""Bounded offline pipeline coverage: four runs shared by four assertions sets."""
from collections import Counter
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.models import STORES
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, read
from monitor_lab.tests import test_discovery as discovery_fixtures
from monitor_lab.tests.test_pipeline_scheduling import make_inputs


class NoNetworkTransport:
    method = 'coverage-fixture-only'

    def get(self, url, timeout):
        raise AssertionError('Replay must not request the network: ' + url)

    def close(self):
        pass


class CoverageIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = allowed_root() / 'tests' / ('coverage-integration-' + uuid.uuid4().hex)
        cls.inputs = cls.root / 'inputs'
        cls.pages = make_inputs(cls.inputs, duplicate=True)
        cls.originals = read(cls.inputs / 'transport_failure/state/stores/koubou.json')['queue']
        # B represents same-cycle collection. A2/C and the backend matrix are
        # covered elsewhere; this suite spends just four pipeline invocations.
        with patch.dict('monitor_lab.pipeline.TRANSPORTS', {'urllib': NoNetworkTransport}):
            cls.a = cls.collect('a-one-cycle', 'A')
            cls.b = cls.collect('b-one-cycle', 'B')
            cls.resumed = cls.collect('b-one-cycle', 'B')

            cls.home = discovery_fixtures.HOME
            cls.list_url = discovery_fixtures.LIST
            cls.empty_url = 'https://shop.tsukumo.co.jp/search/?keyword=none'
            cls.unseen = cls.home + 'i/99999998/'
            cls.outside = cls.home + 'i/99999999/'
            empty = ('<title>検索結果：none｜ツクモ</title><input name="keyword" value="none">'
                     '<div id="sli_noresult">該当する商品がありませんでした。</div>').encode()
            helper = discovery_fixtures.DiscoveryTest()
            helper.root = cls.root
            bundle = helper.bundle(cls.inputs, [
                ('ark', cls.home, 'home', 200, b'<form action="/search/"><input name="keyword"></form>'),
                ('ark', cls.list_url, 'list', 200,
                 f'<a href="{discovery_fixtures.PRODUCT}">Existing product</a>'
                 f'<a href="{cls.outside}">Outside product</a>'.encode()),
                ('tsukumo', cls.empty_url, 'list', 404, empty),
                # Permitted, but no root or discovered link schedules this URL.
                ('ark', cls.unseen, 'product', 200, b'<h1>Unscheduled product</h1>'),
            ], [
                {'id': 'home-search', 'store': 'ark', 'kind': 'search', 'query': 'fixture',
                 'created_at': discovery_fixtures.DATE},
                {'id': 'empty', 'store': 'tsukumo', 'kind': 'list', 'url': cls.empty_url,
                 'created_at': discovery_fixtures.DATE},
            ])
            cls.discovery = cls.collect('discovery', 'B', discovery_input=bundle)

    @classmethod
    def collect(cls, name, architecture, **kwargs):
        output = cls.root / name
        result = run(cls.inputs, 'transport_failure', architecture, 'replay', output,
                     backend='sqlite', **kwargs)
        pointer = read(output / 'publication/current.json')
        # Capture before a resume overwrites per-invocation result/receipts files.
        return {'result': result, 'coverage_file': read(output / 'coverage.json')
                if (output / 'coverage.json').exists() else None,
                'saved_result': read(output / 'result.json'),
                'receipts': read(output / 'receipts.json'),
                'state': read(output / 'state-export.json'), 'pointer': pointer,
                'payload': read(output / 'publication' / pointer['file']),
                'generations': sorted(p.name for p in (output / 'publication/generations').glob('*.json'))}

    def coverage(self, case):
        self.assertIn('coverage', case['result'])
        coverage = case['result']['coverage']
        self.assertEqual(coverage, case['coverage_file'])
        self.assertEqual(coverage, case['saved_result']['coverage'])
        self.assertEqual(1, coverage['schema'])
        self.assertEqual(10, coverage['monitored_store_count'])
        self.assertEqual(['rakuten'], coverage['excluded_stores'])
        self.assertEqual('replay', coverage['mode'])
        self.assertIs(False, coverage['full_store_coverage_proven'])
        self.assertEqual(set(STORES), set(coverage['stores']))
        self.assertIs(type(coverage['stores_with_current_run_observations']), int)
        for row in coverage['stores'].values():
            self.assertEqual({'product', 'list', 'home'}, set(row['permitted_resources']))
            for urls in [*row['permitted_resources'].values(), row['product_resources_observed'],
                         row['product_resources_missing']]:
                self.assertEqual(sorted(set(urls)), urls)
            self.assertEqual({'expected', 'retained', 'missing', 'complete_in_lab', 'pending_in_lab'},
                             set(row['original_tasks']))
            for field in ('selected_tasks', 'confirmed_http_attempts', 'replay_receipts',
                          'current_run_observations', 'other_run_observations'):
                self.assertIs(type(row[field]), int)
                self.assertGreaterEqual(row[field], 0)
            for field in ('task_status_counts', 'pending_reason_counts', 'dispatch_state_counts', 'field_evidence'):
                self.assertIsInstance(row[field], dict)
        return coverage

    def test_a1_and_b_keep_same_ten_store_scope_despite_different_observations(self):
        a, b = self.coverage(self.a), self.coverage(self.b)
        self.assertEqual({s: r['permitted_resources'] for s, r in a['stores'].items()},
                         {s: r['permitted_resources'] for s, r in b['stores'].items()})
        for case, coverage, observations, observed_stores in ((self.a, a, 2, 2), (self.b, b, 6, 3)):
            with self.subTest(architecture=case['result']['architecture']):
                self.assertEqual(observations, case['result']['observations'])
                self.assertEqual(observed_stores, coverage['stores_with_current_run_observations'])
                for field, expected in {'permitted_resources': 6, 'permitted_product_resources': 6,
                                        'product_resources_observed': observations,
                                        'current_run_observations': observations,
                                        'recorded_dispatches': observations, 'confirmed_http_attempts': 0}.items():
                    self.assertEqual(expected, coverage['totals'][field], field)
                observed_urls = {o['offer']['url'] for o in case['state']['records']['observations'].values()}
                for store, row in coverage['stores'].items():
                    permitted = {p['url'] for p in self.pages if p['store'] == store}
                    self.assertEqual(sorted(permitted), row['permitted_resources']['product'])
                    self.assertEqual([], row['permitted_resources']['home'])
                    self.assertEqual([], row['permitted_resources']['list'])
                    self.assertEqual(sorted(permitted & observed_urls), row['product_resources_observed'])
                    self.assertEqual(sorted(permitted - observed_urls), row['product_resources_missing'])
                    self.assertEqual(len(permitted & observed_urls), row['current_run_observations'])
                    self.assertEqual(len(permitted & observed_urls), row['replay_receipts'])
                    self.assertEqual(0, row['confirmed_http_attempts'])
                    self.assertEqual(0, row['other_run_observations'])
        self.assertTrue(all(d['status'] == 'insufficient' and
                            'known_comparator_not_verified_this_run' in d['reasons']
                            for d in self.a['result']['decisions']))
        self.assertEqual(2, len(self.a['result']['decisions']))

    def test_b_resume_preserves_entire_coverage_with_zero_new_replay(self):
        first, resumed = self.coverage(self.b), self.coverage(self.resumed)
        self.assertEqual(6, self.b['result']['replayed_pages'])
        self.assertEqual(0, self.resumed['result']['replayed_pages'])
        self.assertEqual([], self.resumed['receipts'])
        self.assertEqual(first, resumed)
        self.assertEqual(6, sum(r['replay_receipts'] for r in resumed['stores'].values()))
        for field in ('state', 'pointer', 'payload', 'generations'):
            self.assertEqual(self.b[field], self.resumed[field], field)
        self.assertEqual(1, len(self.resumed['generations']))
        for field in ('decisions', 'basis_decisions', 'basis_counts'):
            self.assertEqual(self.b['result'][field], self.resumed['result'][field], field)

    def test_discovery_nonproduct_receipts_and_empty_result_do_not_inflate_observations(self):
        case = self.discovery
        coverage = self.coverage(case)
        self.assertEqual(6, case['result']['observations'])
        self.assertEqual(9, case['result']['replayed_pages'])
        self.assertEqual(1, case['result']['discovery']['confirmed_empty'])
        self.assertEqual(3, coverage['stores_with_current_run_observations'])
        for field, expected in {'permitted_resources': 10, 'permitted_product_resources': 7,
                                'product_resources_observed': 6, 'current_run_observations': 6,
                                'recorded_dispatches': 9, 'confirmed_http_attempts': 0}.items():
            self.assertEqual(expected, coverage['totals'][field], field)
        ark, tsukumo = coverage['stores']['ark'], coverage['stores']['tsukumo']
        self.assertEqual([self.home], ark['permitted_resources']['home'])
        self.assertEqual([self.list_url], ark['permitted_resources']['list'])
        self.assertEqual([self.empty_url], tsukumo['permitted_resources']['list'])
        self.assertEqual([self.unseen], ark['product_resources_missing'])
        self.assertIn(self.unseen, ark['permitted_resources']['product'])
        self.assertNotIn(self.outside, ark['permitted_resources']['product'])
        self.assertEqual(4, ark['replay_receipts'])
        self.assertEqual(3, tsukumo['replay_receipts'])
        self.assertEqual(2, ark['current_run_observations'])
        self.assertEqual(2, tsukumo['current_run_observations'])
        observed = {url for row in coverage['stores'].values() for url in row['product_resources_observed']}
        self.assertTrue(observed.isdisjoint({self.home, self.list_url, self.empty_url, self.unseen, self.outside}))
        receipts = {r['url']: r for r in case['receipts']}
        self.assertTrue({self.home, self.list_url, self.empty_url} <= set(receipts))
        self.assertNotIn(self.unseen, receipts)
        self.assertNotIn(self.outside, receipts)
        self.assertEqual(404, receipts[self.empty_url]['status'])
        outside = [t for t in case['state']['records']['tasks'].values() if t.get('url') == self.outside]
        self.assertEqual(1, len(outside))
        self.assertEqual('evidence_wait', outside[0]['lab_status'])
        self.assertEqual('discovered_url_outside_selected_resources', outside[0]['lab_reason'])
        self.assertEqual([], outside[0]['lab_attempts'])
        self.assertEqual(1, ark['pending_reason_counts']['discovered_url_outside_selected_resources'])

    def test_original_task_history_and_existing_publication_schema_are_preserved(self):
        fields = {'schema_version', 'mode', 'monitored_store_count', 'excluded_stores',
                  'offers', 'decisions', 'basis_decisions', 'notifications'}
        for case in (self.a, self.b, self.resumed, self.discovery):
            with self.subTest(architecture=case['result']['architecture'],
                              discovery='discovery' in case['result'], replayed=case['result']['replayed_pages']):
                coverage = self.coverage(case)
                records, payload = case['state']['records'], case['payload']
                self.assertEqual(fields, set(payload))
                self.assertEqual(1, payload['schema_version'])
                self.assertEqual('isolated_lab', payload['mode'])
                self.assertEqual(10, payload['monitored_store_count'])
                self.assertEqual(['rakuten'], payload['excluded_stores'])
                self.assertEqual(case['result']['decisions'], payload['decisions'])
                self.assertEqual(case['result']['basis_decisions'], payload['basis_decisions'])
                self.assertCountEqual(list(records['decisions'].values()), payload['decisions'])
                self.assertCountEqual([o['offer'] for o in records['observations'].values()], payload['offers'])
                for key, original in self.originals.items():
                    retained = records['tasks']['koubou:' + key]
                    for field, value in original.items():
                        if not field.startswith('lab_'):
                            self.assertEqual(value, retained[field], field)
                    self.assertEqual(key, retained['lab_key'])
                    self.assertEqual('complete', retained['lab_status'])
                self.assertEqual({'expected': 2, 'retained': 2, 'missing': 0,
                                  'complete_in_lab': 2, 'pending_in_lab': 0},
                                 coverage['stores']['koubou']['original_tasks'])
                for store, row in coverage['stores'].items():
                    tasks = [t for t in records['tasks'].values() if t['lab_store'] == store]
                    self.assertEqual(dict(Counter(t['lab_status'] for t in tasks)), row['task_status_counts'])
                    self.assertEqual(sum(bool(t.get('lab_selected')) for t in tasks), row['selected_tasks'])
                    if store != 'koubou':
                        self.assertTrue(all(value == 0 for value in row['original_tasks'].values()))


if __name__ == '__main__':
    unittest.main()
