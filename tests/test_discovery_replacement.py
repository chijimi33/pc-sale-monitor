from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sale_monitor.http import FetchError, Page
from sale_monitor.runner import Collector
from sale_monitor.storage import Store

OLD = "https://www.ark-pc.co.jp/t/c/760/"
NEW = "https://www.ark-pc.co.jp/search/?onsale=1"
AGE = "2026-09-28T00:00:00+00:00"
NOW = "2026-10-02T00:00:00+00:00"
CFG = {"stores": {"ark": {"adapter": "html", "seed_urls": [NEW],
       "product_patterns": [r"/i/\d+/"], "sale_patterns": [r"/special/"],
       "list_url_replacements": {OLD: NEW}}}}
BODY = b'<a href="/i/123/">SSD</a><a rel="next" href="?onsale=1&offset=15">next</a>'


class Fake:
    count = 0

    def __init__(self, result=BODY):
        self.result = result
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        self.count += 1
        if isinstance(self.result, Exception):
            raise self.result
        return Page(url, self.result, NOW)


class DiscoveryReplacement(unittest.TestCase):
    def collector(self, root, fake):
        c = Collector(root, "ark", CFG, "new", fake)
        c.enqueue({"type": "list", "url": OLD, "sale_page": True,
                   "created_at": AGE, "attempts": 4, "last_error": "http_404"})
        c.state["discovery_gaps"] = []
        c.state["list_pages"] = c.state["listed_candidates"] = 0
        return c

    def test_success_enqueues_real_discovery_and_keeps_failure_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(); c = self.collector(Path(d), fake)
            original = deepcopy(next(iter(c.state["queue"].values())))
            c.enqueue({"type": "list", "url": "https://www.ark-pc.co.jp/search/?offset=15&onsale=1",
                       "sale_page": True, "created_at": NOW})
            c.process(original)
            self.assertEqual(fake.urls, [NEW])
            evidence = c.state["discovery_replacements"][OLD]
            self.assertEqual(evidence["original_task"], original)
            self.assertEqual(evidence["http_status"], 200)
            self.assertEqual(evidence["product_count"], 1)
            self.assertEqual(evidence["observed_run_id"], "new")
            self.assertEqual(len(evidence["content_hash"]), 64)
            for task in c.state["queue"].values():
                self.assertEqual(task["created_at"], AGE)
            product = next(t for t in c.state["queue"].values() if t["type"] == "product")
            self.assertEqual(product["source"], NEW)
            self.assertEqual(c.state["offers"], {})
            pagination = next(t for t in c.state["queue"].values() if "offset=" in t.get("url", ""))
            fake.result = b'<a href="/i/456/">SSD</a>'
            c.process(pagination)
            later_product = next(t for t in c.state["queue"].values() if t.get("url", "").endswith("/456/"))
            self.assertEqual(later_product["created_at"], AGE)
            self.assertEqual(later_product["discovery_origin"]["url"], OLD)

    def test_failed_or_empty_replacement_retains_original_pending_age(self):
        for result in (FetchError("http_503"), b"<h1>SALE</h1>"):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as d:
                root = Path(d); c = self.collector(root, Fake(result))
                original_id = next(iter(c.state["queue"]))
                state = c.collect(seconds=1)
                self.assertIn(original_id, state["queue"])
                self.assertEqual(state["queue"][original_id]["created_at"], AGE)
                self.assertFalse(state["cycle_complete"])
                self.assertNotIn("discovery_replacements", state)
                self.assertEqual(Store(root).load("stores/ark.json", {})["queue"][original_id]["created_at"], AGE)

    def test_comparison_and_product_urls_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(FetchError("http_404")); c = self.collector(Path(d), fake)
            for kind in ("comparison", "product"):
                task = {"type": "product" if kind == "product" else "list", "url": OLD,
                        "sale_page": True, "kind": kind}
                with self.assertRaises(FetchError):
                    c.process(task)
            self.assertEqual(fake.urls, [OLD])

    def test_fresh_cycle_seeds_current_entry_and_preserves_old_task(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.collector(Path(d), Fake())
            c.seed()
            tasks = {t["url"]: t for t in c.state["queue"].values()}
            self.assertEqual(set(tasks), {OLD, NEW})
            self.assertEqual(tasks[OLD]["created_at"], AGE)
            fresh = Collector(Path(d) / "fresh", "ark", CFG, "next", Fake())
            fresh.seed()
            self.assertEqual([t["url"] for t in fresh.state["queue"].values()], [NEW])
