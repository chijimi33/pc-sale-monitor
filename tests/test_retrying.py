from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError, Page
from sale_monitor.models import Offer, iso
from sale_monitor.reporting import health
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
OLD = iso(NOW - timedelta(days=3))
CFG = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def patch(self, stack):
        stack.enter_context(patch("sale_monitor.runner.time.time", side_effect=lambda: self.now))
        stack.enter_context(patch("sale_monitor.runner.time.monotonic", side_effect=lambda: self.now))
        stack.enter_context(patch("sale_monitor.runner.time.sleep", side_effect=self.sleep))


class Client:
    def __init__(self, clock, handler):
        self.count = 0
        self.clock = clock
        self.handler = handler
        self.retry_after = {}
        self.transport_retry_after = {}
        self.calls = []

    def get(self, url):
        self.count += 1
        self.calls.append((url, self.clock.now))
        self.handler(self, url)
        return Page(url, b'<h1>Test product</h1><script type="application/ld+json">'
            b'{"@type":"Product","name":"Test product","sku":"MODEL-1","offers":'
            b'{"price":8500,"priceCurrency":"JPY","availability":"https://schema.org/InStock"}}</script>', iso(NOW))


class RetryScheduling(unittest.TestCase):
    def test_host_cooldown_recovers_in_same_run_without_attempting_every_waiter(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            def handle(client, url):
                if clock.now < 1300:
                    client.transport_retry_after["a.example"] = {"until": 1300, "reason": "RemoteDisconnected"}
                    raise FetchError("RemoteDisconnected")
            client = Client(clock, handle)
            c = Collector(Path(tmp), "ark", CFG, "r1", client)
            for n in range(5):
                c.enqueue({"type": "product", "url": f"https://a.example/{n}", "created_at": OLD})
            result = c.collect(seconds=700)
            self.assertEqual(result["queue"], {})
            self.assertTrue(result["cycle_complete"])
            self.assertEqual(client.count, 6)  # One failed fetch, five actual recoveries.
            self.assertEqual(sum(t < 1300 for _, t in client.calls), 1)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["retry_activity"]["recovered_tasks"], 1)
            self.assertEqual(result["recovered_errors"][0]["attempts"], 1)
            self.assertEqual(health(result, NOW)["current_offers"], 5)
            self.assertTrue(all(0 < n <= 30 for n in clock.sleeps))

    def test_shared_home_dependency_waits_then_releases_all_searches(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            def handle(client, url):
                if clock.now < 1300:
                    raise FetchError("TimeoutError")
            client = Client(clock, handle)
            cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": ["https://a.example/"]}}}
            c = Collector(Path(tmp), "ark", cfg, "r1", client)
            for n in range(20):
                c.enqueue({"type": "search", "query": str(n), "kind": "comparison", "created_at": OLD})
            c.process = lambda task: c.page("https://a.example/")
            result = c.collect(seconds=700)
            self.assertEqual(result["queue"], {})
            self.assertEqual(client.count, 2)
            self.assertEqual(result["retry_activity"]["recovered_tasks"], 1)
            self.assertEqual(len(result["done"]), 21)

    def test_long_wait_survives_restart_preserves_queue_age_and_excludes_old_price(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            client = Client(clock, lambda *_: None)
            client.retry_after["a.example"] = 1900
            root = Path(tmp); c = Collector(root, "ark", CFG, "r1", client)
            old = Offer("ark", "one", "https://a.example/one", price_yen=9000,
                        stock="in_stock", observed_at=iso(NOW), observed_run_id="old")
            c.state["offers"][old.key] = old.to_dict()
            c.enqueue({"type": "product", "url": old.url, "kind": "sale", "created_at": OLD, "attempts": 7})
            result = c.collect(seconds=60)
            task = next(iter(result["queue"].values()))
            self.assertEqual((task["created_at"], task["attempts"]), (OLD, 7))
            self.assertEqual(client.count, 0)
            self.assertEqual(health(result, NOW)["current_offers"], 0)
            self.assertEqual(health(result, NOW)["pending_over_24h"], 1)
            self.assertEqual(result["offers"][old.key]["stock"], "in_stock")
            resumed_client = Client(clock, lambda *_: None)
            resumed = Collector(root, "ark", CFG, "r2", resumed_client)
            clock.now = 1850
            resumed.collect(seconds=100)
            self.assertEqual(resumed_client.calls, [(old.url, 1900)])
            self.assertEqual(Store(root).load("stores/ark.json", {})["queue"], {})

    def test_persistent_outage_stops_at_run_budget_and_keeps_original_pending_work(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            def handle(*_): raise FetchError("http_503")
            client = Client(clock, handle)
            c = Collector(Path(tmp), "ark", CFG, "r1", client)
            c.enqueue({"type": "product", "url": "https://a.example/one", "created_at": OLD})
            result = c.collect(seconds=650)
            task = next(iter(result["queue"].values()))
            self.assertEqual([t for _, t in client.calls], [1000, 1300, 1600])
            self.assertEqual(task["created_at"], OLD)
            self.assertEqual(task["attempts"], 3)
            self.assertEqual(task["retry_at_epoch_seconds"], 1900)
            self.assertEqual(len(result["errors"]), 1)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["offers"], {})

    def test_permanent_denial_is_not_retried_in_the_same_run(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            def handle(*_): raise FetchError("http_403")
            client = Client(clock, handle)
            c = Collector(Path(tmp), "ark", CFG, "r1", client)
            c.enqueue({"type": "product", "url": "https://a.example/one"})
            result = c.collect(seconds=700)
            self.assertEqual(client.count, 1)
            self.assertEqual(len(result["queue"]), 1)
            self.assertEqual(clock.sleeps, [])

    def test_task_retry_deadline_survives_same_run_restart_without_host_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            def handle(*_): raise FetchError("http_500")
            root = Path(tmp)
            first = Collector(root, "ark", CFG, "r1", Client(clock, handle))
            first.enqueue({"type": "product", "url": "https://a.example/one", "created_at": OLD})
            first.collect(seconds=60)
            client = Client(clock, lambda *_: None)
            resumed = Collector(root, "ark", CFG, "r1", client)
            resumed.collect(seconds=60)
            self.assertEqual(client.count, 0)
            task = next(iter(resumed.state["queue"].values()))
            self.assertEqual((task["attempts"], task["created_at"]), (1, OLD))
            clock.now = 1299
            resumed.collect(seconds=60)
            self.assertEqual(client.calls, [("https://a.example/one", 1300)])
            self.assertEqual(resumed.state["queue"], {})

    def test_other_host_runs_during_server_wait_and_later_extension_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            def handle(client, url):
                if url == "https://b.example/one":
                    client.retry_after["a.example"] = 1800
            client = Client(clock, handle)
            client.retry_after["a.example"] = 1300
            c = Collector(Path(tmp), "ark", CFG, "r1", client)
            for host in ("a", "b"):
                c.enqueue({"type": "product", "url": f"https://{host}.example/one"})
            c.collect(seconds=900)
            self.assertEqual(client.calls, [("https://b.example/one", 1000), ("https://a.example/one", 1800)])


if __name__ == "__main__":
    unittest.main()
