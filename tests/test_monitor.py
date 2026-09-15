from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from sale_monitor.adapters import amazon_product, yahoo_page
from sale_monitor.engine import evaluate, update_events
from sale_monitor.flyers import collect_flyer, extract_candidates
from sale_monitor.http import Client, FetchError, Page, SafeRedirect
from sale_monitor.models import BRANCHES, STORES, UTC, Offer, allowed_url, iso, same_product
from sale_monitor.parsing import canonical, confirmed_empty_search, discover, parse_product
from sale_monitor.reporting import aggregate, validation
from sale_monitor.runner import Collector
from sale_monitor.storage import Store

NOW = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def offer(store="ark", price=9000, **kwargs):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/item/one", title="test", model="MODEL-A", brand="Brand", seller_id=store,
                price_yen=price, shipping_yen=0, condition="new", stock="in_stock", observed_at=iso(NOW), observed_run_id="run1", verified=True, points_yen=0,
                evidence=[{"url": f"https://{store}.example/item/one", "checked_at": iso(NOW)}])
    data.update(kwargs)
    return Offer(**data)


def competitors(price=10000):
    return [offer("tsukumo", price), offer("sofmap", price)]


class Decisions(unittest.TestCase):
    def test_exact_ten_percent(self):
        self.assertEqual(evaluate(offer(), competitors(), [], NOW)["rule"], "A")

    def test_one_yen_under_ten_percent(self):
        self.assertNotEqual(evaluate(offer(price=9001), competitors(), [], NOW)["status"], "accepted")

    def test_exact_500_yen(self):
        self.assertEqual(evaluate(offer(price=4500), competitors(5000), [], NOW)["rule"], "A")

    def test_499_yen_rejected(self):
        self.assertNotEqual(evaluate(offer(price=4000), competitors(4499), [], NOW)["status"], "accepted")

    def test_previous_wrong_math_regression(self):
        result = evaluate(offer(price=42320), competitors(47321), [], NOW)
        self.assertEqual(result["rule"], "A")
        self.assertGreater(result["difference_percent"], 10)

    def test_duplicate_sellers(self):
        cs = [offer("yahoo", 10000, seller_id="sofmap"), offer("sofmap", 11000)]
        self.assertIn("A_independent_sellers_insufficient", evaluate(offer(), cs, [], NOW)["reasons"])

    def test_same_seller_other_channel(self):
        self.assertIn("A_independent_sellers_insufficient", evaluate(offer("koubou"), [offer("koubou", 10000, channel="store_flyer"), offer("ark", 10000)], [], NOW)["reasons"])

    def test_cheapest_all_sellers(self):
        result = evaluate(offer(), competitors() + [offer("bic", 8000)], [], NOW)
        self.assertEqual(result["reasons"], ["cheaper_current_offer"])

    def test_identity_mismatch(self):
        cs = [offer("tsukumo", 10000, model="MODEL-B"), offer("sofmap", 10000)]
        self.assertNotEqual(evaluate(offer(), cs, [], NOW)["status"], "accepted")

    def test_condition_mismatch(self):
        self.assertFalse(same_product(offer(), offer("bic", condition="used")))

    def test_bundle_mismatch(self):
        self.assertFalse(same_product(offer(variant="1個"), offer("bic", variant="2個")))

    def test_shipping_unknown(self):
        self.assertIn("shipping_unknown", evaluate(offer(shipping_yen=None), competitors(), [], NOW)["reasons"])

    def test_malformed_amount_no_crash(self):
        self.assertIn("price_unknown", evaluate(offer(price="9000"), competitors(), [], NOW)["reasons"])

    def test_expiry_boundary(self):
        self.assertIn("expired", evaluate(offer(expires_at=iso(NOW)), competitors(), [], NOW)["reasons"])

    def test_remaining_zero(self):
        o = offer(coupon={"remaining": 0, "limited": True, "verified": True, "checked_at": iso(NOW)})
        self.assertIn("coupon_unavailable", evaluate(o, competitors(), [], NOW)["reasons"])

    def test_unlimited_remaining(self):
        o = offer(coupon={"remaining": -1, "limited": True, "verified": True, "checked_at": iso(NOW)})
        self.assertEqual(evaluate(o, competitors(), [], NOW)["rule"], "A")

    def test_stale_source(self):
        self.assertIn("stale_source", evaluate(offer(source_updated_at=iso(NOW-timedelta(days=1))), competitors(), [], NOW)["reasons"])

    def test_rakuten_history_excluded(self):
        history = [{"offer": offer("rakuten", 12000, observed_at=iso(NOW-timedelta(days=1))).to_dict()}]
        self.assertNotEqual(evaluate(offer(), [offer("bic", 9500)], history, NOW)["status"], "accepted")

    def test_history_observed_year_low(self):
        history = [{"offer": offer(price=9500, observed_at=iso(NOW-timedelta(days=1))).to_dict()}]
        result = evaluate(offer(), [offer("bic", 9500)], history, NOW)
        self.assertEqual(result["rule"], "B_observed_year_low")
        self.assertEqual(result["history"]["scope"], "observed_period_only")

    def test_history_requires_current_comparison(self):
        history = [{"offer": offer(price=12000, observed_at=iso(NOW-timedelta(days=1))).to_dict()}]
        self.assertIn("B_current_comparison_missing", evaluate(offer(), [], history, NOW)["reasons"])

    def test_b_median_uses_days(self):
        history = [{"offer": offer(price=p, observed_at=iso(NOW-timedelta(days=d))).to_dict()} for d, p in [(31, 10000), (20, 10000), (10, 9000)]]
        result = evaluate(offer(), [offer("bic", 9500)], history, NOW)
        self.assertEqual(result["rule"], "B_recent_median")

    def test_repeat_observations_not_three_days(self):
        history = [{"offer": offer(price=9000, observed_at=iso(NOW-timedelta(days=31, seconds=i))).to_dict()} for i in range(3)]
        self.assertNotEqual(evaluate(offer(), [offer("bic", 9500)], history, NOW)["status"], "accepted")

    def test_unknown_points_separate(self):
        self.assertEqual(evaluate(offer(points_yen=None), competitors(), [], NOW)["rule"], "A")
        self.assertEqual(evaluate(offer(points_yen=None), competitors(), [], NOW, points=True)["reasons"], ["points_unknown"])


class Events(unittest.TestCase):
    def setUp(self):
        self.o = offer()
        self.d = evaluate(self.o, competitors(), [], NOW)

    def test_repeated_observation_no_duplicate(self):
        events, state = update_events(self.o, self.d, None, NOW)
        self.assertEqual(len(events), 1)
        self.o.observed_at = iso(NOW+timedelta(minutes=1))
        again, _ = update_events(self.o, self.d, state, NOW+timedelta(minutes=1))
        self.assertEqual(again, [])

    def test_fixed_id(self):
        self.assertEqual(update_events(self.o, self.d, None, NOW)[0][0]["event_id"], update_events(self.o, self.d, None, NOW)[0][0]["event_id"])

    def test_failure_not_sold_out_and_no_recovery_duplicate(self):
        _, state = update_events(self.o, self.d, None, NOW)
        bad = offer(verified=False, stock="unknown", issues=["latest_fetch_failed"])
        events, unchanged = update_events(bad, evaluate(bad, competitors(), [], NOW), state, NOW)
        self.assertEqual(events, [])
        self.assertEqual(unchanged, state)
        self.assertEqual(update_events(self.o, self.d, unchanged, NOW)[0], [])

    def test_explicit_zero_ends_once(self):
        _, state = update_events(self.o, self.d, None, NOW)
        out = offer(stock="out_of_stock")
        d = evaluate(out, competitors(), [], NOW)
        events, state = update_events(out, d, state, NOW)
        self.assertEqual([e["kind"] for e in events], ["ended"])
        self.assertEqual(update_events(out, d, state, NOW)[0], [])

    def test_shared_flyer_six_branches_one_event(self):
        o = offer("koubou", channel="store_flyer", branches=list(BRANCHES), listed_quantity=10)
        state = None
        events = []
        for branch in BRANCHES:
            new, state = update_events(o, evaluate(o, competitors(), [], NOW), state, NOW)
            events.extend(new)
        self.assertEqual(len(events), 1)


class Parsers(unittest.TestCase):
    def test_empty_search_requires_matching_query_and_store_markup(self):
        url = "https://shop.tsukumo.co.jp/search?keyword=2150000884686"
        body = '<title>検索結果：2150000884686｜ツクモ公式通販サイト</title><input name="keyword" value="2150000884686"><div id="sli_noresult"><div>該当する商品がありませんでした。</div><div><a href="?keyword=2150000884686&amp;end_of_sales=0">販売終了商品も検索結果に表示する</a></div></div>'
        self.assertEqual(confirmed_empty_search("tsukumo", Page(url, body.encode(), iso(NOW), status=404)), "2150000884686")
        self.assertIsNone(confirmed_empty_search("ark", Page(url, body.encode(), iso(NOW), status=404)))
        for candidate in [Page(url, b'<h1>Not Found</h1>', iso(NOW), status=404),
                          Page(url, body.replace('value="2150000884686"', 'value="different"').encode(), iso(NOW), status=404),
                          Page(url.replace('/search?', '/goods/1/?'), body.encode(), iso(NOW), status=404),
                          Page(url, body.encode(), iso(NOW), status=403)]:
            self.assertIsNone(confirmed_empty_search("tsukumo", candidate))

    def test_amazon_first_order_free_shipping_is_conditional(self):
        for delivery, expected in [("無料配送 9月16日 にお届け（初回注文特典）", None), ("無料配送 9月16日 にお届け", 0), ("3,500円以上で無料配送", None)]:
            with self.subTest(delivery=delivery):
                body = '<h1>SSD</h1><input id="ASIN" value="B000000001"><div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">￥9,000</span></span></div><div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">' + delivery + '</div>'
                class Fake:
                    def get(self, url): return Page(url, body.encode(), iso(NOW))
                parsed = amazon_product(Fake(), {"url": "https://www.amazon.co.jp/dp/B000000001"})
                self.assertEqual(parsed.shipping_yen, expected)

    def test_bto_candidates_keep_distinct_ids_and_require_configuration_review(self):
        cfg = {"product_patterns": [r"/bto/customizer/\?pc_id="], "pc_only": True}
        body = '<main><a href="/bto/customizer/?pc_id=3722">BTO PC特価</a><a href="/bto/customizer/?pc_id=3719">BTO PC特価</a></main>'
        products, _, _ = discover(Page("https://www.ark-pc.co.jp/bto/special/bto-weekly-sale/", body.encode(), iso(NOW)), cfg, sale_page=True)
        parsed = [parse_product("ark", Page(p["url"], b'<h1>BTO</h1>', iso(NOW)), cfg) for p in products]
        self.assertEqual({p.product_id for p in parsed}, {"3722", "3719"})
        self.assertTrue(all("bto_configuration_review_needed" in p.issues for p in parsed))

    def test_product_json_not_related_low_price(self):
        html = '''<h1>Part</h1><script type="application/ld+json">{"@type":"Product","name":"Part","brand":{"name":"MSI"},"sku":"4526541047763","offers":{"@type":"Offer","priceCurrency":"JPY","price":10980,"availability":"https://schema.org/InStock","itemCondition":"https://schema.org/NewCondition","shippingDetails":{"shippingRate":{"currency":"JPY","value":"0"}}}}</script><div>関連商品 980円</div>'''
        o = parse_product("ark", Page("https://www.ark-pc.co.jp/i/1/", html.encode(), iso(NOW)), {})
        self.assertEqual(o.price_yen, 10980)
        self.assertEqual(o.shipping_yen, 0)
        self.assertEqual(o.jan, "4526541047763")

    def test_ark_conflicting_primary_expiry_requires_review(self):
        body = '''<script type="application/ld+json">{"@type":"Product","name":"MAG A650BNL","sku":"4526541047831","offers":{"@type":"Offer","priceCurrency":"JPY","price":3980,"priceValidUntil":"2026-09-18","availability":"https://schema.org/InStock","itemCondition":"https://schema.org/NewCondition","shippingDetails":{"shippingRate":{"currency":"JPY","value":0}}}}</script><li class="itemprice"><div class="date-diff2">開催期間:10/01 23:59まで</div><div id="item-15601883">3,980円</div></li><li class="itemprice"><div class="date-diff2">開催期間:12/31 23:59まで</div><div id="item-other">980円</div></li>'''
        page = Page("https://www.ark-pc.co.jp/i/15601883/", body.encode(), iso(NOW))
        o = parse_product("ark", page, {})
        self.assertEqual((o.price_yen, o.shipping_yen, o.stock), (3980, 0, "in_stock"))
        self.assertIsNone(o.expires_at)
        self.assertFalse(o.verified)
        self.assertIn("expiry_conflict_review_needed", evaluate(o, [], [], NOW)["reasons"])
        self.assertEqual(o.evidence[0]["fields"]["expiry"]["schema"], "2026-09-18")
        page.body = body.replace('10/01', '09/18').encode()
        aligned = parse_product("ark", page, {})
        self.assertTrue(aligned.verified)
        self.assertEqual(aligned.expires_at, "2026-09-18T14:59:59+00:00")
        page.body = body.replace('10/01 23:59', '09/18 12:00').encode()
        self.assertIn("expiry_conflict_review_needed", parse_product("ark", page, {}).issues)
        page.body = body.replace('"priceValidUntil":"2026-09-18",', '').encode()
        self.assertIsNone(parse_product("ark", page, {}).expires_at)

    def test_koubou_embedded_variant(self):
        html = '''<h1>COUGAR cooler</h1><dl><dt>商品番号</dt><dd>4541995039782</dd><dt>商品型番</dt><dd>CGR-PSDVARGB-B-360</dd><dt>メーカー</dt><dd>COUGAR</dd><dt>送料</dt><dd>無料</dd></dl><input id="priceIncTax" value="6980"><script>eccube.classCategories={"a":{"b":{"product_code":"CGR-PSDVARGB-B-360","price02":6980,"stock_find":true,"point":"0","limit":"1"}}};</script>'''
        o = parse_product("koubou", Page("https://www.pc-koubou.jp/products/detail.php?product_id=1183984", html.encode(), iso(NOW)), {"default_condition": "new"})
        self.assertEqual((o.price_yen, o.shipping_yen, o.stock, o.model), (6980, 0, "in_stock", "CGR-PSDVARGB-B-360"))

    def test_pagination_and_sale_scope(self):
        cfg = {"product_patterns": ["/i/"], "sale_patterns": []}
        html = '<li>通常 SSD<a href="/i/1/">SSD</a></li><li>特価 SSD<a href="/i/2/">SSD</a></li><li id="listnavi_next"><a href="/?offset=20">2</a></li>'
        a, b, _ = discover(Page("https://a.example/", html.encode(), iso(NOW)), cfg, sale_page=False)
        self.assertEqual([p["url"] for p in a], ["https://a.example/i/2/"])
        self.assertEqual(b, ["https://a.example/?offset=20"])

    def test_yahoo_lyp_excluded(self):
        payload = {"hits": [{"name": "特価 SSD", "url": "https://store.shopping.yahoo.co.jp/a/1.html", "code": "a_1", "price": 9000, "janCode": "4526541047763", "condition": "new", "inStock": True, "seller": {"sellerId": "a"}, "shipping": {"code": 3}, "point": {"lyLimitedBonusAmount": 90, "lyLimitedPremiumBonusAmount": 999}}], "totalResultsAvailable": 1, "totalResultsReturned": 1}
        class Fake:
            def json(self, url): return payload
        with patch.dict("os.environ", {"YAHOO_CLIENT_ID": "test-key"}):
            offers, _, _ = yahoo_page(Fake(), "SSD")
        self.assertEqual(offers[0].points_yen, 90)
        self.assertIsNone(offers[0].shipping_yen)
        self.assertNotIn("test-key", json.dumps(offers[0].to_dict()))
        self.assertNotIn("Premium", json.dumps(offers[0].to_dict()))


class Persistence(unittest.TestCase):
    def test_http_404_retains_body_for_store_specific_search_classification(self):
        from io import BytesIO
        error = HTTPError("https://shop.tsukumo.co.jp/search?keyword=one", 404, "not found", {"Content-Type": "text/html;charset=utf-8"}, BytesIO(b'<h1>search page</h1>'))
        client = Client(delay=0)
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(FetchError) as result: client.get(error.url)
        self.assertEqual(result.exception.page.status, 404)
        self.assertEqual(result.exception.page.body, b'<h1>search page</h1>')

    def test_confirmed_empty_search_completes_without_creating_out_of_stock_offer(self):
        url = "https://shop.tsukumo.co.jp/search?keyword=2150000884686"
        body = '<title>検索結果：2150000884686｜ツクモ公式通販サイト</title><input name="keyword" value="2150000884686"><div id="sli_noresult">該当する商品がありませんでした。</div>'
        class Fake:
            count = 0
            def get(self, target):
                self.count += 1
                raise FetchError("http_404", Page(target, body.encode(), iso(NOW), status=404))
        cfg = {"stores": {"tsukumo": {"adapter": "html", "seed_urls": [], "product_patterns": ["/goods/"], "browser_fallback": True}}}
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(Path(folder), "tsukumo", cfg, "run1", Fake())
            c.enqueue({"type": "list", "url": url, "kind": "comparison"})
            state = c.collect(seconds=1)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["queue"], {})
            self.assertEqual(state["offers"], {})
            self.assertEqual(state["comparison_searches"][url]["result"], "no_results")
            existing = offer("tsukumo").to_dict()
            c.state["offers"]["existing"] = existing.copy()
            c.process({"type": "list", "url": url, "kind": "comparison"})
            self.assertEqual(c.state["offers"]["existing"], existing)
            self.assertEqual(c.client.count, 1)

    def test_repeated_runs_do_not_replace_seven_day_window_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); disk = Store(root)
            disk.save("metrics/start.json", {"generated_at": iso(NOW-timedelta(days=8)), "stores": {}})
            for i in range(50):
                disk.save(f"metrics/rerun-{i}.json", {"generated_at": iso(NOW-timedelta(seconds=i)), "stores": {}})
            report = validation(root, NOW)
            self.assertEqual(report["recent_runs"], 50)
            self.assertLessEqual(report["measured_four_hour_windows"], 2)
            self.assertIn("insufficient_scheduled_runs", report["reasons"])
            self.assertFalse(report["cutover_ready"])

    def test_full_parallel_window_coverage_can_pass_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); disk = Store(root)
            disk.save("metrics/start.json", {"generated_at": iso(NOW-timedelta(days=8)), "stores": {}})
            states = {store: {"mandatory_field_coverage": {key: {"known": 20, "total": 20} for key in ("identity", "seller", "condition", "price", "shipping", "stock", "evidence")}, "pending_over_24h": 0} for store in STORES}
            for i in range(40):
                disk.save(f"metrics/run-{i}.json", {"generated_at": iso(NOW-timedelta(hours=4*i)), "complete_stores": 10, "stores": states})
            disk.save("validation/manual_review.json", {"reviewed_at": iso(NOW), "reviewed_count": 20, "false_positive_count": 0})
            report = validation(root, NOW)
            self.assertEqual(report["measured_four_hour_windows"], 40)
            self.assertTrue(report["cutover_ready"], report["reasons"])
            disk.save("validation/manual_review.json", {"reviewed_at": iso(NOW), "reviewed_count": 20, "false_positive_count": 0, "needs_review_count": 1})
            report = validation(root, NOW)
            self.assertFalse(report["cutover_ready"])
            self.assertIn("manual_audit_unresolved_findings", report["reasons"])

    def test_server_wait_applies_to_other_urls_browser_and_resumed_collector(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": [], "browser_fallback": True}}}
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.http.time.time", return_value=1000):
            root = Path(folder)
            client = Client(browser=True, delay=0)
            c = Collector(root, "ark", cfg, "run1", client)
            error = HTTPError("https://www.ark-pc.co.jp/i/one/", 429, "slow down", {"Retry-After": "180"}, None)
            with patch.object(client.opener, "open", side_effect=error) as op, patch.object(client, "rendered") as render:
                with self.assertRaisesRegex(FetchError, "rate_limited_retry_later"):
                    c.page(error.url)
                with self.assertRaises(FetchError):
                    client.get("https://www.ark-pc.co.jp/i/two/")
                self.assertEqual(op.call_count, 1)
                render.assert_not_called()
            c.save()
            resumed = Collector(root, "ark", cfg, "run2")
            self.assertEqual(resumed.client.retry_after["www.ark-pc.co.jp"], 1180)
            with patch.object(resumed.client.opener, "open") as op:
                with self.assertRaises(FetchError):
                    resumed.client.get("https://www.ark-pc.co.jp/i/three/")
                with self.assertRaises(FetchError):
                    resumed.client.rendered("https://www.ark-pc.co.jp/i/four/")
                op.assert_not_called()

    def test_retry_deadline_expires_and_other_hosts_remain_available(self):
        client = Client(delay=0); client.retry_after["a.example"] = 1200
        response = MagicMock(); response.__enter__.return_value = response
        response.url = "https://b.example/product"; response.read.return_value = b"ok"; response.headers = {}
        with patch.object(client.opener, "open", return_value=response) as op, patch("sale_monitor.http.time.time", return_value=1000):
            self.assertEqual(client.get(response.url).body, b"ok")
            self.assertEqual(op.call_count, 1)
        with patch.object(client.opener, "open", return_value=response) as op, patch("sale_monitor.http.time.time", return_value=1201):
            client.get("https://a.example/product")
            self.assertEqual(op.call_count, 1)
            self.assertNotIn("a.example", client.retry_after)

    def test_retry_after_is_saved_even_on_last_retry_and_accepts_http_date(self):
        client = Client(delay=0)
        failures = [HTTPError("https://a.example/", 503, "retry", {"Retry-After": "0"}, None),
                    HTTPError("https://a.example/", 503, "retry", {"Retry-After": "0"}, None),
                    HTTPError("https://a.example/", 429, "wait", {"Retry-After": "Thu, 01 Jan 1970 00:20:00 GMT"}, None)]
        with patch.object(client.opener, "open", side_effect=failures) as op, patch("sale_monitor.http.time.time", return_value=1000):
            with self.assertRaises(FetchError): client.get("https://a.example/")
            self.assertEqual(op.call_count, 3)
            self.assertEqual(client.retry_after["a.example"], 1200)

    def test_browser_rate_limit_defers_subsequent_http_requests(self):
        client = Client(browser=True, delay=0)
        with patch("playwright.sync_api.sync_playwright") as factory, patch("sale_monitor.http.time.time", return_value=1000):
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            response = browser.new_page.return_value.goto.return_value
            response.status = 429; response.url = "https://a.example/"; response.header_value.return_value = "180"
            with self.assertRaises(FetchError): client.rendered(response.url)
            self.assertEqual(client.retry_after["a.example"], 1180)
            browser.close.assert_called_once()
            with patch.object(client.opener, "open") as op:
                with self.assertRaises(FetchError): client.get("https://a.example/other")
                op.assert_not_called()

    def test_comparison_backlog_does_not_starve_sale_price_refresh(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(Path(folder), "ark", cfg, "run1")
            c.seed()
            c.enqueue({"type": "list", "url": "https://a.example/search?q=old", "kind": "comparison", "priority": 0})
            c.enqueue({"type": "product", "url": "https://a.example/sale", "kind": "sale"})
            executed = []
            def process(task):
                executed.append(task["url"])
                if task["kind"] == "comparison":
                    raise FetchError("RemoteDisconnected")
                c.record(offer())
            c.process = process
            # Allow exactly one task before the job's time deadline.
            with patch("sale_monitor.runner.time.monotonic", side_effect=[0, 0, 2]):
                c.collect(seconds=1)
            self.assertEqual(executed, ["https://a.example/sale"])
            self.assertEqual(len(c.state["queue"]), 1)
            self.assertEqual(next(iter(c.state["offers"].values()))["observed_run_id"], "run1")

    def test_comparison_priority_survives_search_list_product_and_pagination(self):
        from sale_monitor.runner import task_order
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": ["https://a.example/"], "product_patterns": ["/i/"]}}}
        class Fake:
            count = 0
            def get(self, url):
                body = '<form action="/search"><input name="keyword"></form>' if url.endswith('/') else '<a href="/i/one">SSD</a><a rel="next" href="/search?keyword=SSD&page=2">次へ</a>'
                return Page(url, body.encode(), iso(NOW))
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(Path(folder), "ark", cfg, "run1", Fake()); c.seed()
            c.process({"type": "search", "query": "SSD", "kind": "comparison", "priority": 0})
            listing = next(t for t in c.state["queue"].values() if t.get("kind") == "comparison")
            c.process(listing)
            compared = [t for t in c.state["queue"].values() if t.get("kind") == "comparison"]
            self.assertTrue(all(t["priority"] == 0 for t in compared))
            ordered = sorted(compared + [{"type": "search", "kind": "comparison", "priority": 0, "created_at": "2000"}], key=task_order)
            self.assertEqual(ordered[0]["type"], "product")

    def test_unprocessed_previous_cycle_price_cannot_become_current(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            disk = Store(root)
            old = offer(observed_run_id="previous", observed_at=iso(NOW-timedelta(hours=4)))
            disk.save("stores/ark.json", {"store": "ark", "run_id": "run1", "status": "partial", "offers": {old.key: old.to_dict()}})
            index = aggregate(root, root/"public", "run1", NOW)
            self.assertEqual(index["stores"]["ark"]["current_offers"], 0)
            self.assertEqual(disk.load("public/evidence.json", {})["decisions"], [])
            self.assertEqual(disk.load("requests/tsukumo.json", []), [])

    def test_nested_sale_portal_is_followed_without_duplicate_tasks(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": [], "product_patterns": ["/i/"], "sale_patterns": ["/special/"]}}}
        class Fake:
            count = 0
            def get(self, url): return Page(url, b'<a href="/special/sale/">sale</a>', iso(NOW))
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(Path(folder), "ark", cfg, "run1", Fake()); c.seed()
            c.process({"type": "list", "url": "https://www.ark-pc.co.jp/special/portal/", "sale_page": True, "depth": 1})
            self.assertEqual(len(c.state["queue"]), 1)
            c.process({"type": "list", "url": "https://www.ark-pc.co.jp/special/portal/", "sale_page": True, "depth": 2})
            self.assertEqual(len(c.state["queue"]), 1)

    def test_rakuten_request_blocked_before_transport(self):
        client = Client()
        with patch.object(client.opener, "open") as op:
            for host in ("item.rakuten.co.jp", "www.rakuten.ne.jp", "r10.to"):
                with self.assertRaises(FetchError):
                    client.get("https://"+host+"/item")
            op.assert_not_called()

    def test_rakuten_redirect_blocked(self):
        with self.assertRaises(FetchError):
            SafeRedirect().redirect_request(None, None, 302, "", {}, "https://item.rakuten.co.jp/a/b")

    def test_corrupt_state_not_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"bad.json").write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                Store(root).load("bad.json", {})

    def test_interruption_resumes_pending(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": ["https://www.ark-pc.co.jp/"], "product_patterns": ["/i/"], "sale_patterns": []}}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            c = Collector(root, "ark", cfg, "run1")
            c.seed()
            c.enqueue({"type": "product", "url": "https://www.ark-pc.co.jp/i/1/"})
            c.record(offer())
            c.save()
            resumed = Collector(root, "ark", cfg, "run2")
            self.assertEqual(len(resumed.state["queue"]), 2)
            self.assertEqual(len(resumed.state["journal"]), 1)

    def test_missing_job_does_not_revive_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            disk = Store(root)
            disk.save("stores/ark.json", {"store": "ark", "run_id": "old", "status": "complete", "offers": {"o": offer().to_dict()}})
            result = aggregate(root, root/"public", "new", NOW)
            self.assertEqual(result["monitored_store_count"], 10)
            self.assertEqual(result["stores"]["ark"]["status"], "job_missing")
            self.assertEqual(disk.load("public/evidence.json", {})["decisions"], [])

    def test_failed_old_product_does_not_block_fresh_lists(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": ["https://www.ark-pc.co.jp/"], "product_patterns": ["/i/"], "sale_patterns": []}}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            c = Collector(root, "ark", cfg, "run1"); c.seed()
            c.state["done"] = list(c.state["queue"])
            c.state["queue"] = {}
            c.enqueue({"type": "product", "url": "https://www.ark-pc.co.jp/i/failing/"}); c.save()
            resumed = Collector(root, "ark", cfg, "run2"); resumed.seed()
            self.assertEqual({t["type"] for t in resumed.state["queue"].values()}, {"list", "product"})

    def test_failed_search_home_not_requested_for_every_product(self):
        from unittest.mock import Mock
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": ["https://www.ark-pc.co.jp/"]}}}
        client = Mock(); client.get.side_effect = FetchError("http_403")
        with tempfile.TemporaryDirectory() as folder:
            collector = Collector(Path(folder), "ark", cfg, "one", client)
            for _ in range(4):
                with self.assertRaises(FetchError):
                    collector.page("https://www.ark-pc.co.jp/")
            self.assertEqual(client.get.call_count, 1)

    def test_aggregate_idempotent_history_and_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            disk = Store(root)
            for o in [offer(), *competitors()]:
                disk.save(f"stores/{o.store}.json", {"store": o.store, "run_id": "run1", "status": "complete", "offers": {o.key: o.to_dict()}, "journal": [{"run_id": "run1", "observation_id": o.key, "offer": o.to_dict()}]})
            aggregate(root, root/"public", "run1", NOW)
            aggregate(root, root/"public", "run1", NOW)
            self.assertEqual(len(disk.history()), 3)
            self.assertEqual(len(disk.load("events/registry.json", {})["events"]), 1)

    def test_expiry_conflict_removes_retained_notification_without_ending_sale(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); disk = Store(root)
            items = [offer(), *competitors()]
            for o in items:
                disk.save(f"stores/{o.store}.json", {"store": o.store, "run_id": "run1", "status": "complete", "offers": {o.key: o.to_dict()}})
            aggregate(root, root/"public", "run1", NOW)
            original = disk.load("public/notifications.json", {})["events"]
            self.assertEqual(len(original), 1)
            items[0].verified = False
            items[0].issues = ["expiry_conflict_review_needed"]
            for o in items:
                o.observed_run_id = "run2"
                disk.save(f"stores/{o.store}.json", {"store": o.store, "run_id": "run2", "status": "complete", "offers": {o.key: o.to_dict()}})
            aggregate(root, root/"public", "run2", NOW)
            self.assertEqual(disk.load("public/notifications.json", {})["events"], [])
            events = list(disk.load("events/registry.json", {})["events"].values())
            self.assertEqual([e["event_id"] for e in events], [original[0]["event_id"]])
            self.assertNotIn("ended", [e["kind"] for e in events])
            reviews = disk.load("public/review_queue.json", {})["candidates"]
            self.assertIn("expiry_conflict_review_needed", next(r for r in reviews if r["store"] == "ark")["reasons"])


class Flyers(unittest.TestCase):
    def test_monthly_credit_ad_is_not_a_product_price(self):
        self.assertEqual(extract_candidates("ゲーミングPCも月々3,000円から!"), [])
        self.assertEqual(extract_candidates("分割支払手数料0円"), [])
        self.assertEqual(extract_candidates("SSD 9,980円")[0]["price_yen"], 9980)

    def test_partial_review_preserves_bundle_and_does_not_claim_store_inventory(self):
        from hashlib import sha256
        from sale_monitor.models import digest
        first_hash, second_hash = [sha256(body).hexdigest() for body in (b"one", b"two")]
        edition = digest(sorted([first_hash, second_hash]))
        class Fake:
            def get(self, url):
                body = b"one" if url.endswith("flyer1.jpg") else b"two" if url.endswith("flyer2.jpg") else b'<main><img src="/flyer1.jpg"><img src="/flyer2.jpg"></main>'
                return Page(url, body, iso(NOW))
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.flyers.extract_asset", return_value={"text": "", "method": "ocr", "status": "review_needed"}):
            root = Path(folder); disk = Store(root)
            Store(root/"reviews").save(edition+".json", {"edition": edition, "reviewer": "test", "reviewed_at": iso(NOW), "reviewed_asset_hashes": [first_hash], "products": [
                {"title": "memory pair", "brand": "Brand", "model": "MODEL-A", "variant": "8GB x2", "condition": "new", "source_asset_hash": first_hash, "listed_quantity": 150, "price_yen": 9000, "sale_date": "2026-09-12"},
                {"title": "unreviewed product", "source_asset_hash": second_hash}]})
            offers, record = collect_flyer(Fake(), disk, {"index_url": "https://a.example/"}, root/"reviews")
            self.assertEqual(record["status"], "partially_reviewed")
            self.assertEqual(record["pending_asset_hashes"], [second_hash])
            self.assertEqual(len(offers), 1)
            self.assertEqual(offers[0].variant, "8GB x2")
            self.assertEqual(offers[0].stock, "unknown")
            self.assertEqual(offers[0].branches, list(BRANCHES))
            self.assertIn("store_stock_confirmation_required", offers[0].issues)
            self.assertFalse(same_product(offers[0], offer()))

    def test_review_feed_includes_shared_extraction_and_quantity_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            disk = Store(root)
            disk.save("flyers/latest.json", {"edition": "one", "branches": list(BRANCHES), "assets": [{"extraction_path": "flyers/assets/one.json", "content_hash": "one", "url": "https://a.example/flyer.jpg"}]})
            disk.save("flyers/assets/one.json", {"text": "SSD 9,980円 限定10台", "candidates": [{"listed_quantity": 10, "needs_review": True}]})
            aggregate(root, root/"public", "run1", NOW)
            feed = disk.load("public/flyer_review.json", {})
            self.assertEqual(len(feed["assets"]), 1)
            self.assertEqual(feed["assets"][0]["candidates"][0]["listed_quantity"], 10)
            self.assertEqual(feed["quantity_scope"], "common_flyer_not_store_inventory")

    def test_common_assets_cached_even_for_six_consumers(self):
        page = Page("https://www.pc-koubou.jp/shopinfo/contents/sale_flyer.php", b'<main><img src="/flyer.jpg"></main>', iso(NOW))
        class Fake:
            def get(self, url):
                return Page(url, b"image-content", iso(NOW)) if url.endswith(".jpg") else page
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.flyers.extract_asset", return_value={"text": "SSD\n9,980円\n限定10台", "method": "ocr", "status": "review_needed"}) as parse:
            disk = Store(Path(folder))
            records = []
            for branch in BRANCHES:
                offers, record = collect_flyer(Fake(), disk, {"index_url": page.url}, Path(folder)/"reviews")
                records.append(record)
                self.assertEqual(offers, [])
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(len({r["edition"] for r in records}), 1)

    def test_content_change_reparsed(self):
        class Fake:
            body = b"one"
            def get(self, url):
                return Page(url, self.body if url.endswith(".jpg") else b'<main><img src="/flyer.jpg"></main>', iso(NOW))
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.flyers.extract_asset", return_value={"text": "", "method": "ocr", "status": "review_needed"}) as parse:
            disk = Store(Path(folder)); client = Fake()
            _, a = collect_flyer(client, disk, {"index_url": "https://a.example/"}, Path(folder)/"reviews")
            client.body = b"two"
            _, b = collect_flyer(client, disk, {"index_url": "https://a.example/"}, Path(folder)/"reviews")
            self.assertNotEqual(a["edition"], b["edition"])
            self.assertEqual(parse.call_count, 2)

    def test_common_quantity_not_store_inventory(self):
        candidates = extract_candidates("SSD\n9,980円\n限定10台\nお一人様1台")
        self.assertEqual(candidates[0]["listed_quantity"], 10)
        self.assertNotIn("stock", candidates[0])


if __name__ == "__main__":
    unittest.main()
