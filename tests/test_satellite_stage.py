from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import build_opener

from sale_monitor.http import Client, SafeRedirect
from sale_monitor.runner import Collector
from sale_monitor.satellite.agent import encode
from sale_monitor.satellite.control import ControlState
from sale_monitor.satellite.deadline import Handshake
from sale_monitor.satellite.protocol import sha256_bytes, time_string
from sale_monitor.satellite.stage import run_stage
from sale_monitor.storage import atomic_json, read_json
from test_satellite_agent import inputs_fixture
from test_satellite_deadline import FakeClock
from test_satellite_guard import MemoryHTTPS
from test_satellite_protocol import COMMIT, NOW, ticket_fixture
from test_satellite_validation import config_fixture, offer_fixture
from test_tsukumo_shipping import product_page


LIST = "https://shop.tsukumo.co.jp/special/fixture/"
PRODUCT = product_page(shipping=0)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.work = self.home / "work" / COMMIT
        self.work.mkdir(parents=True)
        self.clock = FakeClock()
        self.handshake = self.clock.handshake()
        self.config = config_fixture()
        self.config["stores"]["tsukumo"]["seed_urls"] = [LIST]
        old = offer_fixture(discovery_kind="comparison").to_dict()
        self.row = dict(observation_id="old-observation", run_id="previous", offer=old)
        self.state = dict(schema_version=1, store="tsukumo", run_id="previous", phase="discovery",
            queue={}, done=[], offers={offer_fixture().key: old}, journal=[self.row], cycle_complete=True)
        self.client = None
        self.transport = None
        self.robots_calls = []
        self.collectors = []
        self.plan = None
        self.seconds = 100
        self.collect_until = NOW + 500
        for name, value in (("time", lambda: self.clock.w), ("monotonic", lambda: self.clock.m),
                            ("sleep", self.clock.advance)):
            p = patch("sale_monitor.runner.time." + name, value)
            p.start()
            self.addCleanup(p.stop)
        for module in ("sale_monitor.runner", "sale_monitor.http"):
            p = patch(module + ".iso", side_effect=lambda *args: time_string(self.clock.w))
            p.start()
            self.addCleanup(p.stop)
        p = patch("socket.create_connection", side_effect=AssertionError("Network forbidden"))
        p.start()
        self.addCleanup(p.stop)

    def write_work(self):
        inputs = inputs_fixture(self.config, self.state, self.plan)
        changes = dict(input_sha256={path: sha256_bytes(data) for path, data in inputs.items()},
                       seconds=self.seconds, collect_until=time_string(self.collect_until))
        if self.plan is not None:
            changes.update(phase="verification", accepted_discovery={"satellite_commit": "d" * 40,
                "state_sha256": sha256_bytes(inputs["input/stores/tsukumo.json"])})
        ticket = ticket_fixture(**changes)
        atomic_json(self.work / "ticket.json", ticket)
        atomic_json(self.work / "handshake.json", self.handshake.to_dict())
        atomic_json(self.work / "context.json", {"home": str(self.home)})
        for path, data in inputs.items():
            destination = self.work / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        self.input_bytes = inputs["input/stores/tsukumo.json"]

    def run_stage(self, replies=None, robots=(200, {}, b"User-agent: *\nAllow: /"), discover=None):
        self.write_work()
        replies = replies if replies is not None else [(200, {}, b"<html>list</html>"), (200, {}, PRODUCT.body)]
        self.transport = MemoryHTTPS(replies)
        def client_factory(**kwargs):
            self.client = Client(**kwargs)
            self.client.opener = build_opener(SafeRedirect(self.client._before_redirect, kwargs["guard"]), self.transport)
            return self.client
        def robots_transport(url):
            self.robots_calls.append(url)
            return robots
        def collector(*args, **kwargs):
            instance = Collector(*args, **kwargs)
            self.collectors.append(instance)
            return instance
        discovered = discover if discover is not None else ([{"url": PRODUCT.url, "kind": "sale"}], [], [])
        with patch("sale_monitor.satellite.stage.Collector", side_effect=collector), \
                patch("sale_monitor.runner.discover", return_value=discovered):
            result = run_stage(self.work, deadline_clock=lambda: self.clock.m, wall_clock=lambda: self.clock.w,
                sleep=self.clock.advance, client_factory=client_factory, robots_transport=robots_transport)
        self.result = read_json(self.work / "stage_result.json", None)
        self.output_bytes = (self.work / "state/stores/tsukumo.json").read_bytes()
        self.output = json.loads(self.output_bytes)
        return result

    def test_real_collector_preserves_input_journal_and_uses_only_input_config(self):
        local_config = self.home / "config"
        local_config.mkdir()
        atomic_json(local_config / "sources.json", {"stores": {"tsukumo": {"seed_urls": ["https://foreign.invalid/"]}}})
        from sale_monitor import storage
        original = storage.read_json
        reads = []
        def read(path, default):
            reads.append(Path(path))
            self.assertNotIn("config", Path(path).parts)
            return original(path, default)
        with patch("sale_monitor.storage.read_json", side_effect=read), \
                patch("sale_monitor.satellite.stage.read_json", side_effect=read):
            self.assertEqual(self.run_stage(), 0)
        self.assertEqual(self.result["reason"], "completed")
        self.assertEqual(self.transport.calls, [LIST, PRODUCT.url])
        self.assertEqual(self.robots_calls, ["https://shop.tsukumo.co.jp/robots.txt"])
        self.assertEqual(self.output["journal"][:1], [self.row])
        self.assertEqual(len(self.output["journal"]), 2)
        self.assertEqual(self.output["run_id"], "run-1")
        self.assertEqual(self.output["phase"], "discovery")
        self.assertFalse(self.client.browser)
        self.assertEqual(self.collectors[0].config, {key: self.config[key] for key in ("schema_version", "stores", "seller_aliases")})
        self.assertEqual(self.result["counters"]["ordinary_succeeded"], 2)
        self.assertEqual(self.clock.m, 10)

    def test_verification_uses_supplied_state_and_plan_only(self):
        self.plan = {"run_id": "run-1", "confirm": {"tsukumo": [dict(url=PRODUCT.url,
                     offer_key="not-known", identity="identity", for_candidates=[])]}}
        self.assertEqual(self.run_stage(replies=[(200, {}, PRODUCT.body)]), 0)
        self.assertEqual(self.transport.calls, [PRODUCT.url])
        self.assertEqual(self.output["journal"][:1], [self.row])
        self.assertEqual(self.output["phase"], "verification")
        self.assertEqual(len(self.output["verification"]["results"]), 1)

    def test_foreign_queue_blocks_every_request_including_robots(self):
        self.state["queue"] = {"foreign": dict(type="product", url="https://example.com/item", kind="sale")}
        self.assertEqual(self.run_stage(replies=[]), 1)
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.robots_calls)
        self.assertEqual(self.output_bytes, self.input_bytes)

    def test_foreign_plan_blocks_every_request_including_robots(self):
        self.plan = {"run_id": "run-1", "confirm": {"tsukumo": [dict(url="https://example.com/item",
                     offer_key="key", identity="identity")]}}
        self.assertEqual(self.run_stage(replies=[]), 1)
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.robots_calls)

    def test_carried_search_task_cannot_enable_comparison_lookup(self):
        self.state["queue"] = {"search": dict(type="search", query="fixture", kind="sale")}
        self.assertEqual(self.run_stage(replies=[]), 1)
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.robots_calls)

    def test_foreign_redirect_blocked_before_hop(self):
        self.assertEqual(self.run_stage(replies=[(302, {"Location": "https://example.com/"}, b"")]), 1)
        self.assertEqual(self.transport.calls, [LIST])
        self.assertEqual(self.result["reason"], "error")

    def test_foreign_derived_link_is_never_requested(self):
        self.assertEqual(self.run_stage(replies=[(200, {}, b"list")],
            discover=([{"url": "https://example.com/product", "kind": "sale"}], [], [])), 1)
        self.assertEqual(self.transport.calls, [LIST])

    def test_refusal_saved_and_no_request_after_refusal(self):
        self.assertEqual(self.run_stage(replies=[(403, {}, b"refused")]), 1)
        self.assertEqual(self.transport.calls, [LIST])
        self.assertEqual(self.result["reason"], "refused")
        self.assertEqual(len(ControlState(self.home).active_refusals), 1)
        self.assertEqual(self.output["journal"][:1], [self.row])

    def test_robots_refusal_does_not_reach_store_page(self):
        self.assertEqual(self.run_stage(replies=[], robots=(403, {}, b"refused")), 1)
        self.assertFalse(self.transport.calls)
        self.assertEqual(self.result["reason"], "refused")
        self.assertEqual(self.result["counters"]["requests"], 1)
        self.assertTrue(ControlState(self.home).active_refusals)

    def test_retry_after_and_deadline_stop_before_next_request(self):
        self.assertEqual(self.run_stage(replies=[(429, {"Retry-After": "300"}, b"")]), 1)
        self.assertEqual(self.result["reason"], "retry_after")
        self.assertEqual(self.transport.calls, [LIST])
        self.assertGreater(ControlState(self.home).retry_until("shop.tsukumo.co.jp"), NOW)

    def test_deadline_checked_after_spacing_wait(self):
        self.collect_until = NOW + 8
        self.assertEqual(self.run_stage(replies=[(200, {}, b"list")]), 1)
        self.assertEqual(self.transport.calls, [LIST])
        self.assertEqual(self.result["reason"], "deadline")
        self.assertEqual(self.result["counters"]["requests"], 2)

    def test_delays_never_start_a_request_past_coordinator_deadline(self):
        for outbound, waiting, inbound in ((0, 0, 0), (2, 7, 11), (20, 30, 60)):
            with self.subTest(delays=(outbound, waiting, inbound)):
                self.clock = FakeClock()
                self.handshake = self.clock.handshake(outbound, waiting, inbound)
                self.collect_until = NOW + self.clock.m + 8
                self.run_stage(replies=[(200, {}, b"list"), (200, {}, PRODUCT.body)])
                guard = self.client.guard
                if guard.counters["ordinary_attempted"]:
                    last = ControlState(self.home).data["last_request_at"]["shop.tsukumo.co.jp"]
                    self.assertLess(last, self.collect_until)

    def test_same_run_robots_cache_and_stage_budget_are_restored(self):
        self.state["run_id"] = "run-1"
        self.state["robots"] = {"run_id": "run-1", "responses": {"shop.tsukumo.co.jp": [200, "User-agent: *\nCrawl-delay: 7"]}}
        self.state["budget"] = {"run_id": "run-1", "phase": "discovery", "used": {"shop.tsukumo.co.jp": 3}}
        self.assertEqual(self.run_stage(), 0)
        self.assertFalse(self.robots_calls)
        self.assertEqual(self.client.budget.used["shop.tsukumo.co.jp"], 5)
        self.assertEqual(self.client.budget.interval("shop.tsukumo.co.jp"), 7)
        self.assertEqual(self.output["robots"]["responses"], self.state["robots"]["responses"])

    def test_phase_budget_not_restored_from_discovery_to_verification(self):
        self.state["budget"] = {"run_id": "run-1", "phase": "discovery", "used": {"shop.tsukumo.co.jp": 10}}
        self.plan = {"run_id": "run-1", "confirm": {"tsukumo": []}}
        self.assertEqual(self.run_stage(replies=[]), 0)
        self.assertEqual(self.client.budget.used, {})

    def test_inactive_route_and_invalid_config_never_request(self):
        self.config["route"]["functions"]["sale_discovery"]["status"] = "paused_configuration"
        self.assertEqual(self.run_stage(replies=[]), 1)
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.robots_calls)

    def test_handshake_not_accepted_and_drift_never_request(self):
        self.handshake = Handshake(COMMIT, "unique_nonce_123456", deadline_clock=lambda: self.clock.m,
                                   wall_clock=lambda: self.clock.w)
        self.handshake.begin_ack()
        self.assertEqual(self.run_stage(replies=[]), 1)
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.robots_calls)
        self.assertEqual(self.result["reason"], "error")

    def test_stage_result_is_checkpointed_before_network_attempt(self):
        self.write_work()
        observed = []
        def robots(url):
            observed.append(read_json(self.work / "stage_result.json", None))
            raise TimeoutError()
        self.assertEqual(run_stage(self.work, deadline_clock=lambda: self.clock.m,
            wall_clock=lambda: self.clock.w, sleep=self.clock.advance, robots_transport=robots), 0)
        self.assertEqual(observed[0]["counters"]["requests"], 1)
        self.assertEqual(observed[0]["counters"]["ordinary_attempted"], 0)
        self.assertTrue((self.work / "state/stores/tsukumo.json").is_file())
