import json
import unittest
from datetime import datetime, timezone

from sale_monitor.http import Page
from sale_monitor.parsing import canonical, parse_product


URL = "https://shop.tsukumo.co.jp/goods/0409092744012/"
OTHER = "https://shop.tsukumo.co.jp/goods/0409092745019/"
NOW = "2026-10-11T15:00:00+00:00"


def parse(page_url=URL, schema_url=URL, declared=URL, store="tsukumo", duplicate=False, **changes):
    item = {"@type": "Product", "name": "G-GEAR GE7A-C261B/NT2",
            "sku": "0409092744012", "offers": {
                "@type": "Offer", "url": schema_url, "priceCurrency": "JPY",
                "price": "289980", "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/NewCondition"}}
    item["offers"].update(changes)
    body = (f'<html><head><link rel="canonical" href="{declared}">'
            '<script type="application/ld+json">' + json.dumps([item, item] if duplicate else item) +
            '</script></head><body><h1>G-GEAR GE7A-C261B/NT2</h1></body></html>')
    return parse_product(store, Page(page_url, body.encode(), NOW), {"default_condition": "new"})


class TsukumoProductUrlTests(unittest.TestCase):
    def test_trailing_slash_preserves_primary_product_and_observed_url(self):
        for page_url, schema_url in [(URL[:-1], URL), (URL, URL[:-1])]:
            with self.subTest(page_url=page_url, schema_url=schema_url):
                offer = parse(page_url, schema_url, schema_url)
                self.assertTrue(offer.verified)
                self.assertEqual((offer.jan, offer.price_yen, offer.stock),
                                 ("0409092744012", 289980, "in_stock"))
                self.assertEqual(offer.url, page_url)
                self.assertEqual(offer.evidence[0]["url"], page_url)
                self.assertEqual(offer.evidence[0]["checked_at"], NOW)
                self.assertEqual(offer.key, parse().key)

    def test_another_product_canonical_still_blocks(self):
        offer = parse(URL[:-1], URL, OTHER)
        self.assertIn("canonical_product_mismatch", offer.issues)
        self.assertFalse(offer.verified)

    def test_another_schema_product_cannot_supply_price_or_identity(self):
        offer = parse(URL[:-1], OTHER, URL)
        self.assertIsNone(offer.price_yen)
        self.assertIsNone(offer.jan)
        self.assertFalse(offer.verified)

    def test_ambiguous_products_still_require_review(self):
        offer = parse(URL[:-1], URL, URL, duplicate=True)
        self.assertIsNone(offer.price_yen)
        self.assertIsNone(offer.jan)
        self.assertFalse(offer.verified)

    def test_equivalence_is_limited_to_exact_https_goods_scope(self):
        variants = [
            URL + "?variant=other", URL[:-1] + "?variant=other",
            URL.replace("https://", "http://"),
            URL.replace("shop.tsukumo.co.jp", "other.example"),
            URL.replace("shop.tsukumo.co.jp", "shop.tsukumo.co.jp:443"),
            URL.replace("shop.tsukumo.co.jp", "user@shop.tsukumo.co.jp"),
            URL.replace("/goods/", "/other/"), URL + "/",
            URL.replace("0409092744012", "0409092744013"),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                offer = parse(URL[:-1], variant, variant)
                self.assertIn("canonical_product_mismatch", offer.issues)
                self.assertIsNone(offer.price_yen)
                self.assertFalse(offer.verified)
        bad_id = URL.replace("0409092744012", "0409092744013")
        self.assertIn("canonical_product_mismatch", parse(bad_id[:-1], bad_id, bad_id).issues)

    def test_other_stores_keep_their_url_policy(self):
        for store in ("ark", "bic", "joshin", "sofmap", "yodobashi", "koubou"):
            with self.subTest(store=store):
                offer = parse(URL[:-1], URL, URL, store)
                self.assertFalse(offer.verified)
                self.assertIn("canonical_product_mismatch", offer.issues)

    def test_missing_shipping_stock_and_price_are_not_promoted(self):
        offer = parse(URL[:-1], URL, URL, availability="https://schema.org/OutOfStock")
        self.assertTrue(offer.verified)
        self.assertIsNone(offer.shipping_yen)
        self.assertEqual(offer.errors(datetime(2026, 10, 11, 15, tzinfo=timezone.utc)),
                         ["shipping_unknown", "stock_out_of_stock"])
        missing = parse(URL[:-1], URL, URL, price=None)
        self.assertIsNone(missing.price_yen)
        self.assertFalse(missing.verified)

    def test_task_url_canonicalization_is_unchanged(self):
        self.assertEqual(canonical(URL), URL)
        self.assertEqual(canonical(URL[:-1]), URL[:-1])
        self.assertNotEqual(canonical(URL), canonical(URL[:-1]))


if __name__ == "__main__":
    unittest.main()
