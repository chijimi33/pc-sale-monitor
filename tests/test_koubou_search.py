from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from sale_monitor.http import FetchError, Page
from sale_monitor.koubou_search import legacy_query, parse_search, search_url
from sale_monitor.models import Offer
from sale_monitor.reporting import health
from sale_monitor.runner import Collector, task_order


QUERY = "EZDIY RC21 - PCIe 5.0 x16 200 mm Riser Cable 90-Degree - Black EZDPC247 (PCIe 5.0 ライザーケーブル 200mm ブラック)"
NOW = datetime(2026, 9, 28, 6, tzinfo=timezone.utc)
OLD = "2026-09-18T11:54:01+00:00"
OBSERVED = "2026-09-28T05:55:00+00:00"
CFG = {"stores": {"koubou": {"adapter": "html", "seed_urls": [], "browser_fallback": True,
    "product_patterns": [r"/products/detail\.php\?product_id="]}}, "flyer": {}}


def payload(query=QUERY, hits=0, offset=0):
    return {"kotohaco": {
        "request": {"accountid": "pckoubou", "param": {"q": query, "o": offset, "limit": 20,
            "s2b": "通常", "s5": [""], "fmt": "json", "sort": "Score"}},
        "result": {"info": {"status": 0, "hitnum": hits, "offset": offset,
            "current_page": offset // 20 + 1, "last_page": max(1, (hits + 19) // 20)},
            "items": [{"itemid": str(1000 + n), "url": f"/products/detail.php?product_id={1000+n}",
                "title": f"Model {n}", "price": 100, "stock": "in_stock"}
                for n in range(offset, min(hits, offset + 20))]}}}


def page(data, query=QUERY, offset=0):
    return Page(search_url(query, offset), json.dumps(data, ensure_ascii=False).encode(), OBSERVED)


def legacy_url():
    return "https://www.pc-koubou.jp/user_data/search.php?" + urlencode({"cache_t": 60, "q": QUERY})


class Client:
    def __init__(self, handler):
        self.count = 0
        self.calls = []
        self.handler = handler
        self.retry_after = {}
        self.transport_retry_after = {}

    def get(self, url):
        self.count += 1
        self.calls.append(url)
        return self.handler(url)

    def rendered(self, url):
        raise AssertionError("JSON search must not fall back to an HTML browser response")


class KoubouSearchParsing(unittest.TestCase):
    def test_full_query_is_preserved_and_no_results_requires_explicit_evidence(self):
        self.assertEqual(parse_qs(urlsplit(search_url(QUERY)).query)["q"], [QUERY])
        self.assertEqual(legacy_query(legacy_url()), QUERY)
        products, next_offset, evidence = parse_search(page(payload()), QUERY)
        self.assertEqual(products, [])
        self.assertIsNone(next_offset)
        self.assertEqual(evidence["result"], "no_results")
        self.assertEqual(evidence["query"], QUERY)
        self.assertEqual(evidence["observed_at"], OBSERVED)
        self.assertTrue(evidence["content_hash"])

    def test_html_shell_and_invalid_json_are_not_empty_results(self):
        for raw in [b'<div id="list--display"></div>', b'{', b'{}', b'null']:
            with self.subTest(raw=raw), self.assertRaises(FetchError):
                parse_search(Page(search_url(QUERY), raw, OBSERVED), QUERY)

    def test_mismatched_query_filters_or_pagination_are_rejected(self):
        changes = [
            ("request", "accountid", "other"),
            ("param", "q", "shortened"), ("param", "o", 20), ("param", "limit", 10),
            ("param", "s2b", "中古"), ("param", "s5", ["filtered"]), ("param", "fmt", "html"),
            ("info", "status", 1), ("info", "status", False), ("info", "hitnum", "0"),
            ("info", "last_page", 2), ("info", "current_page", 2), ("info", "offset", 20),
            ("info", "current_page", True),
        ]
        for section, field, value in changes:
            data = payload(); root = data["kotohaco"]
            target = root["request"] if section == "request" else root["request"]["param"] if section == "param" else root["result"]["info"]
            target[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(FetchError):
                parse_search(page(data), QUERY)

    def test_partial_page_duplicate_or_foreign_product_is_rejected_atomically(self):
        samples = []
        data = payload(hits=2); data["kotohaco"]["result"]["items"].pop(); samples.append(data)
        data = payload(hits=2); data["kotohaco"]["result"]["items"][1] = deepcopy(data["kotohaco"]["result"]["items"][0]); samples.append(data)
        for url in ["https://item.rakuten.co.jp/shop/1000", "https://other.example/1000",
                    "/products/detail.php?product_id=9999", "/user_data/search.php?q=1000"]:
            data = payload(hits=1); data["kotohaco"]["result"]["items"][0]["url"] = url; samples.append(data)
        for data in samples:
            with self.subTest(data=data), self.assertRaises(FetchError):
                parse_search(page(data), QUERY)

    def test_redirect_or_http_failure_cannot_confirm_no_results(self):
        for url, status in [("https://item.rakuten.co.jp/test", 200), (search_url(QUERY), 500)]:
            p = page(payload()); p.url = url; p.status = status
            with self.subTest(url=url, status=status), self.assertRaises(FetchError):
                parse_search(p, QUERY)

    def test_all_pages_discover_urls_without_promoting_search_prices(self):
        found = []
        for offset in [0, 20, 40]:
            products, next_offset, evidence = parse_search(page(payload(hits=41, offset=offset), offset=offset), QUERY, offset)
            found.extend(products)
            self.assertEqual(next_offset, offset + 20 if offset < 40 else None)
            self.assertEqual(evidence["result"], "results")
        self.assertEqual(len({p["url"] for p in found}), 41)
        self.assertTrue(all(set(p) == {"url", "title", "source", "kind"} for p in found))

    def test_invalid_request_and_filtered_legacy_url_do_not_drop_conditions(self):
        for query, offset in [("", 0), (None, 0), (" ", 0), (QUERY, -20), (QUERY, 1), (QUERY, True)]:
            with self.subTest(query=query, offset=offset), self.assertRaises(FetchError):
                search_url(query, offset)
        for url in [legacy_url() + "&category=1", legacy_url() + "&q=other", legacy_url() + "#page2",
                    legacy_url().replace("www.pc-koubou.jp", "other.example")]:
            self.assertIsNone(legacy_query(url))


class KoubouSearchCollection(unittest.TestCase):
    def collect(self, collector, seconds=1):
        with patch("sale_monitor.runner.collect_flyer", return_value=([], {"status": "unchanged"})):
            return collector.collect(seconds=seconds)

    def test_legacy_failed_task_completes_only_after_verified_exact_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = Client(lambda url: page(payload()))
            c = Collector(Path(tmp), "koubou", CFG, "r1", client)
            c.enqueue({"type": "list", "kind": "comparison", "url": legacy_url(), "created_at": OLD,
                       "attempts": 66, "last_error": "http_500", "priority": 0})
            task_id = next(iter(c.state["queue"]))
            result = self.collect(c)
            self.assertEqual(result["queue"], {})
            self.assertIn(task_id, result["done"])
            self.assertEqual(result["recovered_errors"][0]["attempts"], 66)
            self.assertEqual(client.calls, [search_url(QUERY)])
            evidence = result["comparison_searches"][search_url(QUERY)]
            self.assertEqual(evidence["created_at"], OLD)
            self.assertEqual(evidence["original_task_url"], legacy_url())
            self.assertEqual(health(result, NOW)["comparison_no_results"], 1)

    def test_failure_retains_age_attempts_and_old_offer_is_not_current(self):
        for reason in ["http_500", "comparison_search_response_unverified"]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                def fail(url):
                    raise FetchError(reason)
                c = Collector(Path(tmp), "koubou", CFG, "r1", Client(fail))
                c.enqueue({"type": "list", "kind": "comparison", "url": legacy_url(), "created_at": OLD, "attempts": 66})
                task_id = next(iter(c.state["queue"]))
                old = Offer("koubou", "old", "https://www.pc-koubou.jp/products/detail.php?product_id=1",
                            price_yen=100, observed_at=OBSERVED, observed_run_id="previous", channel="store")
                c.state["offers"][old.key] = old.to_dict()
                result = self.collect(c, seconds=.05)
                self.assertEqual(result["queue"][task_id]["created_at"], OLD)
                self.assertEqual(result["queue"][task_id]["attempts"], 67)
                self.assertEqual(result["queue"][task_id]["last_error"], reason)
                self.assertEqual(health(result, NOW)["current_offers"], 0)
                self.assertEqual(health(result, NOW)["comparison_no_results"], 0)

    def test_pagination_checkpoint_resume_preserves_age_priority_and_deduplicates(self):
        def handler(url):
            offset = int(parse_qs(urlsplit(url).query)["o"][0])
            return page(payload(hits=41, offset=offset), offset=offset)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); client = Client(handler)
            c = Collector(root, "koubou", CFG, "r1", client); c.seed()
            original = {"type": "search", "kind": "comparison", "query": QUERY, "created_at": OLD, "priority": 0}
            c.process(original); c.process(original)  # interruption before original task acknowledgement
            self.assertEqual(len(c.state["queue"]), 21)
            c.save()
            resumed = Collector(root, "koubou", CFG, "r1", Client(handler))
            while pending := [(k, t) for k, t in resumed.state["queue"].items() if t["type"] == "koubou_search"]:
                task_id, task = pending[0]
                self.assertEqual(task["created_at"], OLD)
                self.assertEqual(task["priority"], 0)
                self.assertLess(task_order(task), task_order(original))
                resumed.process(task)
                resumed.state["done"].append(task_id); del resumed.state["queue"][task_id]
                resumed.save()
            self.assertEqual(len(resumed.state["queue"]), 41)
            self.assertTrue(all(t["type"] == "product" and t["created_at"] == OLD for t in resumed.state["queue"].values()))
            self.assertEqual(resumed.state["offers"], {})
            self.assertEqual(resumed.state["journal"], [])
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(health(resumed.state, NOW)["comparison_search_pages"], 3)

    def test_filtered_html_shell_keeps_task_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = legacy_url() + "&category=1"
            client = Client(lambda u: Page(u, b'<div id="list--display"></div>', OBSERVED))
            c = Collector(Path(tmp), "koubou", CFG, "r1", client)
            c.enqueue({"type": "list", "kind": "comparison", "url": url, "created_at": OLD})
            result = self.collect(c)
            self.assertEqual(len(result["queue"]), 1)
            self.assertEqual(next(iter(result["queue"].values()))["last_error"], "comparison_search_response_unverified")
            self.assertEqual(client.calls, [url])
            self.assertEqual(health(result, NOW)["comparison_no_results"], 0)

    def test_previous_run_search_evidence_not_counted_as_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Collector(Path(tmp), "koubou", CFG, "r1", Client(lambda u: page(payload())))
            c.seed(); c.process({"type": "search", "kind": "comparison", "query": QUERY}); c.save()
            resumed = Collector(Path(tmp), "koubou", CFG, "r2", Client(lambda u: page(payload())))
            self.assertEqual(health(resumed.state, NOW)["comparison_no_results"], 0)
            self.assertEqual(health(resumed.state, NOW)["comparison_search_pages"], 0)


if __name__ == "__main__":
    unittest.main()
