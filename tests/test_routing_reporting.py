"""Offline routing publication regressions; temporary state is QAONLY."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor import reporting, routing_reporting
from sale_monitor.models import STORES, Offer, iso
from sale_monitor.storage import Store, atomic_json, read_json


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
RUN = "QAONLY-routing-report-current"
OLD_RUN = "QAONLY-routing-report-old"
REQUEST_REASONS = ("known_urls_first", "candidate_scope_unverified",
                   "compatible_known_url_missing")
RECEIPT_RESULTS = ("verified_current_product", "search_requested",
                   "search_requested_after_unverified_done_task")


def graph(run_id=RUN):
    return {"run_id": run_id, "requests": {}, "receipts": {}}


def state(name="ark", run_id=RUN):
    return {"store": name, "run_id": run_id, "status": "complete",
            "checkpoint_at": iso(NOW), "cycle_complete": True,
            "offers": {}, "journal": [], "queue": {},
            "comparison_routing": graph(run_id)}


class RoutingSummary(unittest.TestCase):
    def test_missing_null_and_empty_graph_are_not_recorded_not_zero(self):
        for source in ({}, {"comparison_routing": None}, {"comparison_routing": {}}):
            with self.subTest(source=source):
                result = routing_reporting.summary({"run_id": RUN, **source})
                self.assertEqual(result, {"status": "not_recorded", "run_id": None,
                    "request_count": None, "receipt_count": None,
                    "request_reasons": None, "receipt_results": None})

    def test_recorded_empty_containers_are_current_zero_counts(self):
        self.assertEqual(routing_reporting.summary(state()), {
            "status": "current", "run_id": RUN, "request_count": 0,
            "receipt_count": 0, "request_reasons": {}, "receipt_results": {}})

    def test_current_stale_and_unverified_run_identity(self):
        cases = [(RUN, RUN, "complete", "current"),
                 (RUN, OLD_RUN, "complete", "stale"),
                 (RUN, RUN, "job_missing", "stale")]
        for invalid_run in (None, "", 0, True, [], {}):
            cases.extend(((RUN, invalid_run, "complete", "unverified"),
                          (invalid_run, RUN, "complete", "unverified")))
        for state_run, graph_run, job_status, expected in cases:
            with self.subTest(state_run=state_run, graph_run=graph_run, job=job_status):
                source = {"run_id": state_run, "status": job_status,
                          "comparison_routing": graph(graph_run)}
                result = routing_reporting.summary(source)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["run_id"],
                    graph_run if isinstance(graph_run, str) and graph_run else None)
                self.assertEqual((result["request_count"], result["receipt_count"]), (0, 0))
        missing_source_run = {"comparison_routing": graph()}
        missing_graph_run = {"run_id": RUN, "comparison_routing": {
            "requests": {}, "receipts": {}}}
        for source in (missing_source_run, missing_graph_run):
            self.assertEqual(routing_reporting.summary(source)["status"], "unverified")

    def test_invalid_graph_and_containers_keep_unknown_counts_nullable(self):
        for malformed in ([], ["row"], "bad", 0, False):
            with self.subTest(graph=malformed):
                result = routing_reporting.summary({"run_id": RUN,
                    "comparison_routing": malformed})
                self.assertEqual(result["status"], "invalid")
                for field in ("run_id", "request_count", "receipt_count",
                              "request_reasons", "receipt_results"):
                    self.assertIsNone(result[field])
        for broken, healthy, field, count_key, tally_key in (
                ("requests", "receipts", "result", "request_count", "request_reasons"),
                ("receipts", "requests", "reason", "receipt_count", "receipt_results")):
            for malformed in (None, [], "bad", 7):
                with self.subTest(container=broken, value=malformed):
                    routing = {"run_id": RUN, broken: malformed,
                               healthy: {"kept": {field: "unknown"}}}
                    result = routing_reporting.summary({"run_id": RUN,
                        "comparison_routing": routing})
                    self.assertEqual(result["status"], "invalid")
                    self.assertEqual(result["run_id"], RUN)
                    self.assertIsNone(result[count_key])
                    self.assertIsNone(result[tally_key])
                    other_count = "receipt_count" if broken == "requests" else "request_count"
                    other_tally = "receipt_results" if broken == "requests" else "request_reasons"
                    self.assertEqual(result[other_count], 1)
                    self.assertEqual(result[other_tally], {"other_or_unrecognized": 1})
        result = routing_reporting.summary({"comparison_routing": {"run_id": RUN}})
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["request_count"])
        self.assertIsNone(result["receipt_count"])

    def test_fixed_buckets_count_every_unknown_and_malformed_row(self):
        source = state()
        for container, field, known in (("requests", "reason", REQUEST_REASONS),
                                        ("receipts", "result", RECEIPT_RESULTS)):
            records = {str(i): {field: value} for i, value in enumerate(known)}
            malformed = [{field: "future-value"}, {field: ""}, {field: None},
                         {field: [known[0]]}, {field: {"nested": known[0]}},
                         {field: True}, {}, None, [], "bad", 42]
            records.update({f"bad-{i}": row for i, row in enumerate(malformed)})
            source["comparison_routing"][container] = records
        before = deepcopy(source)
        result = routing_reporting.summary(source)
        self.assertEqual(result["status"], "current")
        for tally, total, known in (("request_reasons", "request_count", REQUEST_REASONS),
                                    ("receipt_results", "receipt_count", RECEIPT_RESULTS)):
            self.assertEqual(result[tally], {**dict.fromkeys(known, 1),
                                            "other_or_unrecognized": 11})
            self.assertEqual(result[total], 14)
            self.assertEqual(sum(result[tally].values()), result[total])
        self.assertEqual(source, before)
        # A consumer changing a returned tally must not change the stored graph.
        result["request_reasons"][REQUEST_REASONS[0]] = -1
        self.assertEqual(source, before)
        self.assertEqual(routing_reporting.summary(source)["request_reasons"][REQUEST_REASONS[0]], 1)

    def test_thousands_of_unique_rows_and_large_evidence_stay_bounded(self):
        source = state()
        payload = "QAONLY-EVIDENCE-" + "x" * 2048
        for container, field in (("requests", "reason"), ("receipts", "result")):
            source["comparison_routing"][container] = {
                f"row-{i}": {field: f"future-{i}", "evidence": [{"body": payload}]}
                for i in range(2500)}
        before = deepcopy(source)
        result = routing_reporting.summary(source)
        encoded = json.dumps(result)
        self.assertLess(len(encoded), 800)
        self.assertNotIn("QAONLY-EVIDENCE", encoded)
        self.assertNotIn("future-", encoded)
        self.assertEqual(result["request_count"], 2500)
        self.assertEqual(result["receipt_count"], 2500)
        self.assertEqual(result["request_reasons"], {"other_or_unrecognized": 2500})
        self.assertEqual(result["receipt_results"], {"other_or_unrecognized": 2500})
        self.assertLess(len(json.dumps(reporting.health(source, NOW))), 5000)
        self.assertEqual(source, before)

    def test_routing_tallies_do_not_become_http_or_completion_claims(self):
        source = state()
        source.update(status="partial", cycle_complete=False, request_count=2)
        source["comparison_routing"]["requests"] = {
            str(i): {"reason": "known_urls_first"} for i in range(7)}
        source["comparison_routing"]["receipts"] = {
            str(i): {"result": "verified_current_product"} for i in range(3)}
        result = reporting.health(source, NOW)
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["comparison_routing"]["request_count"], 7)
        self.assertEqual(result["comparison_routing"]["receipt_count"], 3)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["cycle_complete"])
        self.assertEqual((result["current_offers"], result["eligible_offers"]), (0, 0))
        source["status"] = "job_missing"
        result = reporting.health(source, NOW)
        self.assertIsNone(result["request_count"])
        self.assertEqual(result["comparison_routing"]["status"], "stale")
        self.assertEqual(result["comparison_routing"]["receipt_count"], 3)


class RoutingPublication(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="QAONLY-routing-reporting-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_detail_retains_exact_graphs_and_source_context_for_all_stores(self):
        states = {name: state(name) for name in STORES}
        states["ark"]["comparison_routing"].update(
            requests={"request-1": {"reason": "known_urls_first", "urls": ["QAONLY"]}},
            receipts={"receipt-1": {"result": "verified_current_product",
                "evidence": [{"text": "QAONLY-日本語証拠", "nested": [1, None, {}]}]}},
            future_field={"preserve": [False, "unknown"]})
        states["tsukumo"] = state("tsukumo", OLD_RUN)
        states["sofmap"]["comparison_routing"] = graph(OLD_RUN)
        states["bic"]["comparison_routing"] = None
        states["koubou"]["comparison_routing"] = {}
        states["dospara"]["status"] = "job_missing"
        states["joshin"]["comparison_routing"] = {"requests": None, "receipts": []}
        states["yahoo"].pop("comparison_routing")
        states.pop("amazon")
        before = deepcopy(states)
        disk = Store(self.root)
        for name, value in states.items():
            disk.save(f"stores/{name}.json", value)
        public = self.root / "public"
        index = reporting.aggregate(self.root, public, RUN, NOW)
        self.assertEqual(index["files"]["comparison_routing"], "comparison_routing.json")
        details = read_json(public / index["files"]["comparison_routing"], None)
        self.assertEqual(details["run_id"], RUN)
        self.assertEqual(details["generated_at"], iso(NOW))
        self.assertEqual(set(details["stores"]), set(STORES))
        for name in STORES:
            original = before.get(name, {})
            expected_status = ("job_missing" if original.get("run_id") != RUN
                               else original.get("status", "not_run"))
            with self.subTest(store=name):
                self.assertEqual(details["stores"][name], {
                    "run_id": original.get("run_id"),
                    "checkpoint_at": original.get("checkpoint_at"),
                    "status": expected_status,
                    "comparison_routing": original.get("comparison_routing")})
                self.assertEqual(disk.load(f"stores/{name}.json", {}), original)
        expected = {"ark": "current", "tsukumo": "stale", "sofmap": "stale",
                    "dospara": "stale", "bic": "not_recorded", "koubou": "not_recorded",
                    "joshin": "invalid", "yahoo": "not_recorded", "amazon": "not_recorded"}
        for name, status in expected.items():
            self.assertEqual(index["stores"][name]["comparison_routing"]["status"], status)
        # The store and graph agree with each other but belong to a prior aggregate.
        self.assertEqual(details["stores"]["tsukumo"]["comparison_routing"]["run_id"], OLD_RUN)
        self.assertEqual(index["stores"]["tsukumo"]["status"], "job_missing")
        self.assertIsNone(index["stores"]["amazon"]["comparison_routing"]["request_count"])
        self.assertEqual(read_json(public / "latest.json", None), index)
        self.assertEqual(disk.load(f"metrics/{RUN}.json", None), index)
        self.assertEqual(states, before)

    def test_detail_write_failure_preserves_previous_latest_and_metrics(self):
        public = self.root / "public"
        previous = {"run_id": OLD_RUN, "files": {"comparison_routing": "comparison_routing.json"}}
        atomic_json(public / "latest.json", previous)
        atomic_json(public / "comparison_routing.json", {"run_id": OLD_RUN, "stores": {}})
        Store(self.root).save(f"metrics/{OLD_RUN}.json", previous)
        protected = [public / "latest.json", public / "comparison_routing.json",
                     self.root / "metrics" / f"{OLD_RUN}.json"]
        before = {path: path.read_bytes() for path in protected}

        def fail_details(path, value):
            if path == public / "comparison_routing.json":
                raise OSError("QAONLY details publication failed")
            atomic_json(path, value)

        with patch("sale_monitor.reporting.atomic_json", side_effect=fail_details) as write:
            with self.assertRaisesRegex(OSError, "QAONLY details publication failed"):
                reporting.aggregate(self.root, public, RUN, NOW)
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertNotIn(public / "latest.json", [call.args[0] for call in write.call_args_list])
        self.assertFalse((self.root / "metrics" / f"{RUN}.json").exists())

    def test_summary_only_changes_routing_fields_not_decisions_state_or_gates(self):
        states = {name: state(name) for name in ("ark", "tsukumo", "sofmap", "koubou")}
        offers = []
        for name, price in (("ark", 9000), ("tsukumo", 10000), ("sofmap", 10000), ("koubou", None)):
            item = Offer(store=name, product_id="part", url=f"https://{name}.example/part",
                brand="QAONLY", model="UNPRICED" if name == "koubou" else "PART",
                seller_id=name, condition="new", stock="in_stock",
                price_yen=price, shipping_yen=0, points_yen=0, observed_at=iso(NOW),
                observed_run_id=RUN, verified=True, evidence=[{"url": f"https://{name}.example/part"}])
            offers.append(item)
            states[name]["offers"][item.key] = item.to_dict()
            states[name]["queue"] = {"old-task": {"type": "product", "kind": "comparison",
                "url": f"https://{name}.example/pending", "created_at": iso(NOW - timedelta(days=3)),
                "attempts": 8}}
            states[name]["comparison_routing"]["requests"] = {
                "request": {"reason": "known_urls_first", "evidence": ["QAONLY-original"]}}
        receipt = {"observation_id": "QAONLY-journal", "run_id": RUN, "offer": offers[0].to_dict()}
        states["ark"]["journal"] = [receipt]
        history = deepcopy(receipt)
        history.update(observation_id="QAONLY-historical", run_id=OLD_RUN)
        history["offer"].update(observed_at=iso(NOW - timedelta(days=2)), observed_run_id=OLD_RUN)
        original_health = reporting.health

        def raw_graph_health(source, now):
            return {**original_health(source, now),
                    "comparison_routing": source.get("comparison_routing", {})}

        snapshots = []
        for compact in (False, True):
            root = self.root / str(compact)
            disk = Store(root)
            for name, source in states.items():
                disk.save(f"stores/{name}.json", source)
            disk.save("history/ark/2026-10-05.json", [history])
            with patch("sale_monitor.reporting.health", side_effect=original_health if compact else raw_graph_health):
                index = reporting.aggregate(root, root / "public", RUN, NOW)
            validation = disk.load("public/validation.json", {})
            self.assertFalse(validation["cutover_ready"])
            self.assertIn("queue_over_24h", validation["reasons"])
            self.assertGreater(index["review_count"], 0)
            self.assertEqual(index["notification_count"], 1)
            notification = disk.load("public/notifications.json", {})["events"][0]
            self.assertEqual(notification["offer_key"], offers[0].key)
            self.assertEqual(notification["decision"]["rule"], "A")
            self.assertTrue(disk.load("public/evidence.json", {})["decisions"])
            for name, source in states.items():
                saved = disk.load(f"stores/{name}.json", {})
                self.assertEqual(saved["queue"], source["queue"])
                self.assertEqual(saved["comparison_routing"], source["comparison_routing"])
                self.assertEqual(saved["journal"], [])
            self.assertEqual(disk.load("history/ark/2026-10-05.json", []), [history])
            self.assertEqual(disk.load("history/ark/2026-10-07.json", []), [receipt])
            snapshot = {p.relative_to(root).as_posix(): read_json(p, None) for p in root.rglob("*.json")}
            for filename in ("public/latest.json", f"metrics/{RUN}.json"):
                for health in snapshot[filename]["stores"].values():
                    health.pop("comparison_routing")
            snapshots.append(snapshot)
        self.assertEqual(snapshots[0], snapshots[1])


if __name__ == "__main__":
    unittest.main()
