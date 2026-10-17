import json
import unittest

from sale_monitor.http import Page
from sale_monitor.parsing import parse_product


URL = 'https://www.ark-pc.co.jp/i/24301770/'
END = '''<div class="add2cart soldout"><button disabled="disabled">
<span class="cart-stat text-disabled">EOL</span>
<span class="cart-txt"><strong>取り扱い終了</strong></span>
</button></div>'''


def document(button=END, *, canonical=URL, extra='', schema=None):
    data = ('<script type="application/ld+json">' + json.dumps(schema) + '</script>') if schema else ''
    return (f'<html><head><link rel="canonical" href="{canonical}">{data}</head>'
            '<body><div id="middle_col"><div class="item_detailbox">'
            '<div class="detail2col_r"><h1>F420 RGB Core Black</h1>'
            + button + '</div></div></div>' + extra + '</body></html>')


def parse(body, store='ark', url=URL, status=200):
    return parse_product(store, Page(url, body.encode(), '2026-10-13T10:00:00+00:00', status=status),
                         {'default_condition': 'new'})


class ArkEndOfLifeTests(unittest.TestCase):
    def test_explicit_primary_end_is_stock_evidence_without_inventing_price(self):
        for label in ['取り扱い終了', '取扱終了']:
            with self.subTest(label=label):
                row = parse(document(END.replace('取り扱い終了', label)))
                self.assertEqual(row.stock, 'out_of_stock')
                self.assertIsNone(row.price_yen)
                self.assertIsNone(row.shipping_yen)
                self.assertFalse(row.verified)
                proof = row.evidence[0]['fields']['purchase_availability']
                self.assertEqual(proof['button_status'], 'EOL')
                self.assertEqual(proof['button_text'], label)
                self.assertTrue(proof['button_disabled'])

    def test_related_hidden_or_ambiguous_end_does_not_establish_stock(self):
        variants = [END + END, '<aside>' + END + '</aside>', '<template>' + END + '</template>',
                    '<div class="item-recommend-tbl">' + END + '</div>',
                    '<div class="image_item_listbox">' + END + '</div>',
                    '<div class="item_detailbox">' + END + '</div>',
                    '<div hidden>' + END + '</div>', '<div style="display: none">' + END + '</div>',
                    END.replace('</button>', '<span class="cart-txt">在庫あり</span></button>'),
                    END.replace('disabled="disabled"', ''), END.replace('EOL', '在庫なし'),
                    END.replace('取り扱い終了', '取り扱い終了予定'),
                    END + '<form class="add2cart" action="/i/24301770/"><button>購入</button></form>']
        for button in variants:
            with self.subTest(button=button):
                self.assertEqual(parse(document(button)).stock, 'unknown')
        self.assertEqual(parse(document('', extra=END)).stock, 'unknown')

    def test_product_scope_must_be_unique_and_canonical(self):
        original = document()
        variants = [document(canonical='https://www.ark-pc.co.jp/i/24301771/'),
                    original.replace('<div id="middle_col">', '<div hidden id="middle_col">'),
                    original.replace('<link rel="canonical" href="' + URL + '">', ''),
                    original.replace('</head>', '<link rel="canonical" href="' + URL + '"></head>'),
                    original.replace('class="detail2col_r"', 'class="related-detail2col_r"'),
                    original.replace('</body>', '<div id="middle_col"><div class="item_detailbox"><div class="detail2col_r">'
                                     + END + '</div></div></div></body>')]
        for body in variants:
            with self.subTest(body=body):
                self.assertEqual(parse(body).stock, 'unknown')
        self.assertEqual(parse(original, store='tsukumo').stock, 'unknown')
        self.assertEqual(parse(original, url='https://example.org/i/24301770/').stock, 'unknown')
        self.assertEqual(parse(original, status=404).stock, 'unknown')

    def test_schema_stock_conflict_stays_unverified(self):
        for availability, expected in [('InStock', 'in_stock'), ('PreOrder', 'preorder'),
                                       ('OutOfStock', 'out_of_stock')]:
            schema = {'@type': 'Product', 'name': 'F420 RGB Core Black', 'url': URL,
                      'sku': '0810074844581', 'offers': {'@type': 'Offer', 'priceCurrency': 'JPY',
                       'price': 9000, 'availability': 'https://schema.org/' + availability}}
            row = parse(document(schema=schema))
            self.assertEqual(row.stock, expected)
            if availability != 'OutOfStock':
                self.assertIn('purchase_availability_conflict_review_needed', row.issues)
                self.assertFalse(row.verified)
            else:
                self.assertNotIn('purchase_availability_conflict_review_needed', row.issues)


if __name__ == '__main__':
    unittest.main()
