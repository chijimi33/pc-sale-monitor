from __future__ import annotations

from datetime import datetime, timedelta
from http.client import RemoteDisconnected
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
from sale_monitor.reporting import aggregate, health, summarize_errors, validation
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
    def test_dospara_primary_manufacturer_delimiter_preserves_full_product_label(self):
        labels = [("ADATA", "SLEG-900P-2TCS-DP (M.2 2280 2TB) ドスパラ限定モデル"),
                  ("Razer", "Seiren V3 Chroma 32-Bit DSP (RZ19-05990100-R3M1)"),
                  ("エレコム", "WRC-BE36QS-B (11be 無線LANルーター)"),
                  ("MONTECH", "KING 95 PRO White (ATX ガラス ホワイト)"),
                  ("Western Digital", "WD80EAAZ (8TB)")]
        for brand, model in labels:
            with self.subTest(brand=brand):
                body = '<h1 class="p-product-show-detail__h3">' + brand + '  ' + model + '</h1>'
                o = parse_product("dospara", Page("https://www.dospara.co.jp/SBR1144/IC524291.html", body.encode(), iso(NOW)), {})
                self.assertEqual((o.brand, o.model), (brand, model))
                self.assertEqual(o.evidence[0]["fields"]["product_label"]["full_model_label"], model)
        pair = '<h1 class="p-product-show-detail__h3">ADATA  AX5U5600C4616G-DTAMRBK-DP (DDR5 PC5-44800 16GB 2枚組) ドスパラ限定モデル</h1>'
        parsed = parse_product("dospara", Page("https://www.dospara.co.jp/SBR1534/IC611642.html", pair.encode(), iso(NOW)), {})
        self.assertIn("bundle_contents_review_needed", parsed.issues)
        self.assertIsNone(parsed.jan)

    def test_dospara_unstructured_or_conflicting_heading_does_not_invent_model(self):
        url = "https://www.dospara.co.jp/SBR1144/IC524291.html"
        cases = ['<h1 class="p-product-show-detail__h3">Unknown PRODUCT-A</h1>',
                 '<h1>ADATA  PRODUCT-A</h1>',
                 '<h1 class="p-product-show-detail__h3">ADATA  PRODUCT-A</h1>' * 2,
                 '<h1 class="p-product-show-detail__h3">ADATA  PRODUCT-A</h1><script type="application/ld+json">{"@type":"Product","name":"Different product","mpn":"IC524291"}</script>']
        for body in cases:
            self.assertIsNone(parse_product("dospara", Page(url, body.encode(), iso(NOW)), {}).model)
        body = '<h1 class="p-product-show-detail__h3">ADATA  PRODUCT-A (2TB)</h1><table><tr><th>メーカー型番</th><td>PRODUCT-A</td></tr><tr><th>メーカー</th><td>ADATA</td></tr></table>'
        parsed = parse_product("dospara", Page(url, body.encode(), iso(NOW)), {})
        self.assertEqual(parsed.model, "PRODUCT-A")

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

    def test_limited_schema_availability_preserves_evidence_without_inventing_quantity(self):
        url = "https://shop.tsukumo.co.jp/goods/4711289500964/"
        product = {"@type": "Product", "url": url, "name": "Capture card", "sku": "4711289500964", "offers": {"@type": "Offer", "priceCurrency": "JPY", "price": 9000}}
        for availability in ("https://schema.org/LimitedAvailability", "http://schema.org/LimitedAvailability", "LimitedAvailability"):
            for label in ("在庫限り", "在庫わずか"):
                with self.subTest(availability=availability, label=label):
                    product["offers"]["availability"] = availability
                    body = '<script type="application/ld+json">' + json.dumps(product) + '</script><p class="stock-status limited">' + label + '</p><li class="free-shipping">送料無料</li>'
                    parsed = parse_product("tsukumo", Page(url, body.encode(), iso(NOW)), {"default_condition": "new"})
                    self.assertEqual(parsed.stock, "in_stock")
                    self.assertIsNone(parsed.listed_quantity)
                    self.assertEqual(parsed.evidence[0]["fields"]["schema_availability"], availability)
                    others = [offer(s, 10000, jan=parsed.jan, model=parsed.model, brand=parsed.brand) for s in ("ark", "sofmap")]
                    self.assertEqual(evaluate(parsed, others, [], NOW)["status"], "accepted")
                    sold_out = parse_product("tsukumo", Page(url, body.replace(label, "売り切れ").encode(), iso(NOW)), {"default_condition": "new"})
                    self.assertEqual(sold_out.stock, "out_of_stock")

    def test_limited_label_or_related_schema_does_not_prove_current_stock(self):
        url = "https://shop.tsukumo.co.jp/goods/4711289500964/"
        product = {"@type": "Product", "url": url, "name": "Capture card", "sku": "4711289500964", "offers": {"@type": "Offer", "priceCurrency": "JPY", "price": 9000}}
        related = {"@type": "Product", "url": "https://shop.tsukumo.co.jp/goods/4549576252476/", "name": "Other product", "offers": {"@type": "Offer", "priceCurrency": "JPY", "price": 1000, "availability": "https://schema.org/LimitedAvailability"}}
        for availability in (None, "https://schema.org/OnlineOnly", "https://example.org/LimitedAvailability", "https://schema.org/NotLimitedAvailability"):
            with self.subTest(availability=availability):
                product["offers"]["availability"] = availability
                body = '<script type="application/ld+json">' + json.dumps([related, product]) + '</script><p class="stock-status limited">在庫限り</p>'
                parsed = parse_product("tsukumo", Page(url, body.encode(), iso(NOW)), {"default_condition": "new"})
                self.assertEqual(parsed.stock, "unknown")
                self.assertEqual(parsed.price_yen, 9000)

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

    def test_sofmap_primary_stock_is_not_a_quantity_or_related_offer(self):
        product = '<script type="application/ld+json">{"@type":"Product","name":"Notebook","sku":"101644916","offers":{"@type":"Offer","priceCurrency":"JPY","price":349800,"availability":"https://schema.org/OnlineOnly"}}</script>'
        row = '<tr><th>在庫</th><td><span class="ic stock stocklast">在庫限り</span><span>通常24時間以内に出荷<br>お一人様1点まで</span></td></tr>'
        primary = '<main id="main"><section class="infobox"><table class="infotable">' + row + '<tr><th>送料</th><td>¥0 送料無料キャンペーン ※一部離島・山間部および北海道を除く</td></tr></table></section></main>'
        related = '<aside><table class="infotable"><tr><th>在庫</th><td>売り切れ</td></tr></table></aside>'
        def parse(text):
            return parse_product("sofmap", Page("https://www.sofmap.com/product_detail.aspx?sku=101644916", text.encode(), iso(NOW)), {"default_condition": "new"})
        parsed = parse(product + primary + related)
        self.assertEqual((parsed.stock, parsed.price_yen), ("in_stock", 349800))
        self.assertIsNone(parsed.listed_quantity)
        self.assertIsNone(parsed.shipping_yen)
        self.assertEqual(parsed.evidence[0]["fields"]["stock_status"]["source"], "primary_product_information")
        sold_out = primary.replace('在庫限り', '売り切れ').replace('通常24時間以内に出荷', '')
        self.assertEqual(parse(product + sold_out + related.replace('売り切れ', '在庫あり')).stock, "out_of_stock")
        for text in (product, product + related.replace('売り切れ', '在庫あり'),
                     product + primary.replace('通常24時間以内に出荷', ''),
                     product + primary.replace('class="infobox"', 'class="related-infobox"'),
                     product + primary.replace(row, row + row)):
            with self.subTest(text=text):
                self.assertEqual(parse(text).stock, "unknown")

    def test_koubou_embedded_variant(self):
        html = '''<h1>COUGAR cooler</h1><dl><dt>商品番号</dt><dd>4541995039782</dd><dt>商品型番</dt><dd>CGR-PSDVARGB-B-360</dd><dt>メーカー</dt><dd>COUGAR</dd><dt>送料</dt><dd>無料</dd></dl><input id="priceIncTax" value="6980"><script>eccube.classCategories={"a":{"b":{"product_code":"CGR-PSDVARGB-B-360","price02":6980,"stock_find":true,"point":"0","limit":"1"}}};</script>'''
        o = parse_product("koubou", Page("https://www.pc-koubou.jp/products/detail.php?product_id=1183984", html.encode(), iso(NOW)), {"default_condition": "new"})
        self.assertEqual((o.price_yen, o.shipping_yen, o.stock, o.model), (6980, 0, "in_stock", "CGR-PSDVARGB-B-360"))

    def test_koubou_preparing_purchase_button_overrides_stock_flag(self):
        body = '''<h1>COUGAR cooler</h1><dl><dt>商品番号</dt><dd>4541995039782</dd><dt>商品型番</dt><dd>CGR-PSDVARGB-B-360</dd><dt>メーカー</dt><dd>COUGAR</dd><dt>送料</dt><dd>無料</dd></dl><input id="priceIncTax" value="6980"><script>eccube.classCategories={"a":{"b":{"product_code":"CGR-PSDVARGB-B-360","price02":6980,"stock_find":true,"point":"0","limit":"1"}}};</script><li class="productDetail--main__right--price"><div class="btn-addcart"><button class="sold-out" disabled="disabled">商品準備中</button></div></li>'''
        def parse(text):
            return parse_product("koubou", Page("https://www.pc-koubou.jp/products/detail.php?product_id=1", text.encode(), iso(NOW)), {"default_condition": "new"})
        blocked = parse(body)
        others = [offer(s, 10000, jan=blocked.jan, model=blocked.model, brand=blocked.brand) for s in ("ark", "tsukumo")]
        decision = evaluate(blocked, others, [], NOW)
        self.assertEqual(decision["status"], "insufficient")
        self.assertIn("purchase_not_available", decision["reasons"])
        self.assertEqual(blocked.stock, "unknown")
        self.assertEqual(blocked.price_yen, 6980)
        self.assertFalse(blocked.verified)
        prior = {"ever_accepted": True, "facts": {"stock": "in_stock", "accepted": True}}
        events, state = update_events(blocked, decision, prior, NOW)
        self.assertEqual(events, [])
        self.assertEqual(state, prior)  # preparation does not prove stockout/end
        available = parse(body.replace('disabled="disabled">商品準備中', '>カートに入れる'))
        self.assertTrue(available.verified)
        self.assertEqual(evaluate(available, others, [], NOW)["status"], "accepted")
        related = parse(body.replace('productDetail--main__right--price', 'related-product'))
        self.assertTrue(related.verified)  # another product must not suppress this one
        self.assertEqual(related.stock, "in_stock")

    def test_koubou_explicit_sold_out_button_overrides_embedded_stock(self):
        body = '''<h1>Cooler</h1><dl><dt>商品番号</dt><dd>4541995039782</dd><dt>商品型番</dt><dd>CGR-PSDVARGB-B-360</dd><dt>メーカー</dt><dd>COUGAR</dd><dt>送料</dt><dd>無料</dd></dl><input id="priceIncTax" value="6980"><script>eccube.classCategories={"a":{"b":{"product_code":"CGR-PSDVARGB-B-360","price02":6980,"stock_find":true}}};</script><li class="productDetail--main__right--price"><button disabled="disabled">在庫切れです</button></li>'''
        def parse(text):
            return parse_product("koubou", Page("https://www.pc-koubou.jp/products/detail.php?product_id=1161910", text.encode(), iso(NOW)), {"default_condition": "new"})
        sold_out = parse(body)
        self.assertEqual(sold_out.stock, "out_of_stock")
        self.assertTrue(sold_out.verified)
        self.assertEqual(sold_out.evidence[0]["fields"]["purchase_availability"]["variant_stock"], "in_stock")
        others = [offer(s, 10000, jan=sold_out.jan, model=sold_out.model, brand=sold_out.brand) for s in ("ark", "tsukumo")]
        decision = evaluate(sold_out, others, [], NOW)
        self.assertEqual(decision["status"], "insufficient")
        self.assertIn("stock_out_of_stock", decision["reasons"])
        events, state = update_events(sold_out, decision, {"ever_accepted": True, "facts": {"stock": "in_stock", "accepted": True}}, NOW)
        self.assertEqual([e["kind"] for e in events], ["ended"])
        self.assertTrue(state["ended"])
        for text in (body.replace('productDetail--main__right--price', 'related-product'),
                     body.replace('在庫切れです', '購入には確認が必要です'),
                     body.replace('disabled="disabled">在庫切れです', '>カートに入れる')):
            with self.subTest(text=text):
                self.assertEqual(parse(text).stock, "in_stock")

    def test_koubou_bto_starting_price_is_not_an_exact_offer(self):
        body = '''<h1>Configured PC</h1><dl><dt>型番</dt><dd>MODEL-PC-FULL</dd><dt>メーカー</dt><dd>iiyama</dd><dt>送料</dt><dd>無料</dd></dl><script>eccube.classCategories={"a":{"b":{"product_code":"MODEL-PC-FULL","price02":299800,"stock_find":true}}};</script><div class="product-detail-container page_type_pc productDetail"><input id="priceIncTax" value="299800"><div class="productDetail--bottom"><div class="productDetail--bottom__contents"><dl class="productDetail--bottom__contents--pirce"><dt>セール価格</dt><dd><span class="value">299,800</span><span class="currency">円～</span></dd></dl></div></div></div>'''
        def parse(text):
            return parse_product("koubou", Page("https://www.pc-koubou.jp/products/detail.php?product_id=1203742", text.encode(), iso(NOW)), {"default_condition": "new"})
        starting = parse(body)
        self.assertIsNone(starting.price_yen)
        self.assertEqual(starting.evidence[0]["fields"]["printed_price_from_yen"], 299800)
        self.assertEqual(starting.evidence[0]["fields"]["price_basis"], "starting_price")
        self.assertEqual(starting.model, "MODEL-PC-FULL")
        self.assertIn("bto_configuration_review_needed", starting.issues)
        self.assertFalse(starting.verified)
        others = [offer(s, 400000, jan=None, model=starting.model, brand=starting.brand) for s in ("ark", "tsukumo")]
        decision = evaluate(starting, others, [], NOW)
        self.assertEqual(decision["status"], "insufficient")
        self.assertIn("bto_configuration_review_needed", decision["reasons"])
        prior = {"ever_accepted": True, "facts": {"stock": "in_stock", "accepted": True}}
        events, state = update_events(starting, decision, prior, NOW)
        self.assertEqual(events, [])
        self.assertEqual(state, prior)
        fixed = parse(body.replace('円～', '円'))
        self.assertEqual(fixed.price_yen, 299800)
        self.assertNotIn("bto_configuration_review_needed", fixed.issues)
        self.assertEqual(evaluate(fixed, others, [], NOW)["status"], "accepted")

    def test_koubou_related_starting_price_does_not_change_primary_fixed_price(self):
        fixed = '<h1>Cooler</h1><dl><dt>商品型番</dt><dd>MODEL-COOLER</dd></dl><div class="product-detail-container"><input id="priceIncTax" value="6980"></div>'
        related = '<aside class="page_type_pc"><div class="productDetail--bottom"><div class="productDetail--bottom__contents"><dl class="productDetail--bottom__contents--pirce"><dt>セール価格</dt><dd><span class="value">299,800</span><span class="currency">円～</span></dd></dl></div></div></aside>'
        parsed = parse_product("koubou", Page("https://www.pc-koubou.jp/products/detail.php?product_id=1183984", (fixed + related).encode(), iso(NOW)), {"default_condition": "new"})
        self.assertEqual(parsed.price_yen, 6980)
        self.assertNotIn("bto_configuration_review_needed", parsed.issues)
        self.assertNotIn("printed_price_from_yen", parsed.evidence[0]["fields"])

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
    def test_product_error_redirect_retains_original_tasks_and_old_prices_as_history(self):
        cfg = {"stores": {"sofmap": {"adapter": "html", "seed_urls": [], "default_condition": "new"}}}
        error_url = "https://www.sofmap.com/error/exec/_/isprdt=true"
        original_urls = [f"https://www.sofmap.com/product_detail.aspx?sku={sku}" for sku in (1, 2)]
        class Fake:
            count = 0
            def get(self, url):
                self.count += 1
                return Page(error_url, b"<html><title>error</title></html>", iso(NOW))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            c = Collector(root, "sofmap", cfg, "run1", Fake())
            old = offer("sofmap", url=original_urls[0], observed_run_id="old")
            c.state["offers"][old.key] = old.to_dict()
            created = iso(NOW - timedelta(days=2))
            for url in original_urls:
                c.enqueue({"type": "product", "url": url, "kind": "sale", "created_at": created})
            state = c.collect()
            self.assertEqual(state["status"], "partial")
            self.assertFalse(state["cycle_complete"])
            self.assertEqual({q["url"] for q in state["queue"].values()}, set(original_urls))
            self.assertTrue(all(q["created_at"] == created for q in state["queue"].values()))
            self.assertTrue(all(q["last_error"] == "product_error_page" for q in state["queue"].values()))
            self.assertEqual(set(state["offers"]), {old.key})
            retained = state["offers"][old.key]
            self.assertEqual(retained["price_yen"], old.price_yen)
            self.assertEqual(retained["stock"], "in_stock")
            self.assertEqual(retained["observed_run_id"], "old")
            self.assertIn("latest_fetch_failed", retained["issues"])
            report = aggregate(root, root / "public", "run1", NOW)
            self.assertEqual(report["stores"]["sofmap"]["current_offers"], 0)
            self.assertEqual(report["stores"]["sofmap"]["pending_over_24h"], 2)
            self.assertEqual(Store(root).load("public/evidence.json", {})["decisions"], [])
            self.assertEqual(Store(root).load("public/notifications.json", {})["events"], [])

    def test_sofmap_error_page_is_rejected_before_related_product_metadata(self):
        body = b'<h1>Related product</h1><script type="application/ld+json">{"@type":"Product","name":"Related","sku":"4537694358347","offers":{"price":9000,"priceCurrency":"JPY"}}</script>'
        page = Page("https://www.sofmap.com/error/exec/_/isprdt=true", body, iso(NOW))
        with self.assertRaisesRegex(FetchError, "product_error_page") as caught:
            parse_product("sofmap", page, {"default_condition": "new"})
        self.assertIs(caught.exception.page, page)
        normal = Page("https://www.sofmap.com/product_detail.aspx?sku=123&error=0", body, iso(NOW))
        parsed = parse_product("sofmap", normal, {"default_condition": "new"})
        self.assertEqual(parsed.price_yen, 9000)
        self.assertEqual(parsed.product_id, "123")

    def test_error_summary_preserves_all_failures_in_separate_details(self):
        errors = [{"reason": "rate_limited_retry_later", "url": f"https://www.ark-pc.co.jp/i/{n}/"} for n in range(500)]
        errors.extend([errors[0].copy(), {"reason": "http_403"}, {"reason": "http_403"}])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); disk = Store(root)
            disk.save("stores/ark.json", {"store": "ark", "run_id": "run1", "status": "partial", "errors": errors})
            index = aggregate(root, root/"public", "run1", NOW)
            summary = index["stores"]["ark"]["errors"]
            self.assertEqual(len(summary), 2)
            self.assertEqual(summary[0]["affected_tasks"], 501)
            self.assertEqual(summary[0]["distinct_urls"], 500)
            self.assertEqual(len(summary[0]["example_urls"]), 3)
            self.assertEqual(summary[1]["affected_tasks"], 2)
            details = disk.load("public/collection_errors.json", {})
            self.assertEqual(details["generated_at"], index["generated_at"])
            self.assertEqual(details["run_id"], "run1")
            rows = details["stores"]["ark"]["errors"]
            self.assertEqual(len(rows), 501)
            self.assertEqual(sum(r["affected_tasks"] for r in rows), 503)
            self.assertEqual(rows[0]["affected_tasks"], 2)
            self.assertIn(errors[499]["url"], [r["url"] for r in rows])
            self.assertEqual(summarize_errors(rows), summary)
            self.assertEqual(disk.load("stores/ark.json", {})["errors"], errors)
            self.assertEqual(index["files"]["collection_errors"], "collection_errors.json")
            self.assertEqual(index["complete_stores"], 0)
            self.assertEqual(index["monitored_store_count"], 10)

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

    def test_transport_outage_defers_host_preserves_queue_and_failed_prices(self):
        cfg = {"stores": {"tsukumo": {"adapter": "html", "seed_urls": []}}}
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.http.time.time", return_value=1000), patch("sale_monitor.http.time.sleep"):
            client = Client(delay=0)
            root = Path(folder)
            c = Collector(root, "tsukumo", cfg, "run1", client)
            previous = offer("tsukumo", url="https://shop.tsukumo.co.jp/goods/0/", observed_run_id="old")
            c.state["offers"][previous.key] = previous.to_dict()
            for n in range(5):
                c.enqueue({"type": "product", "url": f"https://shop.tsukumo.co.jp/goods/{n}/", "kind": "sale"})
            with patch.object(client.opener, "open", side_effect=RemoteDisconnected("private exception detail")) as op:
                state = c.collect()
            self.assertEqual(op.call_count, 9)
            self.assertEqual(state["request_count"], 9)
            self.assertEqual(len(state["queue"]), 5)
            self.assertEqual(len(state["errors"]), 5)
            self.assertEqual(sum(e["reason"] == "RemoteDisconnected" for e in state["errors"]), 3)
            self.assertEqual(sum(e["reason"] == "transport_retry_later:RemoteDisconnected" for e in state["errors"]), 2)
            self.assertEqual(state["status"], "partial")
            self.assertFalse(state["cycle_complete"])
            retained = state["offers"][previous.key]
            self.assertEqual(retained["stock"], "in_stock")
            self.assertEqual(retained["observed_run_id"], "old")
            self.assertIn("latest_fetch_failed", retained["issues"])
            self.assertNotIn("private exception detail", json.dumps(state))
            self.assertEqual(health(state, NOW)["request_count"], 9)
            self.assertEqual(health(state, NOW)["current_offers"], 0)
            self.assertIsNone(health({**state, "status": "job_missing"}, NOW)["request_count"])
            self.assertIsNone(health({}, NOW)["request_count"])
            resumed = Collector(root, "tsukumo", cfg, "run2")
            self.assertEqual(resumed.client.transport_retry_after["shop.tsukumo.co.jp"], {"until": 1300, "reason": "RemoteDisconnected"})
            with patch.object(resumed.client.opener, "open") as op, patch("playwright.sync_api.sync_playwright") as browser:
                with self.assertRaisesRegex(FetchError, "transport_retry_later:RemoteDisconnected"):
                    resumed.client.get("https://shop.tsukumo.co.jp/goods/new/")
                resumed.client.browser = True
                with self.assertRaises(FetchError):
                    resumed.client.rendered("https://shop.tsukumo.co.jp/goods/new/")
                op.assert_not_called()
                browser.assert_not_called()
            response = MagicMock(); response.__enter__.return_value = response
            response.url = "https://other.example/ok"; response.read.return_value = b"ok"; response.headers = {}
            with patch.object(resumed.client.opener, "open", return_value=response):
                self.assertEqual(resumed.client.get(response.url).body, b"ok")
            with patch("sale_monitor.http.time.time", return_value=1301), patch.object(resumed.client.opener, "open", return_value=response):
                self.assertEqual(resumed.client.get("https://shop.tsukumo.co.jp/recovered").body, b"ok")
                resumed.save()
            self.assertEqual(resumed.state["transport_retry_after"], {})

    def test_page_failure_expires_for_later_search_without_refreshing_old_prices(self):
        home = "https://shop.tsukumo.co.jp/"
        cfg = {"stores": {"tsukumo": {"adapter": "html", "seed_urls": [home]}}}
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.http.time.time", return_value=1000) as clock, patch("sale_monitor.http.time.sleep"):
            client = Client(delay=0)
            c = Collector(Path(folder), "tsukumo", cfg, "run1", client)
            old = offer("tsukumo", observed_run_id="old", issues=["latest_fetch_failed"])
            c.state["offers"][old.key] = old.to_dict()
            origin = iso(NOW-timedelta(days=2))
            task = {"type": "search", "query": "SSD", "kind": "comparison", "created_at": origin}
            c.enqueue(task)
            with patch.object(client.opener, "open", side_effect=RemoteDisconnected()) as op:
                with self.assertRaisesRegex(FetchError, "RemoteDisconnected"):
                    c.process(task)
                clock.return_value = 1299
                with self.assertRaises(FetchError):
                    c.process({**task, "query": "CPU"})
                self.assertEqual(op.call_count, 3)
            response = MagicMock(); response.__enter__.return_value = response
            response.url = home; response.headers = {}
            response.read.return_value = b'<form action="/search"><input name="keyword"></form>'
            clock.return_value = 1300
            with patch.object(client.opener, "open", return_value=response) as op:
                c.process({**task, "query": "CPU"})
                c.page(home)
                self.assertEqual(op.call_count, 1)
            c.save()
            persisted = Store(Path(folder)).load("stores/tsukumo.json", {})
            self.assertEqual(len(persisted["queue"]), 2)
            self.assertTrue(all(t["created_at"] == origin for t in persisted["queue"].values()))
            self.assertEqual(persisted["offers"][old.key], old.to_dict())
            self.assertEqual(health(persisted, NOW)["current_offers"], 0)
            self.assertEqual(client.count, 4)

    def test_page_failure_expiry_respects_server_and_renewed_host_waits(self):
        url = "https://www.ark-pc.co.jp/i/one/"
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": [], "browser_fallback": True}}}
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.http.time.time", return_value=1000) as clock:
            client = Client(browser=True, delay=0)
            c = Collector(Path(folder), "ark", cfg, "run1", client)
            error = HTTPError(url, 429, "slow down", {"Retry-After": "900"}, None)
            with patch.object(client.opener, "open", side_effect=error) as op, patch.object(client, "rendered") as render:
                with self.assertRaisesRegex(FetchError, "rate_limited_retry_later"):
                    c.page(url)
                clock.return_value = 1899
                with self.assertRaises(FetchError): c.page(url)
                self.assertEqual(op.call_count, 1)
                render.assert_not_called()
            # Another URL may extend the host cooldown after the page was cached.
            client.transport_retry_after["www.ark-pc.co.jp"] = {"until": 2300, "reason": "TimeoutError"}
            clock.return_value = 1900
            with patch.object(client.opener, "open") as op, patch.object(client, "rendered") as render:
                with self.assertRaisesRegex(FetchError, "transport_retry_later:TimeoutError"):
                    c.page(url)
                clock.return_value = 2299
                with self.assertRaises(FetchError): c.page(url)
                op.assert_not_called()
                render.assert_not_called()
            response = MagicMock(); response.__enter__.return_value = response
            response.url = url; response.read.return_value = b"recovered"; response.headers = {}
            clock.return_value = 2300
            with patch.object(client.opener, "open", return_value=response) as op:
                self.assertEqual(c.page(url).body, b"recovered")
                self.assertEqual(op.call_count, 1)

    def test_page_failure_for_denied_or_missing_page_stays_cached(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}
        for code in (403, 404):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder, patch("sale_monitor.http.time.time", return_value=1000) as clock:
                client = Client(delay=0)
                c = Collector(Path(folder), "ark", cfg, "run1", client)
                url = "https://www.ark-pc.co.jp/i/missing/"
                body = Page(url, b"missing", iso(NOW), status=code)
                with patch.object(client, "get", side_effect=FetchError(f"http_{code}", body)) as get:
                    with self.assertRaises(FetchError): c.page(url)
                    clock.return_value = 9999
                    with self.assertRaises(FetchError) as cached: c.page(url)
                    self.assertIs(cached.exception.page, body)
                    get.assert_called_once_with(url)

    def test_single_broken_url_and_successful_response_do_not_defer_host(self):
        client = Client(delay=0)
        with patch("sale_monitor.http.time.sleep"), patch.object(client.opener, "open", side_effect=RemoteDisconnected()):
            for _ in range(4):
                with self.assertRaises(FetchError): client.get("https://a.example/broken")
        self.assertEqual(client.transport_retry_after, {})
        response = MagicMock(); response.__enter__.return_value = response
        response.url = "https://a.example/ok"; response.read.return_value = b"ok"; response.headers = {}
        with patch.object(client.opener, "open", return_value=response):
            client.get(response.url)
        self.assertNotIn("a.example", client.transport_failed_urls)
        with patch("sale_monitor.http.time.sleep"), patch.object(client.opener, "open", side_effect=RemoteDisconnected()):
            for n in range(2):
                with self.assertRaises(FetchError): client.get(f"https://a.example/new/{n}")
        self.assertEqual(client.transport_retry_after, {})
        with patch.object(client.opener, "open", side_effect=HTTPError("https://a.example/deny", 403, "denied", {}, None)):
            with self.assertRaisesRegex(FetchError, "http_403"): client.get("https://a.example/deny")
        self.assertNotIn("a.example", client.transport_failed_urls)

    def test_transient_retry_recovery_remains_available_without_host_deferral(self):
        client = Client(delay=0)
        response = MagicMock(); response.__enter__.return_value = response
        response.url = "https://a.example/ok"; response.read.return_value = b"ok"; response.headers = {}
        with patch("sale_monitor.http.time.sleep"), patch.object(client.opener, "open", side_effect=[RemoteDisconnected(), TimeoutError(), response]) as op:
            self.assertEqual(client.get(response.url).body, b"ok")
        self.assertEqual(op.call_count, 3)
        self.assertEqual(client.transport_retry_after, {})
        self.assertEqual(client.transport_failed_urls, {})

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
            origin = iso(NOW-timedelta(days=2))
            c.process({"type": "search", "query": "SSD", "kind": "comparison", "priority": 0, "created_at": origin})
            listing = next(t for t in c.state["queue"].values() if t.get("kind") == "comparison")
            c.process(listing)
            compared = [t for t in c.state["queue"].values() if t.get("kind") == "comparison"]
            self.assertTrue(all(t["priority"] == 0 for t in compared))
            self.assertTrue(all(t["created_at"] == origin for t in compared))
            ordered = sorted(compared + [{"type": "search", "kind": "comparison", "priority": 0, "created_at": iso(NOW)},
                                         {"type": "search", "kind": "comparison", "priority": 3, "created_at": "2000"}], key=task_order)
            self.assertEqual(ordered[0]["type"], "product")

    def test_resumed_comparison_search_precedes_new_same_priority_refresh(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = Collector(root, "ark", cfg, "before")
            first.record(offer(discovery_kind="comparison"))
            first.enqueue({"type": "search", "query": "old-model", "kind": "comparison", "priority": 3, "created_at": iso(NOW-timedelta(days=2))})
            first.save()
            resumed = Collector(root, "ark", cfg, "after")
            executed = []
            resumed.process = lambda task: executed.append(task)
            with patch("sale_monitor.runner.time.monotonic", side_effect=[0, 0, 2]):
                resumed.collect(seconds=1)
            self.assertEqual([t["type"] for t in executed], ["search"])
            self.assertEqual(executed[0]["query"], "old-model")
            self.assertEqual(len(resumed.state["queue"]), 1)
            self.assertEqual(next(iter(resumed.state["queue"].values()))["type"], "product")
            self.assertEqual(next(iter(resumed.state["offers"].values()))["observed_run_id"], "before")

    def test_pending_comparison_promotion_preserves_original_age_and_attempts(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}
        with tempfile.TemporaryDirectory() as folder:
            c = Collector(Path(folder), "ark", cfg, "run1")
            base = {"type": "product", "url": "https://a.example/i/one", "kind": "comparison"}
            c.enqueue({**base, "priority": 3, "created_at": iso(NOW-timedelta(days=1)), "attempts": 2})
            c.enqueue({**base, "priority": 0, "created_at": iso(NOW)})
            task = next(iter(c.state["queue"].values()))
            self.assertEqual((task["priority"], task["attempts"]), (0, 2))
            self.assertEqual(task["created_at"], iso(NOW-timedelta(days=1)))
            c.enqueue({**base, "priority": 3, "created_at": iso(NOW-timedelta(days=2))})
            self.assertEqual(len(c.state["queue"]), 1)
            self.assertEqual(task["priority"], 0)
            self.assertEqual(task["created_at"], iso(NOW-timedelta(days=2)))

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
    def test_persistent_review_keeps_variants_separate_and_ids_stable(self):
        from hashlib import sha256
        from sale_monitor.models import digest
        body = b"same-model-different-bundles"
        content_hash = sha256(body).hexdigest()
        edition = digest([content_hash])
        class Fake:
            def get(self, url):
                return Page(url, body if url.endswith(".jpg") else b'<main><img src="/flyer.jpg"></main>', iso(NOW))
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.flyers.extract_asset", return_value={"text": "", "status": "review_needed"}) as parse:
            root = Path(folder); disk = Store(root)
            row = {"title": "PC", "brand": "Brand", "model": "PC-A", "source_asset_hash": content_hash,
                   "sale_date": "2026-09-12", "price_yen": 100000, "listed_quantity": 150}
            review = {"edition": edition, "reviewer": "test", "reviewed_at": iso(NOW),
                      "reviewed_asset_hashes": [content_hash], "products": [
                          dict(row, variant="base"), dict(row, variant="Office bundle", price_yen=129000)]}
            disk.save(f"flyers/reviews/{edition}.json", review)
            keys = set()
            for branch in BRANCHES:
                offers, record = collect_flyer(Fake(), disk, {"index_url": "https://a.example/"}, root/"reviews")
                self.assertEqual(record["status"], "reviewed")
                self.assertEqual(len({o.key for o in offers}), 2)
                self.assertFalse(same_product(*offers))
                keys.update(o.key for o in offers)
                for item in offers:
                    self.assertEqual(item.branches, list(BRANCHES))
                    self.assertEqual(item.stock, "unknown")
                    self.assertEqual(item.branch_overrides, {})
            self.assertEqual(len(keys), 2)
            self.assertEqual(parse.call_count, 1)
            review["products"][0]["price_yen"] = 90000
            disk.save(f"flyers/reviews/{edition}.json", review)
            offers, _ = collect_flyer(Fake(), disk, {"index_url": "https://a.example/"}, root/"reviews")
            self.assertEqual({o.key for o in offers}, keys)
            self.assertEqual(offers[0].price_yen, 90000)
            # A wrong-edition state record cannot borrow an older config review.
            Store(root/"reviews").save(edition+".json", review)
            disk.save(f"flyers/reviews/{edition}.json", dict(review, edition="other"))
            offers, record = collect_flyer(Fake(), disk, {"index_url": "https://a.example/"}, root/"reviews")
            self.assertEqual(offers, [])
            self.assertEqual(record["status"], "review_needed")

    def test_configuration_example_is_not_a_confirmed_price(self):
        from hashlib import sha256
        from sale_monitor.models import digest
        body = b"configuration-example"
        content_hash = sha256(body).hexdigest()
        edition = digest([content_hash])
        class Fake:
            def get(self, url):
                return Page(url, body if url.endswith(".jpg") else b'<main><img src="/flyer.jpg"></main>', iso(NOW))
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.flyers.extract_asset", return_value={"text": "", "status": "review_needed"}):
            root = Path(folder); disk = Store(root)
            row = {"title": "BTO PC", "model": "PC-A", "condition": "new", "source_asset_hash": content_hash,
                   "sale_date": "2026-09-12", "price_basis": "configuration_example", "price_yen": 159800,
                   "printed_configuration_price_yen": 159800, "source_entry_id": "101",
                   "branch_overrides": {BRANCHES[0]: {"stock": "in_stock", "source_url": "https://a.example/store", "checked_at": iso()}}}
            disk.save(f"flyers/reviews/{edition}.json", {"edition": edition, "reviewer": "test", "reviewed_at": iso(NOW),
                "reviewed_asset_hashes": [content_hash], "products": [row]})
            offers, _ = collect_flyer(Fake(), disk, {"index_url": "https://a.example/"}, root/"reviews")
            self.assertIsNone(offers[0].price_yen)
            self.assertIsNone(offers[0].payment)
            self.assertIn("bto_configuration_review_needed", evaluate(offers[0], [], [], NOW)["reasons"])
            self.assertEqual(offers[0].evidence[0]["fields"]["printed_configuration_price_yen"], 159800)
            self.assertEqual(offers[0].evidence[0]["fields"]["source_entry_id"], "101")

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

    def test_reviewed_starting_price_cannot_become_a_final_payment(self):
        from hashlib import sha256
        from sale_monitor.models import digest
        body = b"starting-price-flyer"
        content_hash = sha256(body).hexdigest()
        edition = digest([content_hash])
        class Fake:
            def get(self, url):
                return Page(url, body if url.endswith(".jpg") else b'<main><img src="/flyer.jpg"></main>', iso(NOW))
        with tempfile.TemporaryDirectory() as folder, patch("sale_monitor.flyers.extract_asset", return_value={"text": "", "status": "review_needed"}):
            root = Path(folder); disk = Store(root)
            row = {"title": "PC from 98,800 yen", "brand": "Brand", "model": "PC-A", "condition": "new",
                   "source_asset_hash": content_hash, "sale_date": "2026-09-12", "price_yen": 98800,
                   "price_basis": "starting_from", "printed_price_from_yen": 98800,
                   "review_notes": ["Configuration and conditional vouchers need confirmation."],
                   "branch_overrides": {BRANCHES[0]: {"stock": "in_stock", "source_url": "https://a.example/store", "checked_at": iso()}}}
            Store(root/"reviews").save(edition+".json", {"edition": edition, "reviewer": "test", "reviewed_at": iso(NOW),
                "reviewed_asset_hashes": [content_hash], "products": [row]})
            offers, _ = collect_flyer(Fake(), disk, {"index_url": "https://a.example/"}, root/"reviews")
            self.assertEqual(offers[0].stock, "in_stock")
            self.assertIsNone(offers[0].price_yen)
            self.assertIsNone(offers[0].payment)
            self.assertIn("starting_price_not_final_price", evaluate(offers[0], [], [], NOW)["reasons"])
            self.assertEqual(offers[0].evidence[0]["fields"]["printed_price_from_yen"], 98800)
            self.assertEqual(offers[0].evidence[0]["fields"]["review_notes"], row["review_notes"])
            self.assertIsNone(offers[0].points_yen)
            self.assertEqual(offers[0].discount_yen, 0)

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
