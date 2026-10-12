from contextlib import ExitStack
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from sale_monitor.http import Client as HttpClient, FetchError
from sale_monitor.retrying import runnable
from sale_monitor.runner import Collector
from sale_monitor.reporting import health
from test_retrying import Clock, NOW, OLD


HOST = 'www.sofmap.com'
BASE = 'https://' + HOST + '/product_detail.aspx?sku='


class Client(HttpClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opener.open = MagicMock(side_effect=HTTPError(BASE + 'unexpected', 403, '', {}, BytesIO()))


def deny(client, url, retry=None):
    headers = {} if retry is None else {'Retry-After': retry}
    with patch.object(client.opener, 'open', side_effect=HTTPError(url, 403, '', headers, BytesIO())):
        with unittest.TestCase().assertRaisesRegex(FetchError, '^http_403$'):
            client.get(url)


def success(client, url):
    response = MagicMock()
    response.__enter__.return_value = response
    response.url = url; response.status = 200; response.headers = {}
    response.read.return_value = b'<h1>OK</h1>'
    with patch.object(client.opener, 'open', return_value=response):
        return client.get(url)


class AccessBackoffTests(unittest.TestCase):
    def test_distinct_denials_defer_without_fabricating_request_counts(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client(delay=0)
            for n in range(3): deny(client, BASE + str(n))
            with patch.object(client.opener, 'open') as network:
                with self.assertRaisesRegex(FetchError, 'access_retry_later:http_403'):
                    client.get(BASE + '4')
                network.assert_not_called()
            self.assertEqual(client.count, 3)
            self.assertEqual(client.evidence.snapshot()['response_status_counts']['http'], {'403': 3})
            self.assertEqual(client.access_retry_after[HOST]['until'], 1300)

    def test_single_url_or_success_between_denials_does_not_block_host(self):
        client = Client(delay=0)
        for _ in range(5): deny(client, BASE + 'same')
        self.assertFalse(getattr(client, 'access_retry_after', {}))
        success(client, BASE + 'ok')
        for n in range(3):
            deny(client, BASE + str(n))
            success(client, BASE + 'ok')
        self.assertFalse(getattr(client, 'access_retry_after', {}))

    def test_host_scope_and_expiry_allow_later_observation(self):
        clock = Clock()
        with ExitStack() as stack:
            clock.patch(stack)
            client = Client(delay=0)
            for n in range(3): deny(client, BASE + str(n))
            success(client, 'https://www.ark-pc.co.jp/i/1/')
            clock.now = 1299
            with self.assertRaisesRegex(FetchError, 'access_retry_later'): client.get(BASE + '4')
            clock.now = 1300
            self.assertEqual(success(client, BASE + '4').status, 200)
            self.assertNotIn(HOST, client.access_retry_after)

    def test_server_deadline_is_respected_on_first_denial(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client(delay=0)
            deny(client, BASE + '1', '900')
            self.assertEqual(client.access_retry_after[HOST]['until'], 1900)
            with self.assertRaisesRegex(FetchError, 'access_retry_later'): client.get(BASE + '2')

    def test_browser_cannot_bypass_shared_denial_deadline(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client(browser=True, delay=0)
            for n in range(3): deny(client, BASE + str(n))
            with patch('playwright.sync_api.sync_playwright') as browser:
                with self.assertRaisesRegex(FetchError, 'access_retry_later'): client.rendered(BASE + '4')
                browser.assert_not_called()

    def test_browser_denials_also_start_shared_deadline(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client(browser=True, delay=0)
            for n in range(3):
                with patch('playwright.sync_api.sync_playwright') as factory:
                    browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
                    response = browser.new_page.return_value.goto.return_value
                    response.status = 403; response.url = BASE + str(n); response.header_value.return_value = None
                    with self.assertRaisesRegex(FetchError, '^http_403$'): client.rendered(BASE + str(n))
            with self.assertRaisesRegex(FetchError, 'access_retry_later'): client.get(BASE + '4')
            self.assertEqual(client.count, 3)

    def test_restart_preserves_waiters_age_attempts_and_history(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            root = Path(tmp); client = Client(delay=0)
            for n in range(3): deny(client, BASE + str(n))
            cfg = {'stores': {'sofmap': {'adapter': 'html', 'seed_urls': []}}}
            collector = Collector(root, 'sofmap', cfg, 'r1', client)
            for n in range(8):
                collector.enqueue({'type': 'product', 'url': BASE + str(n), 'created_at': OLD, 'attempts': 7})
            collector.state['journal'] = [{'observation_id': 'retained', 'run_id': 'older'}]
            before = deepcopy(collector.state['queue'])
            collector.save()
            resumed = Collector(root, 'sofmap', cfg, 'r2', Client(delay=0))
            resumed.seed = lambda: None
            result = resumed.collect(seconds=60)
            self.assertEqual(result['queue'], before)
            self.assertEqual(result['journal'], [{'observation_id': 'retained', 'run_id': 'older'}])
            self.assertEqual(resumed.client.count, 0)
            self.assertFalse(result['cycle_complete'])
            self.assertEqual(result['pending_count'], 8)
            self.assertEqual(health(result, NOW)['current_offers'], 0)
            self.assertEqual(health(result, NOW)['pending_over_24h'], 8)
            self.assertEqual(result['waiting_dependencies'][0]['affected_tasks'], 8)
            self.assertEqual(result['waiting_dependencies'][0]['reason'], 'access_retry_later:http_403')
            self.assertEqual(health(result, NOW)['access_retry_after'][HOST]['until'], 1300)
            ready, waits = runnable(result['queue'], set(), cfg['stores']['sofmap'], resumed.client, {}, 1300)
            self.assertEqual(len(ready), 8)
            self.assertEqual(waits, [])


if __name__ == '__main__':
    unittest.main()
