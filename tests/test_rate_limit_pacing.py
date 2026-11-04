from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from sale_monitor.http import Client, FetchError
from sale_monitor.models import utcnow
from sale_monitor.reporting import health
from sale_monitor.runner import Collector


HOST = 'www.ark-pc.co.jp'
URL = 'https://' + HOST + '/i/123/'
CFG = {'stores': {'ark': {'adapter': 'html', 'seed_urls': []}}}


class Clock:
    def __init__(self): self.now = 1000.0
    def sleep(self, seconds): self.now += seconds
    def time(self): return self.now
    def context(self):
        stack = ExitStack()
        stack.enter_context(patch('sale_monitor.http.time.time', self.time))
        stack.enter_context(patch('sale_monitor.http.time.monotonic', self.time))
        stack.enter_context(patch('sale_monitor.http.time.sleep', self.sleep))
        return stack


class Response:
    status = 200
    headers = {}
    def __init__(self, url): self.url = url
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return b'<html>response</html>'


class RateLimitPacing(unittest.TestCase):
    def test_cooldown_is_preserved_then_subsequent_urls_are_spaced(self):
        clock = Clock(); client = Client(); called = []
        def open_page(request, **kwargs):
            called.append(clock.now)
            if len(called) == 1:
                raise HTTPError(request.full_url, 429, '', {'Retry-After': '180'}, None)
            return Response(request.full_url)
        with clock.context(), patch.object(client.opener, 'open', side_effect=open_page):
            with self.assertRaises(FetchError): client.get(URL)
            with self.assertRaises(FetchError): client.get(URL + 'other')
            self.assertEqual(len(called), 1)
            self.assertEqual(client.retry_after[HOST], 1180)
            clock.sleep(180)
            client.get(URL + 'other')
            client.get(URL + 'next')
        self.assertEqual(called, [1000, 1180, 1182])
        self.assertEqual(client.count, 3)
        self.assertEqual(client.evidence.responses['http'], {'429': 1, '200': 2})

    def test_unrelated_host_and_non429_response_keep_configured_rate(self):
        clock = Clock(); client = Client(); calls = []
        def open_page(request, **kwargs):
            calls.append((request.full_url, clock.now)); return Response(request.full_url)
        with clock.context(), patch.object(client.opener, 'open', side_effect=open_page):
            client.defer_response(HOST, 429, '180')
            other = 'https://www.sofmap.com/product_detail.aspx?sku=1'
            client.get(other); client.get(other)
            client.defer_response('www.sofmap.com', 503, '180')
            clock.sleep(180)
            client.get(other); client.get(other)
        self.assertEqual([t for _, t in calls], [1000, 1001, 1181, 1182])
        self.assertNotIn('www.sofmap.com', client.rate_limit_spacing)

    def test_browser_navigation_and_http_share_host_spacing(self):
        clock = Clock(); client = Client(browser=True); calls = []
        with clock.context(), patch.object(client.opener, 'open', return_value=Response(URL)), \
                patch('playwright.sync_api.sync_playwright') as factory:
            client.defer_response(HOST, 429, '180'); clock.sleep(180)
            client.get(URL)
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            page = browser.new_page.return_value
            response = page.goto.return_value
            response.status = 200; response.url = URL
            page.goto.side_effect = lambda *args, **kwargs: calls.append(clock.now) or response
            page.url = URL; page.content.return_value = '<html>rendered</html>'
            client.rendered(URL)
            self.assertEqual(calls, [1182])
            self.assertEqual(client.count, 2)
            browser.close.assert_called_once()

    def test_repeated_429_is_bounded_without_shortening_server_wait(self):
        clock = Clock(); client = Client()
        with clock.context():
            client.defer_response(HOST, 429, '900')
            for _ in range(20): client.defer_response(HOST, 429, '180')
        self.assertEqual(client.rate_limit_spacing[HOST], 30)
        self.assertEqual(client.retry_after[HOST], 1900)

    def test_same_run_checkpoint_restores_spacing_and_all_task_history(self):
        clock = Clock()
        with clock.context(), tempfile.TemporaryDirectory() as directory:
            root = Path(directory); client = Client()
            c = Collector(root, 'ark', CFG, 'same', client)
            c.enqueue({'type': 'product', 'url': URL, 'created_at': '2026-10-20T00:00:00+00:00',
                       'attempts': 9, 'last_error': 'http_503'})
            original = dict(c.state['queue'])
            client.defer_response(HOST, 429, '900'); c.save()
            resumed = Collector(root, 'ark', CFG, 'same', Client())
            self.assertEqual(resumed.client.rate_limit_spacing, {HOST: 2})
            self.assertEqual(resumed.client.retry_after[HOST], 1900)
            self.assertEqual(resumed.state['queue'], original)
            self.assertEqual(health(resumed.state, utcnow())['rate_limit_spacing_seconds'], {HOST: 2})
            new = Collector(root, 'ark', CFG, 'new', Client()); new.save()
            self.assertEqual(new.client.rate_limit_spacing, {})
            self.assertEqual(new.state['rate_limit_spacing_seconds'], {})
            self.assertEqual(new.client.retry_after[HOST], 1900)
            self.assertEqual(new.state['queue'], original)

    def test_invalid_saved_spacing_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); c = Collector(root, 'ark', CFG, 'same', Client())
            c.state['rate_limit_spacing_seconds'] = {'bad1': '2', 'bad2': -2, 'bad3': True,
                'bad4': 31, 'bad5': float('inf'), HOST: 4}
            c.disk.save(c.name, c.state)
            resumed = Collector(root, 'ark', CFG, 'same', Client())
            self.assertEqual(resumed.client.rate_limit_spacing, {HOST: 4})

    def test_configured_slower_rate_is_never_lowered_by_the_cap(self):
        clock = Clock(); client = Client(delay=40); calls = []
        def opened(request, **kwargs):
            calls.append(clock.now); return Response(request.full_url)
        with clock.context(), patch.object(client.opener, 'open', side_effect=opened):
            client.defer_response(HOST, 429, '180'); clock.sleep(180)
            client.get(URL); client.get(URL)
        self.assertEqual(calls, [1180, 1220])
