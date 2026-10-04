"""Offline Amazon access-challenge regressions; all response bodies are synthetic."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from html import escape
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sale_monitor.adapters import amazon_product
from sale_monitor.http import Client, FetchError, Page
from sale_monitor.models import Offer, UTC, iso
from sale_monitor.reporting import aggregate, health
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


NOW = datetime(2026, 10, 4, 3, tzinfo=UTC)
URLS = tuple(f"https://www.amazon.co.jp/dp/B00000000{n}" for n in range(1, 4))
SOURCE = "https://discovery.example.test/items.json"
CFG = {"stores": {"amazon": {"adapter": "amazon", "source_url": SOURCE}}}
REASON = "amazon_access_challenge"
PATHS = ("/errors_page/validateCaptcha", "/errors/validateCaptcha")


def challenge_form(action=PATHS[0]):
    # Structure only: no captured response, hidden token, or CAPTCHA submission.
    return (f'<form method="get" action="{escape(action, quote=True)}">'
            '<input name="field-keywords" value="">'
            '<button type="submit">Continue</button></form>')


def product_html(url=URLS[0], extra="", delivery="無料配送", buybox=True):
    price = ('<div id="corePrice_feature_div">'
             '<span class="a-price a-text-price"><span class="a-offscreen">￥12,900</span></span>'
             '<span class="a-price"><span class="a-offscreen">￥8,500</span></span></div>') if buybox else ""
    return (f'<html><body><h1 id="productTitle">Fixture SSD</h1>'
            f'<input id="ASIN" value="{url.rsplit("/", 1)[-1]}">{price}'
            '<div id="availability">在庫あり</div>'
            '<div id="merchant-info">販売元 Amazon.co.jp 出荷元 Amazon.co.jp</div>'
            '<div id="newAccordionRow">新品</div>'
            f'<div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">{delivery}</div>'
            '<table><tr><th>メーカー</th><td>Fixture</td></tr>'
            '<tr><th>型番</th><td>SSD-1</td></tr></table>'
            f'{extra}</body></html>')


class AmazonAccessDetection(unittest.TestCase):
    def test_official_captcha_form_paths_raise_before_product_parsing(self):
        for host in ("amazon.co.jp", "www.amazon.co.jp"):
            url = f"https://{host}/dp/B000000001"
            for path in PATHS:
                actions = (path, ".." + path,
                           f"https://{host}{path}?language=ja_JP#form",
                           f"//{host}{path}")
                for action in actions:
                    with self.subTest(host=host, action=action):
                        # Even valid buy-box metadata must not become an Offer.
                        page = Page(url, product_html(extra=challenge_form(action)).encode(), iso(NOW))
                        client = Mock(spec=Client)
                        client.get.return_value = page
                        with patch("sale_monitor.adapters.parse_product") as parse:
                            with self.assertRaises(FetchError) as caught:
                                amazon_product(client, {"url": url})
                        self.assertEqual(str(caught.exception), REASON)
                        self.assertIs(caught.exception.page, page)
                        parse.assert_not_called()
                        client.get.assert_called_once_with(url)
                        client.rendered.assert_not_called()

    def test_incidental_markup_and_unrelated_form_actions_keep_valid_product(self):
        form = challenge_form()
        cases = {
            "text": "<p>captcha /errors_page/validateCaptcha /errors/validateCaptcha</p>",
            "comment": f"<!-- {form} -->",
            "script": f"<script>const example = {json.dumps(form)};</script>",
            "template": f"<template>{form}</template>",
            "noscript": f"<noscript>{form}</noscript>",
            "link": f'<a href="{PATHS[0]}">captcha help</a>',
            "ordinary_form": '<form action="/gp/product/handle-buy-box" id="captcha-help"><button>Buy</button></form>',
            "actionless_form": f'<form data-action="{PATHS[0]}"><input name="captcha"></form>',
            "external_host": challenge_form("https://example.test" + PATHS[0]),
            "lookalike_host": challenge_form("https://www.amazon.co.jp.example.test" + PATHS[1]),
            "host_in_userinfo": challenge_form("https://www.amazon.co.jp@example.test" + PATHS[0]),
            "path_suffix": challenge_form(PATHS[0] + "Help"),
            "child_path": challenge_form(PATHS[1] + "/help"),
            "path_only_in_query": challenge_form("/gp/cart/add.html?next=" + PATHS[0]),
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                client = Mock(spec=Client)
                client.get.return_value = Page(URLS[0], product_html(extra=extra).encode(), iso(NOW))
                offer = amazon_product(client, {"url": URLS[0]})
                self.assertTrue(offer.verified)
                self.assertEqual((offer.product_id, offer.price_yen, offer.seller_id),
                                 ("B000000001", 8500, "amazon"))
                self.assertEqual(offer.stock, "in_stock")
                client.get.assert_called_once_with(URLS[0])

    def test_normal_buybox_shipping_and_discovery_price_rules_are_preserved(self):
        for delivery, buybox, shipping, price in (
                ("無料配送", True, 0, 8500),
                ("3,500円以上で無料配送", True, None, 8500),
                ("無料配送", False, 0, None)):
            with self.subTest(delivery=delivery, buybox=buybox):
                page = Page(URLS[0], product_html(delivery=delivery, buybox=buybox).encode(), iso(NOW))
                client = Mock(spec=Client)
                client.get.return_value = page
                candidate = {"url": URLS[0], "source": SOURCE, "title": "Discovery title",
                             "chimolog": {"price_yen": 7000}}
                offer = amazon_product(client, candidate)
                self.assertEqual((offer.title, offer.model, offer.brand), ("Fixture SSD", "SSD-1", "Fixture"))
                self.assertEqual((offer.price_yen, offer.shipping_yen, offer.verified), (price, shipping, buybox))
                self.assertEqual((offer.seller_id, offer.stock, offer.condition), ("amazon", "in_stock", "new"))
                self.assertEqual(offer.observed_at, page.observed_at)
                self.assertEqual(offer.discovery_url, SOURCE)
                self.assertEqual(offer.issues, [])
                receipt = next(e for e in offer.evidence if e["method"] == "amazon_buybox")
                self.assertEqual((receipt["url"], receipt["checked_at"]), (URLS[0], iso(NOW)))
                self.assertEqual(receipt["fields"]["chimolog_discovery"], {"price_yen": 7000})
                client.get.assert_called_once_with(URLS[0])


class AmazonAccessCollection(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="amazon-access-")))
        self.disk = Store(self.root)
        self.now = NOW
        self.challenge = True
        for module in ("sale_monitor.models", "sale_monitor.runner"):
            self.stack.enter_context(patch(f"{module}.utcnow", side_effect=lambda: self.now))
        # Keep Client.get and its request counter real; replace only the transport.
        self.open = self.stack.enter_context(patch("urllib.request.OpenerDirector.open", side_effect=self.response))
        self.browser = self.stack.enter_context(patch.object(Client, "rendered", side_effect=AssertionError("no browser requests")))
        self.previous = {}
        for url in URLS[:2]:
            offer = Offer("amazon", url.rsplit("/", 1)[-1], url, title="Previous SSD",
                          brand="Fixture", model="SSD-1", seller_id="amazon", condition="new",
                          price_yen=9000, shipping_yen=0, stock="in_stock", verified=True,
                          observed_at=iso(NOW - timedelta(hours=1)), observed_run_id="old",
                          discovery_url=SOURCE, evidence=[{"url": url, "fields": {"price": 9000}}])
            self.previous[offer.key] = offer.to_dict()
        self.failed_key = next(iter(self.previous))
        self.receipt = {"observation_id": "previous-receipt", "run_id": "old",
                        "offer": deepcopy(self.previous[self.failed_key])}
        self.history_name = "history/amazon/2026-10-04.json"
        self.disk.save(self.history_name, [self.receipt])
        # Resume a real pending product queue after discovery has already run.
        self.disk.save("stores/amazon.json", {
            "schema_version": 1, "store": "amazon", "run_id": "run1", "status": "partial",
            "cycle_complete": False, "queue": {}, "done": ["earlier-discovery"],
            "offers": self.previous, "journal": [self.receipt], "list_pages": 1,
        })
        self.collector = Collector(self.root, "amazon", CFG, "run1", Client(delay=0))
        for n, url in enumerate(URLS):
            self.collector.enqueue({"type": "product", "url": url, "kind": "sale", "source": SOURCE,
                                    "created_at": iso(NOW - timedelta(days=3 - n)),
                                    "attempts": 2 if n == 0 else 0, "priority": n})
        self.collector.save()
        self.before = deepcopy(self.collector.state)

    def response(self, request, timeout=None):
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        url = request.full_url
        if url == SOURCE:
            body = json.dumps({"items": [], "fetched_at": iso(self.now),
                               "site": {"updated_at": iso(self.now)}}).encode()
        else:
            self.assertIn(url, URLS, "unexpected offline request")
            body = product_html(url, extra=challenge_form() if self.challenge else "").encode()
        response = BytesIO(body)
        response.url = url
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        return response

    def test_first_challenge_stops_preserves_queue_observations_journal_and_public_history(self):
        state = self.collector.collect(seconds=10)
        self.open.assert_called_once()
        self.assertEqual(self.open.call_args.args[0].full_url, URLS[0])
        self.assertEqual(state["request_count"], 1)
        self.browser.assert_not_called()
        self.assertEqual((state["status"], state["cycle_complete"]), ("access_required", False))
        self.assertEqual(state["done"], self.before["done"])
        self.assertEqual(set(state["queue"]), set(self.before["queue"]))
        failed_id = next(key for key, task in state["queue"].items() if task["url"] == URLS[0])
        expected_queue = deepcopy(self.before["queue"])
        expected_queue[failed_id].update(attempts=3, last_error=REASON,
                                         last_attempt_at=iso(NOW), last_attempt_run_id="run1")
        self.assertEqual(state["queue"], expected_queue)
        self.assertEqual(self.collector.attempted, {failed_id})
        self.assertEqual(sum(t["attempts"] == 0 for t in state["queue"].values()), 2)
        self.assertEqual(state["pending_count"], 3)
        self.assertEqual(state["errors"], [{"task_id": failed_id, "reason": REASON, "url": URLS[0]}])
        retained = deepcopy(self.previous)
        retained[self.failed_key]["issues"] = ["latest_fetch_failed"]
        self.assertEqual(state["offers"], retained)
        self.assertEqual(state["journal"], [self.receipt])
        self.assertEqual(self.disk.history(), [self.receipt])
        block = {"run_id": "run1", "reason": REASON, "url": URLS[0], "checked_at": iso(NOW),
                 "body_sha256": sha256(product_html(extra=challenge_form()).encode()).hexdigest()}
        self.assertEqual(state["access_block"], block)
        self.assertEqual(self.disk.load("stores/amazon.json", {}), state)

        aggregate(self.root, self.root / "public", "run1", NOW)
        public = self.disk.load("public/latest.json", {})["stores"]["amazon"]
        self.assertEqual(public["access_block"], block)
        self.assertEqual((public["status"], public["cycle_complete"]), ("access_required", False))
        self.assertEqual((public["known_offers"], public["current_offers"], public["eligible_offers"]), (2, 0, 0))
        # The youngest task is exactly 24 hours old, not over 24 hours.
        self.assertEqual((public["request_count"], public["pending_count"], public["pending_over_24h"]), (1, 3, 2))
        self.assertEqual(public["oldest_pending_at"], iso(NOW - timedelta(days=3)))
        self.assertEqual(public["pending_by_type"], {"product:sale": 3})
        self.assertEqual(self.disk.load("public/evidence.json", {})["decisions"], [])
        self.assertEqual(self.disk.load("public/notifications.json", {})["events"], [])
        # The old outbox receipt is drained idempotently, never rewritten as current.
        self.assertEqual(self.disk.history(), [self.receipt])
        persisted = self.disk.load("stores/amazon.json", {})
        self.assertEqual(persisted["journal"], [])
        self.assertEqual(persisted["offers"], retained)
        self.assertEqual(persisted["queue"], expected_queue)
        self.assertEqual(persisted["access_block"], block)
        self.open.assert_called_once()

    def test_same_run_resume_makes_no_http_even_after_time_passes(self):
        blocked = deepcopy(self.collector.collect(seconds=10))
        self.open.reset_mock()
        self.now = NOW + timedelta(days=1)
        # A healthy response becoming available must not bypass the saved block.
        self.challenge = False
        for _ in range(2):
            resumed = Collector(self.root, "amazon", CFG, "run1", Client(delay=0))
            state = resumed.collect(seconds=10)
            self.assertEqual((state["status"], state["cycle_complete"]), ("access_required", False))
            for key in ("access_block", "queue", "offers", "journal", "errors", "done", "scheduler"):
                self.assertEqual(state[key], blocked[key], key)
            self.assertEqual(resumed.attempted, self.collector.attempted)
            self.assertEqual(resumed.client.count, 0)
            self.assertEqual(self.disk.load("stores/amazon.json", {}), state)
        self.open.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(self.disk.history(), [self.receipt])

    def test_distinct_run_can_recover_without_promoting_old_observations(self):
        blocked = deepcopy(self.collector.collect(seconds=10))
        self.open.reset_mock()
        self.challenge = False
        self.now = NOW + timedelta(minutes=5)
        recovered = Collector(self.root, "amazon", CFG, "run2", Client(delay=0))
        self.assertFalse(recovered.state.get("access_block"))
        self.assertEqual(recovered.state["offers"], blocked["offers"])
        self.assertEqual(recovered.state["queue"], blocked["queue"])
        # These snapshots are still fresh by age, but belong to the earlier run.
        self.assertEqual(health(recovered.state, self.now)["current_offers"], 0)
        self.open.assert_not_called()
        state = recovered.collect(seconds=10)
        requested = [call.args[0].full_url for call in self.open.call_args_list]
        self.assertCountEqual(requested, [SOURCE, *URLS])
        self.assertEqual(state["request_count"], 4)
        self.assertEqual((state["status"], state["cycle_complete"]), ("complete", True))
        self.assertEqual(state["queue"], {})
        self.assertEqual(state["errors"], [])
        self.assertFalse(state.get("access_block"))
        self.assertEqual(len(state["offers"]), 3)
        for row in state["offers"].values():
            self.assertEqual((row["price_yen"], row["observed_run_id"], row["observed_at"]),
                             (8500, "run2", iso(self.now)))
            self.assertTrue(row["verified"])
            self.assertNotIn("latest_fetch_failed", row["issues"])
        self.assertEqual(state["journal"][0], self.receipt)
        self.assertEqual(len(state["journal"]), 4)
        self.assertEqual([row["run_id"] for row in state["journal"][1:]], ["run2"] * 3)
        self.assertEqual(self.disk.history(), [self.receipt])
        self.assertEqual(self.disk.load("stores/amazon.json", {}), state)
        aggregate(self.root, self.root / "public", "run2", self.now)
        public = self.disk.load("public/latest.json", {})["stores"]["amazon"]
        self.assertIsNone(public["access_block"])
        self.assertEqual(public["current_offers"], 3)
        history = self.disk.history()
        self.assertEqual(len(history), 4)
        self.assertEqual([row for row in history if row["run_id"] == "old"], [self.receipt])
        self.assertEqual(sum(row["run_id"] == "run2" for row in history), 3)
        self.assertFalse(any(row["run_id"] == "run1" for row in history))
        self.browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
