"""Offline remote children through real planning and public JSON."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sale_monitor import cli, pipeline, reporting, routes
from sale_monitor.models import iso
from sale_monitor.runner import Collector
from sale_monitor.satellite import protocol
from sale_monitor.satellite.coordinator import RemoteStages, encode
from sale_monitor.satellite.testing import mailbox_pair
from sale_monitor.satellite.transport import TransportError
from sale_monitor.storage import Store, read_json
from test_pipeline import Fixture, NOW, RUN, item, URLS
from test_runner_phases import FakeClient
from test_satellite_coordinator import Clock, carried_state
from test_satellite_protocol import COMMIT, NONCE, manifest_fixture, refusal_fixture
from test_satellite_validation import config_fixture


class ScriptedSatellite:
    def __init__(self, pc, mode="completed", produce=None):
        self.pc, self.mode, self.produce = pc, mode, produce
        self.tickets, self.inputs, self.plans = [], [], []
        self.pending = {}

    def service(self):
        if self.mode == "absent":
            return
        for commit in self.pc.poll():
            try:
                ticket = json.loads(self.pc.read(commit, "ticket.json"))
            except KeyError:
                echo = json.loads(self.pc.read(commit, "echo.json"))
                pending = self.pending.pop(echo["ticket_commit"], None)
                if pending:
                    self.result(*pending)
                continue
            raw = self.pc.read(commit, "input/stores/tsukumo.json")
            self.tickets.append(ticket)
            self.inputs.append(raw)
            try:
                self.plans.append(json.loads(self.pc.read(commit, "input/plan.json")))
            except KeyError:
                self.plans.append(None)
            ack = manifest_fixture(ticket, kind="ack", nonce=NONCE, ticket_commit=commit,
                started_at=ticket["issued_at"], status="acknowledged", finished_at=None, output_sha256=None)
            self.pc.post({"manifest.json": encode(ack)}, "ack")
            self.pending[commit] = (commit, ticket, raw)

    def result(self, commit, ticket, raw):
        status = self.mode
        if self.mode == "verification_absent" and ticket["phase"] == "verification":
            return
        if status == "verification_absent":
            status = "completed"
        output = encode({**json.loads(raw), "run_id": ticket["run_id"], "phase": ticket["phase"],
            "status": "complete", "list_pages": 1, "listed_candidates": 0}) + b"\r\n"
        if self.produce:
            output = self.produce(ticket, raw)
        if status == "rejected":
            output, status = b"invalid state", "completed"
        manifest = manifest_fixture(ticket, ticket_commit=commit, status=status,
            started_at=ticket["issued_at"], finished_at=ticket["issued_at"],
            output_sha256=None if status.startswith("declined:") else protocol.sha256_bytes(output))
        files = {"manifest.json": encode(manifest)}
        if not status.startswith("declined:"):
            files["stores/tsukumo.json"] = output
        self.pc.post(files, "result")


class SatellitePipelineTests(Fixture):
    def setUp(self):
        super().setUp()
        cfg = config_fixture()
        self.config.update(schema_version=1, seller_aliases={})
        self.config["stores"]["tsukumo"] = cfg["stores"]["tsukumo"]
        self.route_config["routes"]["tsukumo:direct"] = cfg["route"]
        self.route_config["scope"].update(version=2, optional=["tsukumo:direct"])
        self.route_config["run"].update(discovery_minutes=10, verification_minutes=10)
        for route in self.route_config["routes"].values():
            route["functions"]["comparison_lookup"]["status"] = "paused_configuration"
        self.save_config()
        self.clock = Clock(NOW.timestamp())
        self.coord, self.pc = mailbox_pair()
        self.disk.save("stores/tsukumo.json", carried_state())

    def remote(self, mode="completed", produce=None):
        self.script = ScriptedSatellite(self.pc, mode, produce)
        original = self.coord.poll

        def poll():
            self.script.service()
            return original()

        self.coord.poll = poll
        self.remote_stages = RemoteStages(self.coord, self.disk,
            routes.RouteState(self.disk, self.route_config, NOW), RUN,
            sources_config=self.config, clock=self.clock, monotonic=self.clock)
        return self.remote_stages

    def spawn_empty(self, command, **kwargs):
        store = command[command.index("--store") + 1]
        self.assertNotEqual(store, "tsukumo")
        self.disk.save(f"stores/{store}.json", {"schema_version": 1, "store": store, "run_id": RUN,
            "offers": {}, "journal": [], "queue": {}, "done": [], "cycle_complete": True,
            "status": "complete", "list_pages": 1, "listed_candidates": 0, "errors": []})
        return SimpleNamespace(poll=lambda: 0)

    def execute(self, remote, spawn=None):
        with patch("sale_monitor.pipeline.time.sleep", side_effect=self.clock.sleep), patch(
                "sale_monitor.pipeline.Client", side_effect=AssertionError("coordinator network")), patch(
                "sale_monitor.pipeline.utcnow", side_effect=lambda: datetime.fromtimestamp(self.clock.now, timezone.utc)):
            return pipeline.run(self.root, self.public, RUN, config_path=self.config_path,
                routes_path=self.routes_path, remote=remote, spawn=spawn or self.spawn_empty, clock=self.clock)

    def test_unregistered_route_publishes_skip_without_ticket(self):
        del self.route_config["routes"]["tsukumo:direct"]["satellite_agent"]
        self.save_config()
        result = self.execute(self.remote())
        stage = result["run"]["stage_results"]["tsukumo"]["discovery"]
        self.assertEqual(stage["status"], "skipped:agent_unregistered")
        self.assertNotIn("other_agents", stage)
        self.assertEqual(self.script.tickets, [])
        published = read_json(self.public / "latest.json", {})
        self.assertEqual(published["run"]["stage_results"]["tsukumo"]["discovery"], stage)

    def test_other_agent_audit_is_published_without_blocking_registered_result(self):
        remote = self.remote()
        original = self.script.service
        answered = set()

        def service():
            original()
            for handle in remote.handles:
                commit = handle.audit.get("ticket_commit")
                if commit and commit not in answered:
                    manifest = manifest_fixture(handle.ticket, ticket_commit=commit,
                        agent_id="2" * 32, status="declined:agent_mismatch", output_sha256=None,
                        started_at=handle.ticket["issued_at"], finished_at=handle.ticket["issued_at"])
                    self.pc.post({"manifest.json": encode(manifest)}, "other agent")
                    answered.add(commit)

        self.script.service = service
        result = self.execute(remote)
        stage = result["run"]["stage_results"]["tsukumo"]["discovery"]
        self.assertEqual(stage["status"], "completed")
        self.assertEqual(stage["other_agents"], 1)
        published = read_json(self.public / "latest.json", {})
        self.assertEqual(published["run"]["stage_results"]["tsukumo"]["discovery"], stage)

    def test_new_rejection_and_decline_statuses_are_published(self):
        for mode, expected in (("unacknowledged", "satellite_rejected:unacknowledged"),
                               ("declined:agent_mismatch", "satellite_declined:agent_mismatch")):
            with self.subTest(mode=mode):
                self.setUp()
                remote = self.remote(mode)
                if mode == "unacknowledged":
                    def service():
                        for commit in self.pc.poll():
                            ticket = json.loads(self.pc.read(commit, "ticket.json"))
                            raw = self.pc.read(commit, "input/stores/tsukumo.json")
                            self.script.mode = "completed"
                            self.script.result(commit, ticket, raw)
                    self.script.service = service
                result = self.execute(remote)
                stage = result["run"]["stage_results"]["tsukumo"]["discovery"]
                self.assertEqual(stage["status"], expected)
                published = read_json(self.public / "latest.json", {})
                self.assertEqual(published["run"]["stage_results"]["tsukumo"]["discovery"], stage)

    def test_absent_never_spawns_or_requests_and_other_stores_publish(self):
        result = self.execute(self.remote("absent"))
        self.assertEqual(result["run"]["stage_results"]["tsukumo"]["discovery"]["status"], "satellite_absent")
        self.assertEqual(self.clock.now, NOW.timestamp() + 180)
        self.assertTrue(result["run_outcome"]["successful"])
        self.assertEqual(result["run_outcome"]["confirmed_empty_results"], 3)
        self.assertTrue((self.public / "notifications.json").exists())

    def test_absent_window_is_serviced_during_local_children(self):
        remote = self.remote("absent")

        def spawn(command, **kwargs):
            process = self.spawn_empty(command, **kwargs)
            if command[command.index("--store") + 1] == "ark":
                def poll():
                    self.clock.sleep(60)
                    return 0 if self.clock.now >= NOW.timestamp() + 240 else None
                process.poll = poll
            return process

        result = self.execute(remote, spawn)
        self.assertEqual(remote.handles[0].status, "satellite_absent")
        self.assertLess(self.clock.now, NOW.timestamp() + 241)
        self.assertTrue(result["run_outcome"]["successful"])

    def test_remote_none_has_relay_failed_for_both_phases(self):
        result = self.execute(None)
        stages = result["run"]["stage_results"]["tsukumo"]
        self.assertEqual([stages[p] for p in ("discovery", "verification")],
                         [{"status": "relay_failed", "executor": "satellite"}] * 2)
        self.assertTrue(result["run_outcome"]["successful"])

    def test_transport_begin_poll_and_echo_failure_leave_other_stores_successful(self):
        for point in ("begin", "poll", "echo"):
            with self.subTest(point=point):
                self.setUp()
                remote = self.remote()
                if point == "poll":
                    target = patch.object(self.coord, "poll", side_effect=TransportError)
                elif point == "begin":
                    target = patch.object(self.coord, "post", side_effect=TransportError)
                else:
                    original = self.coord.post
                    def post(files, message):
                        if "echo.json" in files:
                            raise TransportError()
                        return original(files, message)
                    target = patch.object(self.coord, "post", side_effect=post)
                with target:
                    result = self.execute(remote)
                self.assertEqual(result["run"]["stage_results"]["tsukumo"]["discovery"]["status"], "relay_failed")
                self.assertTrue(result["run_outcome"]["successful"])

    def test_completed_and_stopped_stages_count_current_empty_results(self):
        for status in ("completed", "stopped:no_response", "declined:refused", "rejected"):
            with self.subTest(status=status):
                self.setUp()
                result = self.execute(self.remote(status))
                self.assertTrue(result["run_outcome"]["successful"])
                if status == "completed":
                    self.assertEqual(result["run_outcome"]["confirmed_empty_results"], 4)
                self.assertEqual(len(self.script.tickets), 1)
                self.assertTrue(result["run"]["stage_results"]["tsukumo"]["verification"]["status"].startswith("skipped:"))

    def test_local_children_use_deadline_formula_in_both_phases(self):
        candidate = item("ark", 9000)
        peer = item("sofmap", 11000, observed_run_id="old")
        self.save_offer(candidate, RUN)
        self.save_offer(peer)
        self.route_config["run"].update(max_minutes=20, discovery_minutes=20, verification_minutes=20)
        self.save_config()
        calls = []

        def children(stores, phase, seconds, *args, **kwargs):
            calls.append((phase, seconds, kwargs["deadline"]))
            if phase == "discovery":
                self.clock.sleep(100)
            return {store: {"status": "completed"} for store in stores}

        with patch.object(pipeline, "_run_children", side_effect=children):
            self.execute(None)
        self.assertEqual([(phase, seconds) for phase, seconds, _ in calls], [("discovery", 780), ("verification", 680)])
        self.assertEqual([deadline - NOW.timestamp() for _, _, deadline in calls], [900, 900])

    def prepare_plan(self):
        self.save_offer(item("ark", 9000), RUN)
        peer = item("tsukumo", 11000, observed_run_id="previous")
        state = carried_state()
        state["offers"] = {peer.key: peer.to_dict()}
        self.disk.save("stores/tsukumo.json", state)
        # Other local stores complete without replacing the candidate fixture.
        return lambda *a, **k: SimpleNamespace(poll=lambda: 0)

    def test_verification_uses_accepted_raw_bytes_provenance_and_store_only_plan(self):
        spawn = self.prepare_plan()
        result = self.execute(self.remote(), spawn)
        self.assertEqual([t["phase"] for t in self.script.tickets], ["discovery", "verification"])
        discovery = self.remote_stages.handles[0]
        verification = self.script.tickets[1]
        self.assertEqual(self.script.inputs[1], discovery.accepted)
        self.assertEqual(verification["accepted_discovery"], {"satellite_commit": discovery.audit["result_commit"],
            "state_sha256": protocol.sha256_bytes(discovery.accepted)})
        plan = self.script.plans[1]
        self.assertEqual(set(plan), {"run_id", "confirm"})
        self.assertEqual(set(plan["confirm"]), {"tsukumo"})
        self.assertEqual(result["run"]["stage_results"]["tsukumo"]["verification"]["status"], "completed")

    def test_unaccepted_verification_preserves_discovery_and_reports_not_reached(self):
        spawn = self.prepare_plan()
        self.execute(self.remote("verification_absent"), spawn)
        state = self.disk.load("stores/tsukumo.json", {})
        self.assertEqual(state["offers"], json.loads(self.remote_stages.handles[0].accepted)["offers"])
        self.assertEqual(state["verification"]["not_reached"], 1)
        self.assertEqual(state["verification"]["results"], {})
        self.assertEqual(self.remote_stages.handles[1].status, "satellite_deadline")

    def test_stage_zero_refusal_prevents_ticket_and_probe(self):
        manifest = manifest_fixture(run_id="previous", active_refusals=[refusal_fixture()])
        self.pc.post({"manifest.json": encode(manifest)}, "old refusal")
        result = self.execute(self.remote())
        self.assertEqual(self.script.tickets, [])
        self.assertEqual(result["routes"]["tsukumo:direct"]["functions"]["sale_discovery"], "paused_refused")
        self.assertEqual(result["routes"]["tsukumo:direct"]["executor"], "satellite")

    def test_absent_decisions_and_events_equal_out_of_scope_baseline(self):
        candidate = item("ark", 9000)
        self.save_offer(candidate, RUN)
        self.save_offer(item("sofmap", 11000), RUN)
        self.save_offer(item("dospara", 12000), RUN)
        spawn = lambda *a, **k: SimpleNamespace(poll=lambda: 0)
        self.execute(self.remote("absent"), spawn)
        decisions = read_json(self.public / "evidence.json", {})["decisions"]
        events = self.disk.load("events/registry.json", {})
        self.assertTrue(events["events"])
        self.route_config["scope"]["optional"] = []
        self.route_config["routes"]["tsukumo:direct"]["functions"] = {
            key: {"status": "paused_configuration"} for key in self.route_config["routes"]["tsukumo:direct"]["functions"]}
        self.save_config()
        # Registry starts from the same empty input in each run.
        (self.root / "events/registry.json").unlink()
        self.execute(None, spawn)
        baseline = read_json(self.public / "evidence.json", {})["decisions"]
        self.assertEqual([(d["offer"], d["payment"], d["points"]) for d in decisions],
                         [(d["offer"], d["payment"], d["points"]) for d in baseline])
        self.assertEqual(events, self.disk.load("events/registry.json", {}))

    def test_real_collector_from_carried_keys_through_planning_to_public_json(self):
        spawn = self.prepare_plan()
        peer = item("tsukumo", 8000, discovery_kind="comparison")
        produced = []

        def produce(ticket, raw):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "stores").mkdir()
                (root / "stores/tsukumo.json").write_bytes(raw)
                effective = json.loads(self.pc.read(self.remote_stages.handles[-1].audit["ticket_commit"], "input/config.json"))
                plan = self.script.plans[-1]
                observed = deepcopy(peer)
                observed.observed_at = ticket["issued_at"]
                with patch("sale_monitor.runner.iso", return_value=ticket["issued_at"]), patch(
                        "sale_monitor.runner.parse_product", return_value=observed):
                    collector = Collector(root, "tsukumo", effective, RUN, client=FakeClient(),
                                          phase=ticket["phase"], plan=plan)
                    collector.collect(1)
                produced.append((root / "stores/tsukumo.json").read_bytes())
                return produced[-1]

        # Discovery keeps the old peer; verification performs a real fake-network fetch.
        def output(ticket, raw):
            if ticket["phase"] == "discovery":
                with patch("sale_monitor.runner.iso", return_value=ticket["issued_at"]):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        Store(root).save("stores/tsukumo.json", json.loads(raw))
                        collector = Collector(root, "tsukumo", self.config, RUN, client=FakeClient(), phase="discovery")
                        collector.collect(0)
                        produced.append((root / "stores/tsukumo.json").read_bytes())
                        return produced[-1]
            return produce(ticket, raw)

        result = self.execute(self.remote(produce=output), spawn)
        stages = result["run"]["stage_results"]["tsukumo"]
        self.assertEqual([stages[p]["status"] for p in ("discovery", "verification")], ["completed", "completed"])
        self.assertEqual(self.script.inputs[1], produced[0])
        self.assertEqual(self.disk.load("stores/tsukumo.json", {})["offers"][peer.key]["price_yen"], 8000)
        decision = next(d for d in read_json(self.public / "evidence.json", {})["decisions"] if d["offer"]["store"] == "ark")
        self.assertEqual(decision["payment"]["status"], "rejected")
        self.assertEqual(decision["comparison_scope"]["confirmed"][0]["scope"], "optional")

    def test_cli_constructs_mailbox_outside_state_repo_and_falls_back_on_failure(self):
        args = SimpleNamespace(state=self.root, config=self.config_path, routes=self.routes_path, run_id=RUN)
        with patch("sale_monitor.satellite.transport.GitMailbox", side_effect=TransportError) as mailbox:
            self.assertIsNone(cli._remote(args))
        self.assertEqual(mailbox.call_args.kwargs["write_branch"], protocol.RELAY_BRANCH)
        self.assertEqual(mailbox.call_args.kwargs["read_branch"], protocol.SATELLITE_BRANCH)
        self.assertFalse(mailbox.call_args.kwargs["workdir"].is_relative_to(self.root.parent))

    def test_direct_run_outcome_accepts_structured_completed_satellite(self):
        state = {"run_id": RUN, "status": "complete", "list_pages": 1, "listed_candidates": 0}
        scope = {"run": {"stage_results": {"tsukumo": {"discovery": {"status": "completed", "executor": "satellite"}}}}}
        self.assertTrue(reporting.run_outcome({"tsukumo": state}, [], RUN, scope)["successful"])
