from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from monitor_lab.capture import verify_capture
from monitor_lab.pipeline import run
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.stores import SQLite
from monitor_lab.study import study
from monitor_lab.tests.test_pipeline_scheduling import make_inputs
from monitor_lab.tests.test_request_plan_capture import fake_study, make_plan, no_network

HOME = 'https://www.ark-pc.co.jp/'
LIST = HOME + 'special/sale/'
PRODUCT = HOME + 'i/12201487/'
OUTSIDE = HOME + 'i/99999999/'
DOSPARA = 'https://www.dospara.co.jp/campaign-list'
BODY = b'''<html><h1>Captured motherboard</h1><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Captured motherboard",
"gtin13":"0195553309745","offers":{"price":7777,"priceCurrency":"JPY",
"availability":"https://schema.org/InStock"}}</script></html>'''


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class CapturePipelineTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / 'tests' / ('capture-pipeline-' + uuid.uuid4().hex)
        self.inputs = self.root / 'inputs'
        make_inputs(self.inputs)
        self.enterContext(no_network())

    def capture(self, resources, responses):
        plan = make_plan(resources)
        write(self.root / 'plan.json', plan)
        output = self.root / 'capture'
        with fake_study({'urllib': responses}):
            study(output, methods=['urllib'], plan=self.root / 'plan.json', budget=120)
        return output

    def add_original(self):
        original = {'type': 'product', 'url': OUTSIDE, 'created_at': '2000-01-01T00:00:00Z', 'attempts': 9}
        path = self.inputs / 'transport_failure/state/stores/ark.json'
        value = read(path); value['queue']['old'] = original; write(path, value)
        manifest = read(self.inputs / 'manifest.json')
        entry = next(r for r in manifest['snapshots']['transport_failure']['files'] if r['path'].endswith('/ark.json'))
        entry.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        manifest.pop('input_hash'); manifest['input_hash'] = digest(manifest)
        write(self.inputs / 'manifest.json', manifest)
        return original

    def test_scoped_capture_runs_discovery_and_decisions_without_primary_fixture_fallback(self):
        original = self.add_original()
        resources = [{'store': 'ark', 'url': HOME, 'kind': 'home'},
                     {'store': 'ark', 'url': LIST, 'kind': 'list'},
                     {'store': 'ark', 'url': PRODUCT, 'kind': 'product'},
                     {'store': 'dospara', 'url': DOSPARA, 'kind': 'list'}]
        source = self.capture(resources, {
            HOME: (200, {}, b'<a href="/i/88/">Ordinary motherboard</a><a href="/special/sale/">SALE SSD</a>'),
            LIST: (200, {}, f'<li>SALE SSD<a href="{PRODUCT}">SALE motherboard</a></li>'
                           f'<li>SALE SSD<a href="{OUTSIDE}">SALE SSD</a></li>'.encode()),
            PRODUCT: (200, {}, BODY), DOSPARA: (200, {}, b'<html>Unsupported adapter</html>')})
        before = (hashes(source), hashes(self.inputs))
        output = self.root / 'run'
        result = run(self.inputs, 'transport_failure', 'B', 'replay', output, capture_input=source)
        with SQLite(output / 'store') as store:
            state = store.snapshot()
        records = state['records']
        observations = list(records['observations'].values())
        self.assertEqual([PRODUCT], [o['offer']['url'] for o in observations])
        self.assertEqual(7777, observations[0]['offer']['price_yen'])
        self.assertEqual(0, result['http_navigation_attempts'])
        self.assertEqual(0, result['coverage']['totals']['confirmed_http_attempts'])
        self.assertGreater(result['coverage']['totals']['recorded_source_http_attempts'], 0)
        self.assertEqual(10, result['coverage']['monitored_store_count'])
        self.assertFalse(result['coverage']['full_store_coverage_proven'])
        self.assertTrue(any(d['candidate_url'] == PRODUCT for d in result['decisions']))
        self.assertTrue(all(d['status'] == 'insufficient' for d in result['decisions']))
        retained = records['tasks']['ark:old']
        self.assertEqual(original, {k: retained[k] for k in original})
        self.assertEqual('evidence_wait', retained['lab_status'])
        self.assertTrue(retained['lab_parent_keys'])
        self.assertFalse(any(t.get('url') == HOME + 'i/88/' for t in records['tasks'].values()))
        unsupported = [t for t in records['tasks'].values() if t.get('url') == DOSPARA]
        self.assertEqual(1, len(unsupported))
        self.assertEqual('external_wait', unsupported[0]['lab_status'])
        self.assertEqual('store_adapter_not_integrated_in_lab', unsupported[0]['lab_reason'])
        self.assertEqual(before, (hashes(source), hashes(self.inputs)))
        resumed = run(self.inputs, 'transport_failure', 'B', 'replay', output, capture_input=source)
        self.assertEqual(0, resumed['replayed_pages'])
        self.assertEqual(result['coverage'], resumed['coverage'])
        self.assertEqual(result['capture_replay']['replayed_actual_attempts'], resumed['capture_replay']['replayed_actual_attempts'])
        with SQLite(output / 'store') as store:
            self.assertEqual(state, store.snapshot())

    def test_late_jan_searches_reuse_one_captured_home_with_provenance_and_identical_resume(self):
        home = 'https://www.sofmap.com/contents/?id=2959&sid=1'
        candidates = {PRODUCT: '0195553309745', HOME + 'i/20300386/': '4711289500124'}
        home_body = (b'<html><a href="/contents/?id=fixture-sale">SALE SSD</a>'
                     b'<form action="/search_result.aspx" method="get">'
                     b'<input name="keyword"></form></html>')
        source = self.capture(
            [{'store': 'sofmap', 'url': home, 'kind': 'home'}] +
            [{'store': 'ark', 'url': url, 'kind': 'product'} for url in candidates],
            {home: (200, {}, home_body), **{
                url: (200, {}, BODY.replace(b'0195553309745', jan.encode()))
                for url, jan in candidates.items()}})
        source_rows = verify_capture(source)['receipts']
        home_sources = [(i, row) for i, row in enumerate(source_rows) if row['url'] == home]
        self.assertEqual(1, len(home_sources))
        source_index, original = home_sources[0]
        before = (hashes(source), hashes(self.inputs))
        output = self.root / 'late-search-run'
        options = {'capture_input': source, 'candidate_urls': list(candidates)}
        result = run(self.inputs, 'transport_failure', 'B', 'replay', output, **options)
        with SQLite(output / 'store') as store:
            state = store.snapshot()
        records, tasks = state['records'], state['records']['tasks']
        dispatches = sorted(records['dispatches'].items(), key=lambda item: item[1]['slot'])

        # These fixtures exercise root-list parsing before either candidate is
        # observed, rather than coalescing pre-existing searches with that root.
        source_dispatch_id, source_dispatch = dispatches[0]
        self.assertEqual(home, source_dispatch['url'])
        self.assertEqual('committed', source_dispatch['state'])
        self.assertIsNone(source_dispatch['processing_error'])
        self.assertFalse(source_dispatch['receipt']['analysis_reuse'])
        self.assertEqual(1, len(source_dispatch['task_ids']))
        root = tasks[source_dispatch['task_ids'][0]]
        self.assertEqual(('list', 'complete'), (root['lab_kind'], root['lab_status']))
        self.assertTrue(root['lab_root_ids'])
        self.assertEqual(original['attempts'], source_dispatch['receipt']['attempts'])
        for field in ('observed_at', 'body_sha256', 'method'):
            self.assertEqual(original[field], source_dispatch['receipt'][field])
        candidate_dispatches = {d['url']: d for _, d in dispatches if d['url'] in candidates}
        self.assertEqual(set(candidates), set(candidate_dispatches))
        self.assertTrue(all(source_dispatch['slot'] < d['slot'] for d in candidate_dispatches.values()))
        self.assertEqual(set(candidates), {o['offer']['url'] for o in records['observations'].values()})

        reuses = [d for _, d in dispatches if d['receipt'].get('analysis_reuse')]
        self.assertEqual(2, len(reuses))
        searches = {key: task for key, task in tasks.items()
                    if task['lab_store'] == 'sofmap' and task.get('lab_kind') == 'search'}
        self.assertEqual(2, len(searches))
        self.assertEqual(set(candidates.values()), {t['query'] for t in searches.values()})
        expected_source = {
            'url': home, 'observed_at': original['observed_at'],
            'body_sha256': original['body_sha256'], 'method': original['method'],
            'source_capture_manifest_sha256': before[0]['capture-manifest.json'],
            'source_receipt_index': source_index, 'source_evidence_mode': original['evidence_mode'],
            'evidence_mode': 'captured_queue_simulation', 'analysis_reuse': True,
            'derived_from_dispatch': source_dispatch_id,
        }
        child_urls = set()
        for key, task in searches.items():
            with self.subTest(jan=task['query']):
                candidate_url = next(url for url, jan in candidates.items() if jan == task['query'])
                self.assertEqual([candidate_url], task['lab_dependencies'])
                self.assertEqual('complete', task['lab_status'])
                self.assertEqual([], task['lab_attempts'])
                self.assertEqual([], task.get('lab_evidence_gaps', []))
                self.assertEqual(1, len(task['lab_analysis_reuses']))
                receipt = task['lab_analysis_reuses'][0]
                dispatch = next(d for d in reuses if key in d['task_ids'])
                self.assertGreater(dispatch['slot'], candidate_dispatches[candidate_url]['slot'])
                self.assertEqual('committed', dispatch['state'])
                self.assertEqual(receipt, dispatch['receipt'])
                self.assertEqual(([], [], 0), (receipt['attempts'], receipt['waits'], receipt['elapsed_seconds']))
                self.assertEqual(expected_source, {field: receipt[field] for field in expected_source})
                self.assertEqual(expected_source, {field: task['lab_resolution'][field] for field in expected_source})
                self.assertEqual(1, len(task['lab_child_keys']))
                child = tasks[task['lab_child_keys'][0]]
                expected_url = 'https://www.sofmap.com/search_result.aspx?keyword=' + task['query']
                self.assertEqual(expected_url, child['url'])
                child_urls.add(child['url'])
                self.assertEqual('list', child['lab_kind'])
                self.assertEqual('evidence_wait', child['lab_status'])
                self.assertEqual('discovered_url_outside_selected_resources', child['lab_reason'])
                self.assertEqual([key], child['lab_parent_keys'])
                self.assertEqual([candidate_url], child['lab_dependencies'])
                self.assertEqual([], child['lab_attempts'])
                self.assertEqual(1, len(child['lab_discovery_evidence']))
                evidence = child['lab_discovery_evidence'][0]
                self.assertEqual(expected_source, {field: evidence[field] for field in expected_source})
        self.assertEqual(2, len(child_urls))
        self.assertTrue(child_urls.isdisjoint(d['url'] for _, d in dispatches))
        self.assertEqual(0, result['http_navigation_attempts'])
        self.assertEqual(0, result['capture_replay']['evidence_gaps'])
        self.assertEqual(2, result['capture_replay']['discovery_analysis_reuses'])
        self.assertEqual(3, result['capture_replay']['replayed_actual_attempts'])
        sofmap = result['coverage']['stores']['sofmap']
        self.assertEqual(1, sofmap['recorded_source_http_attempts'])
        self.assertEqual(2, sofmap['discovery_analysis_reuses'])
        self.assertEqual(0, sofmap['confirmed_http_attempts'])

        resumed = run(self.inputs, 'transport_failure', 'B', 'replay', output, **options)
        self.assertEqual(0, resumed['replayed_pages'])
        self.assertEqual(result['coverage'], resumed['coverage'])
        self.assertEqual(result['capture_replay'], resumed['capture_replay'])
        with SQLite(output / 'store') as store:
            self.assertEqual(state, store.snapshot())
        self.assertEqual(before, (hashes(source), hashes(self.inputs)))

    def test_failed_captured_product_never_falls_back_to_successful_saved_primary_page(self):
        source = self.capture([{'store': 'ark', 'url': PRODUCT, 'kind': 'product'}],
                              {PRODUCT: (403, {}, b'Access denied')})
        result = run(self.inputs, 'transport_failure', 'B', 'replay', self.root / 'run',
                     capture_input=source, candidate_urls=[PRODUCT])
        self.assertEqual(0, result['observations'])
        self.assertEqual([], result['decisions'])
        self.assertEqual(0, result['http_navigation_attempts'])
        self.assertEqual(1, result['capture_replay']['replayed_actual_attempts'])
        self.assertEqual([PRODUCT], result['coverage']['stores']['ark']['product_resources_missing'])
        self.assertEqual(0, result['formal_audits_added'])
        self.assertEqual(0, result['scheduled_stability_samples'])

    def test_captured_comparisons_still_follow_a_next_cycle_and_b_same_cycle_policy(self):
        candidate = 'https://www.pc-koubou.jp/products/detail.php?product_id=1051336'
        tsukumo = 'https://shop.tsukumo.co.jp/goods/0195553309745/'
        resources = [{'store': store, 'url': url, 'kind': 'product'}
                     for store, url in (('koubou', candidate), ('tsukumo', tsukumo), ('ark', PRODUCT))]
        source = self.capture(resources, {r['url']: (200, {}, BODY) for r in resources})
        counts = []
        for name, architecture, cycles in (('a1', 'A', 1), ('a2', 'A', 2), ('b', 'B', 1)):
            result = run(self.inputs, 'transport_failure', architecture, 'replay', self.root / name,
                         backend='sqlite', cycles=cycles, capture_input=source, candidate_urls=[candidate])
            counts.append(result['observations'])
            self.assertEqual(0, result['http_navigation_attempts'])
        self.assertEqual([1, 3, 3], counts)

    def test_capture_cannot_silently_switch_to_live_or_mix_other_discovery_fixtures(self):
        for kwargs in ({'mode': 'live', 'capture_input': self.root / 'missing'},
                       {'mode': 'replay', 'capture_input': self.root / 'missing', 'discovery_input': self.root / 'other'},
                       {'mode': 'replay', 'candidate_urls': [PRODUCT]}):
            mode = kwargs.pop('mode')
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run(self.inputs, 'transport_failure', 'B', mode, self.root / 'rejected', **kwargs)
        self.assertFalse((self.root / 'rejected').exists())


if __name__ == '__main__':
    unittest.main()
