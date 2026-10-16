from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lxml.etree import ParserError

from sale_monitor.http import FetchError, Page
from sale_monitor.models import Offer, iso
from sale_monitor.parsing import document
from sale_monitor.reporting import health
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
OLD = iso(NOW - timedelta(days=3))
CFG = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}
URL = "https://www.ark-pc.co.jp/i/12345678/"
VALID = (b'<h1>Test product</h1><script type="application/ld+json">'
         b'{"@type":"Product","name":"Test product","sku":"MODEL-1",'
         b'"offers":{"price":8500,"priceCurrency":"JPY",'
         b'"availability":"https://schema.org/InStock"}}</script>')


class Client:
    def __init__(self, page):
        self.page = page
        self.count = 0

    def get(self, url):
        self.count += 1
        return self.page


class ParserResponseEvidence(unittest.TestCase):
    def test_empty_and_whitespace_keep_parser_error_type_and_exact_page(self):
        for body in (b"", b" \n\t"):
            with self.subTest(body=body):
                page = Page(URL, body, iso(NOW))
                with self.assertRaises(ParserError) as caught:
                    document(page)
                self.assertIs(type(caught.exception), ParserError)
                self.assertIs(caught.exception.page, page)

    def test_valid_document_is_unchanged(self):
        self.assertEqual(document(Page(URL, VALID, iso(NOW))).xpath("//h1/text()"), ["Test product"])

    def test_non_parser_exception_is_not_wrapped(self):
        with patch("sale_monitor.parsing.html.fromstring", side_effect=ValueError("bad input")):
            with self.assertRaisesRegex(ValueError, "bad input"):
                document(Page(URL, VALID, iso(NOW)))

    def test_failure_checkpoint_preserves_age_attempts_history_and_old_stock(self):
        for body in (b"", b" \n\t"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                page = Page(URL, body, iso(NOW), method="browser", status=200)
                client = Client(page)
                c = Collector(root, "ark", CFG, "r1", client)
                old = Offer("ark", "Test product", URL, price_yen=9000,
                            stock="in_stock", observed_at=OLD, observed_run_id="old")
                c.state["offers"][old.key] = old.to_dict()
                c.state["journal"] = [{"run_id": "old", "offer": old.to_dict()}]
                journal = deepcopy(c.state["journal"])
                c.enqueue({"type": "product", "url": URL, "created_at": OLD, "attempts": 7})
                task_id = next(iter(c.state["queue"]))
                result = c.collect(seconds=1)
                task = result["queue"][task_id]
                self.assertEqual((task["created_at"], task["attempts"], task["last_error"]),
                                 (OLD, 8, "ParserError"))
                self.assertNotIn("retry_at_epoch_seconds", task)
                self.assertNotIn(task_id, result["done"])
                self.assertFalse(result["cycle_complete"])
                self.assertEqual(client.count, 1)
                self.assertEqual(result["journal"], journal)
                self.assertEqual(result["offers"][old.key]["stock"], "in_stock")
                self.assertIn("latest_fetch_failed", result["offers"][old.key]["issues"])
                self.assertEqual(health(result, NOW)["current_offers"], 0)
                self.assertEqual(health(result, NOW)["pending_over_24h"], 1)
                self.assertEqual(result["errors"][0]["response_evidence"], {
                    "status": 200, "method": "browser", "body_bytes": len(body),
                    "body_sha256": hashlib.sha256(body).hexdigest(), "observed_at": iso(NOW)})
                self.assertEqual(Store(root).load("stores/ark.json", {})["errors"], result["errors"])
                # Recovery is a new observation; diagnostics do not complete a failed task.
                resumed = Collector(root, "ark", CFG, "r2", Client(Page(URL, VALID, iso(NOW))))
                recovered = resumed.collect(seconds=1)
                self.assertEqual(recovered["queue"], {})
                self.assertEqual(recovered["errors"], [])
                self.assertEqual(recovered["recovered_errors"][0]["attempts"], 8)
                self.assertEqual(recovered["journal"][:1], journal)

    def test_shared_response_is_one_request_and_multiple_unresolved_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client(Page(URL, b"", iso(NOW)))
            c = Collector(Path(tmp), "ark", CFG, "r1", client)
            for kind in ("sale", "comparison"):
                c.enqueue({"type": "product", "url": URL, "kind": kind, "created_at": OLD})
            result = c.collect(seconds=1)
            self.assertEqual(client.count, 1)
            self.assertEqual(len(result["errors"]), 2)
            self.assertEqual(len(result["queue"]), 2)
            self.assertEqual(result["errors"][0]["response_evidence"], result["errors"][1]["response_evidence"])

    def test_diagnostics_exclude_body_redirect_url_headers_and_exception_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Page(URL + "?token=redirect-secret", b"body-secret", iso(NOW),
                        content_type="text/html; x-secret=header-secret", status=200)
            c = Collector(Path(tmp), "ark", CFG, "r1", Client(page))
            c.enqueue({"type": "product", "url": URL})
            with patch("sale_monitor.parsing.html.fromstring", side_effect=ParserError("message-secret")):
                result = c.collect(seconds=1)
            saved = json.dumps(Store(Path(tmp)).load("stores/ark.json", {}))
            for secret in ("body-secret", "redirect-secret", "header-secret", "message-secret"):
                self.assertNotIn(secret, saved)
            self.assertEqual(result["errors"][0]["reason"], "ParserError")
            self.assertEqual(result["errors"][0]["response_evidence"]["body_bytes"], 11)

    def test_unrelated_fetch_failure_has_no_parser_response_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Collector(Path(tmp), "ark", CFG, "r1", Client(Page(URL, b"", iso(NOW))))
            c.enqueue({"type": "product", "url": URL})
            c.process = lambda task: (_ for _ in ()).throw(FetchError("http_403", page=Page(URL, b"denied", iso(NOW), status=403)))
            result = c.collect(seconds=1)
            self.assertEqual(result["errors"][0]["reason"], "http_403")
            self.assertNotIn("response_evidence", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
