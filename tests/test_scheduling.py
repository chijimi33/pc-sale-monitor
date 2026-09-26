from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sale_monitor.models import Offer, STORES, UTC, iso
from sale_monitor.reporting import aggregate
from sale_monitor.runner import Collector, task_order
from sale_monitor.scheduling import lane, plan_comparisons, select_task
from sale_monitor.storage import Store


NOW = datetime(2026, 9, 27, tzinfo=UTC)


def candidate(**changes):
    data = dict(store="ark", seller_id="ark", product_id="one", url="https://a.example/i/one",
                title="Part", model="PART-1", brand="Brand", condition="new", price_yen=1000,
                shipping_yen=0, stock="in_stock", verified=True, observed_at=iso(NOW),
                observed_run_id="r1", evidence=[{"url": "https://a.example/i/one", "checked_at": iso(NOW)}])
    data.update(changes)
    return Offer(**data)


def task(kind, n, **changes):
    data = dict(type="list" if kind == "discovery" else "product",
                kind="comparison" if kind == "comparison" else "sale",
                url=f"https://a.example/{kind}/{n}", created_at=iso(NOW), priority=3)
    data.update(changes)
    return str(n) + kind, data


class Scheduling(unittest.TestCase):
    def test_all_busy_lanes_make_progress_without_a_request_cap(self):
        pending = [task(kind, n) for kind in ("discovery", "sale", "comparison") for n in range(40)]
        cursor = {}; selected = []
        while pending:
            item = select_task(pending, cursor, task_order, NOW)
            selected.append(lane(item[1])); pending.remove(item)
        self.assertEqual(selected[:4], ["discovery", "sale", "comparison", "sale"])
        self.assertEqual(len(selected), 120)
        self.assertEqual(cursor["selected_by_lane"], {"discovery": 40, "sale": 40, "comparison": 40})

    def test_overdue_comparison_precedes_new_higher_priority_comparison(self):
        old = task("comparison", 1, created_at=iso(NOW-timedelta(days=2)), priority=3, attempts=7)
        new = task("comparison", 2, priority=0)
        selected = select_task([new, old], {}, task_order, NOW)
        self.assertEqual(selected, old)
        self.assertEqual(old[1]["attempts"], 7)
        self.assertEqual(old[1]["created_at"], iso(NOW-timedelta(days=2)))

    def test_missing_lanes_do_not_idle_or_drop_work(self):
        pending = [task("comparison", 1)]
        self.assertEqual(select_task(pending, {"cursor": 0}, task_order, NOW), pending[0])
        self.assertIsNone(select_task([], {}, task_order, NOW))

    def test_short_interrupted_runs_keep_lane_cursor_and_failed_task_age(self):
        cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = Collector(root, "ark", cfg, "r1"); first.seed()
            for kind in ("discovery", "sale", "comparison"):
                _, item = task(kind, 1, created_at=iso(NOW-timedelta(days=2)))
                first.enqueue(item)
            executed = []
            def process(item):
                executed.append(lane(item))
                raise RuntimeError("test failure")
            first.process = process
            with patch("sale_monitor.runner.time.monotonic", side_effect=[0, 0, 2]):
                first.collect(seconds=1)
            second = Collector(root, "ark", cfg, "r2"); second.process = process
            with patch("sale_monitor.runner.time.monotonic", side_effect=[0, 0, 2]):
                second.collect(seconds=1)
            third = Collector(root, "ark", cfg, "r3"); third.process = process
            with patch("sale_monitor.runner.time.monotonic", side_effect=[0, 0, 2]):
                state = third.collect(seconds=1)
            self.assertEqual(executed, ["discovery", "sale", "comparison"])
            self.assertEqual(len(state["queue"]), 3)
            self.assertTrue(all(t["created_at"] == iso(NOW-timedelta(days=2)) for t in state["queue"].values()))
            self.assertTrue(all(t["attempts"] == 1 for t in state["queue"].values()))
            self.assertEqual(state["status"], "partial")

    def test_unusable_candidates_stay_visible_without_new_search_fanout(self):
        rows = [candidate(product_id="ok"), candidate(product_id="fee", shipping_yen=None),
                candidate(product_id="stock", stock="out_of_stock"), candidate(product_id="model", model=None),
                candidate(product_id="coupon", coupon={"verified": False}),
                candidate(product_id="compare", discovery_kind="comparison")]
        planned, details = plan_comparisons(rows, {}, STORES, NOW)
        self.assertEqual(len(planned["tsukumo"]), 1)
        self.assertEqual(planned["ark"], [])
        self.assertEqual(details["summary"]["eligible_sale_offers"], 1)
        self.assertEqual(len(details["held_candidates"]), 4)
        self.assertIn("shipping_unknown", details["summary"]["held_reasons"])
        self.assertIn("stock_out_of_stock", details["summary"]["held_reasons"])

    def test_recovered_candidate_is_planned_on_next_observation(self):
        row = candidate(shipping_yen=None)
        self.assertEqual(plan_comparisons([row], {}, STORES, NOW)[0]["tsukumo"], [])
        row.shipping_yen = 0
        self.assertEqual(len(plan_comparisons([row], {}, STORES, NOW)[0]["tsukumo"]), 1)

    def test_duplicate_identity_keeps_most_urgent_change_priority(self):
        unchanged = candidate()
        changed = candidate(store="koubou", seller_id="koubou")
        planned, _ = plan_comparisons([unchanged, changed], {changed.key: "price_down"}, STORES, NOW)
        self.assertEqual(len(planned["tsukumo"]), 1)
        self.assertEqual(planned["tsukumo"][0]["priority"], 1)

    def test_aggregate_keeps_pending_history_and_explicit_hold_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); disk = Store(root)
            blocked = candidate(shipping_yen=None)
            queued = {"q": {"type": "search", "kind": "comparison", "query": "older",
                            "created_at": iso(NOW-timedelta(days=3)), "attempts": 11}}
            disk.save("stores/ark.json", {"store": "ark", "run_id": "r1", "status": "partial",
                      "offers": {blocked.key: blocked.to_dict()}, "queue": queued})
            result = aggregate(root, root/"public", "r1", NOW)
            self.assertEqual(disk.load("stores/ark.json", {})["queue"], queued)
            self.assertEqual(result["stores"]["ark"]["pending_over_24h"], 1)
            self.assertEqual(result["comparison_planning"]["held_sale_offers"], 1)
            self.assertEqual(disk.load("public/comparison_plan.json", {})["held_candidates"][0]["offer_key"], blocked.key)
            self.assertEqual(disk.load("requests/tsukumo.json", []), [])
            self.assertFalse(disk.load("public/validation.json", {})["cutover_ready"])


if __name__ == "__main__":
    unittest.main()
