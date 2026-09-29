from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from sale_monitor.models import Offer
from sale_monitor.reporting import health
from sale_monitor.runner import Collector
from sale_monitor.scheduling import plan_comparison_refresh
from sale_monitor.storage import Store


NOW = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
OLD = "2026-09-18T11:54:01+00:00"
CFG = {"stores": {"ark": {"adapter": "html", "seed_urls": []}}}


def offer(product, kind="comparison", known=True):
    return Offer("ark", product, "https://www.ark-pc.co.jp/i/" + product + "/",
        model=product if known else None, brand="Test" if known else None,
        observed_at=NOW.isoformat(), observed_run_id="old", price_yen=9000,
        shipping_yen=0, condition="new", stock="in_stock", seller_id="ark",
        discovery_kind=kind, verified=True)


def request(o):
    return {"identity": o.identity, "query": o.brand + " " + o.model}


class ComparisonRefresh(unittest.TestCase):
    def test_exact_identity_required_not_series_name_or_partial_title(self):
        target, other = offer("MODEL-1"), offer("MODEL-10")
        rows = {o.key: o.to_dict() for o in [target, other]}
        selected, info = plan_comparison_refresh(rows, [request(target)])
        self.assertEqual(selected, {target.key})
        self.assertEqual(info["not_newly_refreshed"], 1)
        self.assertEqual(info["reasons"], {"requested_identity": 1, "not_requested_identity": 1})

    def test_unknown_identity_remains_work_and_missing_metadata_is_conservative(self):
        known, unknown = offer("MODEL-1"), offer("unknown", known=False)
        rows = {o.key: o.to_dict() for o in [known, unknown]}
        selected, info = plan_comparison_refresh(rows, [])
        self.assertEqual(selected, {unknown.key})
        self.assertEqual(info["reasons"]["identity_unresolved"], 1)
        for requests in [None, [{"query": "Test MODEL-1"}], [{"identity": None}], [{"identity": ""}], {}]:
            with self.subTest(requests=requests):
                selected, info = plan_comparison_refresh(rows, requests)
                self.assertEqual(selected, set(rows))
                self.assertFalse(info["request_plan_verified"])
                self.assertEqual(info["not_newly_refreshed"], 0)

    def test_jan_identity_uses_same_rules_as_price_comparison(self):
        target, other = offer("one"), offer("two")
        target.jan = "4537694358347"; other.jan = "4526541047831"
        selected, _ = plan_comparison_refresh({o.key: o.to_dict() for o in [target, other]}, [request(target)])
        self.assertEqual(selected, {target.key})

    def setup_state(self, root):
        disk = Store(root)
        sale, active, inactive, pending, unknown = (offer("sale", "sale"), offer("active"),
            offer("inactive"), offer("pending"), offer("unknown", known=False))
        rows = {o.key: o.to_dict() for o in [sale, active, inactive, pending, unknown]}
        task = {"type": "product", "kind": "comparison", "url": pending.url, "created_at": OLD,
            "attempts": 17, "last_error": "http_403", "source": "https://www.ark-pc.co.jp/search/?key=previous"}
        journal = [{"observation_id": "unchanged-id", "run_id": "old", "offer": inactive.to_dict()}]
        disk.save("stores/ark.json", {"store": "ark", "run_id": "old", "status": "partial",
            "queue": {"prior-task-id": task}, "done": ["prior-done"], "offers": rows,
            "journal": journal, "cycle_complete": False})
        disk.save("requests/ark.json", [request(active)])
        disk.save("history/old/ark.json", journal)
        return disk, (sale, active, inactive, pending, unknown), rows, task, journal

    def test_new_seed_keeps_sales_unknown_id_and_original_pending_without_old_price_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); disk, offers, rows, task, journal = self.setup_state(root)
            sale, active, inactive, pending, unknown = offers
            c = Collector(root, "ark", CFG, "new"); c.seed()
            queued = [t["url"] for t in c.state["queue"].values() if t["type"] == "product"]
            self.assertEqual(set(queued), {sale.url, active.url, pending.url, unknown.url})
            self.assertEqual(c.state["queue"]["prior-task-id"], task)
            self.assertEqual(c.state["offers"], rows)
            self.assertEqual(c.state["journal"], journal)
            self.assertEqual(disk.history(), journal)
            self.assertEqual(health(c.state, NOW)["pending_over_24h"], 1)
            self.assertEqual(health(c.state, NOW)["current_offers"], 0)
            # Not-newly-refreshed includes an already pending item: never report
            # this planning counter as a resolved task or request count.
            self.assertEqual(health(c.state, NOW)["comparison_refresh"]["not_newly_refreshed"], 2)

    def test_resumed_run_keeps_checkpoint_queue_and_planning_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_state(root)
            c = Collector(root, "ark", CFG, "new"); c.seed()
            prior_queue = c.state["queue"].copy(); prior_plan = c.state["comparison_refresh"].copy()
            resumed = Collector(root, "ark", CFG, "new"); resumed.seed()
            self.assertEqual(resumed.state["queue"], prior_queue)
            self.assertEqual(resumed.state["comparison_refresh"], prior_plan)

    def test_new_request_can_reactivate_historical_offer_but_never_its_old_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); disk, offers, _, _, _ = self.setup_state(root)
            inactive = offers[2]
            c = Collector(root, "ark", CFG, "new"); c.seed()
            self.assertNotIn(inactive.url, [t.get("url") for t in c.state["queue"].values()])
            disk.save("requests/ark.json", [request(inactive)])
            next_run = Collector(root, "ark", CFG, "next"); next_run.seed()
            self.assertIn(inactive.url, [t.get("url") for t in next_run.state["queue"].values()])
            self.assertEqual(next_run.state["offers"][inactive.key]["observed_run_id"], "old")
            self.assertEqual(health(next_run.state, NOW)["current_offers"], 0)

    def test_absent_request_file_never_discards_existing_refresh_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); o = offer("item"); disk = Store(root)
            disk.save("stores/ark.json", {"store": "ark", "run_id": "old", "status": "complete",
                "queue": {}, "done": [], "offers": {o.key: o.to_dict()}, "journal": [], "cycle_complete": True})
            c = Collector(root, "ark", CFG, "new"); c.seed()
            self.assertEqual([t["url"] for t in c.state["queue"].values()], [o.url])
            self.assertFalse(c.state["comparison_refresh"]["request_plan_verified"])


if __name__ == "__main__":
    unittest.main()
