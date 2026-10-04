"""Offline tariff regressions using small, authored Tsukumo-shaped pages.

The DOM structure follows the 2026-10-04 delivery/product captures. No retail
page, network response, or machine-specific evidence directory is required.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError, Page
from sale_monitor.parsing import parse_product
from sale_monitor.runner import Collector
from sale_monitor.tsukumo_shipping import POLICY_URL, apply_policy, parse_policy, standard_product


OBSERVED = "2026-10-04T01:00:00+00:00"
LATER = "2026-10-04T02:00:00+00:00"
JAN = "4901234567894"
SECOND_JAN = "4006381333931"
TITLE = "Fixture USB memory 128GB"
CFG = {"adapter": "html", "default_condition": "new", "seed_urls": [],
       "browser_fallback": True}
CONFIG = {"stores": {"tsukumo": CFG}, "seller_aliases": {}}


def page(url, body, *, observed=OBSERVED, status=200):
    return Page(url, body.encode("utf-8"), observed, status=status,
                content_type="text/html; charset=utf-8")


def tariff_table(fee=550, tax=50):
    return (f'<table id="soryo"><tr><th>地域</th><th>北海道</th>'
            '<th>本州<div>(関東以外)</div></th>'
            '<th>関東<div>(東京、茨城県、群馬県、埼玉県、千葉県、神奈川県、山梨県、栃木県)</div></th>'
            '<th>四国・九州・沖縄</th><th>離島</th></tr>'
            '<tr><td>送料<span>（税込）</span></td>'
            f'<td class="price" colspan="5">{fee:,}円（内消費税{tax:,}円）</td></tr></table>')


def policy_page(*, fee=550, tax=50, below=3300, extra="", observed=OBSERVED):
    body = ('<main><article><div class="MainArea souryo"><h1>送料・配送について</h1>'
            '<h2>送料について</h2>'
            f'<h4>税込{below:,}円以上送料無料</h4>'
            f'<p>税込{below:,}円以上で日本全国どこでも送料無料でお届けいたします。</p>'
            '<p>eX.computer、G-GEAR 製品を除く<br>※一部対象外商品がございます。<br>'
            '代引き手数料 550円（税込）（内消費税50円）はお客様負担となります。</p>'
            f'<h4>税込{below:,}円未満ご購入の場合</h4>{tariff_table(fee, tax)}'
            '<p>eX.computer、G-GEAR 製品は、１台ごとに送料 2,200円（税込）（内消費税200円）がかかります。</p>'
            '<p>一部対象外商品は、商品ページへ別途記載しておりますのでそちらをご確認ください。</p>'
            f'{extra}<h2>お届け日目安について</h2><p>配送日を選択できます。</p>'
            '</div></article></main>')
    return page(POLICY_URL, body, observed=observed)


def product_page(*, jan=JAN, price=2780, observed=OBSERVED, notes="", specs="",
                 outside="", shipping=None, free_badge=False):
    url = f"https://shop.tsukumo.co.jp/goods/{jan}/"
    schema_offer = {"@type": "Offer", "price": price, "priceCurrency": "JPY",
                    "availability": "https://schema.org/InStock",
                    "itemCondition": "https://schema.org/NewCondition"}
    if shipping is not None:
        schema_offer["shippingDetails"] = {"shippingRate": {"currency": "JPY", "value": shipping}}
    data = {"@type": "Product", "url": url, "name": TITLE, "gtin13": jan,
            "offers": schema_offer}
    badge = '<ul><li class="free-shipping">送料無料</li></ul>' if free_badge else ""
    body = (f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head>'
            f'<body>{outside}<main><article><div class="goods-main">'
            f'<h2 class="goods-name">{TITLE}</h2><div class="freetxt">{notes}</div></div>'
            '<div class="goods-main-right"><div><p class="price">'
            f'<span class="net-price-text">ネット価格</span><strong>&yen;{price:,}</strong>'
            '<span class="including-tax-text">（税込）</span></p>'
            f'<p class="stock-status">在庫あり</p>{badge}'
            '<form><p class="price"><strong>&yen;</strong>'
            f'<strong id="calculated-price">{price:,}</strong></p></form>'
            '</div></div></article><section id="spec-contents">'
            f'<h2>詳細スペック</h2><p>USB 128GB</p>{specs}</section></main></body></html>')
    return page(url, body, observed=observed)


def rewrite(source, old, new):
    if old not in source.text:
        raise AssertionError(f"Fixture replacement target missing: {old!r}")
    return replace(source, body=source.text.replace(old, new).encode("utf-8"))


def product_offer(source):
    return parse_product("tsukumo", source, CFG)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        # Even an accidental real Client construction must never access the web.
        for method in ("get", "rendered"):
            guard = patch(f"sale_monitor.http.Client.{method}",
                          side_effect=AssertionError("Real network/browser forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def assert_unchanged(self, offer, product, policy):
        before = deepcopy(offer.to_dict())
        self.assertFalse(apply_policy(offer, product, policy))
        self.assertEqual(before, offer.to_dict())


class TariffParsingTests(OfflineTests):
    def test_reads_tax_inclusive_tariff_not_cod_tax_or_bto_fee(self):
        for fee, tax, below in ((550, 50, 3300), (770, 70, 4400), (1100, 100, 5500)):
            with self.subTest(fee=fee):
                self.assertEqual(parse_policy(policy_page(fee=fee, tax=tax, below=below)), {
                    "below_yen": below, "shipping_yen": fee,
                    "scope": "standard_goods_single_item_nationwide", "tax_included": True})

    def test_tbody_and_unrelated_later_section_do_not_change_tariff(self):
        source = rewrite(policy_page(), '<table id="soryo">', '<table id="soryo"><tbody>')
        source = rewrite(source, '</table>', '</tbody></table>')
        source = rewrite(source, '配送日を選択できます。', 'クーポン対象外。代引き手数料2,200円。')
        self.assertEqual(550, parse_policy(source)["shipping_yen"])

    def test_requires_success_from_exact_official_policy_url(self):
        source = policy_page()
        urls = [POLICY_URL.replace("https:", "http:"),
                POLICY_URL.replace("shop.tsukumo.co.jp", "other.example"),
                POLICY_URL.replace("shop.tsukumo.co.jp", "shop.tsukumo.co.jp.other.example"),
                POLICY_URL.replace("souryo.html", "terms.html"), POLICY_URL + "?old=1",
                POLICY_URL + "#archive"]
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(parse_policy(replace(source, url=url)))
        for status in (301, 403, 404, 503):
            with self.subTest(status=status):
                self.assertIsNone(parse_policy(replace(source, status=status)))

    def test_unknown_and_empty_pages_have_no_tariff(self):
        for body in ("", "   ", "<", "<html><h1>Maintenance</h1></html>",
                     '<?xml version="1.0" encoding="utf-8"?><html><p>Malformed guide</p></html>',
                     "<main><article><p>送料550円</p></article></main>"):
            with self.subTest(body=body):
                self.assertIsNone(parse_policy(page(POLICY_URL, body)))

    def test_missing_tax_amount_nationwide_or_exclusion_evidence_is_rejected(self):
        changes = [('送料<span>（税込）</span>', '送料<span>（税別）</span>'),
                   ('550円（内消費税50円）</td>', '50円</td>'),
                   ('550円（内消費税50円）</td>', '送料はお届け先によります</td>'),
                   ('550円（内消費税50円）</td>', '0円（内消費税0円）</td>'),
                   ('日本全国どこでも送料無料', '本州のみ送料無料'),
                   ('eX.computer、G-GEAR 製品を除く', '一部製品を除く'),
                   ('一部対象外商品は、商品ページへ別途記載', '対象商品はお問い合わせください'),
                   ('税込3,300円未満ご購入の場合', '税別3,300円未満ご購入の場合'),
                   ('class="MainArea souryo"', 'class="MainArea archive"')]
        for old, new in changes:
            with self.subTest(change=new):
                self.assertIsNone(parse_policy(rewrite(policy_page(), old, new)))

    def test_regional_rates_missing_regions_and_incomplete_colspan_are_rejected(self):
        changes = [('<th>北海道</th>', ''), ('四国・九州・沖縄', '四国・九州'),
                   ('<th>離島</th>', '<th>一部地域</th>'), ('colspan="5"', 'colspan="4"'),
                   ('<td class="price" colspan="5">550円（内消費税50円）</td>',
                    '<td>550円</td><td>550円</td><td>550円</td><td>990円</td><td>別途</td>')]
        for old, new in changes:
            with self.subTest(change=new):
                self.assertIsNone(parse_policy(rewrite(policy_page(), old, new)))

    def test_region_prefix_alone_cannot_establish_nationwide_coverage(self):
        for old, new in [('本州<div>(関東以外)</div>', '本州<div>(大阪府のみ)</div>'),
                         ('関東<div>(東京、茨城県、群馬県、埼玉県、千葉県、神奈川県、山梨県、栃木県)</div>',
                          '関東<div>(東京都のみ)</div>')]:
            with self.subTest(region=new):
                self.assertIsNone(parse_policy(rewrite(policy_page(), old, new)))

    def test_duplicate_or_conflicting_tables_and_thresholds_are_rejected(self):
        for extra in (tariff_table(), tariff_table(990, 90),
                      tariff_table(990, 90).replace('id="soryo"', 'id="revised-soryo"'),
                      '<h4>税込3,300円未満ご購入の場合</h4>',
                      '<h4>税込4,400円未満ご購入の場合</h4>'):
            with self.subTest(extra=extra):
                self.assertIsNone(parse_policy(policy_page(extra=extra)))
        source = policy_page()
        self.assertIsNone(parse_policy(replace(source, body=source.body + source.body)))

    def test_conditional_and_additional_shipping_terms_are_rejected(self):
        for note in ('離島は別途送料', '沖縄は追加送料', '大型商品は送料加算',
                     '会員限定の送料です', 'クーポン利用時のみ'):
            with self.subTest(note=note):
                self.assertIsNone(parse_policy(policy_page(extra=f"<p>{note}</p>")))


class ProductEligibilityTests(OfflineTests):
    def test_primary_price_with_calculated_cart_price_and_unrelated_navigation(self):
        source = product_page(notes='<p>送料無料まであと少し、オススメ商品</p>',
                              outside='<nav>G-GEAR BTO 送料2,200円</nav>')
        offer = product_offer(source)
        self.assertTrue(offer.verified)
        self.assertEqual(JAN, offer.jan)
        self.assertTrue(standard_product(source, offer))

    def test_special_product_terms_in_notes_specifications_or_images_are_rejected(self):
        for note in ('送料別途', '配送料はお問い合わせ', '配送費550円', '運賃が必要',
                     '別途料金がかかります', '別途費用が必要', '着払い', '大型配送',
                     'eX.computer', 'G-GEAR', 'BTO PC'):
            for area in ('notes', 'specs'):
                for markup in (f'<p>{note}</p>', f'<img alt="{note}">'):
                    with self.subTest(note=note, area=area, markup=markup):
                        source = product_page(**{area: markup})
                        offer = product_offer(source)
                        self.assertFalse(standard_product(source, offer))
                        self.assert_unchanged(offer, source, policy_page())

    def test_requires_official_goods_url_matching_valid_product_identity(self):
        source = product_page()
        offer = product_offer(source)
        for url in (source.url.replace('https:', 'http:'),
                    source.url.replace('shop.tsukumo.co.jp', 'other.example'),
                    source.url + '?variation=1', source.url.replace('/goods/', '/search/'),
                    source.url.replace(JAN, SECOND_JAN), source.url.replace(JAN, '4901234567895')):
            with self.subTest(url=url):
                self.assertFalse(standard_product(replace(source, url=url), offer))
        self.assertFalse(standard_product(replace(source, status=403), offer))

    def test_unverified_used_or_mismatched_offer_is_not_enriched(self):
        source = product_page()
        for changes in ({'store': 'sofmap'}, {'seller_id': 'third-party'}, {'jan': SECOND_JAN},
                        {'verified': False}, {'issues': ['canonical_product_mismatch']},
                        {'condition': 'used'}, {'condition': None}, {'title': 'Other USB memory'},
                        {'price_yen': 1}, {'price_yen': True}, {'price_yen': None}, {'price_yen': 0}):
            with self.subTest(changes=changes):
                offer = replace(product_offer(source), **changes)
                self.assertFalse(standard_product(source, offer))
                self.assert_unchanged(offer, source, policy_page())

    def test_missing_or_ambiguous_primary_markup_is_not_standard(self):
        source = product_page()
        changes = [('id="spec-contents"', 'id="other-specs"'),
                   ('class="goods-name"', 'class="recommendation-name"'),
                   ('class="goods-main-right"', 'class="recommendations"'),
                   ('class="including-tax-text"', 'class="tax-unknown"'),
                   ('（税込）', '（税別）'),
                   ('<strong>&yen;2,780</strong>', '<strong>&yen;2,781</strong>'),
                   ('<strong>&yen;2,780</strong>', '<strong>&yen;2,780</strong><strong>50</strong>'),
                   ('</article>', '</article><article><p>Another product</p></article>'),
                   ('</section>', '</section><section id="spec-contents">Other specs</section>')]
        for old, new in changes:
            with self.subTest(change=new):
                self.assertFalse(standard_product(rewrite(source, old, new), product_offer(source)))

    def test_empty_or_unknown_product_page_is_not_standard(self):
        source = product_page()
        for body in ('', ' ', '<html><h1>Maintenance</h1></html>'):
            with self.subTest(body=body):
                self.assertFalse(standard_product(replace(source, body=body.encode()), product_offer(source)))


class ApplyTariffTests(OfflineTests):
    def test_below_threshold_adds_exact_current_evidence_without_rewriting_product(self):
        source = product_page()
        offer = product_offer(source)
        before = deepcopy(offer.to_dict())
        policy = replace(policy_page(), observed_at="2026-10-04T01:05:00+00:00")
        self.assertTrue(apply_policy(offer, source, policy))
        self.assertEqual(550, offer.shipping_yen)
        self.assertEqual(3330, offer.payment)
        self.assertEqual(before['evidence'], offer.evidence[:-1])
        self.assertEqual({k: v for k, v in before.items() if k not in ('shipping_yen', 'evidence')},
                         {k: v for k, v in offer.to_dict().items() if k not in ('shipping_yen', 'evidence')})
        self.assertEqual(offer.evidence[-1], {
            'url': POLICY_URL, 'checked_at': policy.observed_at, 'method': 'http',
            'body_sha256': sha256(policy.body).hexdigest(),
            'fields': {'shipping_policy': {
                'below_yen': 3300, 'shipping_yen': 550,
                'scope': 'standard_goods_single_item_nationwide', 'tax_included': True,
                'product_url': source.url, 'product_price_yen': 2780, 'quantity': 1}}})
        self.assert_unchanged(offer, source, policy)  # No duplicate policy receipt.

    def test_current_threshold_is_exclusive_and_never_implies_free_shipping(self):
        for below in (3300, 4400):
            for price in (below - 1, below, below + 1):
                with self.subTest(below=below, price=price):
                    source = product_page(price=price)
                    offer = product_offer(source)
                    policy = policy_page(below=below, fee=770, tax=70)
                    if price < below:
                        self.assertTrue(apply_policy(offer, source, policy))
                        self.assertEqual(770, offer.shipping_yen)
                    else:
                        self.assert_unchanged(offer, source, policy)
                        self.assertIsNone(offer.payment)

    def test_stale_or_missing_product_and_policy_timestamps_leave_shipping_unknown(self):
        for field in ('product', 'policy'):
            for observed in (None, '', 'not-a-date', '2026-10-03T01:00:00+00:00',
                             '2026-10-04T02:00:01+00:00'):
                with self.subTest(field=field, observed=observed):
                    source, policy = product_page(), policy_page()
                    if field == 'product':
                        source = replace(source, observed_at=observed)
                    else:
                        policy = replace(policy, observed_at=observed)
                    self.assert_unchanged(product_offer(source), source, policy)

    def test_nearby_observations_in_either_order_and_equivalent_timezones(self):
        for observed in ('2026-10-04T00:59:00+00:00', '2026-10-04T01:01:00+00:00',
                         '2026-10-04T10:00:00+09:00'):
            with self.subTest(observed=observed):
                source = product_page()
                offer = product_offer(source)
                self.assertTrue(apply_policy(offer, source, policy_page(observed=observed)))
                self.assertEqual(550, offer.shipping_yen)

    def test_existing_paid_or_free_shipping_and_evidence_are_unchanged(self):
        for shipping in (0, 220, 2200):
            with self.subTest(shipping=shipping):
                source = product_page(shipping=shipping)
                self.assert_unchanged(product_offer(source), source, policy_page())
        source = product_page(free_badge=True)
        offer = product_offer(source)
        self.assertEqual(0, offer.shipping_yen)
        self.assert_unchanged(offer, source, policy_page())


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.count = 0
        self.retry_after = {}
        self.transport_retry_after = {}

    def get(self, url):
        self.calls.append(url)
        self.count += 1
        if url not in self.responses:
            raise AssertionError(f"Unexpected offline request: {url}")
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def rendered(self, url):
        raise AssertionError(f"Optional tariff must not launch browser: {url}")


class CollectorTariffTests(OfflineTests):
    def setUp(self):
        super().setUp()
        parent = Path(__file__).resolve().parents[1] / 'scratch' / 'tsukumo-shipping-tests'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='offline-', dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def collector(self, products, policy, *, run='shipping-run'):
        responses = {p.url: p for p in products}
        responses[POLICY_URL] = policy
        return Collector(self.root, 'tsukumo', deepcopy(CONFIG), run, FakeClient(responses))

    @staticmethod
    def task(source):
        return {'type': 'product', 'kind': 'comparison', 'url': source.url,
                'created_at': OBSERVED, 'attempts': 3, 'priority': 0}

    def test_two_products_share_one_policy_request_and_independent_receipts(self):
        first, second = product_page(), product_page(jan=SECOND_JAN, price=1999)
        policy = policy_page()
        collector = self.collector([first, second], policy)
        for source in (first, second):
            collector.process(self.task(source))
        self.assertEqual([first.url, POLICY_URL, second.url], collector.client.calls)
        self.assertEqual(2, len(collector.state['offers']))
        self.assertEqual(2, len(collector.state['journal']))
        for row in collector.state['offers'].values():
            self.assertEqual(550, row['shipping_yen'])
            self.assertEqual('shipping-run', row['observed_run_id'])
            self.assertEqual(row['url'], row['evidence'][-1]['fields']['shipping_policy']['product_url'])
            self.assertEqual(sha256(policy.body).hexdigest(), row['evidence'][-1]['body_sha256'])
        receipts = deepcopy(collector.state['journal'])
        next(iter(collector.state['offers'].values()))['evidence'][-1]['fields']['shipping_policy']['quantity'] = 9
        self.assertEqual(receipts, collector.state['journal'])
        collector.save()
        self.assertEqual(receipts, collector.disk.load(collector.name, {})['journal'])

    def test_failed_optional_policy_is_requested_once_without_losing_fresh_products(self):
        first, second = product_page(), product_page(jan=SECOND_JAN, price=1999)
        collector = self.collector([first, second], FetchError('http_503'))
        for source in (first, second):
            collector.enqueue(self.task(source))
        result = collector.collect(seconds=2)
        self.assertEqual(1, collector.client.calls.count(POLICY_URL))
        self.assertTrue(result['cycle_complete'])
        self.assertEqual({}, result['queue'])
        self.assertEqual([], result['errors'])
        self.assertEqual(2, len(result['journal']))
        self.assertEqual(2, len(result['offers']))
        for source in (first, second):
            expected = parse_product('tsukumo', source, CFG, self.task(source))
            expected.observed_run_id = collector.run_id
            self.assertEqual(expected.to_dict(), result['offers'][expected.key])
        self.assertEqual(result['journal'], collector.disk.load(collector.name, {})['journal'])

    def test_invalid_or_empty_policy_does_not_drop_product_observation(self):
        source = product_page()
        for body in ('<html>Maintenance</html>', '',
                     '<?xml version="1.0" encoding="utf-8"?><html><p>Malformed guide</p></html>'):
            with self.subTest(body=body):
                collector = self.collector([source], page(POLICY_URL, body))
                collector.process(self.task(source))
                current = collector.state['offers'][product_offer(source).key]
                self.assertEqual(2780, current['price_yen'])
                self.assertIsNone(current['shipping_yen'])
                self.assertEqual(1, len(collector.state['journal']))

    def test_existing_shipping_or_nonstandard_product_never_fetches_optional_policy(self):
        for source in (product_page(shipping=0), product_page(shipping=880),
                       product_page(free_badge=True), product_page(notes='<p>BTO PC</p>')):
            with self.subTest(body=source.text):
                collector = self.collector([source], AssertionError('Tariff should not be requested'))
                collector.process(self.task(source))
                self.assertEqual([source.url], collector.client.calls)
                row = collector.state['offers'][product_offer(source).key]
                self.assertEqual(product_offer(source).shipping_yen, row['shipping_yen'])

    def test_new_collector_rechecks_current_fee_even_when_resuming_same_run(self):
        for run in ('shipping-run', 'next-shipping-run'):
            with self.subTest(run=run), tempfile.TemporaryDirectory(dir=self.root) as folder:
                source = product_page()
                first = Collector(Path(folder), 'tsukumo', deepcopy(CONFIG), 'shipping-run',
                                  FakeClient({source.url: source, POLICY_URL: policy_page()}))
                first.process(self.task(source))
                first.save()
                old_journal = deepcopy(first.state['journal'])
                fresh_source = product_page(price=2690, observed=LATER)
                fresh_policy = policy_page(fee=770, tax=70, observed=LATER)
                second = Collector(Path(folder), 'tsukumo', deepcopy(CONFIG), run,
                                   FakeClient({source.url: fresh_source, POLICY_URL: fresh_policy}))
                second.process(self.task(fresh_source))
                self.assertEqual([source.url, POLICY_URL], second.client.calls)
                row = second.state['offers'][product_offer(source).key]
                self.assertEqual((2690, 770, LATER, run),
                                 (row['price_yen'], row['shipping_yen'], row['observed_at'], row['observed_run_id']))
                self.assertEqual(sha256(fresh_policy.body).hexdigest(), row['evidence'][-1]['body_sha256'])
                self.assertEqual(old_journal, second.state['journal'][:-1])
                second.save()
                self.assertEqual(second.state['journal'], second.disk.load(second.name, {})['journal'])

    def test_new_collector_failed_recheck_cannot_reuse_old_fee_or_mutate_history(self):
        source = product_page()
        first = self.collector([source], policy_page())
        first.process(self.task(source))
        first.save()
        old_journal = deepcopy(first.state['journal'])
        fresh_source = product_page(price=2690, observed=LATER)
        second = self.collector([fresh_source], FetchError('RemoteDisconnected'), run='next-run')
        second.process(self.task(fresh_source))
        row = second.state['offers'][product_offer(source).key]
        self.assertEqual([source.url, POLICY_URL], second.client.calls)
        self.assertEqual((2690, None, LATER), (row['price_yen'], row['shipping_yen'], row['observed_at']))
        self.assertEqual(1, len(row['evidence']))
        self.assertEqual(old_journal, second.state['journal'][:-1])
        self.assertEqual(550, old_journal[0]['offer']['shipping_yen'])


if __name__ == '__main__':
    unittest.main()
