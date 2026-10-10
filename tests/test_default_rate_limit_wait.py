from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from sale_monitor.http import Client, FetchError
from sale_monitor.runner import Collector
from sale_monitor.reporting import health
from test_retry_fairness import Clock, OfflineTransport, NOW, OLD, CFG


URL = 'https://shop.tsukumo.co.jp/goods/123/'


class DefaultRateLimitWait(unittest.TestCase):
    def test_missing_invalid_and_expired_deadlines_defer_entire_host(self):
        for retry in ['', 'not-a-date', '0', '-1', 'NaN', 'Thu, 01 Jan 1970 00:00:00 GMT']:
            with self.subTest(retry=retry), patch('sale_monitor.http.time.time', return_value=1000):
                client = Client(browser=True, delay=0)
                with patch.object(client.opener, 'open', side_effect=HTTPError(URL, 429, '', {'Retry-After': retry}, None)) as op:
                    with self.assertRaises(FetchError): client.get(URL)
                    with self.assertRaises(FetchError): client.get(URL + 'other')
                    with self.assertRaises(FetchError): client.rendered(URL)
                self.assertEqual(op.call_count, 1)
                self.assertEqual(client.retry_after['shop.tsukumo.co.jp'], 1300)

    def test_future_server_deadline_is_preserved_including_short_waits(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client()
            self.assertEqual(client.defer_response('a.example', 429, '30'), 30)
            self.assertEqual(client.retry_after['a.example'], 1030)
            self.assertEqual(client.defer_response('a.example', 429, '900'), 900)
            client.defer_response('a.example', 429, None)
            self.assertEqual(client.retry_after['a.example'], 1900)

    def test_503_default_policy_is_unchanged(self):
        with patch('sale_monitor.http.time.time', return_value=1000):
            client = Client()
            self.assertEqual(client.defer_response('a.example', 503, None), 2)
            self.assertEqual(client.defer_response('a.example', 503, '0'), 0)

    def test_browser_429_without_header_sets_same_deadline(self):
        client = Client(browser=True)
        with patch('playwright.sync_api.sync_playwright') as factory, patch('sale_monitor.http.time.time', return_value=1000):
            response = factory.return_value.__enter__.return_value.chromium.launch.return_value.new_page.return_value.goto.return_value
            response.status = 429; response.url = URL; response.header_value.return_value = None
            with self.assertRaises(FetchError): client.rendered(URL)
        self.assertEqual(client.retry_after['shop.tsukumo.co.jp'], 1300)
        self.assertEqual(client.count, 1)

    def test_persistent_429_reduces_attempts_without_removing_or_refreshing_queue(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            clock = Clock(); clock.install(stack)
            transport = OfflineTransport(clock, lambda u, _: HTTPError(u, 429, '', {}, None))
            c = Collector(Path(folder), 'tsukumo', CFG, 'run', transport.client())
            for n in range(100): c.enqueue({'type': 'product', 'url': URL + str(n), 'created_at': OLD, 'attempts': 2})
            before = deepcopy(c.state['queue'])
            result = c.collect(seconds=650)
            self.assertEqual(len(transport.calls), 3)
            self.assertEqual(set(result['queue']), set(before))
            for key, task in result['queue'].items():
                self.assertEqual(task['created_at'], before[key]['created_at'])
                self.assertGreaterEqual(task['attempts'], before[key]['attempts'])
            self.assertEqual(result['offers'], {})
            self.assertEqual(health(result, clock.utcnow())['pending_over_24h'], 100)

    def test_other_host_and_recovery_after_wait_use_real_collector(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            clock = Clock(); clock.install(stack); start = clock.now
            transport = OfflineTransport(clock, lambda u, _: HTTPError(u, 429, '', {}, None)
                                         if u.startswith(URL) and clock.now < start+300 else None)
            c = Collector(Path(folder), 'tsukumo', CFG, 'run', transport.client())
            for url in [URL, URL+'2', 'https://b.example/healthy']:
                c.enqueue({'type': 'product', 'url': url, 'created_at': OLD})
            result = c.collect(seconds=650)
            self.assertEqual(result['queue'], {})
            self.assertEqual(len(result['offers']), 3)
            self.assertEqual(len(transport.calls), 4)
            self.assertLess(next(t for u,t in transport.calls if 'b.example' in u), start+300)
            self.assertTrue(all(t >= start+300 for u,t in transport.calls[1:] if u.startswith(URL)))

    def test_checkpoint_restart_keeps_deadline_and_original_tasks(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            clock = Clock(); clock.install(stack); start = clock.now
            transport = OfflineTransport(clock, lambda u, _: HTTPError(u, 429, '', {}, None))
            root = Path(folder); c = Collector(root, 'tsukumo', CFG, 'r1', transport.client())
            c.enqueue({'type': 'product', 'url': URL, 'created_at': OLD})
            c.collect(seconds=60)
            resumed_transport = OfflineTransport(clock)
            resumed = Collector(root, 'tsukumo', CFG, 'r2', resumed_transport.client())
            resumed.collect(seconds=60)
            self.assertEqual(resumed_transport.calls, [])
            task = next(iter(resumed.state['queue'].values()))
            self.assertEqual((task['created_at'], task['attempts']), (OLD, 1))
            clock.now = start+302
            resumed.collect(seconds=60)
            self.assertEqual(len(resumed_transport.calls), 1)
            self.assertEqual(resumed.state['queue'], {})


if __name__ == '__main__':
    unittest.main()
