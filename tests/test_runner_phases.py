"""Offline phase isolation, receipts and interruption regressions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sale_monitor.budget import HostBudget, Robots
from sale_monitor.http import Client, FetchError, Page
from sale_monitor.models import Offer, iso
from sale_monitor.runner import Collector
from sale_monitor.storage import Store


SALE_URL = "https://www.ark-pc.co.jp/i/111/"
PEER_URL = "https://www.ark-pc.co.jp/i/222/"
HOME = "https://www.ark-pc.co.jp/"


def offer(url=SALE_URL, kind="sale", **updates):
    data = dict(store="ark", product_id=url.split("/")[-2], url=url, title="Fixture",
                model="MODEL-A", brand="Brand", seller_id="ark", condition="new", stock="in_stock",
                price_yen=9000, shipping_yen=0, points_yen=0, observed_at=iso(), verified=True,
                discovery_kind=kind, evidence=[{"url": url}])
    data.update(updates)
    return Offer(**data)


class FakeClient:
    def __init__(self, pages=None, **kwargs):
        self.count = 0
        self.calls = []
        self.pages = pages or {}
        self.budget = kwargs.get("budget")
        self.robots = kwargs.get("robots")

    def get(self, url):
        self.calls.append(url)
        if self.budget is not None:
            from urllib.parse import urlsplit
            self.budget.spend(urlsplit(url).hostname)
        self.count += 1
        value = self.pages.get(url, b"fixture")
        if isinstance(value, BaseException):
            raise value
        return Page(url, value, iso())

    def rendered(self, url):
        self.count += 1
        return Page(url, b"rendered", iso(), method="browser")


class RunnerPhases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-phases-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.disk = Store(self.root)
        self.cfg = {"stores": {"ark": {"adapter": "html", "seed_urls": [HOME]}}}

    def collector(self, phase, plan=None, client=None, run="run1"):
        return Collector(self.root, "ark", self.cfg, run, client or FakeClient(), phase=phase, plan=plan)

    def plan(self, *offers):
        return {"run_id": "run1", "confirm": {"ark": [{"url": o.url, "offer_key": o.key,
                   "identity": o.identity} for o in offers]}, "explore": {}}

    def state(self, **updates):
        value = {"store": "ark", "schema_version": 1, "run_id": "old", "queue": {}, "done": [],
                 "offers": {}, "journal": [], "cycle_complete": False}
        value.update(updates)
        self.disk.save("stores/ark.json", value)
        return value

    def test_discovery_never_reads_legacy_requests_or_routes_comparisons(self):
        sale, peer = offer(), offer(PEER_URL, "comparison")
        self.state(offers={o.key: o.to_dict() for o in (sale, peer)})
        self.disk.save("requests/ark.json", [{"query": "MODEL-A", "identity": sale.identity}])
        self.disk.save("requests/candidate_identities.json", [sale.identity])
        collector = self.collector("discovery")
        with patch.object(collector.disk, "load", wraps=collector.disk.load) as load, patch.object(
                collector, "route_comparison", side_effect=AssertionError), patch(
                "sale_monitor.runner.comparison_priority.refresh", side_effect=AssertionError):
            collector.seed()
        self.assertFalse(load.called)
        tasks = list(collector.state["queue"].values())
        self.assertEqual({t["type"] for t in tasks}, {"list", "product"})
        self.assertEqual([t["url"] for t in tasks if t["type"] == "product"], [sale.url])
        self.assertFalse(any(t.get("kind") == "comparison" for t in tasks))
        self.assertEqual(collector.state["phase"], "discovery")

    def test_discovery_executes_lists_and_sale_rechecks_but_leaves_comparisons(self):
        sale = offer()
        previous = self.state(offers={sale.key: sale.to_dict()}, queue={"comparison": {
            "type": "product", "url": PEER_URL, "kind": "comparison", "attempts": 7, "created_at": iso()}})
        collector = self.collector("discovery")
        with patch("sale_monitor.runner.discover", return_value=([{"url": sale.url, "kind": "sale"}], [], [])), patch(
                "sale_monitor.runner.parse_product", return_value=sale):
            result = collector.collect(3)
        self.assertEqual(collector.client.calls, [HOME, SALE_URL])
        self.assertEqual(result["queue"], previous["queue"])
        self.assertTrue(result["cycle_complete"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["journal"][0]["run_id"], "run1")

    def test_discovery_leaves_dospara_sale_items_to_the_coupon_adapter(self):
        # A plain product task would skip the dedicated coupon verification.
        sale = offer(store="dospara", url="https://www.dospara.co.jp/IC123.html")
        self.disk.save("stores/dospara.json", {**self.state(), "store": "dospara", "offers": {sale.key: sale.to_dict()}})
        collector = Collector(self.root, "dospara", {"stores": {"dospara": {"adapter": "dospara", "seed_urls": []}}},
                              "run1", FakeClient(), phase="discovery")
        collector.seed()
        self.assertEqual(collector.state["queue"], {})

    def test_verification_records_and_preserves_sale_origin_and_discovery_queue(self):
        sale = offer()
        previous = self.state(queue={"discovery": {"type": "list", "url": HOME, "attempts": 4}},
                              offers={sale.key: sale.to_dict()}, cycle_complete=True)
        collector = self.collector("verification", self.plan(sale))
        with patch.object(collector, "seed", side_effect=AssertionError), patch(
                "sale_monitor.runner.parse_product", return_value=offer(kind="comparison")):
            result = collector.collect(3)
        self.assertEqual(result["queue"], previous["queue"])
        self.assertTrue(result["cycle_complete"])
        self.assertEqual(result["offers"][sale.key]["discovery_kind"], "sale")
        self.assertEqual(result["journal"][-1]["run_id"], "run1")
        receipt = next(iter(result["verification"]["results"].values()))
        self.assertEqual(receipt, {"url": SALE_URL, "plan_identity": sale.identity, "type": "product", "result": "ok", "reason": None})
        self.assertEqual(result["verification_queue"], {})
        self.assertEqual(self.disk.load("stores/ark.json", {})["queue"], previous["queue"])

    def test_killed_stage_resumes_saved_queue_and_successes_without_restarting(self):
        first, second = offer(), offer(PEER_URL)
        previous = self.state(queue={"discovery": {"type": "list", "url": HOME, "attempts": 9}})
        plan = self.plan(first, second)
        client = FakeClient({PEER_URL: KeyboardInterrupt()})
        collector = self.collector("verification", plan, client)
        with patch("sale_monitor.runner.parse_product", return_value=first), self.assertRaises(KeyboardInterrupt):
            collector.collect(3)
        checkpoint = self.disk.load("stores/ark.json", {})
        self.assertEqual(checkpoint["queue"], previous["queue"])
        self.assertEqual(len(checkpoint["verification"]["results"]), 1)
        self.assertEqual(len(checkpoint["verification_queue"]), 1)
        resumed = self.collector("verification", plan)
        with patch("sale_monitor.runner.parse_product", return_value=second):
            result = resumed.collect(3)
        self.assertEqual(resumed.client.calls, [PEER_URL])
        self.assertEqual(len(result["verification"]["results"]), 2)
        self.assertEqual(len(result["journal"]), 2)
        self.assertEqual(result["queue"], previous["queue"])

    def test_unreached_work_is_counted_cleared_and_not_recreated(self):
        planned = self.plan(offer(), offer(PEER_URL))
        self.state(cycle_complete=False)
        result = self.collector("verification", planned).collect(0)
        self.assertEqual(result["verification"]["not_reached"], 2)
        self.assertEqual(result["verification_queue"], {})
        self.assertFalse(result["cycle_complete"])
        resumed = self.collector("verification", planned)
        self.assertEqual(resumed.state["verification_queue"], {})
        self.assertEqual(resumed.collect(0)["verification"]["not_reached"], 2)
        next_run = self.collector("verification", {"confirm": {}, "explore": {}}, run="run2")
        self.assertEqual(next_run.collect(0)["verification"]["not_reached"], 0)

    def test_verification_failure_has_url_receipt_and_no_debt(self):
        sale = offer()
        result = self.collector("verification", self.plan(sale), FakeClient({SALE_URL: FetchError("http_404")})).collect(3)
        self.assertEqual(result["verification"]["failed_urls"], [SALE_URL])
        self.assertEqual(next(iter(result["verification"]["results"].values()))["reason"], "http_404")
        self.assertEqual(result["verification"]["not_reached"], 0)
        self.assertEqual(result["verification_queue"], {})

    def test_budget_exhaustion_stops_without_attempt_or_error(self):
        self.cfg["stores"]["ark"]["seed_urls"] = []
        self.state(queue={"one": {"type": "product", "url": SALE_URL, "kind": "sale", "attempts": 4,
                                  "created_at": iso()}})
        client = FakeClient(budget=HostBudget({}, 0))
        collector = self.collector("discovery", client=client)
        result = collector.collect(3)
        self.assertEqual(result["queue"]["one"]["attempts"], 4)
        self.assertEqual(result["errors"], [])
        self.assertNotIn("one", collector.attempted)
        self.assertEqual(client.calls, [SALE_URL])
        self.assertEqual(result["budget"]["run_id"], "run1")

    def test_budget_usage_is_saved_with_exhausted_hosts(self):
        host = "www.ark-pc.co.jp"
        client = FakeClient(budget=HostBudget({}, 2, used={host: 1}))
        result = self.collector("verification", self.plan(offer()), client).collect(3)
        self.assertEqual(result["budget"], {"run_id": "run1", "phase": "verification", "used": {host: 1}, "exhausted_hosts": [host]})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["verification"]["not_reached"], 1)

    def test_robots_denial_removes_task_without_failure_debt(self):
        self.cfg["stores"]["ark"]["seed_urls"] = []
        self.state(queue={"one": {"type": "product", "url": SALE_URL, "kind": "sale", "created_at": iso(), "attempts": 0}})
        result = self.collector("discovery", client=FakeClient({SALE_URL: FetchError("robots_disallowed")})).collect(3)
        self.assertEqual(result["queue"], {})
        self.assertEqual(result["errors"], [])
        record = result["robots_disallowed"][0]
        self.assertEqual({k: record[k] for k in ("url", "task_id", "run_id")}, {"url": SALE_URL, "task_id": "one", "run_id": "run1"})

    def test_empty_budget_is_not_misreported_as_robots_denial(self):
        fetch = Mock()
        budget = HostBudget({}, 0)
        client = Client(budget=budget, robots=Robots(fetch, budget))
        with self.assertRaisesRegex(FetchError, "host_budget_exhausted"):
            client.get(SALE_URL)
        fetch.assert_not_called()

    def test_http_and_browser_successes_have_distinct_shared_cache_keys(self):
        collector = self.collector("discovery")
        browser = collector._rendered_page(SALE_URL)
        http = collector.page(SALE_URL, allow_browser=False)
        self.assertNotEqual(browser.body, http.body)
        self.assertIs(collector._rendered_page(SALE_URL), browser)
        self.assertIs(collector.page(SALE_URL), http)
        self.assertEqual(collector.client.count, 2)

    def test_koubou_exploration_uses_existing_adapter_and_propagates_identity(self):
        planned = {"confirm": {}, "explore": {"koubou": [{"query": "MODEL-A", "identity": "identity"}]}}
        cfg = {"stores": {"koubou": {"adapter": "html", "seed_urls": []}}}
        collector = Collector(self.root, "koubou", cfg, "run1", FakeClient(), phase="verification", plan=planned)
        product_url = "https://www.pc-koubou.jp/products/detail.php?product_id=123"
        with patch("sale_monitor.runner.koubou_search.parse_search", return_value=(
                [{"url": product_url, "kind": "comparison"}], None, {})), patch(
                "sale_monitor.runner.parse_product", return_value=offer(url=product_url, store="koubou")), patch(
                "sale_monitor.runner.collect_flyer", side_effect=AssertionError):
            result = collector.collect(3)
        self.assertEqual(result["verification"]["searched_identities"], ["identity"])
        self.assertEqual({r["plan_identity"] for r in result["verification"]["results"].values()}, {"identity"})
        self.assertEqual(result["queue"], {})

    def test_search_form_success_alone_does_not_claim_completed_exploration(self):
        search_url = "https://www.ark-pc.co.jp/search/?q=MODEL-A"
        planned = {"confirm": {}, "explore": {"ark": [{"query": "MODEL-A", "identity": "identity"}]}}
        collector = self.collector("verification", planned, FakeClient({search_url: FetchError("http_404")}))
        with patch("sale_monitor.runner.search_form", return_value=search_url):
            result = collector.collect(3)
        self.assertEqual(result["verification"]["searched_identities"], [])
        self.assertEqual(result["verification"]["failed_urls"], [search_url])

    def test_successful_result_listing_records_search_and_descendant_receipts(self):
        search_url = "https://www.ark-pc.co.jp/search/?q=MODEL-A"
        planned = {"confirm": {}, "explore": {"ark": [{"query": "MODEL-A", "identity": "identity"}]}}
        collector = self.collector("verification", planned)
        with patch("sale_monitor.runner.search_form", return_value=search_url), patch(
                "sale_monitor.runner.discover", return_value=([{"url": SALE_URL, "kind": "comparison"}], [], [])), patch(
                "sale_monitor.runner.parse_product", return_value=offer(kind="comparison")):
            result = collector.collect(3)
        self.assertEqual(result["verification"]["searched_identities"], ["identity"])
        self.assertEqual({row["type"] for row in result["verification"]["results"].values()}, {"search", "list", "product"})
        self.assertEqual({row["plan_identity"] for row in result["verification"]["results"].values()}, {"identity"})


if __name__ == "__main__":
    unittest.main()
