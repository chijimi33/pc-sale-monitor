from copy import deepcopy
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.http import Page
from sale_monitor.parsing import confirmed_empty_search
from monitor_lab.discovery import Dispatcher, FORMAT, load_bundle, root_changes
from monitor_lab.pipeline import make_task, run
from monitor_lab.queueing import expand_shared_list
from monitor_lab.scheduler import collect
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.stores import BACKENDS, SQLite
from monitor_lab.tests.test_pipeline_scheduling import Clock, make_inputs
from monitor_lab.tests.test_scheduler import client_for, NOW

HOME = 'https://www.ark-pc.co.jp/'
LIST = HOME + 'search/?keyword=fixture'
PRODUCT = HOME + 'i/12201487/'
CFG = {'product_patterns': [r'/i/\d+/'], 'pc_only': True, 'adapter': 'html', 'seed_urls': [HOME]}
DATE = '2020-01-01T00:00:00+00:00'


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('discovery-' + uuid.uuid4().hex)

    def test_scheduler_executes_selected_list_and_its_product_dependency(self):
        root = make_task('ark', LIST, DATE, role='comparison')
        root.update(lab_kind='list', type='list', kind='comparison')
        client = client_for(Clock(NOW), {LIST: [(200, {}, f'<a href="{PRODUCT}">Motherboard</a>'.encode())],
                                        PRODUCT: [(200, {}, b'<h1>Product</h1>')]})
        def on_page(task, page, receipt, tasks, cycle, records):
            if task['lab_kind'] == 'list':
                children, evidence = expand_shared_list([task], page, CFG)
                return [('tasks', 'child', {**make_task('ark', PRODUCT, children[0]['created_at']), **children[0]})]
            return [('observations', PRODUCT, {'url': page.url})]
        with SQLite(self.root / 'store') as store:
            store.commit('import', [('tasks', 'root', root)])
            collect(store, client, 'lab-discovery', {LIST, PRODUCT}, on_page,
                    architecture='B', cycles=1, max_tasks=20, budget=120)
            state = store.snapshot()['records']
        self.assertEqual([LIST, PRODUCT], [c['url'] for c in client.transport.calls])
        self.assertIn(PRODUCT, state['observations'])

    def test_list_children_keep_discovery_title_expiry_and_source(self):
        root = make_task('ark', LIST, DATE)
        root.update(lab_kind='list', type='list', kind='comparison')
        page = Page(LIST, b'<li e-date="2026-10-05"><a href="/i/12201487/">Motherboard fixture</a></li>', DATE)
        children, evidence = expand_shared_list([root], page, CFG)
        self.assertEqual('2026-10-05', children[0]['expires_at'])
        self.assertEqual('Motherboard fixture', children[0]['title'])
        self.assertEqual(LIST, children[0]['source'])

    def collect_graph(self, tasks, responses, *, name='graph', allowed=None, cap=20, hook=lambda stage: None):
        client = client_for(Clock(NOW), responses)
        allowed = set(responses) if allowed is None else allowed
        dispatcher = Dispatcher({'ark': CFG}, allowed)
        def parse(task, page, receipt, tasks, cycle, records):
            if task['lab_kind'] != 'product':
                return dispatcher.expand(task, page, receipt, tasks)
            return [('observations', page.url, {'url': page.url, 'observed_at': page.observed_at})]
        with SQLite(self.root / name) as store:
            store.commit('import', [('tasks', key, value) for key, value in tasks.items()])
            result = collect(store, client, 'lab-discovery', allowed, parse,
                             architecture='B', cycles=1, max_tasks=cap, budget=120, hook=hook)
            state = store.snapshot()['records']
        return client, state, result

    def list_task(self, age=DATE, dependencies=()):
        value = make_task('ark', LIST, age)
        value.update(type='list', lab_kind='list', kind='comparison', lab_dependencies=list(dependencies))
        return value

    def test_shared_list_keeps_both_requests_and_original_product_history(self):
        original = make_task('ark', PRODUCT, '2022-01-01T00:00:00+00:00', original={'attempts': 9, 'priority': 8, 'source': 'original-source'})
        original['lab_selected'] = False
        roots = {'early': self.list_task(dependencies=['candidate-one']),
                 'late': self.list_task('2021-01-01T00:00:00+00:00', ['candidate-two']), 'original': original}
        client, state, result = self.collect_graph(roots, {LIST: [(200, {}, f'<a href="{PRODUCT}">Product</a>'.encode())],
                                                         PRODUCT: [(200, {}, b'product')]})
        self.assertEqual([LIST, PRODUCT], [c['url'] for c in client.transport.calls])
        for key in ('early', 'late'):
            self.assertEqual(roots[key]['created_at'], state['tasks'][key]['created_at'])
            self.assertEqual('complete', state['tasks'][key]['lab_status'])
            self.assertEqual(['original'], state['tasks'][key]['lab_child_keys'])
        product = state['tasks']['original']
        for key, value in original.items():
            if not key.startswith('lab_'):
                self.assertEqual(value, product[key])
        self.assertEqual(DATE, product['lab_origin_created_at'])
        self.assertEqual(['candidate-one', 'candidate-two'], product['lab_dependencies'])
        self.assertEqual(['early', 'late'], product['lab_parent_keys'])

    def test_shared_home_serves_distinct_queries_and_later_parent_reuses_observed_product(self):
        roots = []
        for index, query in enumerate(('one', 'two')):
            roots.append({'id': query, 'store': 'ark', 'kind': 'search', 'query': query,
                          'created_at': str(2020 + index) + '-01-01T00:00:00+00:00', 'dependencies': ['candidate-' + query]})
        one, two = HOME + 'search/?keyword=one', HOME + 'search/?keyword=two'
        responses = {HOME: [(200, {}, b'<form action="/search/"><input name="keyword"></form>')],
                     one: [(200, {}, f'<a href="{PRODUCT}">Product</a>'.encode())],
                     two: [(200, {}, f'<a href="{PRODUCT}">Product</a>'.encode())], PRODUCT: [(200, {}, b'product')]}
        tasks = {key: task for _, key, task in root_changes({'roots': roots}, {}, {'ark': CFG}, set(responses))}
        client, state, result = self.collect_graph(tasks, responses)
        counts = Counter(c['url'] for c in client.transport.calls)
        self.assertEqual({HOME: 1, one: 1, two: 1, PRODUCT: 1}, counts)
        product = next(t for t in state['tasks'].values() if t.get('url') == PRODUCT)
        self.assertEqual(['candidate-one', 'candidate-two'], product['lab_dependencies'])
        self.assertEqual(2, len(product['lab_parent_keys']))
        self.assertTrue(all(t['lab_status'] == 'complete' for t in state['tasks'].values()))
        self.assertEqual(2, sum(t.get('lab_kind') == 'search' for t in state['tasks'].values()))
        self.assertTrue(all(t['url'] == '' for t in state['tasks'].values() if t['lab_kind'] == 'search'))

    def test_later_parent_reaches_unobserved_descendants_of_an_already_completed_page(self):
        one, two, shared = [HOME + 'search/?keyword=' + query for query in ('one', 'two', 'shared')]
        missing = HOME + 'i/99999999/'
        roots = [{'id': q, 'store': 'ark', 'kind': 'search', 'query': q,
                  'created_at': str(2020 + i) + '-01-01T00:00:00+00:00', 'dependencies': ['candidate-' + q]}
                 for i, q in enumerate(('one', 'two'))]
        responses = {HOME: [(200, {}, b'<form action="/search/"><input name="keyword"></form>')],
                     one: [(200, {}, f'<a rel="next" href="{shared}">Next</a>'.encode())],
                     two: [(200, {}, f'<a rel="next" href="{shared}">Next</a>'.encode())],
                     shared: [(200, {}, f'<a href="{missing}">Unobserved</a>'.encode())]}
        tasks = {k: v for _, k, v in root_changes({'roots': roots}, {}, {'ark': CFG}, set(responses))}
        client, state, result = self.collect_graph(tasks, responses)
        self.assertEqual(1, Counter(r['url'] for r in client.transport.calls)[shared])
        descendant = next(t for t in state['tasks'].values() if t.get('url') == missing)
        self.assertEqual('evidence_wait', descendant['lab_status'])
        self.assertEqual(['candidate-one', 'candidate-two'], descendant['lab_dependencies'])

    def test_pagination_cycle_and_outside_product_are_retained_without_extra_fetch(self):
        second = HOME + 'search/?keyword=fixture&page=2'
        missing = HOME + 'i/99999999/'
        responses = {LIST: [(200, {}, f'<a rel="next" href="{second}">Next</a>'.encode())],
                     second: [(200, {}, f'<a rel="next" href="{LIST}">Back</a><a href="{missing}">Missing</a>'.encode())]}
        client, state, result = self.collect_graph({'root': self.list_task()}, responses)
        self.assertEqual(2, len(client.transport.calls))
        outside = next(t for t in state['tasks'].values() if t.get('url') == missing)
        self.assertEqual('evidence_wait', outside['lab_status'])
        self.assertEqual('discovered_url_outside_selected_resources', outside['lab_reason'])
        self.assertEqual([], outside['lab_attempts'])
        self.assertEqual(DATE, outside['created_at'])
        self.assertEqual(3, len(state['tasks']))

    def test_input_roots_sharing_one_url_keep_their_separate_registration_times(self):
        rows = [{'id': str(i), 'store': 'ark', 'kind': 'list', 'url': LIST, 'created_at': date}
                for i, date in enumerate((DATE, '2021-01-01T00:00:00+00:00'))]
        changes = root_changes({'roots': rows}, {}, {'ark': CFG}, {LIST})
        self.assertEqual(2, len({k for _, k, _ in changes}))
        self.assertEqual([row['created_at'] for row in rows], [task['created_at'] for _, _, task in changes])

    def test_sale_listing_keeps_campaigns_without_turning_all_catalog_links_into_sales(self):
        root = self.list_task()
        root.update(kind='sale', sale_page=False, lab_role='discovery')
        page = Page(LIST, b'<li><a href="/i/1/">ordinary</a></li><li>SALE SSD<a href="/i/2/">Discount</a></li>'
                    b'<a href="/special/sale/">SALE</a>', DATE)
        cfg = {**CFG, 'sale_patterns': [r'/special/']}
        children, detail = expand_shared_list([root], page, cfg)
        self.assertEqual({HOME + 'i/2/', HOME + 'special/sale/'}, {t['url'] for t in children})
        self.assertEqual('candidate', next(t for t in children if t['lab_kind'] == 'product')['lab_role'])

    def test_shared_fetch_preserves_distinct_sale_and_comparison_scopes_on_pagination(self):
        second = HOME + 'search/?keyword=fixture&page=2'
        comparison, sale = self.list_task(), self.list_task()
        sale.update(kind='sale', sale_page=False, lab_role='discovery')
        normal, discount = HOME + 'i/1/', HOME + 'i/2/'
        responses = {LIST: [(200, {}, f'<a rel="next" href="{second}">Next</a>'.encode())],
                     second: [(200, {}, b'<li><a href="/i/1/">ordinary</a></li><li>SALE SSD<a href="/i/2/">Discount</a></li>')],
                     normal: [(200, {}, b'normal')], discount: [(200, {}, b'discount')]}
        client, state, result = self.collect_graph({'comparison': comparison, 'sale': sale}, responses)
        self.assertEqual(1, Counter(c['url'] for c in client.transport.calls)[second])
        self.assertEqual(2, sum(t.get('url') == second for t in state['tasks'].values()))
        product = {t['url']: t for t in state['tasks'].values() if t['lab_kind'] == 'product'}
        self.assertEqual('comparison', product[normal]['lab_role'])
        self.assertEqual('candidate', product[discount]['lab_role'])

    def bundle(self, inputs, resources, roots, name='bundle'):
        path = self.root / name
        entries = []
        for index, (store, url, kind, status, body) in enumerate(resources):
            file = path / 'pages' / (str(index) + '.html')
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body)
            entries.append({'store': store, 'url': url, 'kind': kind, 'status': status, 'path': 'pages/' + file.name,
                            'body_sha256': hashlib.sha256(body).hexdigest(), 'observed_at': DATE, 'evidence_mode': 'fixture'})
        manifest = {'format': FORMAT, 'source_input_hash': read(inputs / 'manifest.json')['input_hash'], 'resources': entries, 'roots': roots}
        manifest['input_hash'] = digest(manifest)
        write(path / 'manifest.json', manifest)
        return path

    def test_run_command_integrates_discovery_and_resumes_without_reopening_children(self):
        inputs = self.root / 'inputs'
        make_inputs(inputs)
        bundle = self.bundle(inputs, [('ark', LIST, 'list', 200, f'<a href="{PRODUCT}">existing</a>'.encode())],
                             [{'id': 'list-one', 'store': 'ark', 'kind': 'list', 'url': LIST, 'created_at': DATE},
                              {'id': 'list-two', 'store': 'ark', 'kind': 'list', 'url': LIST, 'created_at': DATE}])
        output = self.root / 'pipeline'
        first = run(inputs, 'transport_failure', 'B', 'replay', output, discovery_input=bundle)
        with SQLite(output / 'store') as store:
            before = store.snapshot()
        self.assertEqual(before, read(output / 'state-export.json'))
        second = run(inputs, 'transport_failure', 'B', 'replay', output, discovery_input=bundle)
        with SQLite(output / 'store') as store:
            self.assertEqual(before, store.snapshot())
        self.assertEqual(2, first['discovery']['completed'])
        self.assertEqual(6, first['observations'])
        self.assertEqual(7, first['replayed_pages'])
        self.assertEqual(0, second['replayed_pages'])
        self.assertEqual(0, first['http_navigation_attempts'])

    def test_unavailable_pagination_keeps_linked_candidate_on_hold(self):
        inputs = self.root / 'inputs'
        pages = make_inputs(inputs)
        candidate = pages[0]['url']
        missing = HOME + 'search/?keyword=fixture&page=2'
        bundle = self.bundle(inputs, [('ark', LIST, 'list', 200, f'<a rel="next" href="{missing}">Next</a>'.encode())],
                             [{'id': 'comparison', 'store': 'ark', 'kind': 'list', 'url': LIST, 'created_at': DATE,
                               'dependencies': [candidate]}])
        result = run(inputs, 'transport_failure', 'B', 'replay', self.root / 'pipeline', discovery_input=bundle)
        decision = next(d for d in result['decisions'] if d['candidate_url'] == candidate)
        self.assertIn('comparison_discovery_incomplete', decision['reasons'])
        self.assertEqual('insufficient', decision['status'])

    def test_recognized_empty_404_completes_search_but_keeps_actual_http_status(self):
        inputs = self.root / 'inputs'
        make_inputs(inputs)
        url = 'https://shop.tsukumo.co.jp/search/?keyword=none'
        body = '<title>検索結果：none｜ツクモ</title><input name="keyword" value="none"><div id="sli_noresult">該当する商品がありませんでした。</div>'.encode()
        bundle = self.bundle(inputs, [('tsukumo', url, 'list', 404, body)],
                             [{'id': 'empty', 'store': 'tsukumo', 'kind': 'list', 'url': url, 'created_at': DATE}])
        output = self.root / 'pipeline'
        result = run(inputs, 'transport_failure', 'B', 'replay', output, discovery_input=bundle)
        self.assertEqual(1, result['discovery']['confirmed_empty'])
        receipt = next(r for r in read(output / 'receipts.json') if r['url'] == url)
        self.assertEqual(404, receipt['status'])
        self.assertEqual('http_404', receipt['error'])
        self.assertEqual(hashlib.sha256(body).hexdigest(), receipt['body_sha256'])

    def test_empty_page_for_a_different_query_is_not_a_completed_dependency(self):
        url = 'https://shop.tsukumo.co.jp/search/?keyword=none'
        body = '<title>検索結果：none｜ツクモ</title><input name="keyword" value="none"><div id="sli_noresult">該当する商品がありませんでした。</div>'.encode()
        root = {'lab_store': 'tsukumo', 'query': 'another', 'created_at': DATE, 'kind': 'comparison'}
        with self.assertRaisesRegex(ValueError, 'requested query'):
            expand_shared_list([root], Page(url, body, DATE, status=404), {'product_patterns': [r'/goods/\d+']})

    def test_changed_body_or_parent_input_is_rejected_before_experiment_output(self):
        inputs = self.root / 'inputs'
        make_inputs(inputs)
        bundle = self.bundle(inputs, [('ark', LIST, 'list', 200, b'<h1>fixture</h1>')],
                             [{'id': 'one', 'store': 'ark', 'kind': 'list', 'url': LIST, 'created_at': DATE}])
        (bundle / 'pages/0.html').write_bytes(b'changed')
        output = self.root / 'refused'
        with self.assertRaisesRegex(ValueError, 'body changed'):
            run(inputs, 'transport_failure', 'B', 'replay', output, discovery_input=bundle)
        self.assertFalse(output.exists())

    def test_live_client_accepts_only_verified_empty_404_and_retains_denial_gate(self):
        url = 'https://shop.tsukumo.co.jp/search/?keyword=none'
        body = '<title>検索結果：none｜ツクモ</title><input name="keyword" value="none"><div id="sli_noresult">該当する商品がありませんでした。</div>'.encode()
        client = client_for(Clock(NOW), {url: [(404, {}, body)]})
        client.inspect_not_found = lambda page: bool(confirmed_empty_search('tsukumo', page))
        page, receipt = client.fetch(url)
        self.assertEqual(404, page.status)
        self.assertEqual('http_404', receipt.error)
        denied = client_for(Clock(NOW), {url: [(403, {}, body)]})
        denied.inspect_not_found = lambda page: self.fail('Never inspect a denied response as an empty result')
        page, receipt = denied.fetch(url)
        self.assertIsNone(page)
        self.assertTrue(denied.hosts['shop.tsukumo.co.jp']['blocked'])

    def test_invalid_list_stays_pending_with_original_history_and_no_children(self):
        root = self.list_task()
        root['attempts'] = 7
        client = client_for(Clock(NOW), {LIST: [(200, {}, b'<h1>Error shell</h1>')]})
        dispatcher = Dispatcher({'ark': CFG}, {LIST})
        with SQLite(self.root / 'store') as store:
            store.commit('import', [('tasks', 'root', root)])
            with self.assertRaisesRegex(ValueError, 'not an empty search result'):
                collect(store, client, 'lab-bad-page', {LIST},
                        lambda task, page, receipt, tasks, cycle, records: dispatcher.expand(task, page, receipt, tasks),
                        architecture='B', cycles=1, max_tasks=20, budget=120)
            tasks = store.snapshot()['records']['tasks']
        self.assertEqual({'root'}, set(tasks))
        self.assertEqual('waiting', tasks['root']['lab_status'])
        self.assertEqual(7, tasks['root']['attempts'])
        self.assertEqual('normalization:ValueError', tasks['root']['lab_last_error'])

    def test_bounded_collection_keeps_every_discovered_product_and_cap_on_resume(self):
        urls = [HOME + 'i/' + str(index) + '/' for index in range(1, 56)]
        responses = {LIST: [(200, {}, ''.join(f'<a href="{url}">Product</a>' for url in urls).encode())]}
        responses.update({url: [(200, {}, b'product')] for url in urls})
        client, state, result = self.collect_graph({'root': self.list_task()}, responses, cap=3)
        self.assertEqual(56, len(state['tasks']))
        self.assertEqual(3, len(client.transport.calls))
        self.assertEqual(53, sum(t['lab_status'] != 'complete' for t in state['tasks'].values()))
        resumed = client_for(Clock(NOW), responses)
        with SQLite(self.root / 'graph') as store:
            before = store.snapshot()
            collect(store, resumed, 'lab-discovery', set(responses), lambda *args: self.fail('No remaining slots'),
                    architecture='B', cycles=1, max_tasks=3, budget=120)
            self.assertEqual(before, store.snapshot())
        self.assertEqual([], resumed.transport.calls)

    def test_discovered_candidate_generates_unknown_search_in_B_and_next_cycle_in_A(self):
        inputs = self.root / 'inputs'
        make_inputs(inputs)
        new_ark = HOME + 'i/99999999/'
        ts_home = 'https://shop.tsukumo.co.jp/'
        ts_search = ts_home + 'search/?keyword=4537694358347'
        ts_product = ts_home + 'goods/4537694358347/'
        schema = {'@context': 'https://schema.org', '@type': 'Product', 'name': 'Fixture only', 'gtin13': '4537694358347',
                  'offers': {'price': 10000, 'priceCurrency': 'JPY', 'availability': 'https://schema.org/InStock'}}
        product = ('<h1>Fixture only</h1><script type="application/ld+json">' + json.dumps(schema) + '</script>').encode()
        resources = [('ark', LIST, 'list', 200, f'<a href="{new_ark}">SALE SSD</a>'.encode()),
                     ('ark', new_ark, 'product', 200, product),
                     ('tsukumo', ts_home, 'home', 200, b'<form action="/search/"><input name="keyword"></form>'),
                     ('tsukumo', ts_search, 'list', 200, f'<a href="{ts_product}">Fixture only</a>'.encode()),
                     ('tsukumo', ts_product, 'product', 200, product)]
        bundle = self.bundle(inputs, resources, [{'id': 'sale', 'store': 'ark', 'kind': 'list', 'url': LIST,
                                                 'role': 'discovery', 'sale_page': True, 'created_at': DATE}])
        for architecture, cycles, observed in [('A', 1, 3), ('A', 2, 8), ('B', 1, 8)]:
            output = self.root / (architecture + str(cycles))
            result = run(inputs, 'transport_failure', architecture, 'replay', output, discovery_input=bundle, cycles=cycles)
            self.assertEqual(observed, result['observations'], (architecture, cycles))
            receipts = read(output / 'receipts.json')
            self.assertEqual(0 if architecture == 'A' and cycles == 1 else 1,
                             sum(r['url'] == ts_home for r in receipts))
            self.assertEqual(0, result['http_navigation_attempts'])

        # A later sale list can promote a product already observed as a
        # comparator. Its dependency planner must still run, without refetch.
        late_list = HOME + 'search/?keyword=later-sale'
        resources.append(('ark', late_list, 'list', 200, f'<a href="{new_ark}">SALE SSD</a>'.encode()))
        promoted = self.root / 'promoted-inputs'
        make_inputs(promoted)
        late_bundle = self.bundle(promoted, resources, [
            {'id': 'comparison-first', 'store': 'ark', 'kind': 'list', 'url': LIST,
             'role': 'comparison', 'created_at': DATE},
            {'id': 'sale-later', 'store': 'ark', 'kind': 'list', 'url': late_list,
             'role': 'discovery', 'sale_page': True, 'created_at': '2022-01-01T00:00:00+00:00'},
        ])
        out = self.root / 'promoted-output'
        result = run(promoted, 'transport_failure', 'B', 'replay', out, discovery_input=late_bundle)
        self.assertEqual(8, result['observations'])
        receipts = read(out / 'receipts.json')
        self.assertEqual(1, sum(r['url'] == new_ark for r in receipts))
        self.assertEqual(1, sum(r['url'] == ts_home for r in receipts))
        self.assertLess(next(i for i, r in enumerate(receipts) if r['url'] == new_ark),
                        next(i for i, r in enumerate(receipts) if r['url'] == late_list))

    def test_discovered_expiry_reaches_decisions_and_late_conflicts_keep_both_sources(self):
        for name, schema_expiry, second_expiry, expected in [
            ('listing-only', None, None, '2026-10-05T14:59:59+00:00'),
            ('schema-conflict', '2026-10-06', None, None),
            ('late-list-conflict', None, '2026-10-07', None),
        ]:
            with self.subTest(name=name):
                inputs = self.root / name / 'inputs'
                make_inputs(inputs)
                product_url = HOME + 'i/99999999/'
                schema = {'@context': 'https://schema.org', '@type': 'Product', 'name': 'Fixture only',
                          'gtin13': '4537694358347', 'offers': {'price': 10000, 'priceCurrency': 'JPY',
                          'availability': 'https://schema.org/InStock'}}
                if schema_expiry:
                    schema['offers']['priceValidUntil'] = schema_expiry
                product = ('<h1>Fixture only</h1><script type="application/ld+json">' + json.dumps(schema) + '</script>').encode()
                resources = [('ark', LIST, 'list', 200, f'<li e-date="2026-10-05"><a href="{product_url}">SALE SSD</a></li>'.encode()),
                             ('ark', product_url, 'product', 200, product)]
                roots = [{'id': name, 'store': 'ark', 'kind': 'list', 'url': LIST, 'role': 'discovery',
                          'sale_page': True, 'created_at': DATE}]
                if second_expiry:
                    second = HOME + 'search/?keyword=late'
                    resources.append(('ark', second, 'list', 200, f'<li e-date="{second_expiry}"><a href="{product_url}">SALE SSD</a></li>'.encode()))
                    roots.append({**roots[0], 'id': name + '-late', 'url': second, 'created_at': '2022-01-01T00:00:00+00:00'})
                bundle = self.bundle(inputs, resources, roots)
                output = self.root / name / 'output'
                result = run(inputs, 'transport_failure', 'B', 'replay', output, discovery_input=bundle)
                state = read(output / 'state-export.json')['records']
                observed = next(v for v in state['observations'].values() if v['offer']['url'] == product_url)
                self.assertEqual(expected, observed['offer']['expires_at'])
                sources = [s for s in observed['fields']['expires_at']['sources'] if s['kind'] == 'discovery_listing']
                self.assertEqual(2 if second_expiry else 1, len(sources))
                self.assertTrue(all(s['body_sha256'] and s['observed_at'] and s['url'] for s in sources))
                self.assertEqual(LIST, observed['offer']['discovery_url'])
                if expected is None:
                    self.assertIn('expiry_conflict_review_needed', observed['offer']['issues'])
                    decision = next(d for d in result['decisions'] if d['candidate_url'] == product_url)
                    self.assertEqual('insufficient', decision['status'])
                    self.assertIn('expiry_conflict_review_needed', decision['reasons'])
                receipts = read(output / 'receipts.json')
                self.assertEqual(1, sum(r['url'] == product_url for r in receipts))
                if second_expiry:
                    self.assertLess(next(i for i, r in enumerate(receipts) if r['url'] == product_url),
                                    next(i for i, r in enumerate(receipts) if r['url'] == second))

    def test_hard_exit_exposes_either_whole_list_expansion_or_unknown_reservation(self):
        code = r'''
import os,sys
from monitor_lab.discovery import Dispatcher
from monitor_lab.pipeline import make_task
from monitor_lab.scheduler import collect
from monitor_lab.stores import SQLite
from monitor_lab.tests.test_discovery import CFG,DATE,LIST,PRODUCT
from monitor_lab.tests.test_pipeline_scheduling import Clock
from monitor_lab.tests.test_scheduler import client_for,NOW
root=make_task('ark',LIST,DATE);root.update(lab_kind='list',type='list',kind='comparison')
client=client_for(Clock(NOW),{LIST:[(200,{},('<a href="'+PRODUCT+'">Product</a>').encode())],PRODUCT:[(200,{},b'product')]})
dispatcher=Dispatcher({'ark':CFG},{LIST,PRODUCT})
def parse(task,page,receipt,tasks,cycle,records):
    return dispatcher.expand(task,page,receipt,tasks) if task['lab_kind']=='list' else [('observations',page.url,{'url':page.url})]
def stop(stage):
    if stage==sys.argv[2]:os._exit(73)
with SQLite(sys.argv[1]) as store:
    store.commit('import',[('tasks','root',root)])
    collect(store,client,'lab-kill',{LIST,PRODUCT},parse,architecture='B',cycles=1,max_tasks=2,budget=120,hook=stop)
'''
        for stage in ('after_reservation', 'after_fetch', 'after_checkpoint'):
            path = self.root / stage
            child = subprocess.run([sys.executable, '-B', '-c', code, str(path), stage], capture_output=True, text=True,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(73, child.returncode, child.stderr)
            resumed = client_for(Clock(NOW + 130), {})
            with SQLite(path) as store:
                collect(store, resumed, 'lab-kill', {LIST, PRODUCT}, lambda *args: self.fail('Expired budget'),
                        architecture='B', cycles=1, max_tasks=2, budget=120)
                state = store.snapshot()['records']
            self.assertEqual(2 if stage == 'after_checkpoint' else 1, len(state['tasks']))
            self.assertEqual('complete' if stage == 'after_checkpoint' else 'waiting', state['tasks']['root']['lab_status'])
            self.assertEqual(1, state['scheduler']['collection']['reserved_resources'])
            self.assertEqual([], resumed.transport.calls)


if __name__ == '__main__':
    unittest.main()
