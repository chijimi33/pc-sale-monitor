from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.engine import YEAR_LOW_HOLD, evaluate, history_basis
from sale_monitor.models import JST, UTC, Offer, iso
from sale_monitor.planner import plan
from sale_monitor.reporting import aggregate
from sale_monitor.storage import Store, read_json

NOW = datetime(2026, 11, 13, tzinfo=UTC)
ANCHOR = NOW - timedelta(days=1)


def offer(price=18480, at=NOW, store="koubou", run="current", **changes):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/item/one",
                model="MODEL-A", brand="Brand", seller_id=store, condition="new",
                price_yen=price, shipping_yen=0, points_yen=0, stock="in_stock",
                observed_at=iso(at), observed_run_id=run, verified=True,
                evidence=[{"url": f"https://{store}.example/item/one", "checked_at": iso(at)}])
    data.update(changes)
    return Offer(**data)


def row(price, at, run="old", observation_id="old", **changes):
    return {"observation_id": observation_id, "run_id": run,
            "offer": offer(price, at, run=run, **changes).to_dict()}


def baseline():
    return [row(19790, NOW - timedelta(days=10), "base1", "base1"),
            row(19479, NOW - timedelta(days=2), "base2", "base2")]


def anchored(**changes):
    return baseline() + [row(18480, ANCHOR, "anchor", "anchor", **changes)]


class YearLowHeldTests(unittest.TestCase):
    def assert_held(self, result, anchor_id="anchor"):
        self.assertEqual((result["status"], result["rule"], result["reasons"]),
                         ("met", "B_observed_year_low", []))
        self.assertEqual(result["history"]["year_low_mode"], "held")
        self.assertEqual(result["history"]["anchor"]["observation_id"], anchor_id)

    def assert_not_held(self, result):
        self.assertNotIn("year_low_mode", result["history"])
        self.assertNotIn("anchor", result["history"])

    def test_new_keeps_complete_baseline_audit_and_takes_precedence(self):
        result = history_basis(offer(), baseline(), NOW)
        self.assertEqual(result, {"status": "met", "rule": "B_observed_year_low", "reasons": [],
            "history": {"minimum_yen": 19479, "from": iso(NOW - timedelta(days=10)),
                "to": iso(NOW - timedelta(days=2)), "samples": 2,
                "scope": "observed_period_only", "sources": [offer().url],
                "seller_history": {"seller_id": "koubou", "year": {"status": "present", "samples": 2,
                    "from": iso(NOW - timedelta(days=10)), "to": iso(NOW - timedelta(days=2))}},
                "year_low_mode": "new"}})
        # A further strict 500-yen update wins over an otherwise usable anchor.
        lower = history_basis(offer(17980), anchored(), NOW)
        self.assertEqual(lower["history"]["year_low_mode"], "new")
        self.assertEqual(lower["history"]["minimum_yen"], 18480)
        self.assertNotIn("anchor", lower["history"])

    def test_basic_held_audit_uses_anchor_baseline(self):
        rows = anchored()
        before = deepcopy(rows)
        result = history_basis(offer(), rows, NOW)
        self.assert_held(result)
        expected = history_basis(offer(), baseline(), NOW)["history"]
        expected.update(year_low_mode="held", observed_year_minimum_yen=18480,
                        anchor={"observation_id": "anchor", "run_id": "anchor", "observed_at": iso(ANCHOR),
                                "yen": 18480, "expires_at": iso(ANCHOR + YEAR_LOW_HOLD)})
        self.assertEqual(result["history"], expected)
        self.assertEqual(rows, before)

    def test_no_valid_anchor_preserves_complete_legacy_failure(self):
        for run in (None, ""):
            rows = [row(18480, ANCHOR, run)]
            if run is None:
                rows[0].pop("run_id")
            with self.subTest(run=run):
                self.assertEqual(history_basis(offer(), rows, NOW),
                                 {"status": "insufficient", "rule": None, "history": {},
                                  "reasons": ["B_history_insufficient"]})
        # A valid run ID alone cannot make a standing price a strict update.
        rows = [row(18480, NOW - timedelta(days=2)), row(18480, ANCHOR, "anchor")]
        self.assert_not_held(history_basis(offer(), rows, NOW))

    def test_same_run_earlier_own_observation_cannot_supply_seller_history(self):
        rows = [row(20000, NOW - timedelta(days=3), store="ark"),
                row(20000, ANCHOR - timedelta(hours=1), "anchor", "earlier"),
                row(19400, ANCHOR, "anchor", "later")]
        # Without the run exclusion the later observation would pass.
        self.assertEqual(history_basis(offer(19400, ANCHOR), rows[:2], ANCHOR)["status"], "met")
        self.assert_not_held(history_basis(offer(19400), rows, NOW))

    def test_same_run_other_seller_low_is_also_excluded_from_anchor_baseline(self):
        rows = anchored() + [row(18000, ANCHOR - timedelta(hours=1), "anchor", "same-run-low", store="ark")]
        # That low still belongs to Y_now and blocks 18480, but 18000 may be held.
        self.assert_not_held(history_basis(offer(), rows, NOW))
        self.assert_held(history_basis(offer(18000), rows, NOW))

    def test_anchor_product_conditions_keep_low_excluded_by_current_warranty(self):
        rows = [row(18000, NOW - timedelta(days=3), warranty="short"),
                row(20000, NOW - timedelta(days=2), warranty="long"),
                row(19000, ANCHOR, "anchor", "anchor", warranty=None)]
        result = history_basis(offer(19000, warranty="long"), rows, NOW)
        self.assert_not_held(result)
        self.assertEqual(result["status"], "insufficient")

    def test_same_price_small_drop_and_other_seller_follow_are_held(self):
        for price in (18480, 18280):
            for follower in ([], [row(18480, ANCHOR + timedelta(hours=1), "follow", store="ark")]):
                with self.subTest(price=price, follower=bool(follower)):
                    result = history_basis(offer(price), anchored() + follower, NOW)
                    self.assert_held(result)
                    self.assertEqual(result["history"]["anchor"]["yen"], 18480)

    def test_later_market_low_blocks_hold_even_if_current_below_anchor(self):
        rows = anchored() + [row(18280, ANCHOR + timedelta(hours=1), "other", store="ark")]
        self.assert_not_held(history_basis(offer(), rows, NOW))
        self.assert_held(history_basis(offer(18280), rows, NOW))

    def test_raise_fails_and_return_to_original_price_succeeds(self):
        rows = anchored()
        self.assert_not_held(history_basis(offer(18500), rows, NOW))
        rows += [row(18500, ANCHOR + timedelta(hours=1), "raised")]
        self.assert_held(history_basis(offer(), rows, NOW))

    def test_reobservations_and_small_drop_do_not_renew_deadline(self):
        rows = anchored()
        for day, price in ((1, 18480), (2, 18480), (3, 18280), (5, 18280), (7, 18280)):
            rows += [row(price, ANCHOR + timedelta(days=day), f"repeat{day}", f"repeat{day}")]
        now = ANCHOR + YEAR_LOW_HOLD + timedelta(microseconds=1)
        self.assert_not_held(history_basis(offer(18280, now), rows, now))
        # Removing only the seven-day age restriction would keep the original anchor.
        boundary = ANCHOR + YEAR_LOW_HOLD
        self.assert_held(history_basis(offer(18280, boundary), rows, boundary))

    def test_new_five_hundred_yen_update_can_be_a_fresh_anchor(self):
        at = ANCHOR + timedelta(days=6)
        rows = anchored() + [row(17980, at, "fresh", "fresh")]
        now = ANCHOR + timedelta(days=8)
        result = history_basis(offer(17980, now), rows, now)
        self.assert_held(result, "fresh")
        self.assertEqual(result["history"]["minimum_yen"], 18480)
        self.assertEqual(result["history"]["anchor"]["expires_at"], iso(at + YEAR_LOW_HOLD))

    def test_exact_seven_days_inclusive_then_one_microsecond_expires(self):
        for delta, held in ((YEAR_LOW_HOLD, True), (YEAR_LOW_HOLD + timedelta(microseconds=1), False)):
            now = ANCHOR + delta
            with self.subTest(delta=delta):
                result = history_basis(offer(at=now), anchored(), now)
                if held:
                    self.assert_held(result)
                    self.assertEqual(result["history"]["anchor"]["expires_at"], iso(now))
                else:
                    self.assert_not_held(result)

    def test_jst_year_boundary_and_earlier_expiry(self):
        at = datetime(2026, 12, 30, 0, tzinfo=JST)
        end = datetime(2027, 1, 1, 0, tzinfo=JST)
        rows = [row(20000, at - timedelta(days=1)), row(19000, at, "anchor", "anchor")]
        before = end - timedelta(microseconds=1)
        result = history_basis(offer(19000, before), rows, before)
        self.assert_held(result)
        self.assertEqual(result["history"]["anchor"]["expires_at"], iso(end))
        for now in (end, end + timedelta(hours=1)):
            with self.subTest(now=now):
                self.assert_not_held(history_basis(offer(19000, now), rows, now))

    def test_oldest_strict_anchor_then_observation_id_order(self):
        rows = [row(20000, NOW - timedelta(days=5)),
                row(19000, NOW - timedelta(days=3), "first", "z"),
                row(19000, NOW - timedelta(days=3), "first", "a"),
                row(18480, ANCHOR, "second", "second")]
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(reverse=ordered != rows):
                result = history_basis(offer(), ordered, NOW)
                self.assert_held(result, "a")
                self.assertEqual(result["history"]["anchor"]["yen"], 19000)
        rows[1].pop("observation_id")
        self.assert_held(history_basis(offer(), rows, NOW), "")

    def test_invalid_anchor_rows_are_never_usable(self):
        for changes in ({"verified": False}, {"shipping_yen": None}, {"model": "OTHER"},
                        {"url": "https://rakuten.co.jp/item"}, {"stock": "out_of_stock"},
                        {"expires_at": iso(ANCHOR)}, {"source_updated_at": iso(ANCHOR - timedelta(days=1))},
                        {"seller_id": "other"}):
            rows = anchored(**changes)
            with self.subTest(changes=changes):
                result = history_basis(offer(), rows, NOW)
                self.assertNotEqual(result["history"].get("year_low_mode"), "held")
        for at in (NOW, NOW + timedelta(seconds=1)):
            rows = baseline() + [row(18480, at, "future", "future")]
            self.assertEqual(history_basis(offer(), rows, NOW)["history"]["year_low_mode"], "new")

    def test_payment_and_points_strict_origins_are_separate(self):
        # Payment drops 1000; net price drops only 400, so points cannot inherit it.
        rows = [row(20000, NOW - timedelta(days=2), points_yen=600),
                row(19000, ANCHOR, "anchor", "anchor", points_yen=0)]
        self.assert_held(history_basis(offer(19000), rows, NOW))
        self.assert_not_held(history_basis(offer(19000), rows, NOW, points=True))
        # Only points change: payment has no strict origin, net price has one.
        rows = [row(20000, NOW - timedelta(days=2), points_yen=0),
                row(20000, ANCHOR, "anchor", "anchor", points_yen=1000)]
        current = offer(20000, points_yen=1000)
        self.assert_not_held(history_basis(current, rows, NOW))
        points = history_basis(current, rows, NOW, points=True)
        self.assert_held(points)
        self.assertEqual(points["history"]["anchor"]["yen"], 19000)
        self.assertEqual(points["history"]["observed_year_minimum_yen"], 19000)

    def test_unknown_historical_points_cannot_supply_points_origin(self):
        rows = [row(20000, NOW - timedelta(days=2), points_yen=None),
                row(19000, ANCHOR, "anchor", "anchor")]
        self.assert_held(history_basis(offer(19000), rows, NOW))
        self.assert_not_held(history_basis(offer(19000), rows, NOW, points=True))

    def test_late_own_record_can_recognize_anchor(self):
        rows = [row(20000, NOW - timedelta(days=2), store="ark"),
                row(19000, ANCHOR, "anchor", "anchor")]
        self.assert_not_held(history_basis(offer(19000), rows, NOW))
        rows += [row(20500, NOW - timedelta(days=3), "late", "late")]
        self.assert_held(history_basis(offer(19000), rows, NOW))

    def test_late_low_invalidates_anchor_even_when_current_matches_low(self):
        rows = anchored()
        self.assert_held(history_basis(offer(18000), rows, NOW))
        rows += [row(18000, ANCHOR - timedelta(hours=1), "late", "late", store="ark")]
        self.assert_not_held(history_basis(offer(18000), rows, NOW))

    def test_each_row_is_parsed_once_and_anchor_search_short_circuits(self):
        rows = [row(19790, NOW - timedelta(days=10), "base1", "base1"),
                row(19479, NOW - timedelta(days=8), "base2", "base2"),
                row(18480, ANCHOR, "anchor", "anchor")]
        rows += [row(18480, ANCHOR + timedelta(hours=i), f"repeat{i}", f"repeat{i}")
                 for i in range(1, 12)]
        from sale_monitor.engine import _year_low_new
        with patch.object(Offer, "from_dict", wraps=Offer.from_dict) as parse, \
                patch("sale_monitor.engine._year_low_new", wraps=_year_low_new) as strict:
            self.assert_held(history_basis(offer(), list(reversed(rows)), NOW))
        self.assertEqual(parse.call_count, len(rows))
        self.assertEqual(strict.call_count, 2)  # current, then oldest anchor only

    def test_anchor_search_only_runs_after_current_minimum_and_candidate_gate(self):
        from sale_monitor.engine import _year_low_new
        cases = [(offer(18500), anchored()),  # above Y_now
                 (offer(), [row(18480, ANCHOR, "other", store="ark")]),
                 (offer(), [row(18480, ANCHOR, None)]),
                 (offer(), [row(18480, NOW - timedelta(days=8), "too-old")])]
        for candidate, rows in cases:
            with self.subTest(rows=rows), patch("sale_monitor.engine._year_low_new", wraps=_year_low_new) as strict:
                self.assert_not_held(history_basis(candidate, rows, NOW))
                self.assertEqual(strict.call_count, 1)

    def test_current_comparison_guards_and_expired_rejection(self):
        rows = anchored()
        self.assert_held(history_basis(offer(), rows, NOW))
        missing = evaluate(offer(), [], rows, NOW)
        self.assertEqual(missing["status"], "insufficient")
        self.assertIn("B_current_comparison_missing", missing["reasons"])
        cheaper = evaluate(offer(), [offer(18479, store="ark")], rows, NOW)
        self.assertEqual((cheaper["status"], cheaper["reasons"]), ("rejected", ["cheaper_current_offer"]))
        # Give the unchanged median branch enough days/span to return not_met.
        rows += [row(19790, NOW - timedelta(days=40), "older", "older")]
        now = ANCHOR + YEAR_LOW_HOLD + timedelta(microseconds=1)
        peers = [offer(19000, now, store="ark"), offer(19500, now, store="sofmap")]
        expired = evaluate(offer(at=now), peers, rows, now)
        self.assertEqual(expired["status"], "rejected")
        self.assertEqual(expired["reasons"], ["A_price_gap_below_threshold", "B_history_gap_below_threshold"])

    def test_planner_follows_shared_held_result(self):
        candidate, peer = offer(), offer(19000, store="ark", run="old")
        result = plan([candidate], [candidate], {candidate.identity: [peer]},
                      {candidate.identity: anchored()}, ["koubou", "ark"], [], {"ark": 10}, {}, {}, NOW,
                      run_id="current", scope_version=1)
        planned, = result["candidates"]
        self.assertEqual(planned["history_status"], history_basis(candidate, anchored(), NOW)["status"])
        self.assertEqual((planned["status"], planned["priority"][0]), ("planned", 0))
        self.assertEqual(len(result["confirm"]["ark"]), 1)


class YearLowHeldReportingTests(unittest.TestCase):
    def test_hold_expiry_revokes_undelivered_event_still_inside_publication_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, public = Path(tmp) / "state", Path(tmp) / "public"
            disk = Store(root)
            disk.save("history/koubou/baseline.json", [
                row(19790, ANCHOR - timedelta(days=40), "base1", "base1"),
                row(19479, ANCHOR - timedelta(days=32), "base2", "base2"),
                row(18480, ANCHOR, "anchor", "anchor")])

            def run(at, run_id):
                for item in (offer(at=at, run=run_id), offer(19000, at, store="ark", run=run_id),
                             offer(19500, at, store="sofmap", run=run_id)):
                    disk.save(f"stores/{item.store}.json", {"run_id": run_id,
                        "offers": {item.key: item.to_dict()}, "queue": {}, "journal": []})
                with patch("sale_monitor.reporting.Store", return_value=disk), \
                        patch.object(disk, "save", wraps=disk.save) as save:
                    aggregate(root, public, run_id, at)
                registry = next(call.args[1] for call in save.call_args_list if call.args[0] == "events/registry.json")
                return read_json(public / "notifications.json", {})["events"], registry

            # First actionable event occurs one day after the underlying update.
            notices, _ = run(NOW, "first-held")
            notice, = notices
            self.assertEqual(notice["decision"]["history"]["year_low_mode"], "held")
            self.assertTrue(notice["currently_actionable"])
            now = ANCHOR + YEAR_LOW_HOLD + timedelta(microseconds=1)
            notices, registry = run(now, "expired")
            self.assertEqual(notices, [])
            self.assertLess(now - datetime.fromisoformat(notice["occurred_at"]), timedelta(days=7))
            retained, = registry["events"].values()
            self.assertEqual(retained["event_id"], notice["event_id"])
            self.assertEqual(retained["delivery_status"], "unconfirmed")
            self.assertFalse(retained["currently_actionable"])
            self.assertIsNone(retained["current_evidence"])
            registry = disk.load("events/registry.json", {})
            self.assertEqual([e["kind"] for e in registry["events"].values()], ["qualified"])

    def test_event_continuity_delivery_expiry_and_requalification(self):
        # Exercise both callers' history inputs and the real update_events via aggregate.
        for scoped in (False, True):
            with self.subTest(scoped=scoped), tempfile.TemporaryDirectory() as tmp:
                root, public = Path(tmp) / "state", Path(tmp) / "public"
                disk = Store(root)
                rows = [row(19790, ANCHOR - timedelta(days=40), "base1", "base1"),
                        row(19479, ANCHOR - timedelta(days=32), "base2", "base2")]
                disk.save("history/koubou/baseline.json", rows)
                scope = {"version": 1, "in_scope_stores": ["koubou", "ark", "sofmap"],
                         "in_scope": ["koubou:direct", "ark:direct", "sofmap:direct"],
                         "paused_stores": {}, "routes": {}, "out_of_scope": {}} if scoped else None

                def run(at, run_id, price=18480):
                    for item in (offer(price, at, run=run_id),
                                 offer(price + 520, at, store="ark", run=run_id),
                                 offer(price + 1020, at, store="sofmap", run=run_id)):
                        disk.save(f"stores/{item.store}.json", {"store": item.store, "run_id": run_id,
                            "offers": {item.key: item.to_dict()}, "queue": {}, "journal": [
                                {"observation_id": run_id + item.store, "run_id": run_id, "offer": item.to_dict()}]})
                    index = aggregate(root, public, run_id, at, scope=scope)
                    self.assertEqual(index["decision_criteria_version"], 3)
                    decision = next(d for d in read_json(public / "evidence.json", {})["decisions"]
                                    if d["offer"]["store"] == "koubou")
                    notices = [e for e in read_json(public / "notifications.json", {})["events"]
                               if e["offer"]["store"] == "koubou"]
                    return decision, notices

                first, notices = run(ANCHOR, "anchor")
                self.assertEqual(first["payment"]["history"]["year_low_mode"], "new")
                qualified, = notices
                self.assertEqual(qualified["kind"], "qualified")
                event_id = qualified["event_id"]
                held, notices = run(ANCHOR + timedelta(days=1), "held1")
                notice, = notices
                self.assertEqual(held["payment"]["history"]["year_low_mode"], "held")
                self.assertEqual(notice["event_id"], event_id)
                self.assertTrue(notice["currently_actionable"])
                self.assertEqual(notice["current_evidence"], held)
                self.assertEqual(notice["decision"]["history"]["year_low_mode"], "new")
                registry = disk.load("events/registry.json", {})
                own_events = [e for e in registry["events"].values() if e["offer"]["store"] == "koubou"]
                self.assertEqual(len(own_events), 1)
                registry["events"][event_id]["delivery_status"] = "delivered"
                disk.save("events/registry.json", registry)
                held, notices = run(ANCHOR + timedelta(days=2), "held2")
                self.assertEqual(held["payment"]["status"], "accepted")
                self.assertEqual(notices, [])
                expired, notices = run(ANCHOR + YEAR_LOW_HOLD + timedelta(microseconds=1), "expired")
                self.assertEqual(expired["payment"]["status"], "rejected")
                self.assertEqual(notices, [])
                registry = disk.load("events/registry.json", {})
                self.assertFalse(registry["states"][offer().key]["facts"]["accepted"])
                self.assertFalse(any(e["kind"] == "ended" for e in registry["events"].values()))
                renewed, notices = run(ANCHOR + timedelta(days=8), "renewed", 17980)
                notice, = notices
                self.assertEqual(renewed["payment"]["status"], "accepted")
                self.assertEqual((notice["kind"], notice["delivery_status"]), ("qualified", "unconfirmed"))
                self.assertNotEqual(notice["event_id"], event_id)
                self.assertEqual(disk.load("events/registry.json", {})["events"][event_id]["delivery_status"], "delivered")


if __name__ == "__main__":
    unittest.main()
