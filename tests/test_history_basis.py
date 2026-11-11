from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from sale_monitor.engine import evaluate, history_basis
from sale_monitor.models import JST, UTC, Offer, iso

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def offer(store="ark", price=9000, **kwargs):
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


class HistoryBasisTests(unittest.TestCase):
    def test_year_low_needs_only_one_observation_day(self):
        result = history_basis(offer(), history([(1, 9500)]), NOW)
        self.assertEqual(result, {"status": "met", "rule": "B_observed_year_low",
                                  "history": {"minimum_yen": 9500, "from": "2026-09-13T00:00:00+00:00",
                                              "to": "2026-09-13T00:00:00+00:00", "samples": 1,
                                              "scope": "observed_period_only", "sources": [offer().url]}, "reasons": []})

    def test_year_low_499_yen_is_insufficient(self):
        self.assertEqual(history_basis(offer(), history([(1, 9499)]), NOW)["status"], "insufficient")

    def test_median_requires_three_days_and_thirty_days(self):
        for samples in ([(31, 10000), (10, 9000)], [(29, 10000), (20, 10000), (10, 9000)]):
            with self.subTest(samples=samples):
                result = history_basis(offer(), history(samples), NOW)
                self.assertEqual(result["status"], "insufficient")
                self.assertEqual(result["reasons"], ["B_history_insufficient"])

    def test_median_met_at_thirty_day_boundary(self):
        result = history_basis(offer(), history([(30, 10000), (20, 10000), (10, 9000)]), NOW)
        self.assertEqual((result["status"], result["rule"]), ("met", "B_recent_median"))

    def test_median_above_period_minimum_is_not_met(self):
        result = history_basis(offer(), history([(31, 10000), (20, 10000), (10, 8999)]), NOW)
        self.assertEqual(result["status"], "not_met")
        self.assertEqual(result["reasons"], ["B_history_gap_below_threshold"])

    def test_median_gap_thresholds(self):
        for price in (9001, 9600):
            with self.subTest(price=price):
                self.assertEqual(history_basis(offer(price=price), history([(31, 10000), (20, 10000), (10, price)]), NOW)["status"], "not_met")

    def test_repeat_observations_count_as_one_day(self):
        result = history_basis(offer(), history([(31, 9000), (31, 10000), (31, 11000)]), NOW)
        self.assertEqual(result["status"], "insufficient")

    def test_invalid_candidate_and_unknown_points(self):
        for candidate, points, reasons in ((offer(shipping_yen=None), False, ["shipping_unknown"]),
                                           (offer(points_yen=None), True, ["points_unknown"]),
                                           (offer(verified=False), False, ["unverified_product"])):
            with self.subTest(reasons=reasons):
                self.assertEqual(history_basis(candidate, [], NOW, points),
                                 {"status": "insufficient", "rule": None, "history": {}, "reasons": reasons})

    def test_points_are_used_for_historical_and_current_amounts(self):
        rows = history([(1, 9500)], points_yen=1000)
        self.assertEqual(history_basis(offer(), rows, NOW)["status"], "met")
        self.assertEqual(history_basis(offer(), rows, NOW, True)["status"], "insufficient")
        self.assertEqual(history_basis(offer(points_yen=1000), rows, NOW, True)["status"], "met")

    def test_invalid_future_and_mismatched_history_are_ignored(self):
        rows = [{}, {"offer": {}}, *history([(-1, 10000), (0, 10000)]),
                *history([(1, 10000)], model="OTHER"), *history([(1, 10000)], store="rakuten"),
                *history([(1, 10000)], verified=False)]
        self.assertEqual(history_basis(offer(), rows, NOW)["status"], "insufficient")

    def test_old_prices_are_valid_at_observation_time(self):
        self.assertEqual(history_basis(offer(), history([(200, 10000)]), NOW)["status"], "met")

    def test_year_is_japanese_calendar_year(self):
        now = datetime(2026, 1, 1, 1, tzinfo=JST)
        past = offer(price=10000, observed_at=iso(now - timedelta(hours=2)))
        candidate = offer(observed_at=iso(now))
        self.assertEqual(history_basis(candidate, [{"offer": past.to_dict()}], now)["status"], "insufficient")

    def test_no_mutation_and_no_formal_decision_status(self):
        candidate, rows = offer(), history([(31, 10000), (20, 10000), (10, 9000)])
        before = deepcopy((candidate, rows))
        result = history_basis(candidate, rows, NOW)
        self.assertEqual((candidate, rows), before)
        self.assertNotIn("accepted", json.dumps(result))

    def test_evaluate_history_correspondence(self):
        for samples, status in (([(1, 9500)], "met"), ([(31, 10000), (20, 10000), (10, 9000)], "met"),
                                ([(31, 10000), (20, 10000), (10, 8999)], "not_met"), ([], "insufficient")):
            for points in (False, True):
                with self.subTest(samples=samples, points=points):
                    rows = history(samples)
                    basis = history_basis(offer(), rows, NOW, points)
                    decision = evaluate(offer(), [offer("bic", 9500)], rows, NOW, points)
                    self.assertEqual(basis["status"], status)
                    self.assertEqual(decision["history"], basis["history"])
                    self.assertEqual(decision["rule"], basis["rule"])
                    self.assertEqual(decision["status"] == "accepted", status == "met")

    def test_complete_evaluate_regression_including_key_order(self):
        # These complete expected decisions were fixed against the original evaluator.
        candidate = offer()
        peer = offer("bic", 9500)
        comparison = {"seller_id": "bic", "price_yen": 9500, "url": peer.url,
                      "observed_at": peer.observed_at, "evidence": peer.evidence}
        envelope = {"offer_key": candidate.key, "status": "insufficient", "basis": "payment", "rule": None,
                    "reasons": ["A_independent_sellers_insufficient", "B_history_insufficient"],
                    "comparisons": [comparison], "history": {}}
        cases = [([], [peer], envelope)]
        missing = deepcopy(envelope)
        missing.update(reasons=["A_independent_sellers_insufficient", "B_current_comparison_missing"], comparisons=[])
        cases.append((history([(1, 9500)]), [], missing))
        year = deepcopy(envelope)
        year.update(status="accepted", rule="B_observed_year_low", reasons=[],
                    history={"minimum_yen": 9500, "from": "2026-09-13T00:00:00+00:00", "to": "2026-09-13T00:00:00+00:00",
                             "samples": 1, "scope": "observed_period_only", "sources": [candidate.url]})
        cases.append((history([(1, 9500)]), [peer], year))
        cheaper = deepcopy(envelope)
        cheaper.update(status="rejected", reasons=["cheaper_current_offer"])
        cheaper["comparisons"][0]["price_yen"] = 8999
        cases.append((history([(1, 9500)]), [offer("bic", 8999)], cheaper))
        rule_a = deepcopy(envelope)
        rule_a["comparisons"][0]["price_yen"] = 10000
        second = offer("sofmap", 10000)
        rule_a["comparisons"].append({"seller_id": "sofmap", "price_yen": 10000, "url": second.url,
                                     "observed_at": second.observed_at, "evidence": second.evidence})
        rule_a.update(status="accepted", rule="A", reasons=[], reference_yen=10000,
                      difference_yen=1000, difference_percent=10.0)
        cases.append((history([(1, 9500)]), [offer("bic", 10000), second], rule_a))
        for low, status, rule, reasons in ((9000, "accepted", "B_recent_median", []),
                                           (8999, "insufficient", None, ["A_independent_sellers_insufficient", "B_history_gap_below_threshold"])):
            expected = deepcopy(envelope)
            expected.update(status=status, rule=rule, reasons=reasons,
                            history={"median_yen": 10000.0, "minimum_yen": low, "from": "2026-08-14", "to": "2026-09-04",
                                     "days": 3, "samples": 3, "sources": [candidate.url], "scope": "observed_days_only"})
            rows = history([(31, 10000), (20, 10000), (10, low)])
            cases.append((rows, [peer], expected))
            if low == 8999:
                rejected = deepcopy(expected)
                second = offer("sofmap", 9500)
                rejected["comparisons"].append({"seller_id": "sofmap", "price_yen": 9500, "url": second.url,
                                                 "observed_at": second.observed_at, "evidence": second.evidence})
                rejected.update(status="rejected", reasons=["A_price_gap_below_threshold", "B_history_gap_below_threshold"],
                                reference_yen=9500, difference_yen=500, difference_percent=5.2632)
                cases.append((rows, [peer, second], rejected))
        for rows, peers, expected in cases:
            with self.subTest(status=expected["status"], rule=expected["rule"]):
                self.assertEqual(json.dumps(evaluate(candidate, peers, rows, NOW)), json.dumps(expected))


if __name__ == "__main__":
    unittest.main()
