from contextlib import ExitStack
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sale_monitor.http import Client, FetchError
from sale_monitor.models import Offer
from sale_monitor.reporting import health
from sale_monitor.retrying import runnable
from sale_monitor.runner import Collector
from test_retrying import Clock, NOW, OLD
from test_parser_response_evidence import VALID


HOST = 'www.ark-pc.co.jp'
URL = 'https://' + HOST + '/i/12345678/'
MAINTENANCE = 'https://maintenance.ark-pc.co.jp/'
BODY = '<h1>ただいまメンテナンス中です</h1>'.encode()
CFG = {'stores': {'ark': {'adapter': 'html', 'seed_urls': ['https://' + HOST + '/'], 'browser_fallback': True}}}


def response(url=MAINTENANCE, body=BODY):
    result = MagicMock()
    result.__enter__.return_value = result
    result.url = url
    result.status = 200
    result.headers = {'Content-Type': 'text/html;charset=utf-8'}
    result.read.return_value = body
    return result


class ArkMaintenanceTests(unittest.TestCase):
    def test_http_200_counts_once_and_cooldown_blocks_other_paths_and_browser(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client(delay=0, browser=True)
            with patch.object(client.opener, 'open', return_value=response()) as network:
                with self.assertRaisesRegex(FetchError, '^store_maintenance$') as caught:
                    client.get(URL)
                self.assertEqual(caught.exception.page.body, BODY)
                self.assertEqual(client.access_retry_after[HOST], {'until': 1300, 'reason': 'store_maintenance'})
                for url in ('https://' + HOST + '/', URL + '?other=1'):
                    with self.assertRaisesRegex(FetchError, '^access_retry_later:store_maintenance$'):
                        client.get(url)
                with patch('playwright.sync_api.sync_playwright') as browser:
                    with self.assertRaisesRegex(FetchError, 'access_retry_later:store_maintenance'):
                        client.rendered(URL)
                    browser.assert_not_called()
                self.assertEqual(network.call_count, 1)
            self.assertEqual(client.count, 1)
            self.assertEqual(client.evidence.snapshot()['response_status_counts']['http'], {'200': 1})

    def test_browser_maintenance_is_also_rejected_with_response_evidence(self):
        client = Client(browser=True, delay=0)
        with patch('playwright.sync_api.sync_playwright') as factory:
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            page = browser.new_page.return_value
            page.goto.return_value.status = 200
            page.url = MAINTENANCE
            page.content.return_value = BODY.decode()
            with self.assertRaisesRegex(FetchError, '^store_maintenance$') as caught:
                client.rendered(URL)
            self.assertEqual(caught.exception.page.method, 'browser')
            browser.close.assert_called_once()
        self.assertEqual(client.count, 1)
        self.assertEqual(client.evidence.snapshot()['response_status_counts']['browser'], {'200': 1})

    def test_product_list_and_search_remain_pending_with_original_age(self):
        tasks = [{'type': 'product', 'url': URL},
                 {'type': 'list', 'url': 'https://' + HOST + '/', 'sale_page': False},
                 {'type': 'search', 'query': 'MODEL-1', 'kind': 'comparison'}]
        for task in tasks:
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                clock = Clock(); clock.patch(stack)
                client = Client(delay=0, browser=True)
                stack.enter_context(patch.object(client.opener, 'open', return_value=response()))
                fallback = stack.enter_context(patch.object(client, 'rendered'))
                c = Collector(Path(tmp), 'ark', CFG, 'r1', client)
                c.seed = lambda: None
                old = Offer('ark', 'MODEL-1', URL, price_yen=9000, stock='in_stock', observed_at=OLD, observed_run_id='old')
                c.state['offers'][old.key] = old.to_dict()
                c.state['journal'] = [{'run_id': 'old', 'offer': old.to_dict()}]
                journal = deepcopy(c.state['journal'])
                key = c.enqueue({**task, 'created_at': OLD, 'attempts': 7})
                result = c.collect(seconds=60)
                fallback.assert_not_called()
                self.assertEqual(result['queue'][key]['created_at'], OLD)
                self.assertEqual(result['queue'][key]['attempts'], 8)
                self.assertEqual(result['queue'][key]['last_error'], 'store_maintenance')
                self.assertEqual(result['queue'][key]['retry_at_epoch_seconds'], 1300)
                self.assertEqual(result['done'], [])
                self.assertFalse(result['cycle_complete'])
                self.assertEqual(result['journal'], journal)
                self.assertEqual(result['offers'][old.key]['stock'], 'in_stock')
                self.assertEqual(health(result, NOW)['current_offers'], 0)
                self.assertEqual(health(result, NOW)['pending_over_24h'], 1)
                self.assertEqual(result['errors'][0]['response_evidence']['body_sha256'], hashlib.sha256(BODY).hexdigest())
                resumed = Collector(Path(tmp), 'ark', CFG, 'r2', Client(delay=0))
                ready, waits = runnable(resumed.state['queue'], set(), CFG['stores']['ark'], resumed.client, {}, 1299)
                self.assertEqual(ready, [])
                self.assertEqual(sum(x['affected_tasks'] for x in waits), 1)
                self.assertEqual(resumed.state['queue'], result['queue'])

    def test_waiters_are_not_attempts_and_valid_product_recovers_after_expiry(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            client = Client(delay=0)
            def get(request, **kwargs):
                return response() if clock.now < 1300 else response(request.full_url, VALID)
            stack.enter_context(patch.object(client.opener, 'open', side_effect=get))
            cfg = deepcopy(CFG)
            cfg['stores']['ark']['browser_fallback'] = False
            c = Collector(Path(tmp), 'ark', cfg, 'r1', client)
            c.seed = lambda: None
            for n in range(8):
                c.enqueue({'type': 'product', 'url': URL + '?n=' + str(n), 'created_at': OLD})
            before = deepcopy(c.state['queue'])
            held = c.collect(seconds=60)
            self.assertEqual(len(held['queue']), 8)
            self.assertEqual(client.count, 1)
            self.assertEqual(sum(t['attempts'] for t in held['queue'].values()), 1)
            self.assertTrue(all(t['created_at'] == before[k]['created_at'] for k, t in held['queue'].items()))
            recovered = c.collect(seconds=600)
            self.assertEqual(recovered['queue'], {})
            self.assertEqual(len(recovered['done']), 8)
            self.assertEqual(client.count, 9)
            self.assertEqual(recovered['errors'], [])
            self.assertEqual(recovered['recovered_errors'][0]['reason'], 'store_maintenance')

    def test_normal_redirects_and_maintenance_words_in_products_are_unchanged(self):
        for url, body in [(URL, VALID), (URL + '?normal=1', BODY),
                          ('https://www.tsukumo.co.jp/', BODY)]:
            with self.subTest(url=url):
                client = Client(delay=0)
                with patch.object(client.opener, 'open', return_value=response(url, body)):
                    page = client.get(URL)
                self.assertEqual(page.url, url)
                self.assertEqual(page.body, body)
                self.assertEqual(client.access_retry_after, {})


if __name__ == '__main__':
    unittest.main()
