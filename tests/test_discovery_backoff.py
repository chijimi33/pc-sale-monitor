"""Offline discovery backoff without deleting tasks or losing their history."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sale_monitor.http import FetchError, Page
from sale_monitor.models import Offer, iso
from sale_monitor.reporting import health
from sale_monitor.retrying import deferred_until
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
OLD = iso(NOW - timedelta(days=30))
URL = "https://www.pc-koubou.jp/products/detail.php?product_id=123"
LIST = "https://www.pc-koubou.jp/pc/sale.php"
CFG = {"stores": {"koubou": {"adapter": "html", "seed_urls": []}}, "flyer": {}}


def failed_task(**updates):
    return {"type": "product", "url": URL, "kind": "sale", "created_at": OLD,
            "attempts": 17, "last_error": "http_404", "last_attempt_at": iso(NOW - timedelta(hours=1)),
            "last_attempt_run_id": "old", **updates}


class DeferredUntilTests(unittest.TestCase):
    def test_exponential_wait_is_capped_at_seven_days(self):
        for attempts, hours in ((1, 4), (2, 8), (6, 128), (7, 168), (262, 168), (10 ** 100, 168)):
            with self.subTest(attempts=attempts):
                task = failed_task(attempts=attempts, last_attempt_at=iso(NOW))
                self.assertEqual(deferred_until(task), (NOW + timedelta(hours=hours)).timestamp())

    def test_ineligible_or_unparseable_history_is_not_deferred(self):
        cases = [
            {"attempts": 0}, {"attempts": -1}, {"last_error": None}, {"last_error": ""},
            {"last_attempt_at": None}, {"last_attempt_at": "invalid"},
            {"last_error": "http_503"}, {"last_error": "TimeoutError"},
            {"last_error": "access_retry_later:http_403"}, {"last_error": "transport_retry_later:TimeoutError"},
            {"last_error": "host_budget_exhausted"}, {"last_error": "robots_disallowed"},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                self.assertIsNone(deferred_until(failed_task(**updates)))
        self.assertIsNone(deferred_until({}))
        task = failed_task()
        del task["last_attempt_at"]
        self.assertIsNone(deferred_until(task))

    def test_page_restriction_is_deferred_and_offset_timestamps_are_supported(self):
        task = failed_task(attempts=1, last_error="page_access_restricted", last_attempt_at="2026-11-11T09:00:00+09:00")
        self.assertEqual(deferred_until(task), (NOW + timedelta(hours=4)).timestamp())


class DiscoveryBackoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="discovery-backoff-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.disk = Store(self.root)
        for name, kwargs in (
                ("sale_monitor.runner.time.time", {"return_value": NOW.timestamp()}),
                ("sale_monitor.runner.utcnow", {"return_value": NOW}),
                ("sale_monitor.models.utcnow", {"return_value": NOW}),
                ("sale_monitor.runner.collect_flyer", {"return_value": ([], {})})):
            patcher = patch(name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Mock(spec=["count", "get", "rendered"])
        self.client.count = 0
        self.client.get.return_value = Page(URL, b"product", iso(NOW))

    def collector(self, phase="discovery", run_id="run"):
        return Collector(self.root, "koubou", CFG, run_id, self.client, phase=phase)

    def test_deferred_tasks_are_preserved_and_cycle_completes_with_summary(self):
        collector = self.collector()
        first = collector.enqueue(failed_task())
        second = collector.enqueue(failed_task(url=URL + "4", attempts=1, last_error="page_access_restricted"))
        collector.state["queue"]["comparison"] = failed_task(kind="comparison")
        before = deepcopy(collector.state["queue"])
        with patch.object(collector, "process") as process:
            result = collector.collect(1)
        process.assert_not_called()
        self.assertEqual(result["queue"], before)
        self.assertNotIn(first, result["done"])
        self.assertNotIn(second, result["done"])
        self.assertTrue(result["cycle_complete"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["pending_count"], 3)
        self.assertEqual(result["waiting_dependencies"], [])
        self.assertEqual(result["deferred_failures"], {
            "run_id": "run", "count": 2, "by_reason": {"http_404": 1, "page_access_restricted": 1},
            "next_retry_at": iso(NOW + timedelta(hours=3))})
        self.assertEqual(self.disk.load("stores/koubou.json", {})["queue"], before)
        self.assertEqual(health(result, NOW)["deferred_failures"], result["deferred_failures"])

    def test_task_runs_at_deadline_or_after_expiry(self):
        for elapsed in (4, 5):
            with self.subTest(elapsed=elapsed):
                collector = self.collector(run_id="run" + str(elapsed))
                key = collector.enqueue(failed_task(url=URL + str(elapsed), attempts=1,
                                                    last_attempt_at=iso(NOW - timedelta(hours=elapsed))))
                with patch.object(collector, "process") as process:
                    result = collector.collect(1)
                process.assert_called_once()
                self.assertNotIn(key, result["queue"])
                self.assertIn(key, result["done"])
                self.assertEqual(result["deferred_failures"], {"run_id": collector.run_id, "count": 0,
                    "by_reason": {}, "next_retry_at": None})

    def test_due_work_runs_beside_deferred_failure(self):
        collector = self.collector()
        deferred = collector.enqueue(failed_task())
        due = collector.enqueue({"type": "product", "url": URL + "4", "kind": "sale"})
        with patch.object(collector, "process") as process:
            result = collector.collect(1)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[0]["url"], URL + "4")
        self.assertIn(deferred, result["queue"])
        self.assertIn(due, result["done"])
        self.assertEqual(result["deferred_failures"]["count"], 1)
        self.assertEqual(result["status"], "complete")

    def test_relisting_existing_task_bypasses_wait_without_resetting_history(self):
        collector = self.collector()
        key = collector.enqueue(failed_task())
        before = deepcopy(collector.state["queue"][key])
        self.assertEqual(collector.enqueue({"type": "product", "url": URL, "kind": "sale"}, relisted=True), key)
        self.assertEqual(collector.state["queue"][key], {**before, "relisted_run_id": "run"})
        with patch.object(collector, "process") as process:
            result = collector.collect(1)
        process.assert_called_once()
        self.assertIn(key, result["done"])
        self.assertNotIn(key, result["queue"])
        self.assertEqual(result["deferred_failures"]["count"], 0)

    def test_actual_discovery_listing_relists_product_and_runs_it(self):
        collector = self.collector()
        product_key = collector.enqueue(failed_task())
        collector.enqueue({"type": "list", "url": LIST, "sale_page": True})
        current = Offer("koubou", "123", URL, price_yen=4000, stock="in_stock", verified=True)
        with patch("sale_monitor.runner.discover", return_value=([{"url": URL, "kind": "sale"}], [], [])), patch(
                "sale_monitor.runner.parse_product", return_value=current) as parse:
            result = collector.collect(1)
        parse.assert_called_once()
        self.assertEqual([call.args[0] for call in self.client.get.call_args_list], [LIST, URL])
        self.assertIn(product_key, result["done"])
        self.assertEqual(result["queue"], {})
        self.assertEqual(result["offers"][current.key]["observed_run_id"], "run")

    def test_known_offer_seed_does_not_bypass_wait(self):
        collector = self.collector()
        key = collector.enqueue(failed_task())
        old = Offer("koubou", "123", URL, discovery_kind="sale", price_yen=5000, observed_at=OLD)
        collector.state["offers"][old.key] = old.to_dict()
        before = deepcopy(collector.state["queue"][key])
        with patch.object(collector, "process") as process:
            result = collector.collect(1)
        process.assert_not_called()
        self.assertEqual(result["queue"][key], before)
        self.assertNotIn("relisted_run_id", result["queue"][key])
        self.assertEqual(result["offers"][old.key], old.to_dict())

    def test_prior_run_relisting_does_not_bypass_wait(self):
        collector = self.collector()
        key = collector.enqueue(failed_task(relisted_run_id="prior"))
        with patch.object(collector, "process") as process:
            result = collector.collect(1)
        process.assert_not_called()
        self.assertEqual(result["deferred_failures"]["count"], 1)
        self.assertEqual(result["queue"][key]["relisted_run_id"], "prior")

    def test_retained_task_is_retried_when_later_run_reaches_deadline(self):
        collector = self.collector()
        key = collector.enqueue(failed_task(attempts=1, last_attempt_at=iso(NOW)))
        collector.collect(1)
        resumed = self.collector(run_id="next")
        with patch("sale_monitor.runner.time.time", return_value=(NOW + timedelta(hours=4)).timestamp()), patch.object(resumed, "process") as process:
            result = resumed.collect(1)
        process.assert_called_once()
        self.assertIn(key, result["done"])
        self.assertNotIn(key, result["queue"])
        self.assertEqual(result["deferred_failures"]["count"], 0)

    def test_new_permanent_failure_is_deferred_without_becoming_stock_out(self):
        collector = self.collector()
        key = collector.enqueue({"type": "product", "url": URL, "kind": "sale", "created_at": OLD})
        old = Offer("koubou", "123", URL, stock="in_stock", price_yen=5000, observed_at=OLD)
        collector.state["offers"][old.key] = old.to_dict()
        with patch.object(collector, "process", side_effect=FetchError("http_404")) as process:
            result = collector.collect(600)
        process.assert_called_once()
        self.assertEqual(result["queue"][key]["attempts"], 1)
        self.assertEqual(result["queue"][key]["last_error"], "http_404")
        self.assertEqual(result["queue"][key]["created_at"], OLD)
        self.assertEqual(result["offers"][old.key]["stock"], "in_stock")
        self.assertEqual(result["offers"][old.key]["price_yen"], 5000)
        self.assertIn("latest_fetch_failed", result["offers"][old.key]["issues"])
        self.assertTrue(result["cycle_complete"])
        self.assertEqual(result["deferred_failures"]["next_retry_at"], iso(NOW + timedelta(hours=4)))

    def test_relisted_failure_stays_pending_and_does_not_retry_in_same_run(self):
        collector = self.collector()
        key = collector.enqueue(failed_task())
        collector.enqueue({"type": "product", "url": URL, "kind": "sale"}, relisted=True)
        with patch.object(collector, "process", side_effect=FetchError("http_404")) as process:
            result = collector.collect(600)
        process.assert_called_once()
        self.assertIn(key, result["queue"])
        self.assertEqual(result["queue"][key]["attempts"], 18)
        self.assertFalse(result["cycle_complete"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["deferred_failures"]["count"], 0)

    def test_legacy_and_verification_do_not_apply_discovery_wait(self):
        for phase in (None, "verification"):
            with self.subTest(phase=phase):
                collector = self.collector(phase=phase, run_id="run-" + str(phase))
                key = collector.enqueue(failed_task())
                with patch.object(collector, "process") as process:
                    result = collector.collect(1)
                process.assert_called_once()
                self.assertIn(key, result[collector.done_key])
                self.assertNotIn(key, result[collector.queue_key])
                self.assertNotIn("deferred_failures", result)

    def test_non_discovery_list_products_do_not_receive_relisted_marker(self):
        for phase in (None, "verification"):
            with self.subTest(phase=phase):
                collector = self.collector(phase=phase, run_id="run-" + str(phase))
                key = collector.enqueue(failed_task(kind="comparison"))
                collector.state.update(list_pages=0, listed_candidates=0)
                with patch("sale_monitor.runner.discover", return_value=([{"url": URL, "kind": "comparison"}], [], [])):
                    collector.process({"type": "list", "url": LIST, "kind": "comparison", "created_at": OLD})
                self.assertNotIn("relisted_run_id", collector.state[collector.queue_key][key])

    def test_interrupted_discovery_checkpoint_also_reports_deferred_work(self):
        collector = self.collector()
        key = collector.enqueue(failed_task())
        collector.state["access_block"] = {"run_id": "run", "reason": "amazon_access_challenge"}
        result = collector.collect(1)
        self.assertEqual(result["status"], "access_required")
        self.assertIn(key, result["queue"])
        self.assertEqual(result["deferred_failures"]["count"], 1)

    def test_health_preserves_legacy_key_set_and_conditionally_adds_summary(self):
        keys = {
            "status", "checkpoint_at", "run_id", "cycle_complete", "list_pages", "listed_candidates",
            "known_offers", "current_offers", "eligible_offers", "mandatory_field_coverage", "pending_count",
            "pending_by_type", "oldest_pending_at", "retry_after_epoch_seconds", "rate_limit_spacing_seconds",
            "transport_retry_after", "access_retry_after", "scheduler", "comparison_refresh", "comparison_routing",
            "waiting_dependencies", "access_block", "retry_activity", "comparison_search_pages", "request_count",
            "request_count_scope", "collection_deadline", "diagnostic_request_count", "http_evidence",
            "comparison_no_results", "pending_over_24h", "errors", "discovery_gaps", "source_metadata", "flyer",
        }
        baseline = health({}, NOW)
        self.assertEqual(set(baseline), keys)
        summary = {"run_id": "run", "count": 0, "by_reason": {}, "next_retry_at": None}
        result = health({"deferred_failures": summary}, NOW)
        self.assertEqual(set(result), keys | {"deferred_failures"})
        self.assertEqual(result.pop("deferred_failures"), summary)
        self.assertEqual(result, baseline)


if __name__ == "__main__":
    unittest.main()
