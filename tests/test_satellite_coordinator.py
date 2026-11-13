"""Coordinator clocks, safety metadata and byte-preserving acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.routes import RouteState, load_config
from sale_monitor.satellite.coordinator import RemoteStages, encode
from sale_monitor.satellite import protocol, validation
from sale_monitor.satellite.testing import mailbox_pair
from sale_monitor.satellite.transport import TransportError
from sale_monitor.storage import Store
from test_satellite_protocol import NOW, COMMIT, NONCE, manifest_fixture, refusal_fixture
from test_satellite_validation import config_fixture


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def carried_state():
    # Key set of monitor-data's tsukumo checkpoint (2026-11-13), no live payload.
    state = {name: {} for name in ("access_retry_after", "changes", "comparison_refresh", "comparison_routing",
        "comparison_searches", "http_evidence", "offers", "queue", "rate_limit_spacing_seconds", "retry_activity",
        "retry_after", "scheduler", "transport_diagnostic", "transport_retry_after")}
    state.update({name: [] for name in ("discovery_gaps", "done", "errors", "journal", "recovered_errors", "waiting_dependencies")})
    state.update({name: 0 for name in ("list_pages", "listed_candidates", "pending_count", "request_count")})
    state.update({name: protocol.time_string(NOW - 100) for name in
        ("checkpoint_at", "completed_at", "cycle_started_at", "started_at")})
    state.update(schema_version=1, store="tsukumo", run_id="previous", status="partial", cycle_complete=True)
    return state


class CoordinatorFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.disk = Store(self.root / "state")
        cfg = config_fixture()
        self.sources = {"schema_version": 1, "stores": cfg["stores"], "seller_aliases": {}}
        self.config = {"schema_version": 1, "config_revision": 1, "hosts": cfg["hosts"],
                       "routes": {"tsukumo:direct": cfg["route"]}, "scope": {"version": 2, "in_scope": [], "optional": ["tsukumo:direct"]}}
        self.routes = RouteState(self.disk, self.config, NOW)
        self.coord, self.pc = mailbox_pair()
        self.clock = Clock()
        self.input = encode(carried_state()) + b"\r\n"
        self.disk.root.mkdir(parents=True, exist_ok=True)
        self.disk.save("stores/tsukumo.json", carried_state())

    def remote(self):
        return RemoteStages(self.coord, self.disk, self.routes, "run-1", sources_config=self.sources,
                            clock=self.clock, monotonic=self.clock)

    def begin(self, remote):
        return remote.begin("tsukumo", "discovery", self.input,
                            protocol.stage_deadlines(NOW + 5000, NOW, 590, 120, 300))

    def ticket(self, handle):
        return handle.audit["ticket_commit"]

    def ack(self, handle, **updates):
        manifest = manifest_fixture(handle.ticket, ticket_commit=self.ticket(handle), kind="ack", nonce=NONCE,
            output_sha256=None, status="acknowledged", finished_at=None, **updates)
        return self.pc.post({"manifest.json": encode(manifest)}, "ack")

    def result(self, handle, *, body=None, **updates):
        body = body if body is not None else encode({**carried_state(), "run_id": "run-1", "phase": handle.phase}) + b"\r\n"
        manifest = manifest_fixture(handle.ticket, **{**dict(ticket_commit=self.ticket(handle), output_sha256=protocol.sha256_bytes(body)), **updates})
        commit = self.pc.post({"manifest.json": encode(manifest), "stores/tsukumo.json": body}, "result")
        return commit, body


class CoordinatorTests(CoordinatorFixture):
    def test_ticket_has_exact_inputs_effective_config_and_unknown_state_sha(self):
        remote = self.remote()
        h = self.begin(remote)
        commit = self.ticket(h)
        self.assertIsNone(h.ticket["state_sha"])
        self.assertEqual(self.pc.read(commit, "input/stores/tsukumo.json"), self.input)
        for path, hashed in h.ticket["input_sha256"].items():
            self.assertEqual(protocol.sha256_bytes(self.pc.read(commit, path)), hashed)
        self.assertEqual(json.loads(self.pc.read(commit, "input/routes.json")), self.routes.routes["tsukumo:direct"])

    def test_absent_uses_issue_window_not_finish_start(self):
        remote = self.remote()
        h = self.begin(remote)
        self.clock.now += 140
        remote.service()
        self.assertIsNone(h.status)
        status, body = remote.finish(h, self.clock.sleep)
        self.assertEqual((status, body), ("satellite_absent", None))
        self.assertEqual(self.clock.now, NOW + 180)
        self.assertEqual(self.disk.load("satellite/run-1.json", {})["phases"]["discovery"]["outcome"], status)

    def test_ack_boundary_and_echo_once_per_nonce(self):
        remote = self.remote()
        h = self.begin(remote)
        ack = self.ack(h)
        self.clock.now += 180
        remote.service()
        echo = h.audit["echo_commit"]
        self.assertEqual(h.audit["ack_commit"], ack)
        self.assertEqual(json.loads(self.pc.read(echo, "echo.json"))["actions_time"], protocol.time_string(self.clock.now))
        self.ack(h)
        self.clock.sleep(15)
        remote.service()
        self.assertEqual(h.audit["echo_commit"], echo)
        self.assertEqual(len(self.pc._read), 2)
        self.result(h)
        self.clock.sleep(15)
        self.assertEqual(remote.finish(h, self.clock.sleep)[0], "completed")

    def test_ack_after_window_is_absent_and_never_echoed(self):
        remote = self.remote()
        h = self.begin(remote)
        self.ack(h)
        self.clock.sleep(180.01)
        remote.service()
        self.assertEqual(h.status, "satellite_absent")
        self.assertNotIn("echo_commit", h.audit)

    def test_result_at_deadline_and_body_read_just_after(self):
        for delay in (0, .001):
            with self.subTest(delay=delay):
                self.setUp()
                remote = self.remote()
                h = self.begin(remote)
                self.ack(h)
                remote.service()
                commit, body = self.result(h)
                self.clock.now = protocol.epoch(h.ticket["accept_until"])
                original = self.coord.read

                def read(sha, path):
                    value = original(sha, path)
                    if path.startswith("stores/"):
                        self.clock.sleep(delay)
                    return value

                previous = (self.disk.root / "stores/tsukumo.json").read_bytes()
                with patch.object(self.coord, "read", side_effect=read):
                    status, accepted = remote.finish(h, self.clock.sleep)
                self.assertEqual(status, "satellite_deadline" if delay else "completed")
                self.assertEqual((self.disk.root / "stores/tsukumo.json").read_bytes(), previous if delay else body)
                self.assertEqual(accepted, None if delay else body)
                self.assertEqual(h.audit["result_commit"], commit)

    def test_result_without_ack_and_stopped_result_are_accepted(self):
        remote = self.remote()
        h = self.begin(remote)
        commit, body = self.result(h, status="stopped:no_response")
        self.assertEqual(remote.finish(h, self.clock.sleep), ("stopped:no_response", body))
        self.assertEqual(self.disk.load("satellite/position.json", {}), {"commit": commit})
        self.assertEqual(h.audit["counters"]["requests"], 0)

    def test_other_run_or_ticket_never_accepted_and_recorded_late(self):
        remote = self.remote()
        h = self.begin(remote)
        self.result(h, run_id="old-run")
        self.result(h, ticket_commit=COMMIT)
        self.assertEqual(remote.finish(h, self.clock.sleep), ("satellite_absent", None))
        self.assertTrue((self.disk.root / "satellite/late/old-run-discovery.json").exists())
        self.assertTrue((self.disk.root / "satellite/late/run-1-discovery.json").exists())

    def test_invalid_result_keeps_state_but_applies_refusal(self):
        remote = self.remote()
        h = self.begin(remote)
        before = (self.disk.root / "stores/tsukumo.json").read_bytes()
        self.result(h, body=b"bad state", active_refusals=[refusal_fixture()])
        status, body = remote.finish(h, self.clock.sleep)
        self.assertTrue(status.startswith("satellite_rejected:"))
        self.assertIsNone(body)
        self.assertEqual(before, (self.disk.root / "stores/tsukumo.json").read_bytes())
        self.assertEqual(self.routes.status("tsukumo:direct", "sale_discovery"), "paused_refused")

    def test_refusal_from_ack_and_late_previous_run_at_stage_zero(self):
        remote = self.remote()
        h = self.begin(remote)
        self.ack(h, active_refusals=[refusal_fixture()])
        remote.service()
        self.assertEqual(self.routes.status("tsukumo:direct", "product_verify"), "paused_refused")
        self.result(h, run_id="previous-run", active_refusals=[{**refusal_fixture(), "refusal_id": "refusal-2"}])
        # A new mailbox resumes the persisted position, as the CLI does.
        self.routes = RouteState(self.disk, self.config, NOW)
        next_remote = self.remote()
        self.assertEqual(self.begin(next_remote).status, "skipped:route_inactive")
        self.assertEqual(len(self.pc._read), 2)
        self.assertTrue((self.disk.root / "satellite/late/previous-run-discovery.json").exists())
        ids = self.routes.routes["tsukumo:direct"]["functions"]["sale_discovery"]["refusal_ids"]
        self.assertEqual(ids, ["refusal-1", "refusal-2"])

    def test_refusal_survives_revision_and_executor_until_all_ids_cleared(self):
        rows = [refusal_fixture(), {**refusal_fixture(), "refusal_id": "refusal-2"}]
        self.routes.apply_refusals("tsukumo:direct", rows, NOW)
        self.routes.save()
        config = deepcopy(self.config)
        config["config_revision"] += 1
        config["routes"]["tsukumo:direct"]["executor"] = "runner"
        config["routes"]["tsukumo:direct"]["cleared_refusals"] = ["refusal-1"]
        state = RouteState(self.disk, config, NOW)
        self.assertEqual(state.status("tsukumo:direct", "sale_discovery"), "paused_refused")
        state.save()
        config["routes"]["tsukumo:direct"]["cleared_refusals"].append("refusal-2")
        state = RouteState(self.disk, config, NOW)
        self.assertEqual(state.status("tsukumo:direct", "sale_discovery"), "active")
        state.apply_refusals("tsukumo:direct", rows, NOW)
        self.assertEqual(state.status("tsukumo:direct", "sale_discovery"), "active")
        self.assertEqual(state.public_summary()["tsukumo:direct"]["executor"], "runner")

    def test_begin_poll_echo_transport_failures_are_isolated(self):
        for point in ("begin", "poll", "echo", "construction"):
            with self.subTest(point=point):
                self.setUp()
                if point == "construction":
                    with patch.object(self.coord, "poll", side_effect=TransportError):
                        remote = self.remote()
                    h = self.begin(remote)
                else:
                    remote = self.remote()
                    if point == "begin":
                        with patch.object(self.coord, "post", side_effect=TransportError):
                            h = self.begin(remote)
                    else:
                        h = self.begin(remote)
                        self.ack(h)
                        with patch.object(self.coord, "poll" if point == "poll" else "post", side_effect=TransportError):
                            remote.service()
                self.assertEqual(remote.finish(h, self.clock.sleep), ("relay_failed", None))

    def test_declined_without_state_and_throttled_poll(self):
        remote = self.remote()
        h = self.begin(remote)
        m = manifest_fixture(h.ticket, ticket_commit=self.ticket(h), status="declined:code_mismatch", output_sha256=None)
        self.pc.post({"manifest.json": encode(m)}, "decline")
        with patch.object(self.coord, "poll", wraps=self.coord.poll) as poll:
            remote.service()
            remote.service()
            self.assertEqual(poll.call_count, 1)
        self.assertEqual(remote.finish(h, self.clock.sleep), ("satellite_declined:code_mismatch", None))

    def test_state_limits_do_not_relax_wire_document_limits(self):
        state = carried_state()
        state["queue"] = {str(i): {} for i in range(10_001)}
        state["transport_diagnostic"] = {"summary": "x" * 65_536}
        self.assertEqual(validation._load_state(encode(state)), state)
        with self.assertRaises(protocol.ValidationError):
            protocol.bounded(state)
        state["queue"] = {"x": {"summary": "x" * 65_537}}
        with self.assertRaises(protocol.ValidationError):
            validation._load_state(encode(state))

    def test_read_failure_does_not_advance_position_past_unprocessed_refusals(self):
        remote = self.remote()
        h = self.begin(remote)
        ack = self.ack(h)
        commit, _ = self.result(h, active_refusals=[refusal_fixture()])
        original = self.coord.read

        def read(sha, path):
            if sha == ack:
                raise TransportError()
            return original(sha, path)

        with patch.object(self.coord, "read", side_effect=read):
            remote.service()
        self.assertEqual(h.status, "relay_failed")
        self.assertEqual(self.disk.load("satellite/position.json", {}), {})
        self.clock.sleep(30)
        remote.service()
        self.assertEqual(self.disk.load("satellite/position.json", {}), {"commit": commit})
        self.assertEqual(self.routes.status("tsukumo:direct", "sale_discovery"), "paused_refused")
        self.assertTrue((self.disk.root / "satellite/late/run-1-discovery.json").exists())

    def test_late_result_refusal_is_applied_after_absence_in_the_same_run(self):
        remote = self.remote()
        h = self.begin(remote)
        remote.finish(h, self.clock.sleep)
        commit, _ = self.result(h, active_refusals=[refusal_fixture()])
        self.clock.sleep(15)
        remote.service()
        self.assertIsNone(h.accepted)
        self.assertEqual(self.routes.status("tsukumo:direct", "sale_discovery"), "paused_refused")
        row = self.disk.load("satellite/late/run-1-discovery.json", {})["records"][0]
        self.assertEqual(row["satellite_commit"], commit)

    def test_routes_executor_validation_and_filter(self):
        path = self.root / "routes.json"
        for change in ({"executor": "unknown"}, {"executor": "satellite", "store": "ark"}, {"cleared_refusals": [1]}):
            config = deepcopy(self.config)
            config["routes"]["tsukumo:direct"].update(change)
            path.write_bytes(encode(config))
            with self.assertRaises(ValueError):
                load_config(path)
        self.assertEqual(self.routes.active_stores("sale_discovery", "runner"), [])
        self.assertEqual(self.routes.active_stores("sale_discovery", "satellite"), ["tsukumo"])
