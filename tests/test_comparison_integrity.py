from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sale_monitor.comparison_integrity import known_comparators, missing_comparators
from sale_monitor.models import Offer, iso
from sale_monitor.reporting import aggregate
from sale_monitor.runner import Collector, task_order
from sale_monitor.scheduling import select_task
from sale_monitor.storage import Store, read_json

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
CFG = {"stores": {s: {"adapter": "html", "seed_urls": []} for s in ("ark", "tsukumo")}}


def offer(store="ark", price=10000, **changes):
    data = dict(store=store, product_id="one", url=f"https://{store}.example/item/one",
                brand="Brand", model="PART-1", seller_id=store, condition="new", stock="in_stock",
                price_yen=price, shipping_yen=0, points_yen=0, verified=True,
                observed_at=iso(NOW), observed_run_id="new", evidence=[{"url": "fixture"}])
    data.update(changes)
    return Offer(**data)


def state(o):
    return {"store": o.store, "run_id": o.observed_run_id, "status": "complete", "queue": {},
            "offers": {o.key: o.to_dict()}, "journal": [], "done": [], "cycle_complete": True}


class ComparisonIntegrity(unittest.TestCase):
    def test_missing_known_comparator_holds_A_without_reusing_historical_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, public = Path(tmp)/"state", Path(tmp)/"public"
            disk = Store(root)
            for s,p in (("ark",10000),("sofmap",12000),("joshin",12500)):
                disk.save(f"stores/{s}.json", state(offer(s,p)))
            old = offer("tsukumo", 9000, observed_at=iso(NOW-timedelta(days=30)),
                        observed_run_id="old", discovery_kind="comparison", issues=["latest_fetch_failed"])
            disk.save("stores/tsukumo.json", state(old))
            original = disk.load("stores/tsukumo.json", {})
            index = aggregate(root, public, "new", NOW)
            decisions = read_json(public/"evidence.json", {})["decisions"]
            d = next(d for d in decisions if d["offer"]["store"] == "ark")
            self.assertEqual(d["payment"]["status"], "insufficient")
            self.assertEqual(d["payment"]["provisional_rule"], "A")
            self.assertEqual([c["price_yen"] for c in d["payment"]["comparisons"]], [12000,12500])
            self.assertEqual(d["payment"]["missing_comparators"][0]["store"], "tsukumo")
            self.assertEqual(index["notification_count"], 0)
            self.assertEqual(disk.load("stores/tsukumo.json", {})["offers"], original["offers"])
            self.assertEqual(index["comparison_planning"]["known_comparator_hold_offers"], 1)
            # Explicit fresh sold-out evidence closes this gap, without an old price.
            disk.save("stores/tsukumo.json", state(offer("tsukumo",9000,stock="out_of_stock")))
            index = aggregate(root, public, "new", NOW)
            self.assertEqual(index["notification_count"], 1)

    def test_current_unknown_shipping_does_not_close_previous_valid_gap(self):
        past = offer("tsukumo",9000,observed_at=iso(NOW-timedelta(hours=4)))
        current = offer("tsukumo",9000,shipping_yen=None)
        historical = [{"offer":past.to_dict()}, {"bad":"row"}]
        known = known_comparators({"tsukumo":state(current)}, historical, NOW)
        gaps = missing_comparators(offer(),known,{("tsukumo",current.key):current},NOW)
        self.assertEqual(len(gaps),1)
        self.assertIn("shipping_unknown", gaps[0]["current_issues"])

    def test_gap_resolution_is_specific_to_points_or_payment(self):
        past = offer("tsukumo",9000,observed_at=iso(NOW-timedelta(hours=4)),points_yen=100)
        current = offer("tsukumo",9000,points_yen=None)
        known = known_comparators({"tsukumo":state(current)},[{"offer":past.to_dict()}],NOW)
        observed={("tsukumo",current.key):current}
        self.assertEqual(missing_comparators(offer(),known,observed,NOW),[])
        self.assertEqual(len(missing_comparators(offer(),known,observed,NOW,points=True)),1)

    def test_different_model_bundle_seller_and_excluded_source_do_not_create_gap(self):
        others=[offer("tsukumo",model="OTHER"),offer("sofmap",variant="two pack"),
                offer("joshin",seller_id="ark"),offer("rakuten",url="https://rakuten.co.jp/item")]
        known=known_comparators({},[{"offer":o.to_dict()} for o in others],NOW)
        self.assertEqual(missing_comparators(offer(),known,{},NOW),[])

    def test_sold_out_with_failed_fetch_is_not_terminal_evidence(self):
        past=offer("tsukumo",9000)
        known=known_comparators({},[{"offer":past.to_dict()}],NOW)
        current=offer("tsukumo",9000,stock="out_of_stock",issues=["latest_fetch_failed"])
        self.assertEqual(len(missing_comparators(offer(),known,{("tsukumo",current.key):current},NOW)),1)

    def test_journal_keeps_nested_success_receipt_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Collector(Path(tmp),"ark",CFG,"new")
            o=offer(coupon={"verified":True,"checked_at":iso(NOW),"remaining":3})
            c.record(o)
            recorded=deepcopy(c.state["journal"])
            c.state["offers"][o.key]["issues"].append("latest_fetch_failed")
            c.state["offers"][o.key]["evidence"][0]["url"]="changed"
            c.state["offers"][o.key]["coupon"]["remaining"]=0
            self.assertEqual(c.state["journal"],recorded)
            c.save()
            self.assertEqual(Store(Path(tmp)).load("stores/ark.json",{})["journal"],recorded)

    def test_temporarily_missing_or_held_sale_keeps_comparator_refresh_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"state"; disk=Store(root)
            # Candidate is not current and is not eligible: must not vanish from
            # the refresh plan, while its price remains excluded from comparisons.
            candidate=offer(observed_run_id="old",stock="preorder",observed_at=iso(NOW-timedelta(days=1)))
            other=offer("tsukumo",12000,discovery_kind="comparison",observed_run_id="old")
            disk.save("stores/ark.json",state(candidate));disk.save("stores/tsukumo.json",state(other))
            aggregate(root,Path(tmp)/"public","new",NOW)
            self.assertEqual(disk.load("requests/tsukumo.json",[]),[])
            self.assertIn(candidate.identity,disk.load("requests/candidate_identities.json",[]))
            c=Collector(root,"tsukumo",CFG,"next");c.seed()
            self.assertTrue(any(t.get("url")==other.url and t.get("requested") for t in c.state["queue"].values()))
            self.assertEqual(c.state["offers"][other.key]["observed_run_id"],"old")

    def test_current_requests_and_old_backlog_both_progress_across_restart(self):
        pending=[]
        for needed in (True,False):
            for n in range(20):
                pending.append((f"{needed}-{n}",{"type":"product","kind":"comparison","requested":needed,
                    "created_at":iso(NOW-timedelta(days=2 if not needed else 0)),"priority":3}))
        scheduler={}; selected=[]
        for i in range(12):
            item=select_task(pending,scheduler,task_order,NOW);pending.remove(item)
            selected.append(item[1]["requested"])
            scheduler=deepcopy(scheduler)  # persisted/reloaded between short runs
        self.assertEqual(selected,[True,True,True,False]*3)
        self.assertEqual(len(pending),28)


if __name__ == "__main__":
    unittest.main()
