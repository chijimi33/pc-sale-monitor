"""Offline contracts for the intended operational route configuration."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase

from sale_monitor import migrate, routes
from sale_monitor.satellite import validation
from sale_monitor.storage import Store


ROOT = Path(__file__).resolve().parents[1]


class LiveConfigTests(TestCase):
    def setUp(self):
        self.config = routes.load_config(ROOT / "config/routes.json")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = Store(Path(self.tmp.name))
        self.routes = routes.RouteState(self.disk, self.config, now=1000)

    def test_scope_and_revision(self):
        self.assertEqual(self.config["config_revision"], 3)
        self.assertEqual(self.config["scope"]["version"], 2)
        self.assertCountEqual(self.config["scope"]["in_scope"],
                              ["dospara:direct", "ark:direct", "koubou:direct", "sofmap:direct"])
        self.assertEqual(self.config["scope"]["optional"], ["tsukumo:direct"])

    def test_tsukumo_satellite_route_and_runner_defaults(self):
        route = self.config["routes"]["tsukumo:direct"]
        self.assertEqual(route["executor"], "satellite")
        for function in ("sale_discovery", "product_verify"):
            self.assertEqual(route["functions"][function]["status"], "active")
        self.assertNotEqual(route["functions"]["comparison_lookup"]["status"], "active")
        self.assertNotIn("probe", route)
        self.assertEqual(route["cleared_refusals"], ["f4c7e5d5cb4b48708aede54dbfa67179"])
        for route_id, other in self.config["routes"].items():
            if route_id != "tsukumo:direct":
                with self.subTest(route_id=route_id):
                    self.assertEqual(other.get("executor", "runner"), "runner")

    def test_active_stores_are_partitioned_by_executor(self):
        self.assertCountEqual(self.routes.active_stores("sale_discovery", executor="runner"),
                              ["dospara", "ark", "koubou", "sofmap"])
        self.assertEqual(self.routes.active_stores("sale_discovery", executor="satellite"), ["tsukumo"])

    def test_tsukumo_effective_config_and_interval(self):
        self.assertGreaterEqual(self.config["hosts"]["shop.tsukumo.co.jp"]["min_interval_seconds"], 5)
        sources = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
        effective = validation.build_effective_config(sources, self.config, "tsukumo")
        self.assertGreaterEqual(validation.validate_effective_config(effective, "tsukumo"), 5)

    def test_migration_keeps_active_satellite_work_and_only_archives_comparison_debt(self):
        # Migration uses sale_discovery status, regardless of the route executor.
        sale = {"type": "product", "kind": "sale", "attempts": 7, "extra": {"nested": [1, 2]}}
        comparison = {"type": "product", "kind": "comparison", "attempts": 3}
        self.disk.save("stores/tsukumo.json", {
            "store": "tsukumo", "queue": {"sale": sale, "comparison": comparison}, "pending_count": 2,
        })

        result = migrate.apply(self.disk.root, self.routes, now=1000)

        self.assertEqual(result["archived"], {"tsukumo": {"comparison_debt_abolished": 1}})
        self.assertEqual(result["remaining"], {"tsukumo": 1})
        state = self.disk.load("stores/tsukumo.json", {})
        self.assertEqual(state["queue"], {"sale": sale})
        self.assertEqual(state["pending_count"], 1)
        archive = self.disk.load("archive/tsukumo.json", {})["entries"]
        self.assertEqual(set(archive), {"comparison"})
        self.assertEqual(archive["comparison"]["task"], comparison)
        self.assertEqual(archive["comparison"]["reason"], "comparison_debt_abolished")
