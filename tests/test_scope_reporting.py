from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.comparison_integrity import (known_comparators, missing_comparators,
                                               out_of_scope_comparators)
from sale_monitor.models import STORES, Offer, iso
from sale_monitor.reporting import aggregate, historical_rows, validation
from sale_monitor.storage import Store, read_json


NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
RUN = "new"


def offer(store="ark", price=9000, **changes):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/one",
                seller_id=store, brand="Fixture", model="PART-1", condition="new",
                stock="in_stock", price_yen=price, shipping_yen=0, points_yen=0,
                observed_at=iso(NOW), observed_run_id=RUN, verified=True,
                evidence=[{"url": f"https://{store}.example/one"}])
    data.update(changes)
    return Offer(**data)


def scope():
    active = ["dospara", "ark", "koubou", "sofmap"]
    paused = {s: "paused_maintenance" if s == "tsukumo" else "paused_refused"
              for s in STORES if s not in active}
    routes = {s + ":direct": {"store": s, "channel": "direct",
        "functions": {"sale_discovery": paused.get(s, "active"),
                      "product_verify": paused.get(s, "active")},
        "reason": "fixture reason" if s in paused else None} for s in STORES}
    return {"version": 1, "in_scope": [s + ":direct" for s in active],
            "in_scope_stores": active, "paused_stores": paused, "routes": routes,
            "out_of_scope": {s + ":direct": {"status": {"sale_discovery": status},
                                               "reason": "fixture reason"}
                             for s, status in paused.items()}}


class ScopeReporting(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="scope-reporting-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.public = self.root / "public"
        self.disk = Store(self.root)
        self.scope = scope()

    def save(self, *offers, **fields):
        for item in offers:
            name = f"stores/{item.store}.json"
            state = self.disk.load(name, {"store": item.store, "run_id": item.observed_run_id,
                "status": "complete", "cycle_complete": True, "offers": {}, "queue": {},
                "journal": [], "errors": []})
            state["offers"][item.key] = item.to_dict()
            state.update(fields)
            self.disk.save(name, state)

    def candidates(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))

    def aggregate(self, scoped=True):
        return aggregate(self.root, self.public, RUN, NOW, self.scope if scoped else None)

    def decision(self, store="ark", product_id="one"):
        return next(d for d in read_json(self.public / "evidence.json", {})["decisions"]
                    if d["offer"]["store"] == store and d["offer"]["product_id"] == product_id)

    def test_prior_journal_low_blocks_B_only_in_scoped_mode(self):
        for scoped in (False, True):
            for journal_price in (None, 8000):
                with self.subTest(scoped=scoped, journal_price=journal_price):
                    root = self.root / f"{scoped}-{journal_price}"
                    disk = Store(root)
                    for item in (offer(price=10000), offer("dospara", 11000)):
                        disk.save(f"stores/{item.store}.json", {"store": item.store, "run_id": RUN,
                            "offers": {item.key: item.to_dict()}, "queue": {}, "journal": []})
                    old = offer(price=12000, observed_at=iso(NOW - timedelta(days=2)), observed_run_id="old")
                    disk.save("history/ark/old.json", [{"observation_id": "old", "run_id": "old", "offer": old.to_dict()}])
                    if journal_price:
                        low = offer(price=journal_price, observed_at=iso(NOW - timedelta(days=1)), observed_run_id="failed")
                        disk.save("stores/bic.json", {"journal": [{"observation_id": "pending", "run_id": "failed", "offer": low.to_dict()}]})
                    aggregate(root, root / "public", RUN, NOW, self.scope if scoped else None)
                    decision = next(r["payment"] for r in read_json(root / "public/evidence.json", {})["decisions"]
                                    if r["offer"]["store"] == "ark")
                    self.assertEqual(decision["status"] == "accepted", not (scoped and journal_price))

    def test_historical_rows_deduplicate_and_exclude_current_run_without_mutation(self):
        rows = [{"observation_id": "old", "run_id": "old"}, {"observation_id": "current", "run_id": RUN}]
        states = {"ark": {"journal": [rows[0], rows[1], {"observation_id": "pending", "run_id": "old"}]}}
        self.disk.save("history/ark/old.json", rows)
        original = deepcopy(states)
        self.assertEqual(historical_rows(self.disk, states, RUN), [rows[0], states["ark"]["journal"][-1]])
        self.assertEqual(states, original)
        self.assertEqual(self.disk.history(), rows)

    def test_paused_in_scope_comparator_holds_and_exposes_route_status(self):
        self.candidates()
        self.save(offer("sofmap", 8000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2))))
        self.scope["routes"]["sofmap:direct"]["functions"]["product_verify"] = "paused_maintenance"
        self.aggregate()
        decision = self.decision()
        self.assertEqual(decision["payment"]["reasons"], ["known_comparator_not_verified_this_run"])
        row = next(r for r in decision["comparison_scope"]["unconfirmed"] if r["store"] == "sofmap")
        self.assertEqual((row["category"], row["reason"]), ("in_scope_not_confirmed", "paused_maintenance"))

    def test_discovered_url_holds_until_removed_and_never_notifies(self):
        self.candidates()
        candidate = offer()
        urls = [{"store": "sofmap", "url": "https://sofmap.example/pending"}]
        self.disk.save("comparison_tracking.json", {candidate.identity: {"sofmap": {"unverified_urls": [urls[0]["url"]]}}})
        self.aggregate()
        decision = self.decision()
        for basis in ("payment", "points"):
            self.assertEqual(decision[basis]["status"], "insufficient")
            self.assertEqual(decision[basis]["provisional_rule"], "A")
            self.assertEqual(decision[basis]["reasons"], ["in_scope_comparator_url_not_verified"])
            self.assertEqual(decision[basis]["unverified_comparator_urls"], urls)
        row = next(r for r in decision["comparison_scope"]["unconfirmed"] if r.get("url") == urls[0]["url"])
        self.assertEqual((row["category"], row["reason"]), ("in_scope_not_confirmed", "discovered_url_not_verified"))
        self.assertFalse(read_json(self.public / "notifications.json", {})["events"])
        self.disk.save("comparison_tracking.json", {})
        self.aggregate()
        self.assertEqual(self.decision()["payment"]["status"], "accepted")
        self.assertTrue(read_json(self.public / "notifications.json", {})["events"])

    def test_unverified_urls_outside_scope_and_own_store_do_not_hold(self):
        self.candidates()
        self.disk.save("comparison_tracking.json", {offer().identity: {
            s: {"unverified_urls": ["https://example.test/pending"]} for s in ("ark", "tsukumo")}})
        self.aggregate()
        self.assertEqual(self.decision()["payment"]["status"], "accepted")

    def test_comparison_promotion_requires_same_product_for_events_and_reviews(self):
        for changes in ({"condition": "used"}, {"variant": "bundle"}, {"warranty": "other"}, {}):
            with self.subTest(changes=changes):
                root = self.root / str(len(list(self.root.iterdir())))
                disk = Store(root)
                offers = [offer(warranty="standard"), offer("sofmap", 10000, discovery_kind="comparison", **changes)]
                for item in offers:
                    disk.save(f"stores/{item.store}.json", {"store": item.store, "run_id": RUN,
                        "offers": {item.key: item.to_dict()}, "queue": {}})
                aggregate(root, root / "public", RUN, NOW, self.scope)
                reviews = read_json(root / "public/review_queue.json", {})["candidates"]
                self.assertEqual(any(r["store"] == "sofmap" for r in reviews), not changes)
                self.assertNotIn(offers[-1].key, disk.load("events/registry.json", {})["states"] if changes else {})

    def test_legacy_output_keys_remain_unchanged_on_repeated_aggregation(self):
        self.candidates()
        expected = {"schema_version", "generated_at", "run_id", "run_provenance", "mode",
                    "monitored_store_count", "excluded_stores", "complete_stores",
                    "collection_completion_rate", "stores", "notification_count", "review_count",
                    "comparison_planning", "files", "delivery_guarantee", "full_rescan_needed"}
        for _ in range(2):
            result = aggregate(self.root, self.public, RUN, NOW)
            self.assertEqual(set(result), expected)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(set(self.decision()), {"offer", "payment", "points"})
            report = read_json(self.public / "validation.json", {})
            self.assertNotIn("gate_model", report)
            self.assertNotIn("baseline", report)

    def test_out_of_scope_known_offer_does_not_hold_A_or_hide_provenance(self):
        self.candidates()
        old = offer("tsukumo", 8000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2)))
        self.save(old)
        original = deepcopy(self.scope)
        index = self.aggregate()
        decision = self.decision()
        self.assertEqual(decision["payment"]["status"], "accepted")
        self.assertEqual(decision["payment"]["rule"], "A")
        details = decision["comparison_scope"]
        self.assertEqual(details["label"], "比較範囲 v1")
        excluded = next(r for r in details["unconfirmed"] if r["store"] == "tsukumo")
        self.assertEqual(excluded, {"route": "tsukumo:direct", "store": "tsukumo",
            "category": "out_of_scope", "reason": "paused_maintenance",
            "known_offer_url": old.url, "last_verified_at": old.observed_at})
        self.assertEqual({r["store"] for r in details["confirmed"]}, {"dospara", "koubou"})
        for row in details["confirmed"]:
            self.assertEqual(row["channel"], "online")
            self.assertEqual(row["observed_at"], iso(NOW))
        self.assertEqual(len([r for r in details["unconfirmed"] if r["category"] == "out_of_scope"]), 6)
        self.assertIn({"route": "sofmap:direct", "store": "sofmap",
            "category": "in_scope_not_confirmed", "reason": "no_known_offer"}, details["unconfirmed"])
        event = read_json(self.public / "notifications.json", {})["events"][0]
        self.assertEqual(event["current_evidence"]["comparison_scope"], details)
        self.assertEqual(index["routes"], original["routes"])
        self.assertEqual(self.scope, original)

    def test_out_of_scope_known_offer_does_not_hold_B(self):
        self.save(offer(), offer("dospara", 10000))
        old = offer("tsukumo", 11000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=3)))
        self.save(old)
        self.disk.save("history/tsukumo/2026-11-08.json", [{"offer": old.to_dict(), "run_id": "old"}])
        self.aggregate()
        self.assertEqual(self.decision()["payment"]["rule"], "B_observed_year_low")

    def test_missing_in_scope_known_offer_still_holds(self):
        self.candidates()
        self.save(offer("sofmap", 8000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2))))
        self.aggregate()
        decision = self.decision()
        self.assertEqual(decision["payment"]["status"], "insufficient")
        self.assertIn("known_comparator_not_verified_this_run", decision["payment"]["reasons"])
        row = next(r for r in decision["comparison_scope"]["unconfirmed"] if r["store"] == "sofmap")
        self.assertEqual(row["reason"], "not_observed_this_run")

    def test_current_unknown_shipping_retains_all_missing_issues(self):
        self.candidates()
        old = offer("sofmap", 8000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2)))
        self.disk.save("history/sofmap/2026-11-09.json", [{"offer": old.to_dict(), "run_id": "old"}])
        self.save(offer("sofmap", 8000, shipping_yen=None))
        self.aggregate()
        decision = self.decision()
        self.assertEqual(decision["payment"]["status"], "insufficient")
        row = next(r for r in decision["comparison_scope"]["unconfirmed"] if r["store"] == "sofmap")
        self.assertIn("shipping_unknown", row["current_issues"])

    def test_cheaper_current_comparator_rejects_even_outside_scope(self):
        for name in ("sofmap", "tsukumo"):
            with self.subTest(store=name):
                self.candidates()
                self.save(offer(name, 8000))
                self.aggregate()
                decision = self.decision()
                self.assertEqual(decision["payment"]["status"], "rejected")
                self.assertEqual(decision["payment"]["reasons"], ["cheaper_current_offer"])
                self.assertIn(name, [r["store"] for r in decision["comparison_scope"]["confirmed"]])

    def test_paused_store_retains_real_pending_and_archive_counts(self):
        old = offer("tsukumo", observed_run_id="old", observed_at=iso(NOW - timedelta(days=2)))
        queue = {"pending": {"type": "product", "kind": "comparison", "created_at": iso(NOW - timedelta(days=3))}}
        self.save(old, queue=queue)
        self.disk.save("archive/tsukumo.json", {"entries": [{"id": "one"}, {"id": "two"}]})
        result = self.aggregate()
        status = result["stores"]["tsukumo"]
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["route_status"], "paused_maintenance")
        self.assertEqual(status["route_reason"], "fixture reason")
        self.assertEqual((status["pending_count"], status["pending_over_24h"], status["archived_count"]), (1, 1, 2))
        self.assertEqual(status["current_offers"], 0)
        self.assertEqual(result["stores"]["joshin"]["archived_count"], 0)
        self.assertEqual(result["monitored_store_count"], 10)
        self.assertEqual(result["active_store_count"], 4)
        self.assertEqual(result["route_counts"], {"active_direct_stores": 4, "paused_stores": 6})
        self.assertEqual(self.disk.load("stores/tsukumo.json", {})["queue"], queue)

    def test_comparison_discovery_promoted_only_with_current_sale_identity(self):
        for scoped in (False, True):
            with self.subTest(scoped=scoped):
                root = self.root / str(scoped)
                disk = Store(root)
                offers = [offer("ark", 10000), offer("koubou", 11000),
                    offer("sofmap", 9000, discovery_kind="comparison", discovery_url="https://sofmap.example/search")]
                for item in offers:
                    disk.save(f"stores/{item.store}.json", {"store": item.store, "run_id": RUN,
                        "status": "complete", "offers": {item.key: item.to_dict()}, "queue": {}})
                aggregate(root, root / "public", RUN, NOW, self.scope if scoped else None)
                events = disk.load("events/registry.json", {})["events"]
                promoted = [e for e in events.values() if e["offer_key"] == offers[-1].key]
                self.assertEqual(len(promoted), int(scoped))
                if promoted:
                    self.assertEqual(promoted[0]["offer"]["discovery_kind"], "comparison")
                    self.assertEqual(promoted[0]["offer"]["discovery_url"], offers[-1].discovery_url)

    def test_unrelated_or_old_sale_does_not_promote_comparison(self):
        self.save(offer(model="OTHER"), offer("ark", product_id="old", model="PART-1", seller_id="sofmap", observed_run_id="old"),
                  offer("sofmap", 9000, discovery_kind="comparison"),
                  offer("dospara", 10000, discovery_kind="comparison"),
                  offer("koubou", 11000, discovery_kind="comparison"))
        self.aggregate()
        self.assertEqual(self.decision("sofmap")["payment"]["status"], "accepted")
        self.assertFalse(self.disk.load("events/registry.json", {})["events"])
        reviews = read_json(self.public / "review_queue.json", {})["candidates"]
        self.assertFalse(any(r["discovery_kind"] == "comparison" for r in reviews))

    def test_promoted_rejected_comparison_appears_in_reviews(self):
        self.candidates()
        self.save(offer("sofmap", 20000, discovery_kind="comparison"))
        self.aggregate()
        reviews = read_json(self.public / "review_queue.json", {})["candidates"]
        self.assertTrue(any(r["store"] == "sofmap" for r in reviews))

    def test_legacy_plan_and_requests_never_rewritten(self):
        self.candidates()
        protected = ["comparison_plan.json", "requests/candidate_identities.json"] + [f"requests/{s}.json" for s in STORES]
        for name in protected:
            self.disk.save(name, {"sentinel": name})
        before = {name: ((self.root / name).read_bytes(), (self.root / name).stat().st_mtime_ns) for name in protected}
        with patch("sale_monitor.reporting.plan_comparisons", side_effect=AssertionError("legacy planner called")):
            index = self.aggregate()
        self.assertEqual(before, {name: ((self.root / name).read_bytes(), (self.root / name).stat().st_mtime_ns) for name in protected})
        expected = {"run_id": RUN, "status": "no_plan_for_run"}
        self.assertEqual(read_json(self.public / "comparison_plan.json", None), expected)
        self.assertEqual(index["comparison_planning"], expected)

    def test_current_plan_publishes_summary_and_status_counts(self):
        plan = {"generated_at": iso(NOW - timedelta(minutes=20)), "summary": {"reserved": 2},
                "candidates": [{"status": "reserved"}, {"status": "reserved"}, {"status": "deferred"}]}
        self.disk.save(f"plans/{RUN}.json", plan)
        index = self.aggregate()
        published = read_json(self.public / "comparison_plan.json", {})
        self.assertEqual(published["summary"], plan["summary"])
        self.assertEqual(index["comparison_planning"], plan["summary"])
        self.assertEqual(published["candidate_status_counts"], {"reserved": 2, "deferred": 1})
        self.assertEqual(published["generated_at"], plan["generated_at"])
        self.assertEqual(published["scope_version"], 1)
        self.assertEqual(self.disk.load(f"plans/{RUN}.json", None), plan)

    def test_confirmed_empty_list_is_success_without_observations(self):
        self.disk.save("stores/ark.json", {"store": "ark", "run_id": RUN, "status": "complete",
            "list_pages": 1, "listed_candidates": 0, "offers": {}, "errors": []})
        index = self.aggregate()
        outcome = index["run_outcome"]
        self.assertTrue(outcome["successful"])
        self.assertEqual(outcome["current_observations"], 0)
        self.assertEqual(outcome["confirmed_empty_results"], 1)
        self.assertEqual(index["active_complete_stores"], 1)
        self.assertEqual(index["collection_completion_rate"], .1)

    def test_no_current_store_results_is_unsuccessful(self):
        self.save(offer(observed_run_id="old"), list_pages=1, listed_candidates=0)
        outcome = self.aggregate()["run_outcome"]
        self.assertFalse(outcome["successful"])
        self.assertEqual(outcome["stores_with_current_results"], [])

    def test_old_observations_and_all_failed_lists_are_not_success(self):
        for status, errors, pages in (("failed", [{"reason": "http_503"}], 0),
                                      ("complete", [], 0), ("partial", [{"reason": "parse_failed"}], 1)):
            with self.subTest(status=status, errors=errors):
                self.save(offer(observed_run_id="old"), run_id=RUN, status=status,
                          errors=errors, list_pages=pages, listed_candidates=0)
                self.assertFalse(self.aggregate()["run_outcome"]["successful"])

    def test_paused_only_results_do_not_count_as_success(self):
        self.save(offer("tsukumo"), list_pages=1, listed_candidates=0)
        self.assertFalse(self.aggregate()["run_outcome"]["successful"])

    def test_failed_or_stale_observations_do_not_count_as_current(self):
        self.save(offer(issues=["latest_fetch_failed"]),
                  offer("dospara", observed_at=iso(NOW - timedelta(hours=13))))
        outcome = self.aggregate()["run_outcome"]
        self.assertFalse(outcome["successful"])
        self.assertEqual(outcome["current_observations"], 0)

    def test_deadline_or_failed_discovery_is_not_a_confirmed_empty_result(self):
        self.disk.save("stores/ark.json", {"store": "ark", "run_id": RUN, "status": "partial",
            "list_pages": 1, "listed_candidates": 0, "offers": {}, "errors": []})
        for result in ("deadline", "failed", "skipped"):
            with self.subTest(result=result):
                self.scope["run"] = {"stage_results": {"ark": {"discovery": result}}}
                outcome = self.aggregate()["run_outcome"]
                self.assertFalse(outcome["successful"])
                self.assertEqual(outcome["confirmed_empty_results"], 0)

    def test_confirmed_comparison_uses_payment_including_shipping_and_discount(self):
        self.save(offer(), offer("dospara", 10500, shipping_yen=500, discount_yen=1000),
                  offer("koubou", 11000))
        self.aggregate()
        confirmed = self.decision()["comparison_scope"]["confirmed"]
        self.assertEqual(next(r["price_yen"] for r in confirmed if r["store"] == "dospara"), 10000)

    def test_current_observations_are_counted_and_published_consistently(self):
        self.candidates()
        index = self.aggregate()
        self.assertTrue(index["run_outcome"]["successful"])
        self.assertEqual(index["run_outcome"]["current_observations"], 3)
        self.assertEqual(index["schema_version"], 2)
        self.assertEqual(read_json(self.public / "latest.json", None), index)
        self.assertEqual(self.disk.load(f"metrics/{RUN}.json", None), index)
        report = read_json(self.public / "validation.json", {})
        self.assertEqual(report["baseline"]["successful_runs"], 1)

    def test_scope_validation_blocks_cutover_and_reports_empty_baseline(self):
        report = validation(self.root, NOW, scope=self.scope)
        self.assertFalse(report["cutover_ready"])
        self.assertEqual(report["gate_model"], "scope_v1")
        self.assertEqual(report["comparison_scope_version"], 1)
        self.assertIn("service_level_undetermined", report["cutover_blockers"])
        self.assertIn("manual_false_positive_audit_pending", report["cutover_blockers"])
        self.assertEqual(report["baseline"]["runs"], 0)
        self.assertEqual(report["baseline"]["successful_runs"], 0)
        self.assertIsNone(report["baseline"]["latest_success_at"])

    def test_scope_validation_keeps_service_blocker_after_twenty_formal_audits(self):
        audit = {"reviewed_at": iso(NOW), "reviewer": "Fixture human", "reviewed_count": 20,
                 "needs_review_count": 0, "false_positive_count": 0,
                 "audits": [{"event_id": str(i), "offer_key": str(i), "source_run_id": RUN,
                     "reviewed_at": iso(NOW), "status": "passed", "findings": [],
                     "price_check": {"status": "supported", "rule": "A"},
                     "evidence": [{"store": "ark", "url": "https://ark.example/one",
                                   "observed_at": iso(NOW), "body_sha256": "a" * 64}]} for i in range(20)]}
        self.disk.save("validation/manual_review.json", audit)
        report = validation(self.root, NOW, self.scope)
        self.assertEqual(report["manual_review_verified"]["reviewed_count"], 20)
        self.assertFalse(report["cutover_ready"])
        self.assertEqual(report["cutover_blockers"], ["service_level_undetermined"])
        audit["audits"][0]["findings"] = [{"status": "open", "reason": "fixture finding"}]
        self.disk.save("validation/manual_review.json", audit)
        report = validation(self.root, NOW, self.scope)
        self.assertIn("manual_audit_unresolved_findings", report["cutover_blockers"])

    def test_baseline_filters_schema_and_time_and_uses_all_run_counts(self):
        for name, hours, version, success, count in (
                ("a", 12, 2, True, 2), ("b", 8, 2, False, 0), ("c", 4, 2, True, 6),
                ("legacy", 2, 1, True, 100), ("old", 169, 2, True, 100), ("future", -1, 2, True, 100)):
            self.disk.save(f"metrics/{name}.json", {"run_id": name, "generated_at": iso(NOW - timedelta(hours=hours)),
                "schema_version": version, "stores": {}, "run_outcome": {"successful": success, "current_observations": count}})
        baseline = validation(self.root, NOW, self.scope)["baseline"]
        self.assertEqual(baseline, {"runs": 3, "successful_runs": 2, "max_success_interval_hours": 8,
            "current_observations_total": 8, "current_observations_median": 2,
            "latest_success_at": iso(NOW - timedelta(hours=4))})

    def test_comparator_helpers_filter_scope_without_mutating_known(self):
        candidate, other = offer(), offer("tsukumo")
        known = known_comparators({}, [{"offer": other.to_dict()}], NOW)
        before = deepcopy(known)
        self.assertEqual(len(missing_comparators(candidate, known, {}, NOW)), 1)
        self.assertEqual(missing_comparators(candidate, known, {}, NOW, in_scope_stores=["ark"]), [])
        rows = out_of_scope_comparators(candidate, known, ["ark"])
        self.assertEqual(rows, [{"store": "tsukumo", "offer_key": other.key, "seller_id": "tsukumo",
                                 "url": other.url, "last_verified_at": other.observed_at}])
        self.assertEqual(known, before)


if __name__ == "__main__":
    unittest.main()
