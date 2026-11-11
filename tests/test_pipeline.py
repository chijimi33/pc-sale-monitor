"""Offline orchestration, real process trees and publication integration."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from sale_monitor import pipeline
from sale_monitor.models import STORES, Offer, iso
from sale_monitor.storage import Store, atomic_json, read_json
from test_runner_phases import FakeClient


NOW = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
RUN = "pipeline-run"
URLS = {"ark": "https://www.ark-pc.co.jp/i/111/", "sofmap": "https://www.sofmap.com/product_detail.aspx?sku=123",
        "dospara": "https://www.dospara.co.jp/IC123.html", "tsukumo": "https://shop.tsukumo.co.jp/goods/123"}


def item(store, price=10000, **updates):
    data = dict(store=store, product_id="fixture", url=URLS[store], title="Fixture", model="MODEL-A",
                brand="Brand", seller_id=store, condition="new", stock="in_stock", price_yen=price,
                shipping_yen=0, points_yen=0, verified=True, observed_at=iso(NOW), observed_run_id=RUN,
                evidence=[{"url": URLS[store]}])
    data.update(updates)
    return Offer(**data)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pipeline-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.public = self.base / "public"
        self.disk = Store(self.root)
        self.config_path = self.base / "sources.json"
        self.routes_path = self.base / "routes.json"
        self.config = {"stores": {s: {"adapter": "html", "seed_urls": [], "fixture_store": s} for s in STORES}}
        self.route_config = {"schema_version": 1, "config_revision": 1,
            "scope": {"version": 1, "in_scope": [s + ":direct" for s in ("ark", "sofmap", "dospara")]},
            "run": {"max_minutes": 90, "discovery_minutes": 1, "verification_minutes": 1, "exploration_share": .2},
            "hosts": {"default": {"min_interval_seconds": 2}}, "routes": {}}
        for store in STORES:
            channel = pipeline._channel(store)
            active = store in ("ark", "sofmap", "dospara")
            self.route_config["routes"][f"{store}:{channel}"] = {"store": store, "channel": channel,
                "functions": {function: {"status": "active" if active else "paused_refused"}
                              for function in ("sale_discovery", "product_verify", "comparison_lookup")}}
            if active:
                self.config["stores"][store]["seed_urls"] = [URLS[store]]
        self.save_config()
        self.disk.save("schema.json", {"state_schema_version": 2})

    def save_config(self):
        atomic_json(self.config_path, self.config)
        atomic_json(self.routes_path, self.route_config)

    def run_pipeline(self, spawn=None, **kwargs):
        return pipeline.run(self.root, self.public, RUN, config_path=self.config_path,
                            routes_path=self.routes_path, now=NOW, spawn=spawn or (lambda *a, **k: SimpleNamespace(poll=lambda: 0)), **kwargs)

    def save_offer(self, offer, run_id="old"):
        state = {"store": offer.store, "schema_version": 1, "run_id": run_id, "offers": {offer.key: offer.to_dict()},
                 "journal": [], "queue": {}, "done": [], "cycle_complete": True, "status": "complete"}
        self.disk.save(f"stores/{offer.store}.json", state)
        return state

    def plan_file(self, offer):
        path = self.root / "plans" / f"{RUN}.json"
        atomic_json(path, {"run_id": RUN, "confirm": {offer.store: [{"url": offer.url, "offer_key": offer.key,
                    "identity": offer.identity}]}, "explore": {}})
        return path


class StageTests(Fixture):
    def stage(self, store="ark", phase="discovery", plan_path=None):
        return pipeline.stage(self.root, store, phase, RUN, 2, self.config_path, self.routes_path, plan_path)

    def test_paused_store_has_zero_client_creation_and_no_writes(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch("sale_monitor.pipeline.Client", side_effect=AssertionError):
            self.assertEqual(self.stage("tsukumo"), {"store": "tsukumo", "phase": "discovery", "status": "skipped"})
            self.assertEqual(self.stage("tsukumo", "verification")["status"], "skipped")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_same_run_budget_usage_is_restored_and_next_run_gets_new_budget(self):
        host = "www.ark-pc.co.jp"
        value = self.save_offer(item("ark"))
        value["budget"] = {"run_id": RUN, "phase": "discovery", "used": {host: 1}, "exhausted_hosts": [host]}
        self.disk.save("stores/ark.json", value)
        clients = []

        def make_client(**kwargs):
            client = FakeClient(**kwargs)
            clients.append(client)
            return client

        with patch("sale_monitor.pipeline.Client", side_effect=make_client), patch(
                "sale_monitor.runner.parse_product", return_value=item("ark")):
            result = self.stage()
        self.assertEqual(result["budget"]["used"], {host: 1})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(clients[0].calls), 1)
        self.assertEqual(next(iter(result["queue"].values()))["attempts"], 0)
        with patch("sale_monitor.pipeline.Client", side_effect=make_client), patch(
                "sale_monitor.runner.parse_product", return_value=item("ark")), patch(
                "sale_monitor.runner.discover", return_value=([{"url": URLS["ark"], "kind": "sale"}], [], [])):
            result = pipeline.stage(self.root, "ark", "discovery", "next-run", 10,
                                    self.config_path, self.routes_path)
        self.assertGreater(result["budget"]["used"][host], 0)
        self.assertEqual(result["budget"]["run_id"], "next-run")

    def test_verification_resumes_its_own_usage_within_the_same_run(self):
        peer = item("ark", discovery_kind="comparison")
        state = self.save_offer(peer)
        state["budget"] = {"run_id": RUN, "phase": "verification", "used": {"www.ark-pc.co.jp": 1}}
        self.disk.save("stores/ark.json", state)
        with patch("sale_monitor.pipeline.Client", side_effect=FakeClient):
            result = self.stage(phase="verification", plan_path=self.plan_file(peer))
        self.assertEqual(result["budget"]["used"], {"www.ark-pc.co.jp": 1})
        self.assertEqual(result["verification"]["not_reached"], 1)
        self.assertEqual(result["errors"], [])

    def test_exhausted_discovery_budget_does_not_starve_verification(self):
        # Each stage is given its own time budget. Carrying discovery usage
        # over would stop verification before its first planned request.
        peer = item("ark", discovery_kind="comparison")
        state = self.save_offer(peer)
        state["budget"] = {"run_id": RUN, "phase": "discovery", "used": {"www.ark-pc.co.jp": 1},
                           "exhausted_hosts": ["www.ark-pc.co.jp"]}
        self.disk.save("stores/ark.json", state)
        with patch("sale_monitor.pipeline.Client", side_effect=FakeClient), patch(
                "sale_monitor.runner.parse_product", return_value=peer):
            result = self.stage(phase="verification", plan_path=self.plan_file(peer))
        self.assertEqual(result["budget"]["phase"], "verification")
        self.assertEqual(result["verification"]["not_reached"], 0)
        self.assertEqual([row["result"] for row in result["verification"]["results"].values()], ["ok"])

    def test_missing_or_wrong_run_verification_plan_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "verification_plan_required"):
            self.stage(phase="verification")
        path = self.plan_file(item("ark"))
        plan = read_json(path, {})
        plan["run_id"] = "other"
        atomic_json(path, plan)
        with self.assertRaisesRegex(ValueError, "verification_plan_required"):
            self.stage(phase="verification", plan_path=path)

    def test_inactive_lookup_does_not_seed_exploration(self):
        self.route_config["routes"]["ark:direct"]["functions"]["comparison_lookup"]["status"] = "paused_refused"
        self.save_config()
        path = self.plan_file(item("sofmap"))
        plan = read_json(path, {})
        plan["explore"] = {"ark": [{"query": "MODEL-A", "identity": "identity"}]}
        atomic_json(path, plan)
        with patch("sale_monitor.pipeline.Client", side_effect=FakeClient):
            result = self.stage(phase="verification", plan_path=path)
        self.assertEqual(result["verification"]["results"], {})

    def test_robots_policy_cached_across_stages_within_run(self):
        from sale_monitor.http import Client
        host = "www.ark-pc.co.jp"
        self.config["stores"]["ark"]["seed_urls"] = []
        self.save_config()
        state = self.save_offer(item("ark", discovery_kind="comparison"))
        state["robots"] = {"run_id": RUN, "responses": {host: [200, "User-agent: PCSaleMonitor\nDisallow: /private\nCrawl-delay: 3"]}}
        self.disk.save("stores/ark.json", state)
        clients = []

        def make_client(**kwargs):
            kwargs["budget"].sleep = lambda seconds: None
            client = Client(**kwargs)
            response = MagicMock(status=200, url=URLS["ark"], headers={})
            response.__enter__.return_value = response
            response.read.return_value = b"fixture"
            client.opener.open = Mock(return_value=response)
            clients.append(client)
            return client

        with patch("sale_monitor.pipeline._fetch_robots", side_effect=AssertionError), patch(
                "sale_monitor.pipeline.Client", side_effect=make_client), patch(
                "sale_monitor.runner.parse_product", return_value=item("ark")):
            result = pipeline.stage(self.root, "ark", "verification", RUN, 60,
                                    self.config_path, self.routes_path, self.plan_file(item("ark")))
        self.assertEqual(result["robots"]["responses"], state["robots"]["responses"])
        self.assertEqual(result["budget"]["used"], {host: 1})
        self.assertEqual(clients[0].budget.interval(host), 3)
        clients[0].opener.open.assert_called_once()

    def test_robots_raw_fetch_has_exact_user_agent_no_redirects_and_timeout(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = b"User-agent: *"
        opener = Mock()
        opener.open.return_value = response
        with patch("sale_monitor.pipeline.build_opener", return_value=opener) as build:
            self.assertEqual(pipeline._fetch_robots("https://example.invalid/robots.txt"), (200, "User-agent: *"))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), pipeline.USER_AGENT)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 15})
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, None, None, "https://example.invalid/other"))


class PipelineTests(Fixture):
    def test_stage_D_expands_shared_receipts_and_passes_observed_keys_including_legacy(self):
        first, second = item("ark"), item("dospara", model="OTHER")
        peer = item("sofmap", discovery_kind="comparison")
        requested = URLS["sofmap"] + "&requested=1"
        identities = sorted([first.identity, second.identity])
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                for current in (first, second, peer):
                    self.save_offer(current, RUN)
                receipt = {"type": "product", "result": "ok", "url": requested,
                           "observed_offer_key": peer.key, "observed_url": peer.url,
                           "plan_identity": identities[0]}
                if not legacy:
                    receipt["plan_identities"] = identities
                state = self.disk.load("stores/sofmap.json", {})
                state["verification"] = {"run_id": RUN, "results": {"shared": receipt}}
                self.disk.save("stores/sofmap.json", state)
                tracking = {identity: {"sofmap": {"unverified_urls": [requested]}} for identity in identities}
                self.disk.save("comparison_tracking.json", tracking)
                with patch("sale_monitor.pipeline.planner.update_tracking", wraps=pipeline.planner.update_tracking) as update:
                    self.run_pipeline()
                verified = update.call_args.kwargs["discoveries"]["sofmap"]["verified"]
                expected_identities = identities[:1] if legacy else identities
                self.assertEqual(verified, [{"identity": identity, "url": requested, "offer_key": peer.key}
                                            for identity in expected_identities])
                result = self.disk.load("comparison_tracking.json", {})
                for identity in expected_identities:
                    values = result[identity]["sofmap"]
                    self.assertEqual(values["unverified_urls"], [])
                    reason = "became_known_comparator" if identity == peer.identity else "identity_mismatch"
                    self.assertEqual(values["settled_urls"][0]["reason"], reason)

    def test_planning_and_evaluation_include_unarchived_old_run_low(self):
        self.save_offer(item("ark", 10000), RUN)
        self.save_offer(item("dospara", 11000), RUN)
        high = item("ark", 12000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=2)))
        low = item("ark", 8000, observed_run_id="failed", observed_at=iso(NOW - timedelta(days=1)))
        self.disk.save("history/ark/old.json", [{"observation_id": "high", "run_id": "old", "offer": high.to_dict()}])
        self.disk.save("stores/bic.json", {"journal": [{"observation_id": "low", "run_id": "failed", "offer": low.to_dict()}]})
        self.run_pipeline()
        planned = self.disk.load(f"plans/{RUN}.json", {})
        candidate = next(row for row in planned["candidates"] if row["store"] == "ark")
        self.assertNotEqual(candidate["history_status"], "met")
        decision = next(row for row in read_json(self.public / "evidence.json", {})["decisions"] if row["offer"]["store"] == "ark")
        self.assertNotEqual(decision["payment"]["status"], "accepted")
        self.assertTrue(any(row["observation_id"] == "low" for row in self.disk.history()))

    def test_paused_in_scope_verification_remains_in_bundle_without_budget(self):
        self.route_config["routes"]["sofmap:direct"]["functions"]["product_verify"]["status"] = "paused_maintenance"
        self.config["stores"]["sofmap"]["seed_urls"] = []
        self.save_config()
        self.save_offer(item("ark", 9000), RUN)
        self.save_offer(item("dospara", 11000), RUN)
        self.save_offer(item("tsukumo", 11000), RUN)
        self.save_offer(item("sofmap", 8000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=1))))
        self.run_pipeline()
        planned = self.disk.load(f"plans/{RUN}.json", {})
        candidate = next(row for row in planned["candidates"] if row["store"] == "ark")
        self.assertEqual(candidate["status"], "comparator_unverifiable")
        self.assertNotIn("sofmap", planned["confirm"])
        self.assertNotIn("sofmap", planned["summary"]["confirm_budget"])
        decision = next(row for row in read_json(self.public / "evidence.json", {})["decisions"] if row["offer"]["store"] == "ark")
        self.assertIn("known_comparator_not_verified_this_run", decision["payment"]["reasons"])

    def test_interrupted_verification_preserves_discoveries_and_is_idempotent(self):
        identity = item("ark").identity
        queue = {"pending": {"type": "product", "plan_identity": identity, "url": "pending"},
                 "failed": {"type": "product", "plan_identity": identity, "url": "failed"},
                 "confirm": {"type": "product", "plan_identity": identity, "url": "confirm", "plan_offer_key": None}}
        state = self.save_offer(item("sofmap"))
        state.update(queue={"discovery": {"keep": 17}}, verification_queue=queue, verification={
            "run_id": RUN, "results": {"failed": {"result": "failed", "reason": "http_404"}}, "not_reached": 0})
        self.disk.save("stores/sofmap.json", state)
        for _ in range(2):
            pipeline._finish_interrupted_verification(self.disk, "sofmap", RUN)
        result = self.disk.load("stores/sofmap.json", {})
        self.assertEqual(result["verification"]["unverified_discoveries"], [
            {"identity": identity, "url": "failed", "reason": "http_404"},
            {"identity": identity, "url": "pending", "reason": "not_reached"}])
        self.assertEqual(result["verification"]["not_reached"], 2)
        self.assertEqual(result["queue"], state["queue"])
        self.assertEqual(result["verification_queue"], {})

    def test_schema_mismatch_writes_nothing_and_never_spawns_or_probes(self):
        self.disk.save("schema.json", {"state_schema_version": 1})
        before = {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        with patch("sale_monitor.pipeline.Client", side_effect=AssertionError), self.assertRaisesRegex(
                RuntimeError, "state_migration_required"):
            self.run_pipeline(spawn=Mock(side_effect=AssertionError))
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()})

    def test_due_probes_run_once_and_refused_probe_is_never_automatic(self):
        for store, status in (("tsukumo", "paused_maintenance"), ("bic", "paused_refused")):
            route = self.route_config["routes"][store + ":direct"]
            route["probe"] = {"url": "https://example.invalid/", "interval_hours": 24, "successes_to_resume": 2}
            for function in route["functions"].values():
                function["status"] = status
        self.save_config()
        probe_result = {"ok": True, "explicit_refusal": False, "summary": "probe_ok"}
        with patch("sale_monitor.pipeline.routes.probe", return_value=probe_result) as probe:
            self.run_pipeline()
        self.assertEqual([call.args[1] for call in probe.call_args_list], ["tsukumo:direct"])
        saved = self.disk.load("routes.json", {})
        self.assertEqual(saved["routes"]["tsukumo:direct"]["last_probe"]["summary"], "probe_ok")

    def test_probe_candidate_does_not_start_paused_store_children(self):
        route = self.route_config["routes"]["tsukumo:direct"]
        route["probe"] = {"url": "https://example.invalid/robots.txt", "interval_hours": 24, "successes_to_resume": 2}
        for function in route["functions"].values():
            function["status"] = "paused_maintenance"
        self.save_config()
        state = pipeline.routes.RouteState(self.disk, self.route_config, now=NOW)
        state.record_probe("tsukumo:direct", {"ok": True, "explicit_refusal": False, "summary": "ok"},
                           NOW - timedelta(days=1))
        state.save()
        spawn = Mock(side_effect=lambda *a, **k: SimpleNamespace(poll=lambda: 0))
        with patch("sale_monitor.pipeline.routes.probe", return_value={
                "ok": True, "explicit_refusal": False, "summary": "robots_ok"}):
            self.run_pipeline(spawn=spawn)
        summary = self.disk.load("routes.json", {})["routes"]["tsukumo:direct"]
        self.assertEqual(summary["recovery_candidate"]["consecutive_successes"], 2)
        self.assertEqual({row["status"] for row in summary["functions"].values()}, {"paused_maintenance"})
        self.assertNotIn("tsukumo", {call.args[0][call.args[0].index("--store") + 1] for call in spawn.call_args_list})

    def test_parallel_children_skip_paused_stores_and_keep_failure_exit_codes(self):
        calls = []

        def spawn(command, **kwargs):
            calls.append((command, kwargs))
            store = command[command.index("--store") + 1]
            return SimpleNamespace(poll=lambda: 7 if store == "sofmap" else 0)

        result = self.run_pipeline(spawn=spawn)
        self.assertEqual({cmd[cmd.index("--store") + 1] for cmd, _ in calls}, {"ark", "sofmap", "dospara"})
        self.assertTrue(all((kw.get("start_new_session") if os.name == "posix" else kw.get("creationflags")) for _, kw in calls))
        self.assertEqual(result["run"]["stage_results"]["sofmap"]["discovery"], {"status": "failed", "exit_code": 7})
        self.assertTrue((self.root / "plans" / f"{RUN}.json").exists())
        self.assertTrue((self.root / "comparison_tracking.json").exists())
        self.assertTrue((self.public / "latest.json").exists())

    def test_one_hang_is_stopped_and_other_results_are_published(self):
        processes = {}
        ticks = iter(range(100))

        def spawn(command, **kwargs):
            store = command[command.index("--store") + 1]
            process = SimpleNamespace(poll=lambda: None if store == "sofmap" else 0, returncode=None)
            processes[store] = process
            return process

        with patch("sale_monitor.pipeline.DEADLINE_GRACE", 0), patch(
                "sale_monitor.pipeline.time.sleep"), patch("sale_monitor.pipeline._stop_process_tree") as stop:
            result = self.run_pipeline(spawn=spawn, clock=lambda: next(ticks) * 100)
        stop.assert_called_once_with(processes["sofmap"])
        self.assertEqual(result["run"]["stage_results"]["sofmap"]["discovery"]["status"], "deadline")
        self.assertEqual(result["run"]["stage_results"]["ark"]["discovery"]["status"], "completed")
        self.assertTrue((self.public / "notifications.json").exists())

    def test_confirmed_empty_collection_is_success_with_structured_stage_results(self):
        def spawn(command, **kwargs):
            store = command[command.index("--store") + 1]
            self.disk.save(f"stores/{store}.json", {"store": store, "run_id": RUN, "offers": {}, "journal": [],
                "queue": {}, "done": [], "cycle_complete": True, "status": "complete", "list_pages": 1,
                "listed_candidates": 0, "errors": []})
            return SimpleNamespace(poll=lambda: 0)

        result = self.run_pipeline(spawn=spawn)
        self.assertTrue(result["run_outcome"]["successful"])
        self.assertEqual(result["run_outcome"]["confirmed_empty_results"], 3)

    def test_deadline_cleans_verification_checkpoint_only(self):
        state = self.save_offer(item("sofmap"))
        state.update(queue={"discovery": {"keep": 17}}, verification_queue={"unreached": {}, "failed": {}},
                     verification={"run_id": RUN, "results": {"failed": {"result": "failed"}}, "not_reached": 2})
        self.disk.save("stores/sofmap.json", state)
        pipeline._finish_interrupted_verification(self.disk, "sofmap", RUN)
        result = self.disk.load("stores/sofmap.json", {})
        self.assertEqual(result["verification_queue"], {})
        self.assertEqual(result["verification"]["not_reached"], 3)
        self.assertEqual(result["queue"], state["queue"])
        self.assertEqual(result["journal"], state["journal"])

    def test_aggregate_failure_leaves_states_plan_and_tracking_on_disk(self):
        state = self.save_offer(item("ark"), RUN)
        with patch("sale_monitor.pipeline.reporting.aggregate", side_effect=RuntimeError("publish_failed")), self.assertRaisesRegex(
                RuntimeError, "publish_failed"):
            self.run_pipeline()
        self.assertEqual(self.disk.load("stores/ark.json", {}), state)
        self.assertTrue((self.root / "plans" / f"{RUN}.json").exists())
        self.assertTrue((self.root / "comparison_tracking.json").exists())

    def test_live_clock_advances_before_planning_and_publication(self):
        times = iter([NOW, NOW + timedelta(seconds=30), NOW + timedelta(seconds=60)])

        def spawn(command, **kwargs):
            store = command[command.index("--store") + 1]
            self.save_offer(item(store, observed_at=iso(NOW + timedelta(seconds=10))), RUN)
            return SimpleNamespace(poll=lambda: 0)

        with patch("sale_monitor.pipeline.utcnow", side_effect=lambda: next(times)):
            result = pipeline.run(self.root, self.public, RUN, config_path=self.config_path,
                                  routes_path=self.routes_path, spawn=spawn)
        self.assertEqual(result["generated_at"], iso(NOW + timedelta(seconds=60)))
        self.assertEqual(self.disk.load(f"plans/{RUN}.json", {})["summary"]["candidates"], 3)
        self.assertEqual(result["run_outcome"]["current_observations"], 3)

    def test_actual_successful_child_exit_is_recorded(self):
        with patch("sale_monitor.pipeline.DEADLINE_GRACE", .2):
            result = pipeline._run_children(["ark"], "discovery", 5, self.root, RUN,
                self.config_path, self.routes_path,
                lambda command, **kwargs: subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"], **kwargs),
                time.monotonic)
        self.assertEqual(result, {"ark": {"status": "completed", "exit_code": 0}})


class VerticalPipeline(Fixture):
    def discovery_url_roundtrip(self, final_price):
        self.route_config["hosts"]["default"]["min_interval_seconds"] = 20
        self.route_config["run"]["exploration_share"] = 1
        self.save_config()
        first_url = URLS["sofmap"] + "&discovered=1"
        extra_url = URLS["sofmap"] + "&discovered=2"
        run_id, observed_at, pending_url = RUN, NOW, None
        reached = []

        def spawn(command, **kwargs):
            store = command[command.index("--store") + 1]
            phase = command[command.index("--phase") + 1]
            plan_path = Path(command[command.index("--plan") + 1]) if "--plan" in command else None
            pipeline.stage(self.root, store, phase, run_id, 60, self.config_path, self.routes_path, plan_path)
            return SimpleNamespace(poll=lambda: 0)

        def client(**kwargs):
            kwargs["budget"].sleep = lambda seconds: None
            return FakeClient(**kwargs)

        def discover(page, cfg, **kwargs):
            store = cfg["fixture_store"]
            if kwargs.get("comparison"):
                urls = [URLS[store], extra_url] if store == "sofmap" else [URLS[store]]
                return [{"url": url, "kind": "comparison"} for url in urls], [], []
            return ([{"url": page.url, "kind": "sale"}] if store == "ark" else []), [], []

        def parse(store, page, cfg, task):
            reached.append((run_id, page.url))
            price = 9000 if store == "ark" else final_price if page.url == pending_url else 11000
            return item(store, price, url=page.url, product_id="extra" if page.url == extra_url else "fixture",
                        observed_at=iso(observed_at), discovery_kind=task.get("kind", "sale"))

        with patch("sale_monitor.pipeline.Client", side_effect=client), patch(
                "sale_monitor.runner.discover", side_effect=discover), patch(
                "sale_monitor.runner.sofmap_search.parse_listing", return_value=(
                    [{"url": url, "kind": "comparison"} for url in (first_url, extra_url)], [], {})), patch(
                "sale_monitor.runner.search_form", side_effect=lambda page, query: page.url + "&search=1"), patch(
                "sale_monitor.runner.parse_product", side_effect=parse):
            self.run_pipeline(spawn=spawn, clock=lambda: 0)
            first = next(row for row in read_json(self.public / "evidence.json", {})["decisions"] if row["offer"]["store"] == "ark")
            self.assertEqual(first["payment"]["provisional_rule"], "A")
            self.assertEqual(first["payment"]["reasons"], ["in_scope_comparator_url_not_verified"])
            self.assertFalse(read_json(self.public / "notifications.json", {})["events"])
            tracking = self.disk.load("comparison_tracking.json", {})
            pending_url, = tracking[item("ark").identity]["sofmap"]["unverified_urls"]
            self.assertNotIn((RUN, pending_url), reached)

            run_id, observed_at = RUN + "2", NOW + timedelta(hours=1)
            self.route_config["run"]["exploration_share"] = 0
            self.save_config()
            pipeline.run(self.root, self.public, run_id, config_path=self.config_path, routes_path=self.routes_path,
                         now=observed_at, spawn=spawn, clock=lambda: 0)
        planned = self.disk.load(f"plans/{run_id}.json", {})
        request = next(row for row in planned["confirm"]["sofmap"] if row["url"] == pending_url)
        self.assertIsNone(request["offer_key"])
        self.assertIn((run_id, pending_url), reached)
        self.assertEqual(self.disk.load("comparison_tracking.json", {})[item("ark").identity]["sofmap"]["unverified_urls"], [])
        return next(row for row in read_json(self.public / "evidence.json", {})["decisions"] if row["offer"]["store"] == "ark")

    def test_discovered_url_next_run_cheaper_price_rejects_candidate(self):
        decision = self.discovery_url_roundtrip(8000)
        self.assertEqual(decision["payment"]["status"], "rejected")
        self.assertEqual(decision["payment"]["reasons"], ["cheaper_current_offer"])

    def test_discovered_url_next_run_higher_price_allows_candidate(self):
        decision = self.discovery_url_roundtrip(12000)
        self.assertEqual(decision["payment"]["status"], "accepted")
        self.assertEqual(decision["payment"]["rule"], "A")
        self.assertTrue(any(event["offer"]["store"] == "ark" for event in read_json(self.public / "notifications.json", {})["events"]))

    def execute(self, fail=False, skip_verification=False):
        # Discovery has no comparator sale to refresh at this store.
        self.config["stores"]["sofmap"]["seed_urls"] = [URLS["sofmap"]]
        self.save_config()
        self.save_offer(item("sofmap", 8000, discovery_kind="comparison", observed_run_id="old", observed_at=iso(NOW - timedelta(days=1))))
        self.save_offer(item("tsukumo", 7000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=1))))
        self.disk.save("history/ark/2026-11-10.json", [{"run_id": "old", "offer": item(
            "ark", 11000, observed_run_id="old", observed_at=iso(NOW - timedelta(days=1))).to_dict()}])
        calls = []

        def spawn(command, **kwargs):
            phase = command[command.index("--phase") + 1]
            store = command[command.index("--store") + 1]
            calls.append((phase, store))
            plan_path = Path(command[command.index("--plan") + 1]) if "--plan" in command else None
            if phase == "verification":
                self.assertTrue(plan_path.exists())
            pipeline.stage(self.root, store, phase, RUN, 60, self.config_path, self.routes_path, plan_path)
            return SimpleNamespace(poll=lambda: 0)

        def client(**kwargs):
            kwargs["budget"].sleep = lambda seconds: None
            return FakeClient({URLS["sofmap"]: FetchError("http_404")} if fail else {}, **kwargs)

        def discover(page, cfg, **kwargs):
            if cfg["fixture_store"] == "sofmap":
                return [], [], []
            return [{"url": page.url, "kind": "sale"}], [], []

        def parse(store, page, cfg, task):
            return item(store, {"ark": 9000, "dospara": 11000, "sofmap": 10000}[store],
                        discovery_kind=task.get("kind", "sale"))

        from sale_monitor.http import FetchError
        clock = (lambda: 5200) if skip_verification else (lambda: 0)
        if skip_verification:
            ticks = iter([0, 0, 5200])
            clock = lambda: next(ticks, 5200)
        with patch("sale_monitor.pipeline.Client", side_effect=client), patch(
                "sale_monitor.runner.discover", side_effect=discover), patch(
                "sale_monitor.runner.parse_product", side_effect=parse):
            result = self.run_pipeline(spawn=spawn, clock=clock)
        return result, calls

    def test_discovery_plan_verification_and_publication_are_in_order(self):
        result, calls = self.execute()
        self.assertEqual([p for p, _ in calls], ["discovery"] * 3 + ["verification"])
        self.assertEqual(calls[-1][1], "sofmap")
        state = self.disk.load("stores/sofmap.json", {})
        self.assertEqual(next(iter(state["offers"].values()))["observed_run_id"], RUN)
        self.assertEqual(len([row for row in self.disk.history() if row["run_id"] == RUN and row["offer"]["store"] == "sofmap"]), 1)
        self.assertEqual(state["verification_queue"], {})
        evidence = read_json(self.public / "evidence.json", {})["decisions"]
        candidate = next(row for row in evidence if row["offer"]["store"] == "ark")
        self.assertEqual(candidate["payment"]["status"], "accepted")
        self.assertIn(candidate["payment"]["rule"], ("A", "B_observed_year_low", "B_recent_median"))
        self.assertIn("sofmap", {r["store"] for r in candidate["comparison_scope"]["confirmed"]})
        self.assertIn("tsukumo:direct", {r["route"] for r in candidate["comparison_scope"]["unconfirmed"]})
        self.assertTrue(read_json(self.public / "notifications.json", {})["events"])
        tracking = self.disk.load("comparison_tracking.json", {})
        self.assertEqual(tracking[item("ark").identity]["sofmap"]["last_success_at"], iso(NOW))
        self.assertEqual(result["run"]["stage_results"]["sofmap"]["verification"]["status"], "completed")

    def test_failed_known_comparator_holds_candidate_without_old_price_substitution(self):
        self.execute(fail=True)
        evidence = read_json(self.public / "evidence.json", {})["decisions"]
        candidate = next(row for row in evidence if row["offer"]["store"] == "ark")
        self.assertNotEqual(candidate["payment"]["status"], "accepted")
        self.assertIn("known_comparator_not_verified_this_run", candidate["payment"]["reasons"])
        self.assertFalse(any(r["seller_id"] == "sofmap" for r in candidate["payment"]["comparisons"]))
        state = self.disk.load("stores/sofmap.json", {})
        self.assertEqual(state["verification"]["failed_urls"], [URLS["sofmap"]])
        self.assertEqual(self.disk.load("comparison_tracking.json", {})[item("ark").identity]["sofmap"]["consecutive_failures"], 1)

    def test_verification_skipped_when_less_than_five_minutes_remain(self):
        result, calls = self.execute(skip_verification=True)
        self.assertFalse(any(phase == "verification" for phase, store in calls))
        self.assertEqual(result["run"]["stage_results"]["sofmap"]["verification"]["status"], "skipped")


class RealProcessTrees(unittest.TestCase):
    def test_real_child_group_is_killed_and_missing_group_is_ignored(self):
        for error in (None, ProcessLookupError()):
            with self.subTest(error=error), subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"]) as process:
                process._pipeline_owned_group = True
                with patch("os.killpg", create=True, side_effect=error) as killpg, patch(
                        "os.getpgrp", return_value=-1, create=True):
                    process.wait(timeout=5)
                    pipeline._kill_remaining_process_group(process)
                    pipeline._kill_remaining_process_group(process)
                    killpg.assert_called_once_with(process.pid, getattr(signal, "SIGKILL", 9))

    def test_fake_spawn_never_signals_a_process_group(self):
        with patch("os.killpg", create=True) as killpg:
            fake = SimpleNamespace(pid=os.getpid(), poll=lambda: 0)
            result = pipeline._run_children(["ark"], "discovery", 1, "unused", RUN, "unused", "unused",
                                            lambda *args, **kwargs: fake, time.monotonic)
        self.assertEqual(result["ark"]["status"], "completed")
        killpg.assert_not_called()

    def test_posix_timeout_helpers_never_signal_fake_or_unowned_real_groups(self):
        for marked in (False, True):
            fake = Mock(pid=123, _pipeline_owned_group=marked, _pipeline_group_cleaned=False)
            fake.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1)]
            with self.subTest(marked=marked), patch("os.killpg", create=True) as killpg:
                pipeline._stop_posix_process_tree(fake)
                killpg.assert_not_called()
                fake.terminate.assert_called_once_with()
                fake.kill.assert_called_once_with()
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as process, patch(
                "os.killpg", create=True) as killpg:
            pipeline._stop_posix_process_tree(process)
            process.wait(timeout=5)
            killpg.assert_not_called()

    def test_own_group_is_never_signalled_even_with_ownership_marker(self):
        with subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"]) as process:
            process._pipeline_owned_group = True
            with patch("os.getpgrp", return_value=process.pid, create=True), patch("os.killpg", create=True) as killpg:
                self.assertFalse(pipeline._signal_owned_process_group(process, signal.SIGTERM))
                process.wait(timeout=5)
                pipeline._kill_remaining_process_group(process)
                killpg.assert_not_called()

    def test_owned_timeout_sends_final_group_signal_before_wait_and_only_once(self):
        with subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"]) as process:
            process._pipeline_owned_group = True
            calls = []
            with patch("os.getpgrp", return_value=-1, create=True), patch(
                    "os.killpg", side_effect=lambda pid, sig: calls.append(sig), create=True), patch.object(
                    process, "wait", wraps=process.wait) as wait:
                pipeline._stop_posix_process_tree(process)
                wait.assert_not_called()
                process.wait(timeout=5)
                pipeline._kill_remaining_process_group(process)
            self.assertEqual(calls, [signal.SIGTERM, getattr(signal, "SIGKILL", 9)])

    @unittest.skipUnless(os.name == "posix", "POSIX orphaned process-group cleanup")
    def test_exited_worker_reaps_its_term_ignoring_grandchild(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory(prefix="pipeline-exit-") as directory:
                root = Path(directory)
                heartbeat = root / "heartbeat"
                grandchild = ("import signal,time\nfrom pathlib import Path\n"
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                              f"p=Path({str(heartbeat)!r})\n"
                              "while True:\n with p.open('ab') as f: f.write(b'beat\\n')\n time.sleep(.03)\n")
                worker = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                          f"subprocess.Popen([sys.executable,'-c',{grandchild!r}])\n"
                          f"p=Path({str(heartbeat)!r})\n"
                          "while not p.exists(): time.sleep(.01)\n"
                          f"raise SystemExit({exit_code})\n")
                spawned = []

                def spawn(command, **kwargs):
                    process = subprocess.Popen([sys.executable, "-c", worker], **kwargs)
                    spawned.append(process)
                    return process

                started = time.monotonic()
                try:
                    with patch("sale_monitor.pipeline.DEADLINE_GRACE", .1):
                        result = pipeline._run_children(["ark"], "discovery", 3, root, RUN,
                            Path("unused"), Path("unused"), spawn, time.monotonic)
                    self.assertLess(time.monotonic() - started, 8)
                    self.assertEqual(result, {"ark": {
                        "status": "completed" if exit_code == 0 else "failed", "exit_code": exit_code}})
                    self.assertTrue(heartbeat.exists(), "grandchild must start before the worker exits")
                    time.sleep(.1)
                    before = heartbeat.read_bytes()
                    self.assertTrue(before)
                    time.sleep(.2)
                    self.assertEqual(heartbeat.read_bytes(), before)
                finally:
                    for process in spawned:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)

    def test_deadline_stops_real_worker_and_its_grandchild(self):
        with tempfile.TemporaryDirectory(prefix="pipeline-tree-") as directory:
            root = Path(directory)
            child_pid = root / "grandchild.pid"
            heartbeat = root / "heartbeat"
            grandchild = ("import os,time\nfrom pathlib import Path\n"
                          f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
                          f"p=Path({str(heartbeat)!r})\n"
                          "while True:\n p.write_text(str(time.time_ns()))\n time.sleep(.03)\n")
            worker = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(600)")
            spawned = []

            def spawn(command, **kwargs):
                process = subprocess.Popen([sys.executable, "-c", worker], **kwargs)
                spawned.append(process)
                return process

            started = time.monotonic()
            try:
                with patch("sale_monitor.pipeline.DEADLINE_GRACE", .1):
                    result = pipeline._run_children(["ark"], "discovery", 1.5, root, RUN,
                        Path("unused"), Path("unused"), spawn, time.monotonic)
                self.assertLess(time.monotonic() - started, 8)
                self.assertEqual(result["ark"]["status"], "deadline")
                self.assertIsNotNone(spawned[0].poll())
                self.assertTrue(child_pid.exists(), "grandchild must start before the timeout")
                self.assertTrue(heartbeat.exists())
                before = heartbeat.read_bytes()
                time.sleep(.15)
                self.assertEqual(heartbeat.read_bytes(), before)
                pid = int(child_pid.read_text())
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    handle = kernel.OpenProcess(0x1000, False, pid)
                    if handle:
                        try:
                            exit_code = ctypes.c_ulong()
                            self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                            self.assertNotEqual(exit_code.value, 259)
                        finally:
                            kernel.CloseHandle(handle)
                else:
                    proc = Path(f"/proc/{pid}/stat")
                    if proc.exists():
                        self.assertEqual(proc.read_text().split()[2], "Z")
                    else:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
            finally:
                for process in spawned:
                    if process.poll() is None:
                        pipeline._stop_process_tree(process)


if __name__ == "__main__":
    unittest.main()
