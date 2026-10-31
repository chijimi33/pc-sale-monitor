from copy import deepcopy
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError, Page
from sale_monitor.models import Offer
from sale_monitor.runner import Collector
from sale_monitor.storage import Store

URL = 'https://www.pc-koubou.jp/products/detail.php?product_id=123'
AGE = '2026-10-01T00:00:00+00:00'
NOW = '2026-10-31T00:00:00+00:00'
BODY = ('<html><head><title>ページが見つかりません</title></head><body>'
        '<main id="contents"><div id="p-error"><div class="p-error-message well">'
        'ご指定のページはございません。</div></div></main></body></html>').encode()
CFG = {'stores': {'koubou': {'adapter': 'html', 'seed_urls': [],
        'product_patterns': [r'/products/detail.php'], 'browser_fallback': True}}, 'flyer': {}}


class Fake:
    def __init__(self, page=None):
        self.page = page or Page(URL, BODY, NOW, status=404)
        self.count = self.http = self.browser = 0

    def get(self, url):
        self.count += 1
        self.http += 1
        if self.page.status == 200:
            return self.page
        raise FetchError('http_' + str(self.page.status), page=self.page)

    def rendered(self, url):
        self.count += 1
        self.browser += 1
        return Page(url, b'<h1>Recovered product</h1>', NOW, method='browser')


class KoubouNotFound(unittest.TestCase):
    def test_exact_404_keeps_original_response_and_avoids_browser(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake()
            c = Collector(Path(d), 'koubou', CFG, 'one', fake)
            for _ in range(2):
                with self.assertRaisesRegex(FetchError, 'http_404') as caught:
                    c.page(URL)
                self.assertIs(caught.exception.page, fake.page)
            self.assertEqual((fake.http, fake.browser), (1, 0))

    def test_ambiguous_or_different_scope_404_still_uses_browser(self):
        cases = [
            Page(URL, b'<title>Unknown error</title>', NOW, status=404),
            Page(URL, b'', NOW, status=404),
            Page(URL.replace('123', '456'), BODY, NOW, status=404),
            Page(URL, BODY.replace('ページが見つかりません'.encode(), b'Product'), NOW, status=404),
            Page(URL, BODY.replace(b'id="contents"', b'id="other"'), NOW, status=404),
            Page(URL, BODY.replace(b'id="p-error"', b'id="p-error" hidden'), NOW, status=404),
            Page(URL, BODY.replace(b'id="p-error"', b'id="p-error" style="display: none"'), NOW, status=404),
            Page(URL, BODY.replace(b'</body>', b'<script type="application/ld+json">{"@type":"Product"}</script></body>'), NOW, status=404),
            Page(URL, BODY.replace(b'</head>', '<title>ページが見つかりません</title></head>'.encode()), NOW, status=404),
            Page(URL, BODY.replace('ご指定のページはございません。'.encode(), b'Service temporarily unavailable'), NOW, status=404),
        ]
        for page in cases:
            with self.subTest(page=page.body[:80]), tempfile.TemporaryDirectory() as d:
                fake = Fake(page)
                c = Collector(Path(d), 'koubou', CFG, 'one', fake)
                self.assertEqual(c.page(URL).method, 'browser')
                self.assertEqual(fake.browser, 1)

    def test_non_product_or_foreign_url_and_other_store_keep_fallback(self):
        cases = [('koubou', 'https://www.pc-koubou.jp/search/?q=123'),
                 ('koubou', URL + '&product_id=456'),
                 ('koubou', URL.replace('www.pc-koubou.jp', 'example.com')),
                 ('ark', URL)]
        for store, url in cases:
            with self.subTest(store=store, url=url), tempfile.TemporaryDirectory() as d:
                cfg = {'stores': {store: CFG['stores']['koubou']}}
                fake = Fake(Page(url, BODY, NOW, status=404))
                c = Collector(Path(d), store, cfg, 'one', fake)
                self.assertEqual(c.page(url).method, 'browser')
                self.assertEqual(fake.browser, 1)

    def test_other_status_does_not_trigger_the_404_shortcut(self):
        for status in [200, 403, 500]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                fake = Fake(Page(URL, BODY, NOW, status=status))
                c = Collector(Path(d), 'koubou', CFG, 'one', fake)
                c.page(URL)
                self.assertEqual(fake.browser, 0 if status == 200 else 1)

    def test_missing_response_body_keeps_browser_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake()
            fake.get = lambda url: (_ for _ in ()).throw(FetchError('http_404'))
            c = Collector(Path(d), 'koubou', CFG, 'one', fake)
            self.assertEqual(c.page(URL).method, 'browser')

    def test_pending_age_attempt_and_old_price_survive_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake = Fake()
            c = Collector(root, 'koubou', CFG, 'one', fake)
            key = c.enqueue({'type': 'product', 'url': URL, 'created_at': AGE, 'attempts': 4})
            offer = Offer('koubou', '123', URL, price_yen=5000, observed_at=AGE, observed_run_id='old')
            prior = offer.to_dict()
            c.state['offers'][offer.key] = deepcopy(prior)
            with patch('sale_monitor.runner.collect_flyer', return_value=([], {'status': 'review_needed'})):
                state = c.collect(seconds=1)
            self.assertIn(key, state['queue'])
            self.assertNotIn(key, state['done'])
            self.assertEqual(state['queue'][key]['created_at'], AGE)
            self.assertEqual(state['queue'][key]['attempts'], 5)
            self.assertEqual(state['queue'][key]['last_error'], 'http_404')
            self.assertEqual(state['offers'][offer.key]['price_yen'], 5000)
            self.assertEqual(state['offers'][offer.key]['observed_run_id'], 'old')
            self.assertEqual(state['journal'], [])
            self.assertFalse(state['cycle_complete'])
            self.assertEqual(fake.browser, 0)
            evidence = state['errors'][0]['response_evidence']
            self.assertEqual(evidence['status'], 404)
            self.assertEqual(evidence['body_sha256'], hashlib.sha256(BODY).hexdigest())
            self.assertEqual(Store(root).load('stores/koubou.json', {})['queue'], state['queue'])

    def test_next_run_refetches_retained_task_and_can_recover(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = Collector(root, 'koubou', CFG, 'one', Fake())
            key = c.enqueue({'type': 'product', 'url': URL, 'created_at': AGE, 'attempts': 4})
            with patch('sale_monitor.runner.collect_flyer', return_value=([], {})):
                c.collect(seconds=1)
            fake = Fake(Page(URL, b'<h1>Available again</h1>', NOW))
            recovered = Collector(root, 'koubou', CFG, 'two', fake)
            offer = Offer('koubou', '123', URL, price_yen=4000, observed_at=NOW, verified=True)
            with patch('sale_monitor.runner.collect_flyer', return_value=([], {})), patch('sale_monitor.runner.parse_product', return_value=offer):
                state = recovered.collect(seconds=1)
            self.assertNotIn(key, state['queue'])
            self.assertIn(key, state['done'])
            self.assertEqual((fake.http, fake.browser), (1, 0))
            self.assertEqual(state['offers'][offer.key]['observed_run_id'], 'two')
            self.assertEqual(state['recovered_errors'][0]['attempts'], 5)
