"""Product-parser regressions for the headset rows held by the live collector."""
from copy import deepcopy
from datetime import datetime
from html import escape
import json
from pathlib import Path
import tempfile
import unittest

from sale_monitor.http import Page
from sale_monitor.models import same_product
from sale_monitor.parsing import parse_product
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


NOW = '2026-10-11T10:10:06+00:00'
URL = 'https://www.sofmap.com/product_detail.aspx?sku=101633162'
JAN = '4571585649879'
CFG = {'default_condition': 'new'}
TITLES = [
    'RZ12-05590100-R3AC ゲーミングヘッドセット Hammerhead V3 ブラック ［φ3.5mmミニプラグ＋USB-C /両耳 /イヤホンタイプ］',
    'ロジクールG PRO X ゲーミングヘッドセット[USB・3.5mm ステレオミニプラグ/ブラック] G-PHS-003',
    'TGR089-HTN-ONE ゲーミングヘッドセット THE STANDARD 初音ミクEdition ［φ3.5mmミニプラグ /両耳 /イヤホンタイプ］ 【864】',
    'G431 ゲーミングヘッドセット ロジクールG [φ3.5mmミニプラグ＋USB /両耳 /ヘッドバンドタイプ]',
]


def product_page(title, *, variant=None, identity=True, price=True, extra=''):
    # In-memory product markup; only the titles above come from observed rows.
    # This fixture is not a captured merchant page or a production observation.
    product = {'@type': 'Product', 'url': URL, 'name': title, 'brand': 'Example',
               'offers': {'priceCurrency': 'JPY', 'availability': 'https://schema.org/InStock'}}
    if price:
        product['offers']['price'] = 6636
    rows = '<tr><th>商品番号</th><td>101633162</td></tr>'
    if identity:
        rows += '<tr><th>JANコード</th><td>' + JAN + '</td></tr>'
    if variant:
        rows += '<tr><th>商品構成</th><td>' + escape(variant) + '</td></tr>'
    body = ('<div id="wrapper" class="item"><main id="main" class="item"><section class="infobox">'
            '<h1>' + escape(title) + '</h1></section></main><section id="detail_area">'
            '<section id="tab2" class="prod_detail"><table>' + rows + '</table></section>'
            '</section></div><script type="application/ld+json">' + json.dumps(product, ensure_ascii=False) +
            '</script>' + extra)
    return Page(URL, body.encode('utf-8'), NOW, content_type='text/html;charset=utf-8')


class HeadsetClassificationTests(unittest.TestCase):
    def test_observed_single_headset_titles_keep_identity_and_shipping_hold(self):
        for title in TITLES:
            with self.subTest(title=title):
                offer = parse_product('sofmap', product_page(title), CFG)
                self.assertEqual(offer.title, title)
                self.assertEqual((offer.jan, offer.price_yen, offer.seller_id), (JAN, 6636, 'sofmap'))
                self.assertTrue(offer.verified)
                self.assertNotIn('bundle_contents_review_needed', offer.issues)
                self.assertIsNone(offer.variant)
                self.assertIsNone(offer.shipping_yen)
                self.assertEqual(offer.errors(datetime.fromisoformat(NOW)), ['shipping_unknown'])

    def test_explicit_sets_addons_quantities_and_product_joins_still_require_review(self):
        for title in ['ヘッドセット 2個セット', 'ヘッドセット ２点', 'ヘッドセット 10台',
                      'ヘッドセット まとめ買い', 'ヘッドセット 4枚組',
                      'PC ヘッドセット付き', 'ヘッドセット スタンド付',
                      'ヘッドセット マウス同梱', 'ヘッドセット 特典付き',
                      'キーボード＋ヘッドセット', 'ヘッドセット＋マイク',
                      'ヘッドセット H1 & マウス M1', 'ヘッドセット とマウスのセット']:
            with self.subTest(title=title):
                offer = parse_product('sofmap', product_page(title), CFG)
                self.assertIn('bundle_contents_review_needed', offer.issues)
                self.assertFalse(offer.verified)
                self.assertIsNone(offer.variant)

    def test_halfwidth_category_and_fullwidth_quantity_are_not_confused(self):
        single = parse_product('sofmap', product_page('ゲーミングﾍｯﾄﾞｾｯﾄ H1'), CFG)
        pair = parse_product('sofmap', product_page('ゲーミングﾍｯﾄﾞｾｯﾄ H1 ２個ｾｯﾄ'), CFG)
        self.assertTrue(single.verified)
        self.assertIn('bundle_contents_review_needed', pair.issues)

    def test_non_headset_bundle_guard_is_unchanged(self):
        for title in ['キーボード・マウス セット', 'DDR5 2枚組', 'メモリ 4枚組', 'SSD まとめ買い']:
            with self.subTest(title=title):
                self.assertIn('bundle_contents_review_needed', parse_product('sofmap', product_page(title), CFG).issues)

    def test_known_bundle_configuration_is_retained_and_not_equated_to_single_item(self):
        single = parse_product('sofmap', product_page(TITLES[0]), CFG)
        pair = parse_product('sofmap', product_page('ヘッドセット 2個セット', variant='2個'), CFG)
        self.assertEqual(pair.variant, '2個')
        self.assertNotIn('bundle_contents_review_needed', pair.issues)
        self.assertFalse(same_product(single, pair))

    def test_category_exception_does_not_create_missing_identity_price_or_shipping(self):
        for options in [{'identity': False}, {'price': False}]:
            with self.subTest(options=options):
                offer = parse_product('sofmap', product_page(TITLES[0], **options), CFG)
                self.assertFalse(offer.verified)
                self.assertIsNone(offer.shipping_yen)
                self.assertTrue({'identity_missing', 'price_unknown'} & set(offer.errors(datetime.fromisoformat(NOW))))

    def test_fresh_reparse_does_not_rewrite_historical_observation_or_formal_audit(self):
        old = parse_product('sofmap', product_page('ヘッドセット 2個セット'), CFG)
        old.observed_run_id = 'previous'
        fresh = parse_product('sofmap', product_page(TITLES[0]), CFG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'stores': {'sofmap': {'adapter': 'html', 'seed_urls': []}}}
            previous = Collector(root, 'sofmap', config, 'previous')
            previous.record(old)
            previous.save()
            old_journal = deepcopy(previous.state['journal'])
            disk = Store(root)
            disk.save('validation/manual_review.json', {'records': [{'event_id': 'preserve', 'status': 'needs_review'}]})
            audit = (root/'validation/manual_review.json').read_bytes()
            collector = Collector(root, 'sofmap', config, 'current')
            collector.record(fresh)
            self.assertEqual(collector.state['journal'][:len(old_journal)], old_journal)
            self.assertEqual((root/'validation/manual_review.json').read_bytes(), audit)
            self.assertIsNone(fresh.shipping_yen)
            self.assertEqual(fresh.observed_run_id, 'current')


if __name__ == '__main__':
    unittest.main()
