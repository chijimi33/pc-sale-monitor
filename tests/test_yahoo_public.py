from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError, Page
from sale_monitor.models import digest
from sale_monitor.retrying import dependency_url, runnable
from sale_monitor.runner import Collector
from sale_monitor.storage import Store
from sale_monitor.yahoo_public import parse_product, parse_search, search_url


NOW = '2026-10-17T20:47:00+00:00'
OLD = '2026-10-01T00:00:00+00:00'
URL = 'https://store.shopping.yahoo.co.jp/example/part-1.html'
QUERY = 'セール SSD'
CFG = {'stores': {'yahoo': {'adapter': 'yahoo', 'queries': [], 'public_html_fallback': True}}}


def page(data, url, schema=None):
    body = '<link rel="canonical" href="' + url + '"><script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
    if schema is not None:
        body += '<script id="item-json-ld">' + json.dumps(schema) + '</script>'
    return Page(url, body.encode(), NOW)


def search_data(number=1, more=False, truncated=False, sale=True):
    row = {'url': URL, 'itemId': 'example_part-1', 'storeId': 'example',
           'store': {'url': 'https://store.shopping.yahoo.co.jp/example/'},
           'name': '特価 SSD' if sale else 'SSD', 'price': 5000, 'originalPrice': None, 'discountRate': None}
    pager = {'current': number, 'start': number, 'end': number, 'total': number + int(more or truncated),
             'totalPage': number + int(more), 'next': None, 'nextUrl': None,
             'baseNormalizedUrl': {'url': search_url(QUERY)}}
    alt = deepcopy(pager)
    if more:
        next_url = search_url(QUERY, number + 1)
        pager.update(next={'url': next_url, 'pageNumber': number + 1}, nextUrl={'url': next_url})
        alt.update(next=next_url, nextUrl={'url': next_url})
    return {'page': '/search/[[...params]]', 'query': {'params': [QUERY, '0'] + ([str(number)] if number > 1 else [])},
            'props': {'initialState': {'bff': {'searchError': None,
                'searchResults': {'items': {str(number): [{'type': 'PAGER', 'content': pager},
                                                       {'type': 'RESULT', 'content': {'items': [row]}}]}},
                'alternativePagination': alt, 'searchResultHeader': {'hitCount': pager['total'],
                    'title': {'main': {'taxonomies': [{'items': [{'text': QUERY, 'url': search_url(QUERY)}]}]}}}}}}}


def product_data():
    active = {'price': 5000, 'isApplicablePrice': True, 'isPremiumPrice': False, 'isYourTimeSalePrice': False, 'isYouBadge': False,
              'isRegularPrice': True, 'isBargainPrice': False}
    item = {'url': URL, 'sellerManagedItemId': 'part-1', 'name': 'Example SSD', 'janCode': '0192876826362',
            'brandName': 'Example', 'isUsed': False, 'isTaxable': True, 'applicablePrice': 5000,
            'priceTable': {'price1': active, 'price2': None}, 'isOnSale': True, 'isNormalPurchaseDisable': False,
            'axisNum': 0, 'multipleVariations': None, 'individualItemList': [], 'individualItemOptionList': [],
            'inscriptionOptionList': [], 'selectOptionList': [],
            'stock': {'isAvailable': True, 'quantity': 2, 'isStockVaried': False, 'isReserveOverdraft': False},
            'delivery': {'shippingType': 'InStock', 'isBackOrder': False}}
    point = {'title': 'ストアポイント', 'point': 45, 'entryText': None, 'isReachedLimit': False,
             'immediatePoint': 0, 'grantTiming': 'AfterPointOnly', 'bonusType': 'LyLimited'}
    data = {'props': {'pageProps': {'user': None, 'item': item, 'seller': {'id': 'example'},
            'point': {'currentCampaignList': [{'partsCampaignList': [point, {'title': 'LINE連携', 'point': 500}]}]},
            'postage': {'fee': 0, 'prefecture': '全国一律', 'conditionFreeText': '',
                        'notice': '条件により送料が異なる場合があります。', 'isPostageFree': True}}}}
    schema = {'@type': 'Product', 'gtin13': '0192876826362', 'offers': {'url': URL, 'price': 5000,
              'priceCurrency': 'JPY', 'itemCondition': 'https://schema.org/NewCondition',
              'availability': 'https://schema.org/InStock'}}
    return data, schema


def product(data=None, schema=None):
    if data is None:
        data, schema = product_data()
    return parse_product(page(data, URL, schema), {'url': URL, 'kind': 'comparison'})


class PublicSearchTests(unittest.TestCase):
    def test_discovery_does_not_create_offers_and_follows_provider_next(self):
        products, nxt, evidence = parse_search(page(search_data(more=True), search_url(QUERY)), QUERY)
        self.assertEqual(nxt, search_url(QUERY, 2))
        self.assertEqual(products[0]['url'], URL)
        self.assertNotIn('price_yen', products[0])
        self.assertFalse(evidence['provider_window_truncated'])

    def test_next_page_and_terminal_provider_window_are_distinct(self):
        for truncated in (False, True):
            _, nxt, evidence = parse_search(page(search_data(2, truncated=truncated), search_url(QUERY, 2)), QUERY, 2)
            self.assertIsNone(nxt)
            self.assertEqual(evidence['provider_window_truncated'], truncated)

    def test_comparison_includes_non_sale_but_sale_requires_explicit_evidence(self):
        p = page(search_data(sale=False), search_url(QUERY))
        self.assertEqual(parse_search(p, QUERY)[0], [])
        self.assertEqual(len(parse_search(p, QUERY, comparison=True)[0]), 1)

    def test_changed_queries_filters_and_redirects_are_not_completed(self):
        for url in (search_url('other'), search_url(QUERY, 2), search_url(QUERY) + '?used=1',
                    search_url(QUERY).replace('shopping.yahoo.co.jp', 'example.com')):
            with self.subTest(url=url), self.assertRaises(FetchError):
                parse_search(page(search_data(), url), QUERY)

    def test_conflicting_pagination_counts_and_foreign_products_fail_closed(self):
        for variant in ('count', 'header', 'cursor', 'foreign', 'wrong_item', 'empty', 'no_pager'):
            d = search_data(more=True); b = d['props']['initialState']['bff']; sections = b['searchResults']['items']['1']
            if variant == 'count': b['alternativePagination']['total'] = 500
            if variant == 'header': b['searchResultHeader']['title']['main']['taxonomies'][0]['items'][0]['text'] = 'other'
            if variant == 'cursor': sections[0]['content']['next']['url'] = search_url(QUERY, 3)
            if variant == 'foreign': sections[1]['content']['items'][0]['url'] = 'https://example.com/part.html'
            if variant == 'wrong_item': sections[1]['content']['items'][0]['itemId'] = 'different'
            if variant == 'empty': sections[1]['content']['items'] = []
            if variant == 'no_pager': sections.pop(0)
            with self.subTest(variant=variant), self.assertRaises(FetchError):
                parse_search(page(d, search_url(QUERY)), QUERY)

    def test_challenge_shell_not_no_results(self):
        with self.assertRaises(FetchError):
            parse_search(Page(search_url(QUERY), b'<html>Verify access</html>', NOW), QUERY)


class PublicProductTests(unittest.TestCase):
    def test_general_price_identity_and_base_points_with_shipping_held(self):
        o = product()
        self.assertEqual((o.price_yen, o.stock, o.points_yen, o.jan), (5000, 'in_stock', 45, '0192876826362'))
        self.assertIsNone(o.shipping_yen)
        self.assertIsNone(o.payment)
        self.assertIn('shipping_unknown', o.errors(datetime(2026, 10, 17, 21, tzinfo=timezone.utc)))
        self.assertNotIn('LINE', json.dumps(o.evidence))

    def test_selected_bargain_and_exact_expiry_not_premium(self):
        d, s = product_data(); i = d['props']['pageProps']['item']; active = i['priceTable']['price1']
        active.update(isRegularPrice=False, isBargainPrice=True)
        i.update(premiumPrice=1, salePeriodStart=1791957600, salePeriodEnd=1792425600,
                 isBeforeSalePeriod=False, isAfterSalePeriod=False)
        o = product(d, s)
        self.assertEqual(o.price_yen, 5000)
        self.assertEqual(o.expires_at, '2026-10-19T16:00:00+00:00')
        i['salePeriodEnd'] = 1
        self.assertIsNone(product(d, s).price_yen)

    def test_personalized_member_and_conflicting_prices_never_adopted(self):
        for variant in ('premium', 'personal', 'badge', 'jsonld', 'applicable', 'tax', 'currency', 'double', 'minimum'):
            d, s = product_data(); i = d['props']['pageProps']['item']; active = i['priceTable']['price1']
            if variant == 'premium': active['isPremiumPrice'] = True
            if variant == 'personal': active['isYourTimeSalePrice'] = True
            if variant == 'badge': active['isYouBadge'] = True
            if variant == 'jsonld': s['offers']['price'] = 1
            if variant == 'applicable': i['applicablePrice'] = 1
            if variant == 'tax': i['isTaxable'] = False
            if variant == 'currency': s['offers']['priceCurrency'] = 'USD'
            if variant == 'double': i['priceTable']['price2'] = deepcopy(active)
            if variant == 'minimum': i['stock']['minPurchaseQuantity'] = 2
            with self.subTest(variant=variant): self.assertIsNone(product(d, s).price_yen)

    def test_logged_in_wrong_seller_and_wrong_product_rejected(self):
        for variant in ('login', 'seller', 'url', 'jan'):
            d, s = product_data(); pp = d['props']['pageProps']
            if variant == 'login': pp['user'] = {'loggedIn': True}
            if variant == 'seller': pp['seller']['id'] = 'different'
            if variant == 'url': s['offers']['url'] = URL.replace('part-1', 'part-2')
            if variant == 'jan': s['gtin13'] = '0649528787347'
            with self.subTest(variant=variant), self.assertRaises(FetchError): product(d, s)

    def test_paid_multiple_or_variant_options_hold_price_and_stock(self):
        for variant in ('paid', 'multiple', 'axis', 'inscription'):
            d, s = product_data(); i = d['props']['pageProps']['item']
            option = {'name': 'delivery confirmation', 'choiceList': [{'name': 'yes', 'charge': None, 'isSelectable': True}]}
            i['selectOptionList'] = [option]
            self.assertEqual(product(d, s).price_yen, 5000)
            if variant == 'paid': option['choiceList'][0]['charge'] = 300
            if variant == 'multiple': option['choiceList'].append({'name': 'other', 'charge': None, 'isSelectable': True})
            if variant == 'axis': i['axisNum'] = 1
            if variant == 'inscription': i['inscriptionOptionList'] = [{'name': 'engraving'}]
            with self.subTest(variant=variant):
                o = product(d, s); self.assertIsNone(o.price_yen); self.assertEqual(o.stock, 'unknown')

    def test_stock_contradictions_reserve_and_backorder_are_unknown(self):
        for variant in ('schema', 'reserve', 'backorder', 'zero', 'disabled', 'varied'):
            d, s = product_data(); i = d['props']['pageProps']['item']
            if variant == 'schema': s['offers']['availability'] = 'https://schema.org/OutOfStock'
            if variant == 'reserve': i['stock']['isReserveOverdraft'] = True
            if variant == 'backorder': i['delivery']['isBackOrder'] = True
            if variant == 'zero': i['stock']['quantity'] = 0
            if variant == 'disabled': i['isNormalPurchaseDisable'] = True
            if variant == 'varied': i['stock']['isStockVaried'] = True
            with self.subTest(variant=variant): self.assertEqual(product(d, s).stock, 'unknown')

    def test_unavailable_and_used_condition_are_not_new_in_stock(self):
        d, s = product_data(); i = d['props']['pageProps']['item']
        i.update(isUsed=True, usedConditionText='やや傷や汚れあり'); s['offers']['itemCondition'] = 'https://schema.org/UsedCondition'
        i['stock']['isAvailable'] = False; s['offers']['availability'] = 'https://schema.org/OutOfStock'
        o = product(d, s)
        self.assertEqual((o.stock, o.condition, o.variant), ('out_of_stock', 'used', 'やや傷や汚れあり'))
        self.assertIn('used_warranty_review_needed', o.issues)

    def test_campaign_or_premium_points_are_not_base_points(self):
        for key, value in (('entryText', 'entry required'), ('bonusType', 'LyLimitedPremium'),
                           ('isReachedLimit', True), ('immediatePoint', 45)):
            d, s = product_data(); p = d['props']['pageProps']['point']['currentCampaignList'][0]['partsCampaignList'][0]
            p[key] = value
            with self.subTest(key=key): self.assertIsNone(product(d, s).points_yen)


class FakeClient:
    def __init__(self, pages): self.pages = pages; self.count = 0
    def get(self, url):
        self.count += 1
        result = self.pages[url]
        if isinstance(result, Exception): raise result
        return result


class PublicQueueTests(unittest.TestCase):
    def test_native_collection_records_current_product_without_unverified_shipping(self):
        d, s = product_data()
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            root = Path(tmp); disk = Store(root)
            task = {'type': 'yahoo', 'query': QUERY, 'start': 1, 'kind': 'comparison',
                    'created_at': OLD, 'attempts': 4, 'last_error': 'old_failure'}
            disk.save('stores/yahoo.json', {'queue': {'parent': task}, 'offers': {}, 'done': ['prior'],
                'journal': [{'prior': True}], 'cycle_complete': False, 'run_id': 'r', 'list_pages': 0, 'listed_candidates': 0})
            c = Collector(root, 'yahoo', CFG, 'r', FakeClient({
                search_url(QUERY): page(search_data(), search_url(QUERY)), URL: page(d, URL, s)}))
            state = c.collect(seconds=1)
            self.assertEqual(state['queue'], {})
            self.assertEqual(len(state['done']), 3)
            self.assertEqual(state['journal'][0], {'prior': True})
            self.assertEqual(len(state['journal']), 2)
            row = next(iter(state['offers'].values()))
            self.assertEqual((row['price_yen'], row['observed_at'], row['observed_run_id']), (5000, NOW, 'r'))
            self.assertIsNone(row['shipping_yen'])
            self.assertIn('seller_identity_review_needed', row['issues'])
            self.assertEqual(state['retry_activity']['recovered_tasks'], 1)

    def test_search_children_keep_age_priority_and_existing_attempts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            c = Collector(Path(tmp), 'yahoo', CFG, 'r', FakeClient({search_url(QUERY): page(search_data(more=True), search_url(QUERY))}))
            c.seed()
            old = {'type': 'product', 'url': URL, 'kind': 'comparison', 'created_at': OLD, 'attempts': 7}
            existing = c.enqueue(old)
            c.process({'type': 'yahoo', 'query': QUERY, 'start': 1, 'kind': 'comparison', 'created_at': OLD, 'priority': 1, 'requested': True})
            self.assertEqual(c.state['queue'][existing]['attempts'], 7)
            for row in c.state['queue'].values():
                self.assertEqual((row['created_at'], row['priority'], row['comparison_queries']), (OLD, 1, [QUERY]))
                self.assertTrue(row['requested'])
            self.assertEqual(c.state['offers'], {})
            self.assertEqual(c.state['done'], [])

    def test_invalid_search_leaves_existing_parent_age_attempts_and_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            root = Path(tmp); disk = Store(root)
            task = {'type': 'yahoo', 'query': QUERY, 'start': 1, 'created_at': OLD, 'attempts': 4}
            disk.save('stores/yahoo.json', {'queue': {'parent': task}, 'offers': {}, 'done': ['prior'],
                'journal': [{'prior': True}], 'cycle_complete': False, 'run_id': 'r', 'list_pages': 0})
            c = Collector(root, 'yahoo', CFG, 'r', FakeClient({search_url(QUERY): Page(search_url(QUERY), b'<html>challenge</html>', NOW)}))
            result = c.collect(seconds=1)
            self.assertEqual(result['queue']['parent']['created_at'], OLD)
            self.assertEqual(result['queue']['parent']['attempts'], 5)
            self.assertEqual(result['done'], ['prior'])
            self.assertEqual(result['journal'], [{'prior': True}])
            self.assertEqual(result['status'], 'partial')

    def test_api_offset_is_not_reinterpreted_as_html_page(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            c = Collector(Path(tmp), 'yahoo', CFG, 'r', FakeClient({})); c.seed()
            with self.assertRaisesRegex(FetchError, 'cursor_requires_configuration'):
                c.process({'type': 'yahoo', 'query': QUERY, 'start': 101})
            self.assertEqual(c.client.count, 0)

    def test_api_with_key_keeps_existing_adapter_no_http_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'YAHOO_CLIENT_ID': 'synthetic-test-id'}):
            c = Collector(Path(tmp), 'yahoo', CFG, 'r', FakeClient({})); c.seed()
            with patch('sale_monitor.runner.adapters.yahoo_page', side_effect=FetchError('http_403')) as api:
                with self.assertRaisesRegex(FetchError, 'http_403'): c.process({'type': 'yahoo', 'query': QUERY})
            api.assert_called_once(); self.assertEqual(c.client.count, 0)

    def test_cooldown_tracks_public_host_only_without_key(self):
        task = {'type': 'yahoo', 'query': QUERY}; cfg = CFG['stores']['yahoo']
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(dependency_url(task, cfg), 'https://shopping.yahoo.co.jp/search/')
            client = FakeClient({}); client.access_retry_after = {'shopping.yahoo.co.jp': {'until': 50, 'reason': 'http_403'}}
            ready, waits = runnable({'task': task}, set(), cfg, client, {}, 1)
            self.assertEqual(ready, []); self.assertEqual(waits[0]['affected_tasks'], 1)
        with patch.dict('os.environ', {'YAHOO_CLIENT_ID': 'synthetic-test-id'}):
            self.assertIn('shopping.yahooapis.jp', dependency_url(task, cfg))

    def test_disabled_fallback_still_requires_configuration(self):
        cfg = deepcopy(CFG); cfg['stores']['yahoo']['public_html_fallback'] = False
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            c = Collector(Path(tmp), 'yahoo', cfg, 'r', FakeClient({}))
            self.assertEqual(c.collect(seconds=1)['status'], 'configuration_needed')
            self.assertEqual(c.client.count, 0)
