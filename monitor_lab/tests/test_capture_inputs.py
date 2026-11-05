"""Offline capture input contracts, using real immutable E-drive bundles."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.models import STORES
from monitor_lab.acquire import Receipt, TRANSPORTS
from monitor_lab.capture import Capture, LEGACY_FORMAT, verify_capture
from monitor_lab.capture_inputs import load_capture_inputs
from monitor_lab.request_plan import FORMAT, scope_report
from monitor_lab.safety import allowed_root, atomic_bytes, digest, read, write


HOME = 'https://www.pc-koubou.jp/'
LIST = 'https://www.pc-koubou.jp/goods/parts_goods_tokusen.php'
PRODUCT = 'https://www.pc-koubou.jp/products/detail.php?product_id=1'
MISSING = 'https://www.pc-koubou.jp/products/detail.php?product_id=2'
HELD = 'https://www.pc-koubou.jp/products/detail.php?product_id=3'
ADAPTER_HOME = 'https://www.dospara.co.jp/'
ADAPTER_LIST = 'https://www.dospara.co.jp/campaign-list'
ADAPTER_PRODUCT = 'https://www.dospara.co.jp/fixture-product.html'
CREATED = '2026-10-02T09:00:00+00:00'
RESOURCES = [
    {'store': 'koubou', 'url': HOME, 'kind': 'home'},
    {'store': 'koubou', 'url': LIST, 'kind': 'list'},
    {'store': 'koubou', 'url': PRODUCT, 'kind': 'product'},
    {'store': 'koubou', 'url': MISSING, 'kind': 'product'},
    {'store': 'dospara', 'url': ADAPTER_HOME, 'kind': 'home'},
    {'store': 'dospara', 'url': ADAPTER_LIST, 'kind': 'list'},
    {'store': 'dospara', 'url': ADAPTER_PRODUCT, 'kind': 'product'},
    {'store': 'koubou', 'url': HELD, 'kind': 'product'},
]
FIELDS = {'store', 'url', 'kind', 'observed_at', 'content_type', 'status',
          'body_sha256', 'source_receipt_index'}


def bundle_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


class CaptureInputsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = allowed_root() / 'tests' / ('capture-inputs-' + uuid.uuid4().hex)
        cls.settings = read(Path(__file__).resolve().parents[2] / 'config/sources.json')
        cls.config = cls.settings['stores']
        cls.source = cls.make_capture('mixed')
        cls.source_hashes = bundle_hashes(cls.source)
        cls.browser = cls.make_capture('browser', methods=('browser',),
                                      resources=[row for row in RESOURCES if row['store'] == 'koubou'])
        cls.browser_hashes = bundle_hashes(cls.browser)
        cls.primary = [
            {'store': 'koubou', 'url': PRODUCT, 'name': 'koubou-b550', 'group': 'b550',
             'path': 'never-read/old.html', 'body_sha256': '0' * 64,
             'observed_at': '2099-01-01T00:00:00Z', 'status': 200,
             'content_type': 'old/type', 'price_yen': 1, 'sale_page': True, 'role': 'candidate'},
            {'store': 'ark', 'url': MISSING, 'name': 'wrong-store', 'group': 'wrong-store'},
            {'store': 'koubou', 'url': 'https://www.pc-koubou.jp/unplanned',
             'name': 'outside-capture', 'group': 'b550'},
        ]

    @classmethod
    def make_capture(cls, name, *, methods=('urllib', 'pooled'), resources=None,
                     change=None, scoped=True, complete=True):
        resources = deepcopy(RESOURCES if resources is None else resources)
        selected = {row['store'] for row in resources}
        plan = {'format': FORMAT, 'created_at': CREATED, 'source_data_sha': '1' * 40,
                'resources': resources,
                'not_requested': [{'store': store, 'reason': 'Outside offline fixture scope'}
                                  for store in STORES if store not in selected]}
        plan['plan_hash'] = digest(plan)
        metadata = {'experiment_id': 'lab-capture-inputs-' + name, 'mode': 'fixture',
                    'methods': list(methods), 'plan_hash': plan['plan_hash'],
                    'request_plan': [[row['store'], row['url']] for row in resources]}
        if scoped:
            metadata['scope_plan'] = plan
        capture = Capture(cls.root / name, metadata, cls.settings)
        for method in methods:
            for index, resource in enumerate(resources):
                url = resource['url']
                status = 404 if url == MISSING else None if url == HELD or method == 'pooled' and url == PRODUCT else 200
                observed = f'2026-10-04T{3 if method == "urllib" else 4:02}:{index:02}:00+00:00'
                # Product-like markup in home/list fixtures must never be parsed.
                body = (f'<html><h1>{method} {url}</h1><input id="priceIncTax" value="10980"></html>').encode()
                receipt = Receipt(url, status, observed,
                                  hashlib.sha256(body).hexdigest() if status is not None else None,
                                  TRANSPORTS[method].method, {},
                                  error='shared_host_wait' if status is None else 'http_404' if status == 404 else None,
                                  content_type='text/html; charset=shift_jis' if status is not None else '',
                                  body_bytes=len(body) if status is not None else None,
                                  evidence_mode='fixture', response_url=url if status is not None else None)
                if status is not None:
                    receipt.body_file = capture.body(receipt, body)
                    receipt.attempts = [{'url': url, 'status': status}]
                    if method == 'browser':
                        receipt.body_kind = 'browser_response_body'
                        receipt.parser_body_kind = 'rendered_dom'
                        dom_body = b'<html><h1>Distinct rendered product</h1></html>'
                        dom = Receipt(url, None, observed, hashlib.sha256(dom_body).hexdigest(),
                                      'browser', {}, content_type='text/html; charset=utf-8',
                                      body_kind='rendered_dom', body_bytes=len(dom_body))
                        dom.body_file = capture.body(dom, dom_body)
                        receipt.rendered_dom = asdict(dom)
                row = {'store': resource['store'], **asdict(receipt)}
                if scoped:
                    row['resource_kind'] = resource['kind']
                if change:
                    change(method, index, row)
                capture.append(row, {})
        if complete:
            result = {'experiment_id': metadata['experiment_id'], 'mode': 'fixture',
                      'receipts': capture.receipts}
            if scoped:
                result.update(plan_hash=plan['plan_hash'], scope=scope_report(plan, capture.receipts))
            capture.checkpoint(result=result)
            verify_capture(capture.root)
        return capture.root

    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'urllib.request.OpenerDirector.open',
                       'monitor_lab.evidence.normalize'):
            self.enterContext(patch(target, side_effect=AssertionError('No network or price parsing')))
        for transport in TRANSPORTS.values():
            self.enterContext(patch.object(transport, '__init__', side_effect=AssertionError('No transport construction')))
        self.before_primary = deepcopy(self.primary)
        self.before_settings = deepcopy(self.settings)

    def tearDown(self):
        self.assertEqual(self.source_hashes, bundle_hashes(self.source))
        self.assertEqual(self.browser_hashes, bundle_hashes(self.browser))
        self.assertEqual(self.before_primary, self.primary)
        self.assertEqual(self.before_settings, self.settings)

    def load(self, *, capture=None, method='pooled', primary_pages=None, config=None, candidate_urls=None):
        return load_capture_inputs(capture or self.source, method,
                                   self.primary if primary_pages is None else primary_pages,
                                   self.config if config is None else config, candidate_urls)

    def copied_capture(self, name, source=None):
        source = source or self.source
        output = self.root / name
        manifest = read(source / 'capture-manifest.json')
        for entry in manifest['files']:
            atomic_bytes(output / entry['path'], (source / entry['path']).read_bytes())
        atomic_bytes(output / 'capture-manifest.json', (source / 'capture-manifest.json').read_bytes())
        return output

    def rehash_manifest(self, root):
        manifest = read(root / 'capture-manifest.json')
        for entry in manifest['files']:
            body = (root / entry['path']).read_bytes()
            entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        manifest['files_sha256'] = digest(manifest['files'])
        write(root / 'capture-manifest.json', manifest)

    def test_alias_selection_and_global_receipt_indices_preserve_all_outcomes(self):
        for method in ('urllib', 'pooled'):
            with self.subTest(method=method):
                result = self.load(method=method)
                self.assertEqual(TRANSPORTS[method].method, result['source_method'])
                self.assertEqual(verify_capture(self.source), result['verified'])
                self.assertEqual(len(RESOURCES), len(result['resources']))
                for resource in result['resources'].values():
                    receipt = result['verified']['receipts'][resource['source_receipt_index']]
                    self.assertEqual(FIELDS, set(resource))
                    self.assertEqual(result['source_method'], receipt['method'])
                    self.assertEqual(resource['kind'], receipt['resource_kind'])
                    for field in FIELDS - {'kind', 'source_receipt_index'}:
                        self.assertEqual(receipt[field], resource[field])
                self.assertEqual(404, result['resources'][MISSING]['status'])
                self.assertIsNone(result['resources'][HELD]['status'])
                self.assertIsNone(result['resources'][HELD]['body_sha256'])
        self.assertEqual(8, self.load()['resources'][HOME]['source_receipt_index'])
        self.assertIsNone(self.load()['resources'][PRODUCT]['status'])
        self.assertEqual(200, self.load(method='urllib')['resources'][PRODUCT]['status'])

    def test_product_pages_copy_only_matching_names_and_groups(self):
        result = self.load()
        products = {row['url'] for row in RESOURCES if row['kind'] == 'product'}
        self.assertEqual(products, {row['url'] for row in result['pages']})
        for page in result['pages']:
            self.assertEqual(FIELDS | {'name', 'group'}, set(page))
            self.assertEqual(result['resources'][page['url']], {key: page[key] for key in FIELDS})
            expected = ('koubou-b550', 'b550') if page['url'] == PRODUCT else (
                'captured:' + digest([page['store'], page['url']])[:24],) * 2
            self.assertEqual(expected, (page['name'], page['group']))
        self.assertIsNone(next(page for page in result['pages'] if page['url'] == PRODUCT)['body_sha256'])

    def test_roots_keep_products_comparisons_and_all_nonproducts_discovery(self):
        result = self.load()
        roots = result['roots']['roots']
        self.assertEqual(len(RESOURCES), len(roots))
        self.assertEqual(len(roots), len({row['id'] for row in roots}))
        pages = {page['url']: page for page in result['pages']}
        for planned, root in zip(RESOURCES, roots):
            self.assertEqual((planned['store'], planned['url']), (root['store'], root['url']))
            self.assertEqual(CREATED, root['created_at'])
            self.assertIs(False, root['sale_page'])
            if planned['kind'] == 'product':
                self.assertEqual(('product', 'comparison', pages[root['url']]['group']),
                                 (root['kind'], root['role'], root['group']))
            else:
                self.assertEqual(('list', 'discovery', root['id']), (root['kind'], root['role'], root['group']))
        self.assertEqual([], result['candidate_urls'])

    def test_adapter_holds_retain_both_home_and_list_in_denominator(self):
        result = self.load()
        by_url = {root['url']: root for root in result['roots']['roots']}
        self.assertEqual({by_url[url]['id']: 'store_adapter_not_integrated_in_lab'
                          for url in (ADAPTER_HOME, ADAPTER_LIST)}, result['root_holds'])
        self.assertTrue({ADAPTER_HOME, ADAPTER_LIST, ADAPTER_PRODUCT} <= set(result['resources']))
        self.assertNotIn(by_url[ADAPTER_PRODUCT]['id'], result['root_holds'])

    def test_explicit_candidates_are_sorted_and_only_change_planned_product_roles(self):
        candidates = [HELD, PRODUCT]
        before = list(candidates)
        result = self.load(candidate_urls=candidates)
        self.assertEqual(sorted(candidates), result['candidate_urls'])
        self.assertEqual(before, candidates)
        self.assertEqual(set(candidates), {root['url'] for root in result['roots']['roots'] if root['role'] == 'candidate'})
        self.assertEqual(self.load()['resources'], result['resources'])
        self.assertNotEqual(self.load()['roots']['input_hash'], result['roots']['input_hash'])
        self.assertEqual(result['roots'], self.load(candidate_urls=list(reversed(candidates)))['roots'])

    def test_invalid_candidate_shapes_and_duplicates_fail_before_verification(self):
        for candidates in (PRODUCT, (PRODUCT,), {PRODUCT}, {}, 1, [PRODUCT, PRODUCT], [None], [[]]):
            with self.subTest(candidates=candidates), patch('monitor_lab.capture_inputs.verify_capture') as verify:
                with self.assertRaises(ValueError):
                    self.load(candidate_urls=candidates)
                verify.assert_not_called()

    def test_unplanned_and_nonproduct_candidates_are_rejected(self):
        for candidate in (HOME, LIST, ADAPTER_HOME, ADAPTER_LIST, 'https://www.pc-koubou.jp/unplanned', PRODUCT + '#variant'):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, 'planned product'):
                self.load(candidate_urls=[candidate])

    def test_unknown_method_and_receipt_alias_fail_before_verification(self):
        for method in ('pooled_http11', 'other', '', None, []):
            with self.subTest(method=method), patch('monitor_lab.capture_inputs.verify_capture') as verify:
                with self.assertRaises(ValueError):
                    self.load(method=method)
                verify.assert_not_called()

    def test_known_but_unrequested_method_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'not requested'):
            self.load(method='browser')

    def test_store_settings_must_equal_retained_config_even_for_unrequested_stores(self):
        for store in ('koubou', 'amazon'):
            config = deepcopy(self.config)
            config[store]['adapter'] = 'changed'
            with self.subTest(store=store), self.assertRaisesRegex(ValueError, 'configuration'):
                self.load(config=config)
        with self.assertRaises(ValueError):
            self.load(config=self.settings)

    def test_browser_resources_preserve_response_receipts_and_distinct_dom(self):
        result = self.load(capture=self.browser, method='browser')
        resource = result['resources'][PRODUCT]
        receipt = result['verified']['receipts'][resource['source_receipt_index']]
        self.assertEqual('browser', result['source_method'])
        self.assertEqual(receipt['body_sha256'], resource['body_sha256'])
        self.assertNotEqual(receipt['rendered_dom']['body_sha256'], resource['body_sha256'])
        self.assertEqual(receipt['content_type'], resource['content_type'])
        self.assertIsNone(result['resources'][HELD]['body_sha256'])
        self.assertEqual(404, result['resources'][MISSING]['status'])

    def test_latest_observed_at_uses_only_selected_rows_including_held(self):
        self.assertEqual('2026-10-04T03:07:00+00:00', self.load(method='urllib')['observed_at'])
        self.assertEqual('2026-10-04T04:07:00+00:00', self.load()['observed_at'])

    def test_observed_at_compares_instants_and_preserves_original_string(self):
        times = ['2026-10-04T14:00:00+09:00', '2026-10-04T06:00:00+02:00'] + ['2026-10-04T03:00:00Z'] * 6
        source = self.make_capture('timestamps', change=lambda method, index, row: row.update(observed_at=times[index]))
        before = bundle_hashes(source)
        result = self.load(capture=source)
        self.assertEqual(times[0], result['observed_at'])
        self.assertEqual(times, [resource['observed_at'] for resource in result['resources'].values()])
        self.assertEqual(before, bundle_hashes(source))

    def test_any_invalid_selected_date_is_rejected_including_failed_and_held_rows(self):
        for index, invalid in enumerate(('2026-02-30T03:00:00Z', 'invalid', '', None, 123, [], {}, 'invalid')):
            with self.subTest(index=index, invalid=invalid):
                source = self.make_capture('invalid-date-' + str(index), change=lambda method, number, row:
                                           row.update(observed_at=invalid) if method == 'pooled' and number == index else None)
                before = bundle_hashes(source)
                with self.assertRaisesRegex(ValueError, 'valid observed_at'):
                    self.load(capture=source)
                self.assertEqual('2026-10-04T03:07:00+00:00', self.load(capture=source, method='urllib')['observed_at'])
                self.assertEqual(before, bundle_hashes(source))

    def test_no_valid_selected_time_fails_instead_of_using_now_or_other_method(self):
        source = self.make_capture('invalid-times', change=lambda method, index, row:
                                   row.update(observed_at='invalid') if method == 'pooled' else None)
        with self.assertRaisesRegex(ValueError, 'observed_at'):
            self.load(capture=source)
        self.assertEqual('2026-10-04T03:07:00+00:00', self.load(capture=source, method='urllib')['observed_at'])

    def test_manifest_hash_is_original_bytes_and_roots_are_stable_when_relocated(self):
        source = self.copied_capture('whitespace')
        manifest = source / 'capture-manifest.json'
        original = manifest.read_bytes() + b' \r\n'
        atomic_bytes(manifest, original)
        result = self.load(capture=source)
        self.assertEqual(hashlib.sha256(original).hexdigest(), result['manifest_sha256'])
        self.assertNotEqual(digest(result['verified']['manifest']), result['manifest_sha256'])
        moved = self.copied_capture('relocated', source)
        self.assertEqual(result, self.load(capture=moved))
        self.assertNotEqual(self.load()['roots']['input_hash'], result['roots']['input_hash'])
        self.assertTrue({root['id'] for root in self.load()['roots']['roots']}.isdisjoint(
            {root['id'] for root in result['roots']['roots']}))
        self.assertEqual(original, manifest.read_bytes())

    def test_root_hash_binds_method_and_primary_group_without_mutating_inputs(self):
        pooled, urllib = self.load(), self.load(method='urllib')
        self.assertEqual(pooled['plan_hash'], pooled['verified']['metadata']['scope_plan']['plan_hash'])
        self.assertEqual(pooled['roots']['roots'], urllib['roots']['roots'])
        self.assertNotEqual(pooled['roots']['input_hash'], urllib['roots']['input_hash'])
        primary = deepcopy(self.primary)
        primary[0]['group'] = 'another-group'
        self.assertNotEqual(pooled['roots']['input_hash'], self.load(primary_pages=primary)['roots']['input_hash'])

    def test_legacy_unscoped_capture_is_rejected_after_successful_capture_verification(self):
        source = self.make_capture('unscoped', scoped=False)
        verified = verify_capture(source)
        records = verified['manifest']['records']
        manifest = deepcopy(verified['manifest'])
        manifest.update(format=LEGACY_FORMAT)
        manifest.pop('records')
        manifest.pop('generation')
        aliases = {records['receipts']: 'receipts.partial.json', records['hosts']: 'hosts.json',
                   records['result']: 'result.json'}
        for entry in manifest['files']:
            if entry['path'] in aliases:
                entry['path'] = aliases[entry['path']]
        manifest['files_sha256'] = digest(manifest['files'])
        write(source / 'capture-manifest.json', manifest)
        self.assertTrue(verify_capture(source)['manifest']['complete'])
        before = bundle_hashes(source)
        with self.assertRaisesRegex(ValueError, 'scope_plan'):
            self.load(capture=source)
        self.assertEqual(before, bundle_hashes(source))

    def test_complete_modern_unscoped_capture_is_rejected(self):
        source = self.make_capture('modern-unscoped', scoped=False)
        with self.assertRaisesRegex(ValueError, 'scope_plan'):
            self.load(capture=source)

    def test_incomplete_scoped_capture_is_rejected(self):
        source = self.make_capture('partial', complete=False)
        before = bundle_hashes(source)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.load(capture=source)
        self.assertEqual(before, bundle_hashes(source))

    def test_tampered_body_is_rejected_and_preserved(self):
        source = self.copied_capture('changed-body')
        receipt = verify_capture(source)['receipts'][0]
        atomic_bytes(source / receipt['body_file'], b'forged body')
        before = bundle_hashes(source)
        with self.assertRaises(ValueError):
            self.load(capture=source)
        self.assertEqual(before, bundle_hashes(source))

    def test_rehashed_metadata_and_receipt_plan_drift_are_rejected(self):
        for change in ('metadata', 'kind', 'method', 'url', 'missing'):
            with self.subTest(change=change):
                source = self.copied_capture('tampered-' + change)
                verified = verify_capture(source)
                records = verified['manifest']['records']
                if change == 'metadata':
                    metadata = verified['metadata']
                    metadata['request_plan'][0][1] = PRODUCT
                    write(source / 'study.json', metadata)
                else:
                    rows = verified['receipts']
                    if change == 'kind':
                        rows[0]['resource_kind'] = 'product'
                    elif change == 'method':
                        rows[8]['method'] = 'pooled'
                    elif change == 'url':
                        rows[0]['url'] = 'https://www.pc-koubou.jp/unplanned'
                    else:
                        rows.pop()
                        manifest = read(source / 'capture-manifest.json')
                        manifest['receipt_count'] -= 1
                        write(source / 'capture-manifest.json', manifest)
                    result = verified['result']
                    result['receipts'] = deepcopy(rows)
                    result['scope'] = scope_report(verified['metadata']['scope_plan'], rows)
                    write(source / records['receipts'], rows)
                    write(source / records['result'], result)
                self.rehash_manifest(source)
                before = bundle_hashes(source)
                with self.assertRaises(ValueError):
                    self.load(capture=source)
                self.assertEqual(before, bundle_hashes(source))

    def test_manifest_change_during_verification_is_rejected(self):
        source = self.copied_capture('changed-pointer')
        def changed_manifest(root):
            verified = verify_capture(root)
            path = root / 'capture-manifest.json'
            atomic_bytes(path, path.read_bytes() + b' ')
            return verified
        with patch('monitor_lab.capture_inputs.verify_capture', side_effect=changed_manifest):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                self.load(capture=source)


if __name__ == '__main__':
    unittest.main()
