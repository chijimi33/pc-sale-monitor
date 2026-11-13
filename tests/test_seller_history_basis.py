from copy import deepcopy
from datetime import datetime, timedelta
from fractions import Fraction
from itertools import product
from pathlib import Path
from statistics import median
import tempfile
import unittest

from sale_monitor.engine import amount, evaluate, history_basis, update_events
from sale_monitor.models import JST, UTC, Offer, allowed_url, iso, same_product, timestamp
from sale_monitor.planner import plan
from sale_monitor.reporting import aggregate, historical_rows
from sale_monitor.storage import Store, read_json

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def offer(store="koubou", price=9000, **kwargs):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/item/one",
                model="MODEL-A", brand="Brand", seller_id=store, condition="new",
                price_yen=price, shipping_yen=0, points_yen=0, stock="in_stock",
                observed_at=iso(NOW), observed_run_id="run1", verified=True,
                evidence=[{"url": f"https://{store}.example/item/one", "checked_at": iso(NOW)}])
    data.update(kwargs)
    return Offer(**data)


def history(samples, **kwargs):
    return [{"offer": offer(price=price, observed_at=iso(NOW - timedelta(days=days)),
                            observed_run_id="old", **kwargs).to_dict()} for days, price in samples]


def old_history_basis(offer, history, now, points=False):
    """Frozen pre-v2 history rule, independent of the production implementation."""
    result = {"status": "insufficient", "rule": None, "history": {}, "reasons": []}
    errors = offer.errors(now)
    if errors:
        result["reasons"] = errors
        return result
    price = amount(offer, points)
    if price is None:
        result["reasons"] = ["points_unknown"]
        return result
    records = []
    for row in history:
        try:
            past = Offer.from_dict(row["offer"])
            checked = timestamp(past.observed_at)
            if checked is None or checked >= now or not same_product(offer, past) or past.errors(checked):
                continue
            value = amount(past, points)
            if value is not None and allowed_url(past.url):
                records.append((checked, value, past.url))
        except (KeyError, TypeError, ValueError):
            continue
    year = [r for r in records if r[0].astimezone(JST).year == now.astimezone(JST).year]
    if year:
        minimum = min(r[1] for r in year)
        if minimum - price >= 500:
            result.update(status="met", rule="B_observed_year_low", history={"minimum_yen": minimum, "from": iso(min(r[0] for r in year)), "to": iso(max(r[0] for r in year)), "samples": len(year), "scope": "observed_period_only", "sources": sorted(set(r[2] for r in year))})
            return result
    recent = [r for r in records if now - timedelta(days=90) <= r[0] < now]
    daily = {}
    for date, value, url in recent:
        key = date.astimezone(JST).date().isoformat()
        daily[key] = min(daily.get(key, value), value)
    if len(daily) >= 3 and (now - min(r[0] for r in recent)).days >= 30:
        baseline = Fraction(median([Fraction(v) for v in daily.values()]))
        delta = baseline - price
        result["history"] = {"median_yen": float(baseline), "minimum_yen": min(daily.values()), "from": min(daily), "to": max(daily), "days": len(daily), "samples": len(recent), "sources": sorted(set(r[2] for r in recent)), "scope": "observed_days_only"}
        if delta >= 500 and delta * 10 >= baseline and price <= min(daily.values()):
            result.update(status="met", rule="B_recent_median")
            return result
        result.update(status="not_met", reasons=["B_history_gap_below_threshold"])
    else:
        result["reasons"].append("B_history_insufficient")
    return result


class SellerHistoryBasisTests(unittest.TestCase):
    def test_standing_price_sparse_seller(self):
        own_days = [40, 34, 28, 22, 16, 8, 2]
        rows = history([(d, 10000) for d in own_days]) + history([(d, 13000) for d in range(1, 41)], store="ark")
        self.assertEqual(old_history_basis(offer(price=10000), rows, NOW)["status"], "met")
        result = history_basis(offer(price=10000), rows, NOW)
        self.assertEqual(result["status"], "not_met")
        self.assertEqual(result["reasons"], ["B_seller_history_gap_below_threshold"])
        audit = result["history"]["seller_history"]
        self.assertEqual(audit, {"seller_id": "koubou", "recent": {
            "status": "gap_below_threshold", "samples": 7, "days": 7,
            "from": iso(NOW - timedelta(days=40)), "to": iso(NOW - timedelta(days=2)),
            "market_samples_on_seller_days": 14, "market_median_on_seller_days_yen": 10000.0}})
        decision = evaluate(offer(price=10000), [offer("ark", 10500), offer("sofmap", 10500)], rows, NOW)
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(decision["reasons"], ["A_price_gap_below_threshold", "B_seller_history_gap_below_threshold"])

    def test_own_drop_below_ten_percent(self):
        rows = history([(d, 159700) for d in [40, 34, 28, 22, 16, 8, 2]])
        rows += history([(d, 211910) for d in range(1, 41)], store="ark")
        # An earlier year observation prevents the independent year-low branch.
        rows += history([(120, 148980)], store="ark")
        result = history_basis(offer(price=148980), rows, NOW)
        self.assertEqual(old_history_basis(offer(price=148980), rows, NOW)["status"], "met")
        self.assertEqual(result["status"], "not_met")
        self.assertEqual(result["reasons"], ["B_seller_history_gap_below_threshold"])
        self.assertEqual(result["history"]["seller_history"]["recent"]["market_median_on_seller_days_yen"], 159700.0)

    def test_invalid_own_observations_do_not_supply_days(self):
        rows = history([(40, 10000), (2, 9000)])
        rows += history([(35, 10000), (30, 10000)], shipping_yen=None)
        rows += history([(d, 12000) for d in range(1, 41)], store="ark")
        result = history_basis(offer(), rows, NOW)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["reasons"], ["B_seller_history_insufficient"])
        audit = result["history"]["seller_history"]["recent"]
        self.assertEqual((audit["status"], audit["samples"], audit["days"]), ("insufficient", 2, 2))
        self.assertEqual(audit["market_samples_on_seller_days"], 4)

    def test_year_low_with_own_history(self):
        rows = history([(10, 19790), (2, 19479)]) + history([(1, 19800)], store="ark")
        result = history_basis(offer(price=18480), rows, NOW)
        self.assertEqual((result["status"], result["rule"], result["reasons"]), ("met", "B_observed_year_low", []))
        self.assertEqual(result["history"], {"minimum_yen": 19479, "samples": 3,
            "from": iso(NOW - timedelta(days=10)), "to": iso(NOW - timedelta(days=1)),
            "scope": "observed_period_only", "sources": sorted([offer().url, offer("ark").url]), "year_low_mode": "new",
            "seller_history": {"seller_id": "koubou", "year": {"status": "present", "samples": 2,
                "from": iso(NOW - timedelta(days=10)), "to": iso(NOW - timedelta(days=2))}}})

    def test_year_low_without_own_history(self):
        result = history_basis(offer(), history([(1, 10000)], store="ark"), NOW)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["reasons"], ["B_seller_history_missing_in_period", "B_history_insufficient"])
        self.assertEqual(result["history"], {"seller_history": {"seller_id": "koubou", "year": {
            "status": "missing", "samples": 0, "from": None, "to": None}}})

    def test_missing_year_and_market_gap_combine_to_insufficient(self):
        rows = history([(40, 9500), (20, 9500), (1, 9500)], store="ark")
        basis = history_basis(offer(), rows, NOW)
        self.assertEqual(basis["status"], "insufficient")
        self.assertEqual(basis["reasons"], ["B_seller_history_missing_in_period", "B_history_gap_below_threshold"])
        self.assertEqual(basis["history"]["median_yen"], 9500.0)
        self.assertNotIn("recent", basis["history"]["seller_history"])
        decision = evaluate(offer(), [offer("ark", 9500), offer("sofmap", 9500)], rows, NOW)
        self.assertEqual(decision["status"], "insufficient")
        self.assertEqual(decision["reasons"], ["A_price_gap_below_threshold", *basis["reasons"]])

    def test_missing_year_and_recent_reasons_are_deduplicated(self):
        result = history_basis(offer(), history([(40, 10000), (20, 10000), (1, 10000)], store="ark"), NOW)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["reasons"], ["B_seller_history_missing_in_period"])
        self.assertEqual(result["history"]["seller_history"]["recent"], {
            "status": "missing", "samples": 0, "days": 0, "from": None, "to": None,
            "market_samples_on_seller_days": 0, "market_median_on_seller_days_yen": None})

    def test_genuine_median_b(self):
        rows = history([(40, 10000), (20, 10000), (1, 9000)])
        result = history_basis(offer(), rows, NOW)
        self.assertEqual((result["status"], result["rule"], result["reasons"]), ("met", "B_recent_median", []))
        self.assertEqual(result["history"]["seller_history"]["recent"], {
            "status": "met", "samples": 3, "days": 3,
            "from": iso(NOW - timedelta(days=40)), "to": iso(NOW - timedelta(days=1)),
            "market_samples_on_seller_days": 3, "market_median_on_seller_days_yen": 10000.0})

    def test_seller_span_counts_from_own_earliest_observation(self):
        for earliest, status in [(29, "insufficient"), (30, "met")]:
            with self.subTest(earliest=earliest):
                rows = history([(earliest, 10000), (20, 10000), (1, 9000)])
                rows += history([(d, 12000) for d in [60, 40, 10]], store="ark")
                result = history_basis(offer(), rows, NOW)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["reasons"], ["B_seller_history_insufficient"] if earliest == 29 else [])

    def test_ninety_day_window_includes_lower_boundary_only(self):
        for earliest, status, samples in [(90, "met", 3), (90 + 1 / 86400, "insufficient", 2)]:
            with self.subTest(earliest=earliest):
                rows = history([(earliest, 10000), (20, 10000), (1, 9000)])
                rows += history([(60, 12000), (40, 12000)], store="ark")
                result = history_basis(offer(), rows, NOW)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["history"]["seller_history"]["recent"]["samples"], samples)

    def test_jst_year_boundary_requires_own_observation_this_year(self):
        now = datetime(2026, 1, 2, 1, tzinfo=JST)
        candidate = offer(observed_at=iso(now))
        other = offer("ark", 10000, observed_at=iso(datetime(2026, 1, 1, 0, tzinfo=JST)))
        for own_time, status in [(datetime(2025, 12, 31, 23, 59, 59, tzinfo=JST), "insufficient"),
                                 (datetime(2026, 1, 1, 0, tzinfo=JST), "met")]:
            with self.subTest(own_time=own_time):
                rows = [{"offer": other.to_dict()}, {"offer": offer(price=10000, observed_at=iso(own_time)).to_dict()}]
                result = history_basis(candidate, rows, now)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["history"]["seller_history"]["year"]["samples"], int(status == "met"))

    def test_multiple_own_observations_on_one_jst_day_count_once(self):
        rows = history([(40, 10000), (1, 9000)])
        # Different UTC dates, same JST date (2026-08-05).
        rows += [{"offer": offer(price=10000, observed_at="2026-08-04T16:00:00+00:00").to_dict()}]
        rows += history([(60, 12000), (20, 12000), (10, 12000)], store="ark")
        result = history_basis(offer(), rows, NOW)
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["reasons"], ["B_seller_history_insufficient"])
        self.assertEqual(result["history"]["seller_history"]["recent"]["days"], 2)
        self.assertEqual(result["history"]["seller_history"]["recent"]["samples"], 3)

    def test_points_unknown_own_records_do_not_count(self):
        rows = history([(2, 10000)], points_yen=None) + history([(1, 10000)], store="ark")
        self.assertEqual(history_basis(offer(), rows, NOW)["status"], "met")
        points = history_basis(offer(), rows, NOW, points=True)
        self.assertEqual(points["status"], "insufficient")
        self.assertEqual(points["history"]["seller_history"]["year"]["samples"], 0)

    def test_seller_identity_is_seller_id_not_store(self):
        rows = history([(1, 10000)], store="ark", seller_id="koubou")
        self.assertEqual(history_basis(offer(), rows, NOW)["status"], "met")
        rows = history([(1, 10000)], seller_id="someone_else")
        self.assertEqual(history_basis(offer(), rows, NOW)["status"], "insufficient")

    def test_all_days_failure_does_not_evaluate_seller_condition(self):
        result = history_basis(offer(), history([(40, 9000), (20, 9000), (1, 9000)], store="ark"), NOW)
        self.assertEqual(result["status"], "not_met")
        self.assertNotIn("seller_history", result["history"])

    def test_even_seller_day_median_uses_fraction(self):
        rows = history([(40, 10000), (30, 10001), (20, 10001), (1, 9000)])
        result = history_basis(offer(), rows, NOW)
        self.assertEqual(result["rule"], "B_recent_median")
        self.assertEqual(result["history"]["seller_history"]["recent"]["market_median_on_seller_days_yen"], 10000.5)

    def test_seller_days_use_market_minimum_including_other_sellers(self):
        rows = history([(40, 11000), (20, 11000), (1, 9000)])
        rows += history([(40, 10000), (20, 10000), (1, 12000)], store="ark")
        result = history_basis(offer(), rows, NOW)
        self.assertEqual(result["rule"], "B_recent_median")
        self.assertEqual(result["history"]["seller_history"]["recent"]["market_median_on_seller_days_yen"], 10000.0)
        self.assertEqual(result["history"]["seller_history"]["recent"]["market_samples_on_seller_days"], 6)

    def test_seller_day_five_hundred_yen_boundary(self):
        for baseline, status in [(1499, "not_met"), (1500, "met")]:
            with self.subTest(baseline=baseline):
                rows = history([(40, baseline), (20, baseline), (1, 1000)])
                rows += history([(d, 2000) for d in range(1, 41)], store="ark")
                result = history_basis(offer(price=1000), rows, NOW)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["reasons"], ["B_seller_history_gap_below_threshold"] if baseline == 1499 else [])

    def test_recent_met_overrides_missing_year_and_clears_reasons(self):
        now = datetime(2026, 1, 2, 1, tzinfo=JST)
        candidate = offer(observed_at=iso(now))
        rows = [{"offer": offer(store, price, observed_at=iso(when)).to_dict()}
                for store, price, when in [
                    ("koubou", 10000, datetime(2025, 12, 1, tzinfo=JST)),
                    ("koubou", 10000, datetime(2025, 12, 10, tzinfo=JST)),
                    ("koubou", 9000, datetime(2025, 12, 31, tzinfo=JST)),
                    ("ark", 10000, datetime(2026, 1, 1, tzinfo=JST))]]
        result = history_basis(candidate, rows, now)
        self.assertEqual((result["status"], result["rule"], result["reasons"]), ("met", "B_recent_median", []))
        self.assertEqual(result["history"]["seller_history"]["year"]["status"], "missing")
        self.assertEqual(result["history"]["seller_history"]["recent"]["status"], "met")

    def test_new_rule_is_monotonic_against_old_rule(self):
        met_count = 0
        for own_count, span, own_price, other_price, suppress_year, points in product(
                range(4), [29, 30, 90, 91], [9000, 9500, 10000], [9000, 10000, 13000], [False, True], [False, True]):
            with self.subTest(own_count=own_count, span=span, own_price=own_price,
                              other_price=other_price, suppress_year=suppress_year, points=points):
                rows = history([(d, own_price) for d in [span, 20, 1][:own_count]])
                rows += history([(d, other_price) for d in [60, 40, 30, 20, 10, 1]], store="ark")
                if suppress_year:
                    rows += history([(120, 9000)], store="ark")
                if history_basis(offer(), rows, NOW, points)["status"] == "met":
                    met_count += 1
                    self.assertEqual(old_history_basis(offer(), rows, NOW, points)["status"], "met")
        self.assertGreater(met_count, 0)

    def test_current_run_is_excluded_from_history_including_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = Store(Path(tmp))
            own = {"observation_id": "own", "run_id": "run1", **history([(1, 10000)])[0]}
            other = {"observation_id": "other", "run_id": "old", **history([(2, 10000)], store="ark")[0]}
            disk.save("history/koubou/2026-09-13.json", [own, other])
            states = {"koubou": {"journal": [own, other]}}
            rows = historical_rows(disk, states, "run1")
            self.assertEqual(rows, [other])
            result = history_basis(offer(), rows, NOW)
            self.assertEqual(result["status"], "insufficient")
            self.assertEqual(result["history"]["seller_history"]["year"]["status"], "missing")

    def test_events_preserve_accepted_state_on_seller_history_insufficient(self):
        candidate = offer()
        peers = [offer("ark", 9500), offer("sofmap", 9500)]
        accepted = evaluate(candidate, peers, history([(1, 10000)]), NOW)
        self.assertEqual(accepted["status"], "accepted")
        events, state = update_events(candidate, accepted, None, NOW)
        self.assertEqual([e["kind"] for e in events], ["qualified"])
        before = deepcopy(state)
        issued = deepcopy(events)
        insufficient = evaluate(candidate, peers, history([(1, 10000)], store="ark"), NOW)
        self.assertEqual(insufficient["status"], "insufficient")
        events2, state2 = update_events(candidate, insufficient, state, NOW)
        self.assertEqual((events2, state2), ([], before))
        self.assertEqual(events, issued)
        events3, state3 = update_events(candidate, accepted, state2, NOW)
        self.assertEqual((events3, state3), ([], before))

    def test_events_requalify_after_rejected(self):
        candidate = offer()
        peers = [offer("ark", 9500), offer("sofmap", 9500)]
        accepted = evaluate(candidate, peers, history([(1, 10000)]), NOW)
        events, state = update_events(candidate, accepted, None, NOW)
        rejected = evaluate(candidate, peers, history([(40, 9000), (20, 9000), (1, 9000)]), NOW)
        self.assertEqual(rejected["status"], "rejected")
        events2, state = update_events(candidate, rejected, state, NOW)
        self.assertEqual(events2, [])
        events3, state = update_events(candidate, accepted, state, NOW)
        self.assertEqual([e["kind"] for e in events3], ["qualified"])
        self.assertNotEqual(events[0]["event_id"], events3[0]["event_id"])

    def test_evaluate_criteria_version_on_all_return_paths(self):
        for candidate, peers, rows, points in [
                (offer(shipping_yen=None), [], [], False), (offer(points_yen=None), [], [], True),
                (offer(), [offer("ark", 8999)], [], False),
                (offer(), [offer("ark", 10000), offer("sofmap", 10000)], [], False),
                (offer(), [], [], False), (offer(), [offer("ark", 9500)], history([(1, 10000)]), False),
                (offer(), [offer("ark", 9500)], [], False)]:
            with self.subTest(candidate=candidate, peers=peers, points=points):
                decision = evaluate(candidate, peers, rows, NOW, points)
                self.assertEqual(decision["criteria_version"], 3)
                keys = list(decision)
                self.assertEqual(keys[keys.index("basis") + 1], "criteria_version")

    def test_aggregate_version_and_rule_in_scoped_and_legacy_paths(self):
        for scoped in [False, True]:
            with self.subTest(scoped=scoped), tempfile.TemporaryDirectory() as tmp:
                root, public = Path(tmp) / "state", Path(tmp) / "public"
                disk = Store(root)
                for item in [offer(), offer("ark", 9500)]:
                    disk.save(f"stores/{item.store}.json", {"store": item.store, "run_id": "run1",
                        "offers": {item.key: item.to_dict()}, "queue": {}, "journal": []})
                disk.save("history/ark/2026-09-13.json", [{"run_id": "old", **history([(1, 10000)], store="ark")[0]}])
                scope = {"version": 1, "in_scope_stores": ["koubou", "ark"],
                         "in_scope": ["koubou:direct", "ark:direct"], "paused_stores": {}, "routes": {}, "out_of_scope": {}} if scoped else None
                index = aggregate(root, public, "run1", NOW, scope=scope)
                self.assertEqual(index["decision_criteria_version"], 3)
                self.assertEqual(read_json(public / "latest.json", {})["decision_criteria_version"], 3)
                decision = next(d["payment"] for d in read_json(public / "evidence.json", {})["decisions"]
                                if d["offer"]["seller_id"] == "koubou")
                self.assertEqual(decision["criteria_version"], 3)
                self.assertEqual(decision["status"], "insufficient")
                self.assertEqual(decision["history"]["seller_history"]["year"]["status"], "missing")

    def test_planner_keeps_seller_history_failures_after_met_candidate(self):
        candidates = [offer(model=m, product_id=m) for m in ["met", "missing", "gap"]]
        peers = [offer("ark", 10000, model=c.model, product_id=c.product_id,
                       url=f"https://ark.example/item/{c.product_id}") for c in candidates]
        histories = {
            candidates[0].identity: history([(1, 10000)], model="met"),
            candidates[1].identity: history([(1, 10000)], store="ark", model="missing"),
            candidates[2].identity: history([(d, 9000) for d in [40, 20, 1]], model="gap")
                + history([(d, 13000) for d in range(1, 41)], store="ark", model="gap")}
        result = plan(list(reversed(candidates)), candidates, {p.identity: [p] for p in peers}, histories,
                      ["ark", "koubou"], [], {"ark": 20, "koubou": 20}, {}, {}, NOW,
                      run_id="run1", scope_version=1)
        self.assertEqual([r["offer_key"] for r in result["candidates"]], [c.key for c in candidates])
        self.assertEqual([r["history_status"] for r in result["candidates"]], ["met", "insufficient", "not_met"])
        self.assertEqual([r["status"] for r in result["candidates"]], ["planned"] * 3)
        self.assertEqual(len(result["confirm"]["ark"]), 3)


if __name__ == "__main__":
    unittest.main()
