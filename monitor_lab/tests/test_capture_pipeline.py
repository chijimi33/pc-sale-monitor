from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

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
