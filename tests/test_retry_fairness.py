"""Offline regressions for retry fairness, using real collection and host waits."""
from collections import Counter, defaultdict
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
from http.client import RemoteDisconnected
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from sale_monitor.http import Client
from sale_monitor.models import Offer, UTC, iso
from sale_monitor.runner import Collector, task_order
from sale_monitor.scheduling import lane, select_task
from sale_monitor.storage import Store


NOW = datetime(2026, 10, 4, tzinfo=UTC)
OLD = iso(NOW - timedelta(days=3))
CFG = {"stores": {"tsukumo": {"adapter": "html", "seed_urls": []}}}
GROUPS = ("discovery", "sale", "comparison:requested", "comparison:other")
FIRST = "first_attempt_this_run"
RETRY = "retry_same_run"


class Clock:
    def __init__(self):
        self.now = NOW.timestamp()
        self.sleeps = []

    def sleep(self, seconds):
        if seconds <= 0:
            raise AssertionError("waits must advance time")
        self.sleeps.append(seconds)
        self.now += seconds

    def utcnow(self):
        return datetime.fromtimestamp(self.now, UTC)

    def install(self, stack):
        # runner and the real HTTP client share the time module.
        for name in ("time", "monotonic"):
            stack.enter_context(patch(f"sale_monitor.runner.time.{name}", side_effect=lambda: self.now))
        stack.enter_context(patch("sale_monitor.runner.time.sleep", side_effect=self.sleep))
        stack.enter_context(patch("sale_monitor.runner.utcnow", side_effect=self.utcnow))
        stack.enter_context(patch("sale_monitor.models.utcnow", side_effect=self.utcnow))


class OfflineResponse:
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __init__(self, url, transport):
        self.url = url
        self.transport = transport

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.transport.active -= 1

    def read(self):
        product = {"@type": "Product", "name": "Test part", "sku": self.url.rsplit("/", 1)[-1],
                   "offers": {"price": 8500, "priceCurrency": "JPY",
                              "availability": "https://schema.org/InStock"}}
        return ('<h1>Test part</h1><script type="application/ld+json">'
                + json.dumps(product) + '</script>').encode()


class OfflineTransport:
    """Replace only the socket boundary; Client retries/throttling stay real."""
    def __init__(self, clock, failure=lambda url, count: None):
        self.clock = clock
        self.failure = failure
        self.calls = []
        self.counts = Counter()
        self.active = 0
        self.max_active = 0
        self.threads = set()

    def open(self, request, timeout=None):
        url = request.full_url
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.threads.add(threading.get_ident())
        self.calls.append((url, self.clock.now))
        self.counts[url] += 1
        self.clock.now += 1  # A request consumes a nonzero amount of the run budget.
        error = self.failure(url, self.counts[url])
        if error is not None:
            self.active -= 1
            raise error
        return OfflineResponse(url, self)

    def client(self):
        client = Client(delay=2)
        client.opener = self
        return client


def task(group, label, **changes):
    row = {"type": "list" if group == "discovery" else "product",
           "kind": "comparison" if group.startswith("comparison:") else "sale",
           "url": f"https://shop.example/{label}", "created_at": OLD,
           "priority": 3, "attempts": 7,
           "source": "https://shop.example/original-sale"}
    if group.startswith("comparison:"):
        row["requested"] = group.endswith(":requested")
    row.update(changes)
    return label, row


class CollectorRetryFairness(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="retry-fairness-")))
        self.clock = Clock()
        self.clock.install(self.stack)

    def assert_serial_and_throttled(self, transport):
        self.assertEqual(transport.max_active, 1)
        self.assertEqual(transport.active, 0)
        self.assertEqual(transport.threads, {threading.get_ident()})
        by_host = defaultdict(list)
        for url, tick in transport.calls:
            by_host[urlsplit(url).hostname].append(tick)
        for ticks in by_host.values():
            self.assertTrue(all(b - a >= 2 for a, b in zip(ticks, ticks[1:])))

    def test_corrected_configuration_clears_only_its_error_on_same_run_resume(self):
        cfg = {"stores": {"yahoo": {"adapter": "yahoo", "queries": []}}}
        transport = OfflineTransport(self.clock)
        first = Collector(self.root, "yahoo", cfg, "r1", transport.client())
        original = task("sale", "denied", last_error="http_403",
                        last_attempt_run_id="r1")[1]
        first.enqueue(original)
        task_id = next(iter(first.state["queue"]))
        unresolved = {"task_id": task_id, "reason": "http_403", "url": original["url"]}
        first.state["errors"] = [unresolved]
        with patch.dict("os.environ", {"YAHOO_CLIENT_ID": ""}):
            first.collect(seconds=10)
            pending = deepcopy(first.state["queue"])
            resumed = Collector(self.root, "yahoo", cfg, "r1", transport.client())
            resumed.collect(seconds=10)
        self.assertEqual(resumed.state["errors"], [unresolved, {"reason": "YAHOO_CLIENT_ID_missing"}])
        with patch.dict("os.environ", {"YAHOO_CLIENT_ID": "test-placeholder"}):
            corrected = Collector(self.root, "yahoo", cfg, "r1", transport.client())
            corrected.collect(seconds=10)
        self.assertEqual(corrected.state["errors"], [unresolved])
        self.assertEqual(corrected.state["queue"], pending)
        self.assertEqual(transport.calls, [])
        self.assertEqual(Store(self.root).load("stores/yahoo.json", {})["errors"], [unresolved])

    def test_stuck_old_urls_leave_cooled_host_opportunities_for_healthy_backlog(self):
        stuck = {f"https://shop.example/stuck-{n}" for n in range(5)}
        transport = OfflineTransport(self.clock, lambda url, _: RemoteDisconnected() if url in stuck else None)
        collector = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
        for n in range(5):
            collector.enqueue(task("sale", f"stuck-{n}", priority=0)[1])
        for n in range(24):
            collector.enqueue(task("sale", f"healthy-{n}", created_at=iso(NOW), attempts=0)[1])
        before = deepcopy(collector.state["queue"])
        state = collector.collect(seconds=2100)

        healthy = {key for key, row in before.items() if row["url"] not in stuck}
        self.assertEqual(set(state["done"]), healthy, "old failures must not monopolize each host recovery")
        self.assertEqual(set(state["queue"]), set(before) - healthy)
        self.assertEqual({row["url"] for row in state["offers"].values()},
                         {before[key]["url"] for key in healthy})
        self.assertTrue(all(row["observed_run_id"] == "r1" for row in state["offers"].values()))
        # Each exhausted transport attempt has three socket calls, with a real
        # host cooldown after three distinct failing URLs.
        for key, row in state["queue"].items():
            self.assertGreaterEqual(row["attempts"] - before[key]["attempts"], 2)
            self.assertEqual(transport.counts[row["url"]], 3 * (row["attempts"] - before[key]["attempts"]))
            for field in ("created_at", "priority", "source", "url", "kind"):
                self.assertEqual(row[field], before[key][field])
            self.assertEqual(row["last_attempt_run_id"], "r1")
        metrics = state["scheduler"]["selected_by_attempt_kind"]
        self.assertEqual(metrics[FIRST], len(before))
        self.assertGreater(metrics[RETRY], 0)
        self.assertEqual(sum(metrics.values()), len(healthy) + sum(
            row["attempts"] - before[key]["attempts"] for key, row in state["queue"].items()))
        self.assertEqual(len(state["errors"]), len(stuck))
        self.assertEqual(state["status"], "partial")
        self.assertTrue(any(b - a >= 300 for (_, a), (_, b) in zip(transport.calls, transport.calls[1:])))
        self.assertTrue(all(0 < delay <= 30 for delay in self.clock.sleeps))
        self.assert_serial_and_throttled(transport)

    def test_retry_after_survives_restart_while_other_host_and_recovery_progress(self):
        blocked = "https://shop.example/limited"
        peer = "https://shop.example/healthy"
        other = "https://other.example/healthy"
        def failure(url, count):
            if url == blocked and count == 1:
                return HTTPError(url, 429, "wait", {"Retry-After": "900"}, None)
        transport = OfflineTransport(self.clock, failure)
        first = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
        for priority, url in enumerate((blocked, peer, other)):
            first.enqueue(task("sale", str(priority), url=url, priority=priority)[1])
        first.collect(seconds=60)
        self.assertEqual([url for url, _ in transport.calls], [blocked, other])
        until = first.client.retry_after["shop.example"]
        checkpoint = Store(self.root).load("stores/tsukumo.json", {})
        self.assertEqual(checkpoint["retry_after"]["shop.example"], until)
        self.assertEqual(len(checkpoint["queue"]), 2)

        resumed = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
        resumed.collect(seconds=60)
        self.assertEqual(len(transport.calls), 2, "neither bucket can bypass the saved Retry-After")
        self.clock.now = until - 1
        state = resumed.collect(seconds=30)
        self.assertEqual([url for url, _ in transport.calls], [blocked, other, peer, blocked])
        self.assertTrue(all(tick >= until for _, tick in transport.calls[2:]))
        self.assertEqual(state["queue"], {})
        self.assertEqual(state["errors"], [])
        self.assertEqual(state["retry_activity"]["recovered_tasks"], 1)
        self.assertEqual(state["scheduler"]["selected_by_attempt_kind"], {FIRST: 3, RETRY: 1})
        self.assert_serial_and_throttled(transport)

    def test_same_run_403_restart_keeps_error_metadata_and_history_next_run_can_retry(self):
        url = "https://shop.example/denied"
        transport = OfflineTransport(self.clock, lambda u, _: HTTPError(u, 403, "denied", {}, None))
        first = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
        original = task("sale", "denied", attempts=11, last_attempt_run_id="older-run",
                        discovery_origin={"created_at": OLD, "url": "https://shop.example/sale"})[1]
        first.enqueue(original)
        past = Offer("tsukumo", "denied", url, price_yen=9000, stock="in_stock",
                     observed_at=OLD, observed_run_id="older-run",
                     evidence=[{"url": url, "checked_at": OLD}], coupon={"remaining": 3})
        first.state["offers"][past.key] = past.to_dict()
        journal = [{"run_id": "older-run", "offer": past.to_dict(), "observation_id": "past"}]
        first.state["journal"] = deepcopy(journal)
        first.disk.save("history/2026/old.json", journal)
        history_path = self.root / "history/2026/old.json"
        history_bytes = history_path.read_bytes()
        state = first.collect(seconds=60)
        task_id, failed = next(iter(state["queue"].items()))
        errors = deepcopy(state["errors"])
        self.assertEqual(errors, [{"task_id": task_id, "reason": "http_403", "url": url}])
        self.assertEqual(failed["attempts"], 12)
        self.assertNotIn("retry_at_epoch_seconds", failed)
        for key, value in original.items():
            if key not in ("attempts", "last_attempt_run_id"):
                self.assertEqual(failed[key], value)

        resumed = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
        self.assertEqual(resumed.state["errors"], errors)
        resumed.collect(seconds=700)
        self.assertEqual(transport.counts[url], 1, "a same-run restart must not repeat a permanent denial")
        saved = Store(self.root).load("stores/tsukumo.json", {})
        self.assertEqual(saved["errors"], errors)
        self.assertEqual(saved["queue"][task_id], failed)
        self.assertEqual(saved["queue"][task_id]["last_attempt_run_id"], "r1")
        self.assertEqual(saved["journal"], journal)
        self.assertEqual(saved["offers"][past.key]["observed_run_id"], "older-run")
        self.assertEqual(saved["offers"][past.key]["price_yen"], 9000)
        self.assertEqual(history_path.read_bytes(), history_bytes)

        recovery = OfflineTransport(self.clock)
        next_run = Collector(self.root, "tsukumo", CFG, "r2", recovery.client())
        self.assertEqual(next_run.state["errors"], [])
        recovered = next_run.collect(seconds=60)
        self.assertEqual(recovery.counts[url], 1)
        self.assertEqual(recovered["queue"], {})
        self.assertEqual(recovered["recovered_errors"][0]["attempts"], 12)
        self.assertEqual(recovered["scheduler"]["selected_by_attempt_kind"], {FIRST: 1})
        self.assertEqual(recovered["journal"][:1], journal)
        self.assertEqual(history_path.read_bytes(), history_bytes)

    def test_same_run_short_restarts_preserve_fresh_and_retry_opportunities(self):
        blocked = "https://shop.example/stuck"
        transport = OfflineTransport(self.clock, lambda url, _: HTTPError(
            url, 503, "wait", {"Retry-After": "300"}, None) if url == blocked else None)
        first = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
        first.enqueue(task("sale", "stuck", priority=0)[1])
        for n in range(8):
            first.enqueue(task("sale", f"healthy-{n}", created_at=iso(NOW), attempts=0)[1])
        first.collect(seconds=1)
        for _ in range(8):
            saved = Store(self.root).load("stores/tsukumo.json", {})
            deadlines = list(saved.get("retry_after", {}).values())
            self.clock.now = max([self.clock.now + 2, *deadlines])
            resumed = Collector(self.root, "tsukumo", CFG, "r1", transport.client())
            resumed.collect(seconds=1)
        selected = [RETRY if url == blocked else FIRST for url, _ in transport.calls[1:]]
        self.assertEqual(selected, [FIRST, FIRST, RETRY, FIRST, FIRST, FIRST, RETRY, FIRST])
        self.assertEqual(len(resumed.state["done"]), 6)
        self.assertEqual(resumed.state["scheduler"]["selected_by_attempt_kind"], {FIRST: 7, RETRY: 2})
        self.assertEqual(len(resumed.state["errors"]), 1)
        self.assert_serial_and_throttled(transport)


class RetryBucketScheduling(unittest.TestCase):
    def test_independent_group_cursors_prevent_phase_lock_and_survive_json_reload(self):
        pending = []
        attempted = set()
        for group in GROUPS:
            for n in range(40):
                pending.append(task(group, f"{group}-fresh-{n}", attempts=9,
                                    last_attempt_run_id="previous-run"))
            label, row = task(group, f"{group}-retry", priority=0,
                              created_at=iso(NOW - timedelta(days=5)))
            pending.append((label, row))
            attempted.add(label)
        original = deepcopy(dict(pending))
        scheduler = {}
        lanes, comparisons = [], []
        by_group = defaultdict(list)
        for _ in range(64):
            pair = select_task(pending, scheduler, task_order, NOW, attempted)
            self.assertIsNotNone(pair)
            key, row = pair
            group = lane(row)
            lanes.append(group)
            if group == "comparison":
                comparisons.append(row["requested"])
                group += ":requested" if row["requested"] else ":other"
            by_group[group].append(RETRY if key in attempted else FIRST)
            if key not in attempted:
                pending.remove(pair)
            self.assertEqual(row, original[key], "selection must not rewrite task metadata")
            scheduler = json.loads(json.dumps(scheduler))

        self.assertEqual(lanes, ["discovery", "sale", "comparison", "sale"] * 16)
        self.assertEqual(comparisons, [True, True, True, False] * 4)
        self.assertEqual({group: len(items) for group, items in by_group.items()},
                         {"discovery": 16, "sale": 32, "comparison:requested": 12, "comparison:other": 4})
        for group, items in by_group.items():
            with self.subTest(group=group):
                self.assertEqual(items, [FIRST, FIRST, FIRST, RETRY] * (len(items) // 4))
        self.assertEqual(scheduler["selected_by_attempt_kind"], {FIRST: 48, RETRY: 16})
        self.assertEqual(attempted, {f"{group}-retry" for group in GROUPS})
        self.assertTrue(all(row == original[key] for key, row in pending))

    def test_empty_bucket_fallback_keeps_overdue_priority_and_age_order(self):
        ranks = [("overdue-low", 3, 6 * 24), ("urgent-new", 0, 1),
                 ("overdue-high-new", 1, 2 * 24), ("urgent-old", 0, 2),
                 ("overdue-high-old", 1, 4 * 24)]
        expected = ["overdue-high-old", "overdue-high-new", "overdue-low", "urgent-old", "urgent-new"]
        for group in GROUPS:
            for retry_only in (False, True):
                with self.subTest(group=group, retry_only=retry_only):
                    pending = [task(group, key, priority=priority,
                                    created_at=iso(NOW - timedelta(hours=hours)))
                               for key, priority, hours in ranks]
                    before = deepcopy(dict(pending))
                    attempted = set(before) if retry_only else set()
                    # Start with a slot whose preferred bucket is empty.
                    scheduler = {"retry_cursor_by_group": {group: 0 if retry_only else 3}}
                    selected = []
                    while pending:
                        pair = select_task(pending, scheduler, task_order, NOW, attempted)
                        selected.append(pair[0])
                        self.assertEqual(pair[1], before[pair[0]])
                        pending.remove(pair)
                    self.assertEqual(selected, expected)
                    self.assertIsNone(select_task([], scheduler, task_order, NOW, attempted))
                    self.assertEqual(scheduler["selected_by_attempt_kind"],
                                     {RETRY if retry_only else FIRST: len(expected)})


if __name__ == "__main__":
    unittest.main()
