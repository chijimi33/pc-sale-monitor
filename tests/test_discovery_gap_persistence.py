from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sale_monitor.http import FetchError, Page
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


URL = "https://www.ark-pc.co.jp/special/ark_onlinestore_night_coupon_sale/"
OTHER = "https://www.ark-pc.co.jp/special/other/"
AGE = "2026-10-20T00:00:00+00:00"
NOW = "2026-10-24T00:00:00+00:00"
REASON = "no_sale_candidates_parser_review"
CFG = {"stores": {"ark": {"adapter": "html", "seed_urls": [],
    "product_patterns": [r"/i/\d+/"], "sale_patterns": [r"/special/"]}}}
EMPTY = b"<html><h1>Campaign</h1></html>"
PRODUCT = b'<a href="/i/123/">SSD</a>'


class Fake:
    def __init__(self, body=EMPTY):
        self.body = body
        self.count = 0

    def get(self, url):
        self.count += 1
        if isinstance(self.body, Exception):
            raise self.body
        return Page(url, self.body, NOW)


class DiscoveryGapPersistence(unittest.TestCase):
    def test_redirected_products_do_not_resolve_original_gap(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self.collector(root)
            task_id = self.add_task(c)
            c.collect(seconds=1)
            original = deepcopy(c.state["discovery_gaps"])
            c = self.collector(root, "second")
            c.client.get = lambda url: Page("https://www.ark-pc.co.jp/", PRODUCT, NOW)
            state = c.collect(seconds=1)
            self.assertIn(task_id, state["queue"])
            self.assertNotIn(task_id, state["done"])
            self.assertEqual(state["queue"][task_id]["created_at"], AGE)
            self.assertEqual(state["queue"][task_id]["attempts"], 6)
            self.assertEqual(state["queue"][task_id]["last_error"], "discovery_gap_scope_unverified")
            self.assertEqual(state["discovery_gaps"], original)
            self.assertEqual(state.get("discovery_gap_resolutions", []), [])
            self.assertEqual(state["offers"], {})
            self.assertEqual(len(state["queue"]), 1)

    def test_explicit_replacement_can_resolve_only_its_exact_response(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self.collector(root)
            task_id = self.add_task(c)
            c.collect(seconds=1)
            cfg = deepcopy(CFG)
            cfg["stores"]["ark"]["list_url_replacements"] = {URL: OTHER}
            c = Collector(root, "ark", cfg, "second", Fake(PRODUCT))
            def get(url):
                if url == OTHER:
                    return Page(url, PRODUCT, NOW)
                raise FetchError("http_503")
            c.client.get = get
            state = c.collect(seconds=1)
            self.assertNotIn(task_id, state["queue"])
            self.assertIn(task_id, state["done"])
            self.assertEqual(state["discovery_gaps"], [])
            receipt = state["discovery_gap_resolutions"][0]
            self.assertEqual(receipt["finding"]["url"], URL)
            self.assertEqual(receipt["requested_url"], OTHER)
            self.assertEqual(receipt["response_url"], OTHER)
            self.assertEqual(state["offers"], {})
            self.assertEqual(len(state["queue"]), 1)
            self.assertEqual(next(iter(state["queue"].values()))["created_at"], AGE)

    def collector(self, root, run="first", body=EMPTY):
        return Collector(root, "ark", deepcopy(CFG), run, Fake(body))

    def add_task(self, collector, url=URL):
        return collector.enqueue({"type": "list", "url": url,
            "sale_page": True, "created_at": AGE, "attempts": 4})

    def test_legacy_unresolved_gap_survives_new_run_without_rediscovery(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self.collector(root)
            finding = {"url": URL, "reason": REASON}
            c.state["discovery_gaps"] = [finding]
            c.save()
            for run in ("second", "third"):
                state = self.collector(root, run).collect(seconds=0)
                self.assertEqual(state["discovery_gaps"], [finding])
                self.assertEqual(state["status"], "partial")
                self.assertEqual(state["queue"], {})
                self.assertEqual(state["offers"], {})

    def test_empty_response_retains_task_age_attempts_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self.collector(root)
            task_id = self.add_task(c)
            state = c.collect(seconds=1)
            self.assertIn(task_id, state["queue"])
            self.assertNotIn(task_id, state["done"])
            self.assertEqual(state["queue"][task_id]["created_at"], AGE)
            self.assertEqual(state["queue"][task_id]["attempts"], 5)
            self.assertEqual(state["offers"], {})
            self.assertEqual(state["journal"], [])
            self.assertEqual(state["status"], "partial")
            self.assertFalse(state["cycle_complete"])
            self.assertEqual(Store(root).load("stores/ark.json", {})["queue"], state["queue"])

    def test_repeated_empty_response_keeps_one_finding_and_original_task(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self.collector(root)
            task_id = self.add_task(c)
            c.collect(seconds=1)
            original = deepcopy(c.state["discovery_gaps"][0])
            state = self.collector(root, "second").collect(seconds=1)
            self.assertIn(task_id, state["queue"])
            self.assertEqual(state["queue"][task_id]["attempts"], 6)
            self.assertEqual(state["queue"][task_id]["created_at"], AGE)
            self.assertEqual(len(state["discovery_gaps"]), 1)
            finding = state["discovery_gaps"][0]
            self.assertEqual(finding["original_task"], original["original_task"])
            self.assertEqual(finding["first_seen_at"], original["first_seen_at"])
            self.assertEqual(finding["response_evidence"]["observed_run_id"], "second")
            self.assertEqual(len(finding["response_evidence"]["content_hash"]), 64)

    def test_success_resolves_matching_gap_with_evidence_and_keeps_products_pending(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(Path(d), body=PRODUCT)
            task_id = self.add_task(c)
            finding = {"url": URL, "reason": REASON}
            c.state["discovery_gaps"] = [deepcopy(finding)]
            c.seed()
            c.process(c.state["queue"][task_id])
            self.assertEqual(c.state["discovery_gaps"], [])
            resolution = c.state["discovery_gap_resolutions"][0]
            self.assertEqual(resolution["finding"], finding)
            self.assertEqual(resolution["observed_run_id"], "first")
            self.assertEqual(resolution["product_count"], 1)
            self.assertTrue(any(t["type"] == "product" for t in c.state["queue"].values()))
            self.assertEqual(c.state["offers"], {})

    def test_unrelated_or_comparison_success_cannot_clear_finding(self):
        for url, kind in [(OTHER, None), (URL, "comparison")]:
            with self.subTest(url=url, kind=kind), tempfile.TemporaryDirectory() as d:
                c = self.collector(Path(d), body=PRODUCT)
                c.seed()
                finding = {"url": URL, "reason": REASON}
                c.state["discovery_gaps"] = [deepcopy(finding)]
                c.process({"type": "list", "url": url, "sale_page": True,
                    "kind": kind, "created_at": AGE})
                self.assertEqual(c.state["discovery_gaps"], [finding])
                self.assertFalse(c.state.get("discovery_gap_resolutions"))

    def test_transport_failure_does_not_clear_existing_gap(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(Path(d), body=FetchError("http_503"))
            task_id = self.add_task(c)
            finding = {"url": URL, "reason": REASON}
            c.state["discovery_gaps"] = [deepcopy(finding)]
            state = c.collect(seconds=1)
            self.assertEqual(state["discovery_gaps"], [finding])
            self.assertEqual(state["queue"][task_id]["created_at"], AGE)
            self.assertEqual(state["status"], "partial")

    def test_nested_campaign_does_not_count_as_finished_product_coverage(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(Path(d), body=b'<a href="/special/other/">Sale</a>')
            self.add_task(c)
            finding = {"url": URL, "reason": REASON}
            c.state["discovery_gaps"] = [deepcopy(finding)]
            c.seed()
            c.process({"type": "list", "url": URL, "sale_page": True, "created_at": AGE})
            self.assertEqual(c.state["discovery_gaps"], [])
            self.assertTrue(any(t.get("url") == OTHER for t in c.state["queue"].values()))
            self.assertEqual(c.state["offers"], {})
            self.assertEqual(c.state["discovery_gap_resolutions"][0]["campaign_count"], 1)

    def test_pagination_only_or_ended_text_is_not_verification(self):
        for body in (b'<a rel="next" href="?page=2">Next</a>', b'<h1>Sale ended</h1>'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as d:
                c = self.collector(Path(d), body=body)
                task_id = self.add_task(c)
                state = c.collect(seconds=1)
                self.assertIn(task_id, state["queue"])
                self.assertEqual(state["status"], "partial")
                self.assertFalse(state.get("discovery_gap_resolutions"))

