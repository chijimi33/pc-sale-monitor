from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from sale_monitor.models import STORES
from monitor_lab.acquire import PooledTransport, UrllibTransport
from monitor_lab.capture import verify_capture
from monitor_lab.request_plan import FORMAT, load_plan, scope_report, validate_plan
from monitor_lab.safety import allowed_root, digest, read, write
from monitor_lab.study import study


def signed(plan):
    plan = deepcopy(plan)
    plan.pop('plan_hash', None)
    return {**plan, 'plan_hash': digest(plan)}


def sample_plan():
    return signed({'format': FORMAT, 'created_at': '2026-10-04T00:00:00+09:00',
                   'source_data_sha': '1' * 40,
                   'resources': [{'store': 'ark', 'url': 'https://www.ark-pc.co.jp/', 'kind': 'home'},
                                 {'store': 'ark', 'url': 'https://www.ark-pc.co.jp/search/?onsale=1', 'kind': 'list'},
                                 {'store': 'ark', 'url': 'https://www.ark-pc.co.jp/i/12201487/', 'kind': 'product'}],
                   'not_requested': [{'store': store, 'reason': 'not_selected_for_this_experiment'}
                                     for store in STORES if store != 'ark']})


class RequestPlanTest(unittest.TestCase):
    def setUp(self):
        self.config = read(Path(__file__).resolve().parents[2] / 'config/sources.json')['stores']
        self.root = allowed_root() / 'tests' / ('request-plan-' + uuid.uuid4().hex)

    def test_signed_plan_preserves_intent_without_creating_observation_values(self):
        plan = sample_plan()
        write(self.root / 'plan.json', plan)
        actual = load_plan(self.root / 'plan.json', self.config)
        self.assertEqual(plan, actual)
        actual['resources'][0]['url'] = 'changed'
        self.assertNotEqual(actual, plan)
        self.assertTrue(all(set(row) == {'store', 'url', 'kind'} for row in plan['resources']))

    def test_urls_are_bound_to_store_and_never_use_credentials_or_other_origins(self):
        for url in ('http://www.ark-pc.co.jp/i/1/', 'https://shop.tsukumo.co.jp/goods/1/',
                    'https://www.ark-pc.co.jp.evil.invalid/', 'https://127.0.0.1/',
                    'https://user:secret@www.ark-pc.co.jp/', 'https://www.ark-pc.co.jp:8443/',
                    'https://www.ark-pc.co.jp:bad/', 'https://www.ark-pc.co.jp/#product',
                    'https://www.ark-pc.co.jp/\n', 'https://www.rakuten.co.jp/'):
            with self.subTest(url=url):
                plan = sample_plan()
                plan['resources'][0]['url'] = url
                with self.assertRaises(ValueError):
                    validate_plan(signed(plan), self.config)

    def test_scope_has_all_ten_stores_and_cannot_duplicate_urls_or_omit_missing_stores(self):
        variants = []
        plan = sample_plan(); plan['not_requested'].pop(); variants.append(plan)
        plan = sample_plan(); plan['not_requested'].append({'store': 'ark', 'reason': 'conflict'}); variants.append(plan)
        plan = sample_plan(); plan['not_requested'].append(deepcopy(plan['not_requested'][0])); variants.append(plan)
        plan = sample_plan(); plan['resources'].append({'store': 'ark', 'url': 'https://WWW.ARK-PC.CO.JP:443/', 'kind': 'home'}); variants.append(plan)
        plan = sample_plan(); plan['resources'] = []; variants.append(plan)
        plan = sample_plan(); plan['resources'] = [{'store': 'ark', 'url': f'https://www.ark-pc.co.jp/i/{i}/', 'kind': 'product'} for i in range(21)]; variants.append(plan)
        for plan in variants:
            with self.subTest(plan=plan):
                with self.assertRaises(ValueError):
                    validate_plan(signed(plan), self.config)

    def test_malformed_intent_never_becomes_verified_response_metadata(self):
        for change in ('timestamp', 'commit', 'observed_status', 'kind', 'omitted_reason', 'unknown_store', 'checksum'):
            plan = sample_plan()
            if change == 'timestamp': plan['created_at'] = 'yesterday'
            elif change == 'commit': plan['source_data_sha'] = 'main'
            elif change == 'observed_status': plan['resources'][0]['status'] = 200
            elif change == 'kind': plan['resources'][0]['kind'] = ['product']
            elif change == 'omitted_reason': plan['not_requested'][0]['reason'] = ' '
            elif change == 'unknown_store': plan['not_requested'][0]['store'] = ['yahoo']
            else: plan['created_at'] = '2026-10-03T00:00:00+09:00'
            if change != 'checksum': plan = signed(plan)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan(plan, self.config)

    def test_response_counts_are_not_product_parsing_or_unique_url_counts(self):
        plan = sample_plan()
        product = plan['resources'][2]['url']
        rows = [{'store': 'ark', 'url': product, 'resource_kind': 'product', 'status': 200,
                 'attempts': [{}], 'error': None} for _ in range(2)]
        rows += [{'store': 'ark', 'url': plan['resources'][0]['url'], 'resource_kind': 'home', 'status': 200,
                  'attempts': [{}], 'error': None},
                 {'store': 'ark', 'url': plan['resources'][1]['url'], 'resource_kind': 'list', 'status': None,
                  'attempts': [], 'error': 'shared_host_wait'}]
        result = scope_report(plan, rows)
        self.assertEqual(10, result['monitored_store_count'])
        self.assertEqual(set(STORES), set(result['stores']))
        self.assertFalse(result['full_store_coverage_proven'])
        ark = result['stores']['ark']
        self.assertEqual(3, ark['confirmed_http_attempts'])
        self.assertEqual(3, ark['successful_responses'])
        self.assertEqual(2, ark['successful_product_responses'])
        self.assertEqual([product], ark['successful_product_urls'])
        self.assertEqual('not_selected_for_this_experiment', result['stores']['yahoo']['not_requested_reason'])

    def test_invalid_plan_and_budget_fail_before_transport_or_output(self):
        plan = sample_plan(); plan['resources'][0]['url'] = 'https://example.invalid/'
        file = self.root / 'bad-plan.json'; write(file, signed(plan))
        with patch('monitor_lab.study.TRANSPORTS', {'unused': lambda: self.fail('Transport was constructed')}):
            for budget in (0, 2101, float('nan'), float('inf'), True, '25'):
                with self.subTest(budget=budget), self.assertRaises(ValueError):
                    study(self.root / 'output', methods=['unused'], budget=budget)
            with self.assertRaises(ValueError):
                study(self.root / 'output', methods=['unused'], plan=file)
        self.assertFalse((self.root / 'output').exists())

    def test_real_transport_classes_keep_cli_alias_separate_from_receipt_identity(self):
        plan = sample_plan()
        plan['resources'] = plan['resources'][:1]
        file = self.root / 'plan.json'; write(file, signed(plan))
        body = (200, {'Content-Type': 'text/html'}, b'<html>Fixture home</html>')
        with patch.object(UrllibTransport, 'get', return_value=body) as urllib_get, \
                patch.object(PooledTransport, 'get', return_value=body) as pooled_get, \
                patch('socket.socket', side_effect=AssertionError('Offline transport contract test')):
            result = study(self.root / 'capture', plan=file, budget=10)
        verified = verify_capture(self.root / 'capture')
        self.assertEqual(['urllib', 'pooled'], verified['metadata']['methods'])
        self.assertEqual(['urllib', 'pooled_http11'], [row['method'] for row in result['receipts']])
        self.assertEqual(2, result['successful_pages'])
        self.assertEqual((1, 1), (urllib_get.call_count, pooled_get.call_count))


if __name__ == '__main__':
    unittest.main()
