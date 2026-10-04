from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.comparison_integrity import known_comparators
from sale_monitor.engine import evaluate
from sale_monitor.models import STORES, Offer, iso, same_product
from sale_monitor.reporting import comparison_inputs_by_identity, aggregate
from sale_monitor.scheduling import plan_comparisons
from sale_monitor.storage import Store, read_json


NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def offer(store="ark", price=9000, **changes):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/item/one",
                brand="Brand", model="PART-1", seller_id=store, condition="new",
                stock="in_stock", price_yen=price, shipping_yen=0, points_yen=0,
                verified=True, observed_at=iso(NOW), observed_run_id="new",
                evidence=[{"url": f"https://{store}.example/receipt"}])
    data.update(changes)
    return Offer(**data)


def observation(o, days=1, run_id="old", observation_id="receipt"):
    data = o.to_dict()
    data["observed_at"] = iso(NOW - timedelta(days=days))
    return {"run_id": run_id, "observation_id": observation_id, "offer": data}


def full_list_inputs(current, historical):
    identities = {o.identity for o in current}
    return ({identity: current for identity in identities},
            {identity: historical for identity in identities})


class ReportingIndex(unittest.TestCase):
    def assert_decisions_equal(self, candidates, current, historical):
        original = deepcopy(([o.to_dict() for o in current], historical))
        peers, past = comparison_inputs_by_identity(current, historical)
        for candidate, points in product(candidates, (False, True)):
            with self.subTest(candidate=candidate.to_dict(), points=points):
                self.assertEqual(
                    evaluate(candidate, current, historical, NOW, points=points),
                    evaluate(candidate, peers.get(candidate.identity, []),
                             past.get(candidate.identity, []), NOW, points=points))
        self.assertEqual(([o.to_dict() for o in current], historical), original)

    def test_same_identity_conditions_variants_and_warranties_match_full_list(self):
        candidates = [offer(condition=condition, variant=variant, warranty=warranty)
                      for condition, variant, warranty in product(
                          ("new", "used"), (None, "1 pack", "2 pack"),
                          (None, "1 year", "2 years"))]
        current = [offer("bic", 1, model="UNRELATED")]
        historical = [observation(offer(price=1, model="UNRELATED"))]
        for i, candidate in enumerate(candidates):
            current.append(offer("tsukumo", 9500 + i, product_id=str(i),
                                 seller_id=f"seller-{i}", condition=candidate.condition,
                                 variant=candidate.variant, warranty=candidate.warranty,
                                 points_yen=100 if i % 2 else None))
            historical.append(observation(offer(price=10000 + i,
                condition=candidate.condition, variant=candidate.variant,
                warranty=candidate.warranty, points_yen=i * 10), days=i + 1))
        self.assert_decisions_equal(candidates, current, historical)

    def test_missing_warranty_does_not_form_transitive_product_groups(self):
        a = offer(warranty="1 year")
        b = offer("tsukumo", warranty=None)
        c = offer("sofmap", warranty="2 years")
        self.assertTrue(same_product(a, b))
        self.assertTrue(same_product(b, c))
        self.assertFalse(same_product(a, c))
        current = [a, b, c, offer("bic", 1, model="UNRELATED")]
        historical = [observation(o, days=i + 1) for i, o in enumerate(current)]
        self.assert_decisions_equal(current, current, historical)
        peers, past = comparison_inputs_by_identity(current, historical)
        for candidate, count in ((a, 1), (b, 2), (c, 1)):
            decision = evaluate(candidate, peers[candidate.identity], past[candidate.identity], NOW)
            self.assertEqual(len(decision["comparisons"]), count)

    def test_order_ties_and_duplicate_history_preserve_evidence(self):
        candidate = offer()
        first = offer("tsukumo", 9500, url="https://tsukumo.example/first")
        second = offer("tsukumo", 9500, url="https://tsukumo.example/second")
        other = offer("bic", 1, model="UNRELATED")
        repeated = observation(offer(price=10000), days=2)
        historical = [repeated, observation(other), deepcopy(repeated), repeated]
        current = [other, first, second]
        self.assert_decisions_equal([candidate], current, historical)
        peers, past = comparison_inputs_by_identity(current, historical)
        self.assertEqual([id(row) for row in past[candidate.identity]],
                         [id(historical[i]) for i in (0, 2, 3)])
        result = evaluate(candidate, peers[candidate.identity], past[candidate.identity], NOW)
        self.assertEqual(result["rule"], "B_observed_year_low")
        self.assertEqual(result["history"]["samples"], 3)
        self.assertEqual(result["comparisons"][0]["url"], first.url)
        # Equal-price independent sellers also retain their original evidence order.
        current = [first, offer("sofmap", 9500), second]
        self.assert_decisions_equal([candidate], current, historical)

    def test_recent_median_and_repeated_days_match_full_list(self):
        candidate, peer = offer(), offer("tsukumo", 9500)
        historical = [observation(offer(price=price), days=days)
                      for days, price in ((31, 10000), (20, 10000), (10, 9000))]
        for rows, expected_rule in ((historical, "B_recent_median"),
                                    ([historical[-1]] * 3, None)):
            self.assert_decisions_equal([candidate], [peer], rows)
            peers, past = comparison_inputs_by_identity([peer], rows)
            result = evaluate(candidate, peers[candidate.identity], past[candidate.identity], NOW)
            self.assertEqual(result["rule"], expected_rule)

    def test_null_identity_payment_and_unknown_points_match_full_list(self):
        unknown = offer(model=None, brand=None)
        candidate = offer(points_yen=None)
        current = [unknown, candidate, offer("tsukumo", 10000), offer("sofmap", 10000)]
        historical = [observation(unknown), observation(offer(price=11000))]
        self.assert_decisions_equal(current, current, historical)
        peers, past = comparison_inputs_by_identity(current, historical)
        self.assertIn(None, peers)
        self.assertIn(None, past)
        self.assertEqual(evaluate(candidate, peers[candidate.identity], past[candidate.identity], NOW)["rule"], "A")
        self.assertEqual(evaluate(candidate, peers[candidate.identity], past[candidate.identity], NOW,
                                  points=True)["reasons"], ["points_unknown"])

    def test_normalized_model_and_jan_identity_match_full_list(self):
        candidate = offer(brand="Ｂｒａｎｄ", model=" part-1 ")
        jan_candidate = offer(jan="4901234567894", model="DIFFERENT")
        current = [candidate, offer("tsukumo", 10000), offer("sofmap", 10000),
                   jan_candidate, offer("bic", 10000, jan="4901234567894")]
        historical = [observation(o) for o in current]
        self.assertEqual(candidate.identity, current[1].identity)
        self.assertEqual(jan_candidate.identity, "jan:4901234567894")
        self.assert_decisions_equal(current, current, historical)

    def test_skippable_malformed_history_matches_full_list(self):
        candidate = offer()
        historical = [{}, {"offer": {}}, {"offer": {"store": "ark"}},
                      observation(offer(price=10000))]
        self.assert_decisions_equal([candidate], [offer("tsukumo", 9500)], historical)

    def test_non_mapping_payload_preserves_lazy_evaluator_failure(self):
        candidate = offer()
        for payload, points in product((None, [], "bad", 7), (False, True)):
            with self.subTest(payload=payload, points=points):
                historical = [{"offer": payload}]
                current = [offer("tsukumo", 10000), offer("sofmap", 10000)]
                self.assert_decisions_equal([candidate], current, historical)
                # With only one seller, B reaches the malformed payload and the
                # existing AttributeError must still escape, for either basis.
                peers, past = comparison_inputs_by_identity(current[:1], historical)
                with self.assertRaises(AttributeError):
                    evaluate(candidate, current[:1], historical, NOW, points=points)
                with self.assertRaises(AttributeError):
                    evaluate(candidate, peers[candidate.identity], past[candidate.identity], NOW, points=points)

    def test_aggregate_matches_full_lists_and_preserves_run_filters_and_state(self):
        sale, peer = offer(), offer("tsukumo", 9500)
        held = offer("koubou", 10000, product_id="held", model="HOLD")
        offers = [sale, peer, offer("sofmap", 1, model="UNRELATED"),
                  offer("joshin", model=None, brand=None), held,
                  offer("tsukumo", 12000, product_id="held", model="HOLD"),
                  offer("sofmap", 12500, product_id="held", model="HOLD"),
                  offer("ark", 1, product_id="old-run", observed_run_id="old"),
                  offer("ark", 1, product_id="failed", issues=["latest_fetch_failed"])]
        old = observation(offer(price=10000), days=2)
        excluded = observation(offer(price=1, observed_run_id="old"), run_id="new")
        history = [old, deepcopy(old), observation(offer("bic", 9000, model="HOLD")),
                   excluded, {"run_id": "new", "offer": None}, {}, {"offer": {}}]
        states = {}
        for o in offers:
            state = states.setdefault(o.store, {"store": o.store, "run_id": "new",
                "status": "complete", "cycle_complete": True, "offers": {}, "journal": [],
                "queue": {"pending": {"type": "product", "created_at": iso(NOW - timedelta(days=3)),
                                       "attempts": 7, "url": "https://example.com/pending"}}})
            state["offers"][o.key] = o.to_dict()
        missing = offer("amazon", 1, model="MISSING-JOB")
        states["amazon"] = {"store": "amazon", "run_id": "old", "status": "complete",
                            "offers": {missing.key: missing.to_dict()}, "queue": {}, "journal": []}
        states["ark"]["journal"] = [observation(sale, days=0, run_id="new", observation_id="new-receipt")]
        snapshots = []
        with tempfile.TemporaryDirectory() as tmp:
            for indexed in (False, True):
                root = Path(tmp) / str(indexed)
                disk = Store(root)
                for name, state in states.items():
                    disk.save(f"stores/{name}.json", state)
                disk.save("history/ark/2026-10-02.json", history)
                indexer = comparison_inputs_by_identity if indexed else full_list_inputs
                with patch("sale_monitor.reporting.comparison_inputs_by_identity", side_effect=indexer), \
                     patch("sale_monitor.reporting.known_comparators", wraps=known_comparators) as known, \
                     patch("sale_monitor.reporting.plan_comparisons", wraps=plan_comparisons) as plan:
                    aggregate(root, root / "public", "new", NOW)
                self.assertEqual(known.call_args.args[1], [r for r in history if r.get("run_id") != "new"])
                planned = plan.call_args.args[0]
                expected_current = [Offer.from_dict(row) for name in STORES
                    if states.get(name, {}).get("run_id") == "new"
                    for row in states[name]["offers"].values()
                    if row["observed_run_id"] == "new" and "latest_fetch_failed" not in row["issues"]]
                self.assertEqual([o.key for o in planned], [o.key for o in expected_current])
                for name, state in states.items():
                    self.assertEqual(disk.load(f"stores/{name}.json", {})["queue"], state["queue"])
                self.assertEqual(disk.load("history/ark/2026-10-02.json", []), history)
                decisions = disk.load("public/evidence.json", {})["decisions"]
                sale_decision = next(d for d in decisions if d["payment"]["offer_key"] == sale.key)
                self.assertEqual(sale_decision["payment"]["rule"], "B_observed_year_low")
                self.assertEqual(sale_decision["payment"]["history"]["samples"], 2)
                held_decision = next(d for d in decisions if d["payment"]["offer_key"] == held.key)
                self.assertEqual(held_decision["payment"]["reasons"], ["known_comparator_not_verified_this_run"])
                snapshots.append({p.relative_to(root).as_posix(): read_json(p, None)
                                  for p in root.rglob("*.json")})
        self.assertEqual(snapshots[0], snapshots[1])

    def test_aggregate_preserves_known_comparator_validation_failure(self):
        malformed = observation(offer(model=42))
        for indexer in (full_list_inputs, comparison_inputs_by_identity):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                Store(root).save("history/ark/2026-10-03.json", [malformed])
                with patch("sale_monitor.reporting.comparison_inputs_by_identity", side_effect=indexer) as indexed:
                    with self.assertRaises(TypeError):
                        aggregate(root, root / "public", "new", NOW)
                indexed.assert_not_called()
                self.assertFalse((root / "events/registry.json").exists())


if __name__ == "__main__":
    unittest.main()
