"""Offline contracts for optional comparison stores."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor import pipeline
from sale_monitor.models import iso
from sale_monitor.planner import plan, update_tracking
from sale_monitor.reporting import aggregate
from sale_monitor.routes import RouteState, load_config
from sale_monitor.storage import Store, atomic_json, read_json
from test_scope_reporting import NOW, RUN, offer, scope
from test_pipeline import Fixture, item, URLS, NOW as PIPELINE_NOW
from test_runner_phases import FakeClient


def optional_scope():
    result = scope()
    result.update(version=2, optional=["tsukumo:direct"], optional_stores=["tsukumo"],
                  optional_verifiable_stores=["tsukumo"])
    result["out_of_scope"].pop("tsukumo:direct")
    result["paused_stores"].pop("tsukumo")
    result["routes"]["tsukumo:direct"]["functions"]["product_verify"] = "active"
    return result


def build(candidates=None, peers=(), **updates):
    candidates = [offer()] if candidates is None else candidates
    known = {}
    for peer in peers:
        known.setdefault(peer.identity, []).append(peer)
    args = dict(candidates=candidates, current=candidates, known=known, history_by_identity={},
                scope_stores=["ark", "dospara", "koubou", "sofmap"], lookup_stores=["dospara"],
                budgets={s: 10 for s in ("ark", "dospara", "koubou", "sofmap", "tsukumo")},
                tracking={}, changes={}, now=NOW, run_id=RUN, scope_version=2,
                optional_stores=["tsukumo"], optional_verifiable_stores=["tsukumo"])
    args.update(updates)
    return plan(**args)


class OptionalConfigTests(unittest.TestCase):
    def test_validation_and_scope_membership(self):
        config = load_config("config/routes.json")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "routes.json"
            for optional, error in ((["unknown:direct"], "unknown_scope_route"),
                                    ([config["scope"]["in_scope"][0]], "overlapping_scope_route"),
                                    ("tsukumo:direct", "invalid_scope")):
                with self.subTest(optional=optional):
                    value = deepcopy(config)
                    value["scope"]["optional"] = optional
                    atomic_json(path, value)
                    with self.assertRaisesRegex(ValueError, error):
                        load_config(path)
            config["scope"]["optional"] = ["tsukumo:direct"]
            atomic_json(path, config)
            loaded = load_config(path)
            routes = RouteState(Store(Path(temp) / "state"), loaded, NOW)
            result = routes.scope()
            self.assertEqual(result["optional"], ["tsukumo:direct"])
            self.assertEqual(result["optional_stores"], ["tsukumo"])
            self.assertEqual(result["optional_verifiable_stores"], [])
            self.assertNotIn("tsukumo:direct", result["out_of_scope"])
            routes.routes["tsukumo:direct"]["functions"]["product_verify"]["status"] = "active"
            self.assertEqual(routes.scope()["optional_verifiable_stores"], ["tsukumo"])


class OptionalPlannerTests(unittest.TestCase):
    def test_empty_and_absent_keep_legacy_plan_shape(self):
        baseline = build(optional_stores=None)
        self.assertEqual(baseline, build(optional_stores=[]))
        self.assertNotIn("optional", baseline["summary"])
        self.assertTrue(all("optional" not in row for row in baseline["candidates"]))
        self.assertNotIn("tsukumo", baseline["confirm"])

    def test_mandatory_plan_is_byte_independent_of_optional_inputs(self):
        candidates = [offer(product_id=str(i), price=9000 + i * 1000) for i in range(4)]
        mandatory = [offer("dospara", 10000), offer("koubou", 11000)]
        baseline = build(candidates, mandatory, optional_stores=[])
        tracking = {candidates[0].identity: {"tsukumo": {
            "unverified_urls": ["https://tsukumo.example/new"],
            "last_attempt_at": iso(NOW), "consecutive_failures": 5}}}
        optional = [offer("tsukumo", 1, observed_run_id="old")]
        result = build(candidates, mandatory + optional, tracking=tracking)
        result["confirm"].pop("tsukumo", None)
        result["summary"].pop("optional")
        for row in result["candidates"]:
            row.pop("optional")
        self.assertEqual(json.dumps(result), json.dumps(baseline))

    def test_ready_statuses_reserve_optional_urls(self):
        candidate, mandatory, optional = offer(), offer("dospara"), offer("tsukumo")
        for peers, current, status in (([mandatory, optional], [candidate], "planned"),
                ([mandatory, optional], [candidate, mandatory], "already_confirmed"),
                ([optional], [candidate], "no_known_comparator")):
            with self.subTest(status=status):
                result = build(peers=peers, current=current)
                row = result["candidates"][0]
                self.assertEqual(row["status"], status)
                self.assertEqual(row["optional"], [{"store": "tsukumo", "offer_key": optional.key,
                    "url": optional.url, "state": "to_verify"}])
                self.assertEqual(result["confirm"]["tsukumo"], [{"url": optional.url,
                    "offer_key": optional.key, "identity": candidate.identity, "for_candidates": [candidate.key]}])
                self.assertNotIn("tsukumo", result["explore"])

    def test_other_statuses_do_not_reserve(self):
        mandatory, optional = offer("dospara"), offer("tsukumo")
        for candidate, updates, status in ((offer(shipping_yen=None), {}, "candidate_ineligible"),
                (offer(), {"verifiable_stores": ["ark"]}, "comparator_unverifiable"),
                (offer(), {"tracking": {offer().identity: {"dospara": {
                    "last_attempt_at": iso(NOW), "consecutive_failures": 1}}}}, "comparator_backoff"),
                (offer(), {"budgets": {"dospara": 0, "tsukumo": 10}}, "skipped_budget")):
            with self.subTest(status=status):
                result = build([candidate], [mandatory, optional], **updates)
                self.assertEqual(result["candidates"][0]["status"], status)
                self.assertEqual(result["candidates"][0]["optional"][0]["state"], "mandatory_not_ready")
                self.assertFalse(result["confirm"].get("tsukumo"))

    def test_backoff_unverifiable_and_current_confirmation(self):
        peer = offer("tsukumo")
        tracking = {peer.identity: {"tsukumo": {"last_attempt_at": iso(NOW), "consecutive_failures": 1,
                    "unverified_urls": [peer.url]}}}
        for updates, state in (({}, "backoff"), ({"optional_verifiable_stores": []}, "unverifiable"),
                                ({"current": [offer(), peer]}, "confirmed_this_run")):
            with self.subTest(state=state):
                result = build(peers=[peer], tracking=tracking, **updates)
                self.assertEqual(result["candidates"][0]["optional"][0]["state"], state)
                self.assertFalse(result["confirm"].get("tsukumo"))

    def test_budget_uses_full_optional_allocation_and_requests_are_shared(self):
        first, second = offer(product_id="first"), offer(product_id="second")
        peer = offer("tsukumo")
        extra = peer.url + "/extra"
        result = build([first, second], [peer], budgets={"tsukumo": 1},
            tracking={first.identity: {"tsukumo": {"unverified_urls": [peer.url, extra, extra]}}})
        self.assertEqual(result["summary"]["optional"]["confirm_budget"], {"tsukumo": 1})
        self.assertEqual(result["summary"]["optional"]["reserved_requests"], {"tsukumo": 1})
        self.assertEqual(result["summary"]["optional"]["by_state"], {"skipped_budget": 2, "to_verify": 2})
        self.assertEqual(result["confirm"]["tsukumo"][0]["for_candidates"], sorted([first.key, second.key]))
        self.assertEqual([i["state"] for i in result["candidates"][0]["optional"]], ["to_verify", "skipped_budget"])
        result = build(peers=[peer], budgets={"tsukumo": 0})
        self.assertEqual(result["candidates"][0]["optional"][0]["state"], "skipped_budget")

    def test_optional_candidate_uses_mandatory_but_never_own_optional_store(self):
        candidate = offer("tsukumo")
        result = build([candidate], [offer("dospara"), offer("tsukumo", seller_id="other")],
            tracking={candidate.identity: {"tsukumo": {"unverified_urls": ["https://tsukumo.example/new"]}}})
        self.assertEqual(result["candidates"][0]["status"], "planned")
        self.assertEqual(result["candidates"][0]["optional"], [])
        self.assertTrue(result["confirm"]["dospara"])

    def test_optional_filters_and_no_exploration(self):
        peers = [offer("tsukumo", model="OTHER"), offer("tsukumo", condition="used"),
                 offer("tsukumo", seller_id="ark"), offer("tsukumo", channel="shop")]
        result = build(peers=peers, lookup_stores=["tsukumo"])
        self.assertEqual(result["candidates"][0]["optional"], [])
        self.assertFalse(result["confirm"].get("tsukumo"))
        self.assertNotIn("tsukumo", result["explore"])

    def test_priority_determines_optional_budget_winner(self):
        first, second = offer(model="FIRST", product_id="first"), offer(model="SECOND", product_id="second")
        peers = [offer("tsukumo", model=o.model, product_id=o.product_id,
                       url=f"https://tsukumo.example/{o.product_id}") for o in (first, second)]
        result = build([first, second], peers, budgets={"tsukumo": 1}, changes={second.key: "new"})
        self.assertEqual(result["confirm"]["tsukumo"][0]["for_candidates"], [second.key])
        self.assertEqual(result["candidates"][1]["optional"][0]["state"], "skipped_budget")

    def test_tracking_success_failure_not_reached_and_url_settlement(self):
        candidate, peer = offer(), offer("tsukumo")
        tracking = {candidate.identity: {"tsukumo": {"unverified_urls": [peer.url]}}}
        result = build(peers=[], tracking=tracking)
        before = deepcopy(tracking)
        unchanged = update_tracking(tracking, result, [candidate], {}, {}, NOW)
        self.assertNotIn("last_attempt_at", unchanged[candidate.identity]["tsukumo"])
        failed = update_tracking(tracking, result, [candidate], {"tsukumo": {peer.url}}, {}, NOW)
        self.assertEqual(failed[candidate.identity]["tsukumo"]["consecutive_failures"], 1)
        succeeded = update_tracking(tracking, result, [candidate, peer], {}, {}, NOW)
        entry = succeeded[candidate.identity]["tsukumo"]
        self.assertEqual(entry["last_attempt_at"], iso(NOW))
        self.assertEqual(entry["last_success_at"], iso(NOW))
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["unverified_urls"], [])
        self.assertEqual(entry["settled_urls"][0]["reason"], "became_known_comparator")
        ended = update_tracking(tracking, result, [candidate, offer("tsukumo", stock="out_of_stock")], {}, {}, NOW)
        self.assertEqual(ended[candidate.identity]["tsukumo"]["ended_urls"], {peer.url: RUN})
        self.assertEqual(tracking, before)

    def test_shared_url_updates_every_optional_identity(self):
        candidates = [offer(model="FIRST", product_id="first"), offer(model="SECOND", product_id="second")]
        url = "https://tsukumo.example/shared"
        tracking = {c.identity: {"tsukumo": {"unverified_urls": [url]}} for c in candidates}
        result = build(candidates, tracking=tracking)
        self.assertEqual(len(result["confirm"]["tsukumo"]), 1)
        failed = update_tracking(tracking, result, candidates, {"tsukumo": {url}}, {}, NOW)
        for candidate in candidates:
            self.assertEqual(failed[candidate.identity]["tsukumo"]["consecutive_failures"], 1)
            self.assertEqual(failed[candidate.identity]["tsukumo"]["last_attempt_at"], iso(NOW))


class OptionalReportingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="optional-scope-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.disk = Store(self.root)
        self.scope = optional_scope()

    def save(self, *offers, **updates):
        for other in offers:
            name = f"stores/{other.store}.json"
            state = self.disk.load(name, {"store": other.store, "offers": {}, "queue": {}, "journal": []})
            state.update(run_id=RUN, status="complete", **updates)
            state["offers"][other.key] = other.to_dict()
            self.disk.save(name, state)

    def run_report(self, configured=None):
        aggregate(self.root, self.root / "public", RUN, NOW, configured or self.scope)
        return next(d for d in read_json(self.root / "public/evidence.json", {})["decisions"]
                    if d["offer"]["store"] == "ark")

    def optional_rows(self, decision):
        return [r for r in decision["comparison_scope"]["unconfirmed"] if r["category"] == "optional_not_confirmed"]

    def planned(self, state="to_verify", peer=None):
        peer = peer or offer("tsukumo")
        result = build(peers=[peer])
        result["candidates"][0]["optional"][0]["state"] = state
        self.disk.save(f"plans/{RUN}.json", result)
        return peer

    def test_empty_and_absent_keep_all_public_json_decisions_and_events_identical(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        baseline = scope()
        snapshots = []
        for variant in (baseline, {**baseline, "optional": [], "optional_stores": [], "optional_verifiable_stores": []}):
            root = self.root / str(len(snapshots))
            disk = Store(root)
            for path in (self.root / "stores").glob("*.json"):
                disk.save("stores/" + path.name, read_json(path, {}))
            aggregate(root, root / "public", RUN, NOW, variant)
            snapshots.append(({p.name: p.read_bytes() for p in (root / "public").glob("*.json")},
                              disk.load("events/registry.json", {})))
        self.assertEqual(*snapshots)

    def test_satellite_absent_does_not_change_prices_or_event_generation(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000),
                  offer("tsukumo", 1, observed_run_id="old", observed_at=iso(NOW - timedelta(days=1))))
        baseline = scope()
        baseline["version"] = 2
        baseline["run"] = {"stage_results": {"tsukumo": {"discovery": {"status": "satellite_absent"}}}}
        self.scope["run"] = baseline["run"]
        decisions, registries = [], []
        for configured in (baseline, self.scope):
            self.disk.save("events/registry.json", {"states": {}, "events": {}})
            decisions.append(self.run_report(configured))
            registries.append(self.disk.load("events/registry.json", {}))
        for key in ("offer", "payment", "points"):
            self.assertEqual(decisions[0][key], decisions[1][key])
        self.assertEqual(*registries)
        self.assertEqual(decisions[1]["payment"]["status"], "accepted")
        self.assertEqual(self.optional_rows(decisions[1])[0]["reason"], "satellite_absent")

    def test_optional_current_price_rejects_or_establishes_A_and_B(self):
        for price, mandatory, history, status, rule in ((8000, True, False, "rejected", None),
                (11000, True, False, "accepted", "A"),
                (11000, False, True, "accepted", "B_observed_year_low")):
            with self.subTest(price=price, mandatory=mandatory):
                self.save(offer(), offer("tsukumo", price, discovery_kind="comparison"))
                if mandatory:
                    self.save(offer("dospara", 10000))
                else:
                    self.disk.save("stores/dospara.json", {})
                if history:
                    past = offer(price=11000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2)))
                    self.disk.save("history/ark/old.json", [{"offer": past.to_dict(), "run_id": "old"}])
                decision = self.run_report()
                self.assertEqual((decision["payment"]["status"], decision["payment"]["rule"]), (status, rule))
                self.assertEqual(next(r for r in decision["comparison_scope"]["confirmed"]
                                      if r["store"] == "tsukumo")["scope"], "optional")
                self.assertFalse(self.optional_rows(decision))

    def test_accepted_rejected_accepted_creates_new_qualified_event(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        self.assertEqual(self.run_report()["payment"]["status"], "accepted")
        self.save(offer("tsukumo", 8000, discovery_kind="comparison"))
        self.assertEqual(self.run_report()["payment"]["status"], "rejected")
        self.save(offer("tsukumo", 8000, discovery_kind="comparison", observed_run_id="old"))
        later = NOW + timedelta(minutes=1)
        aggregate(self.root, self.root / "public", RUN, later, self.scope)
        decision = next(d for d in read_json(self.root / "public/evidence.json", {})["decisions"] if d["offer"]["store"] == "ark")
        self.assertEqual(decision["payment"]["status"], "accepted")
        events = [e for e in self.disk.load("events/registry.json", {})["events"].values()
                  if e["offer"]["store"] == "ark" and e["kind"] == "qualified"]
        self.assertEqual(len(events), 2)
        self.assertEqual(max(e["occurred_at"] for e in events), iso(later))

    def test_all_stage_statuses_pass_through_and_discovery_takes_priority(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        self.planned("skipped_budget")
        for phase in ("discovery", "verification"):
            for reason in ("satellite_absent", "satellite_declined:refused", "satellite_deadline",
                    "satellite_rejected:hash", "relay_failed", "deadline", "failed", "skipped"):
                with self.subTest(phase=phase, reason=reason):
                    stages = {"discovery": {"status": "completed"}, "verification": {"status": "failed"}}
                    stages[phase] = {"status": reason}
                    self.scope["run"] = {"stage_results": {"tsukumo": stages}}
                    decision = self.run_report()
                    self.assertEqual(self.optional_rows(decision)[0]["reason"], reason)
                    self.assertEqual(decision["payment"]["status"], "accepted")

    def test_plan_reasons_fetch_failure_and_not_reached_never_hold(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        for state, expected in (("mandatory_not_ready", "mandatory_not_ready"), ("backoff", "backoff"),
                                ("skipped_budget", "skipped_budget"), ("to_verify", "not_reached")):
            with self.subTest(state=state):
                self.planned(state)
                decision = self.run_report()
                self.assertEqual(self.optional_rows(decision)[0]["reason"], expected)
                self.assertEqual(decision["payment"]["status"], "accepted")
        peer = self.planned()
        self.save(offer("tsukumo", observed_run_id="old"), verification={"run_id": RUN, "failed_urls": [peer.url]})
        decision = self.run_report()
        self.assertEqual(self.optional_rows(decision)[0]["reason"], "fetch_failed")
        self.assertEqual(decision["payment"]["status"], "accepted")

    def test_unverifiable_optional_uses_route_status(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        self.planned("unverifiable")
        self.scope["routes"]["tsukumo:direct"]["functions"]["product_verify"] = "paused_refused"
        self.assertEqual(self.optional_rows(self.run_report())[0]["reason"], "paused_refused")

    def test_unknown_optional_store_is_not_searched_without_url(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        self.scope["run"] = {"stage_results": {"tsukumo": {"discovery": {"status": "satellite_absent"}}}}
        row, = self.optional_rows(self.run_report())
        self.assertEqual(row["reason"], "not_searched")
        self.assertNotIn("url", row)
        self.assertIsNone(row["last_verified"])
        self.assertIs(row["used_in_comparison"], False)

    def test_partial_confirmation_reports_each_remaining_url(self):
        candidate = offer()
        peers = [offer("tsukumo", 11000, product_id=str(i), url=f"https://tsukumo.example/{i}") for i in range(3)]
        result = build(peers=peers)
        self.disk.save(f"plans/{RUN}.json", result)
        self.save(candidate, offer("dospara", 10000), peers[0])
        self.disk.save("comparison_tracking.json", {candidate.identity: {"tsukumo": {"unverified_urls": [p.url for p in peers]}}})
        rows = self.optional_rows(self.run_report())
        self.assertEqual({r["url"] for r in rows}, {p.url for p in peers[1:]})
        self.assertTrue(all(r["reason"] == "not_reached" for r in rows))

    def test_last_verified_excludes_wrong_product_basis_and_future(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        peer = self.planned()
        past = offer("tsukumo", 10000, shipping_yen=300, discount_yen=100,
                     points_yen=200, observed_run_id="old", observed_at=iso(NOW - timedelta(days=3)))
        invalid = [offer("tsukumo", 1, variant="other", observed_run_id="old", observed_at=iso(NOW - timedelta(days=1))),
                   offer("tsukumo", 1, observed_run_id="old", observed_at=iso(NOW + timedelta(days=1))),
                   offer("tsukumo", 1, shipping_yen=None, observed_run_id="old", observed_at=iso(NOW - timedelta(days=1)))]
        self.disk.save("history/tsukumo/old.json", [{"offer": o.to_dict(), "run_id": "old"} for o in [past, *invalid]])
        row, = self.optional_rows(self.run_report())
        self.assertEqual(row["url"], peer.url)
        self.assertEqual(row["last_verified"], {"observed_at": past.observed_at, "yen": 10200, "stock": "in_stock"})
        self.assertNotIn("newer_evidence", row)

    def test_newer_stock_and_failed_fetch_are_both_visible(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        peer = self.planned()
        old = offer("tsukumo", 5000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=3)))
        ended = offer("tsukumo", 5000, observed_run_id="ended", stock="out_of_stock", observed_at=iso(NOW - timedelta(days=2)))
        self.disk.save("history/tsukumo/old.json", [{"offer": o.to_dict(), "run_id": o.observed_run_id} for o in [old, ended]])
        self.save(ended, checkpoint_at=iso(NOW), verification={"run_id": RUN, "failed_urls": [peer.url]})
        row, = self.optional_rows(self.run_report())
        self.assertEqual(row["last_verified"]["yen"], 5000)
        self.assertEqual(row["newer_evidence"], [{"observed_at": ended.observed_at, "yen": 5000, "stock": "out_of_stock"},
                                               {"observed_at": iso(NOW), "reason": "fetch_failed"}])
        self.assertIs(row["used_in_comparison"], False)

    def test_past_failed_queue_evidence_is_preserved(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        peer = self.planned()
        past = offer("tsukumo", observed_run_id="old", observed_at=iso(NOW - timedelta(days=3)))
        self.save(past, queue={"failure": {"type": "product", "url": peer.url, "last_error": "http_503",
                  "last_attempt_at": iso(NOW - timedelta(days=1))}})
        row, = self.optional_rows(self.run_report())
        self.assertEqual(row["newer_evidence"], [{"observed_at": iso(NOW - timedelta(days=1)), "reason": "fetch_failed"}])

    def test_failed_snapshot_keeps_past_price_and_shows_failure(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        self.planned()
        past = offer("tsukumo", observed_run_id="old", observed_at=iso(NOW - timedelta(days=3)),
                     issues=["latest_fetch_failed"])
        self.save(past, checkpoint_at=iso(NOW - timedelta(days=1)))
        row, = self.optional_rows(self.run_report())
        self.assertEqual(row["last_verified"]["observed_at"], past.observed_at)
        self.assertEqual(row["newer_evidence"], [{"observed_at": iso(NOW - timedelta(days=1)), "reason": "fetch_failed"}])

    def test_new_observation_followed_by_failure_is_displayed_but_not_compared(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        self.planned()
        past = offer("tsukumo", 1000, observed_at=iso(NOW - timedelta(hours=1)), issues=["latest_fetch_failed"])
        self.save(past, checkpoint_at=iso(NOW), verification={"run_id": RUN, "failed_urls": [past.url]})
        decision = self.run_report()
        self.assertEqual(decision["payment"]["status"], "accepted")
        row, = self.optional_rows(decision)
        self.assertEqual(row["last_verified"]["yen"], 1000)
        self.assertEqual(row["newer_evidence"], [{"observed_at": iso(NOW), "reason": "fetch_failed"}])

    def test_reached_optional_product_with_invalid_conditions_reports_its_issue(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        peer = self.planned()
        self.save(offer("tsukumo", shipping_yen=None), verification={"run_id": RUN,
            "results": {"receipt": {"url": peer.url, "result": "ok", "observed_offer_key": peer.key}}})
        decision = self.run_report()
        self.assertEqual(self.optional_rows(decision)[0]["reason"], "shipping_unknown")
        self.assertEqual(decision["payment"]["status"], "accepted")

    def test_unknown_failure_timestamp_is_not_invented(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        peer = self.planned()
        self.save(offer("tsukumo", observed_run_id="old"), verification={"run_id": RUN, "failed_urls": [peer.url]})
        row, = self.optional_rows(self.run_report())
        self.assertEqual(row["newer_evidence"], [{"observed_at": None, "reason": "fetch_failed"}])

    def test_redirect_receipt_only_confirms_its_requested_url(self):
        candidate, peer = offer(), offer("tsukumo", 11000, url="https://tsukumo.example/redirected")
        requested = "https://tsukumo.example/requested"
        pending = "https://tsukumo.example/pending"
        self.disk.save("comparison_tracking.json", {candidate.identity: {"tsukumo": {"unverified_urls": [requested, pending]}}})
        self.save(candidate, offer("dospara", 10000))
        self.save(peer, verification={"run_id": RUN, "results": {"receipt": {
            "url": requested, "result": "ok", "observed_offer_key": peer.key}}})
        rows = self.optional_rows(self.run_report())
        self.assertEqual([row["url"] for row in rows], [pending])

    def test_points_basis_uses_its_own_last_verified_amount(self):
        candidate = offer(points_yen=2000)
        peer = offer("tsukumo")
        past = offer("tsukumo", 10000, points_yen=500, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2)))
        recent_unknown = offer("tsukumo", 11000, points_yen=None, observed_run_id="old", observed_at=iso(NOW - timedelta(days=1)))
        self.disk.save("history/tsukumo/old.json", [{"offer": o.to_dict(), "run_id": "old"} for o in [past, recent_unknown]])
        self.save(candidate, offer("dospara", 8500), offer("koubou", 8500))
        self.planned(peer=peer)
        decision = self.run_report()
        self.assertEqual(decision["payment"]["status"], "rejected")
        self.assertEqual(decision["points"]["status"], "accepted")
        row, = self.optional_rows(decision)
        self.assertEqual(row["last_verified"], {"observed_at": past.observed_at, "yen": 9500, "stock": "in_stock"})

    def test_scope_label_is_shared_by_decisions_notifications_and_index(self):
        self.save(offer(), offer("dospara", 10000), offer("koubou", 11000))
        decision = self.run_report()
        label = "比較範囲 v2（必須4店＋任意: ツクモ）"
        self.assertEqual(decision["comparison_scope"]["label"], label)
        index = read_json(self.root / "public/latest.json", {})
        self.assertEqual(index["comparison_scope"]["label"], label)
        self.assertEqual(index["comparison_scope"]["optional"], ["tsukumo:direct"])
        event = read_json(self.root / "public/notifications.json", {})["events"][0]
        self.assertEqual(event["current_evidence"]["comparison_scope"]["label"], label)


class OptionalPipelineTests(Fixture):
    def test_optional_only_confirmation_enters_verification_and_tracking(self):
        self.route_config["scope"]["optional"] = ["tsukumo:direct"]
        for route in self.route_config["routes"].values():
            route["functions"]["comparison_lookup"]["status"] = "paused_refused"
        self.route_config["routes"]["tsukumo:direct"]["functions"]["product_verify"]["status"] = "active"
        self.config["stores"]["tsukumo"]["seed_urls"] = [URLS["tsukumo"]]
        self.save_config()
        candidate, peer = item("ark", 9000), item("tsukumo", 10000)
        peer.observed_run_id = "old"
        self.save_offer(candidate, run_id=candidate.observed_run_id)
        self.save_offer(peer)
        stages = []

        def children(stores, phase, *args, **kwargs):
            stages.append((phase, list(stores)))
            if phase == "verification":
                peer.observed_run_id = candidate.observed_run_id
                self.save_offer(peer, run_id=peer.observed_run_id)
            return {s: {"status": "completed"} for s in stores}

        with patch.object(pipeline, "_run_children", side_effect=children):
            self.run_pipeline()
        self.assertNotIn("tsukumo", stages[0][1])
        self.assertEqual(stages[1], ("verification", ["tsukumo"]))
        result = self.disk.load(f"plans/{candidate.observed_run_id}.json", {})
        self.assertEqual(result["confirm"]["tsukumo"][0]["url"], peer.url)
        self.assertNotIn("tsukumo", result["explore"])
        self.assertEqual(self.disk.load("comparison_tracking.json", {})[candidate.identity]["tsukumo"]["last_success_at"], iso(PIPELINE_NOW))

    def test_existing_collector_consumes_optional_request_through_publication(self):
        self.route_config["scope"]["optional"] = ["tsukumo:direct"]
        for route in self.route_config["routes"].values():
            route["functions"]["comparison_lookup"]["status"] = "paused_refused"
        self.route_config["routes"]["tsukumo:direct"]["functions"]["product_verify"]["status"] = "active"
        self.config["stores"]["tsukumo"]["seed_urls"] = [URLS["tsukumo"]]
        self.save_config()
        candidate, peer = item("ark", 9000), item("tsukumo", 11000, observed_run_id="old")
        self.save_offer(candidate, run_id=candidate.observed_run_id)
        self.save_offer(peer)
        verified = item("tsukumo", 8000, discovery_kind="comparison")

        def children(stores, phase, seconds, root, run_id, config_path, routes_path, spawn, clock, plan_path=None, **kwargs):
            if phase == "verification":
                for store in stores:
                    pipeline.stage(root, store, phase, run_id, seconds, config_path, routes_path, plan_path)
            return {store: {"status": "completed"} for store in stores}

        with patch.object(pipeline, "_run_children", side_effect=children), patch.object(
                pipeline, "Client", side_effect=FakeClient), patch("sale_monitor.runner.parse_product", return_value=verified):
            self.run_pipeline()
        state = self.disk.load("stores/tsukumo.json", {})
        self.assertEqual(len(state["verification"]["results"]), 1)
        self.assertEqual(next(iter(state["verification"]["results"].values()))["result"], "ok")
        decision = next(d for d in read_json(self.public / "evidence.json", {})["decisions"] if d["offer"]["store"] == "ark")
        self.assertEqual(decision["payment"]["status"], "rejected")
        self.assertEqual(decision["comparison_scope"]["confirmed"][0]["scope"], "optional")


if __name__ == "__main__":
    unittest.main()
