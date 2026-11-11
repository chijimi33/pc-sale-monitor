import unittest

from sale_monitor.http import Page
from sale_monitor.parsing import parse_product

URL = "https://www.dospara.co.jp/SBR1/IC123456.html"
PRODUCT = '<script>var productJson = {"productID": "IC123456", "pname": "ASUS TEST-1", "amttax": "10000", "stkname": "在庫あり"};</script>'
COUPON = """<script>switch (productId) { case 'IC123456': productMap.amount = '1000'; productMap.couponcode = 'abc123'; break; }</script>"""


def page(body):
    html = "<html><body><h1 class='product-show-detail'>ASUS TEST-1</h1>" + body + "</body></html>"
    return Page(URL, html.encode(), "2026-11-11T00:00:00+00:00")


class DosparaPageCoupon(unittest.TestCase):
    def test_plain_product_task_holds_a_page_that_shows_a_coupon(self):
        offer = parse_product("dospara", page(PRODUCT + COUPON), {"default_condition": "new"}, {"kind": "comparison"})
        self.assertIn("coupon_unverified_on_product_page", offer.issues)

    def test_plain_product_task_without_coupon_is_not_held_for_it(self):
        offer = parse_product("dospara", page(PRODUCT), {"default_condition": "new"}, {"kind": "comparison"})
        self.assertNotIn("coupon_unverified_on_product_page", offer.issues)

    def test_unidentified_page_mentioning_a_coupon_is_held(self):
        offer = parse_product("dospara", page(COUPON), {"default_condition": "new"}, {"kind": "comparison"})
        self.assertIn("coupon_unverified_on_product_page", offer.issues)

    def test_dedicated_adapter_path_is_unchanged(self):
        offer = parse_product("dospara", page(PRODUCT + COUPON), {"default_condition": "new"})
        self.assertNotIn("coupon_unverified_on_product_page", offer.issues)


if __name__ == "__main__":
    unittest.main()
