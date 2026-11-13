"""Real coordinator/agent contracts, with HTTP replaced below the safety guard."""
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch
from urllib.request import build_opener

from sale_monitor.http import Client, SafeRedirect
from sale_monitor.models import digest
from sale_monitor.routes import RouteState
from sale_monitor.satellite import agent as agent_module, protocol, stage, validation
from sale_monitor.satellite.agent import Agent, make_mailbox
from sale_monitor.satellite.control import ControlState
from sale_monitor.satellite.coordinator import RemoteStages, encode
from sale_monitor.satellite.testing import MemoryMailbox, mailbox_pair
from sale_monitor.satellite.transport import GitMailbox
from sale_monitor.storage import atomic_json, read_json
from test_satellite_agent import FakeProcess
from test_satellite_coordinator import CoordinatorFixture
from test_satellite_guard import MemoryHTTPS
from test_satellite_protocol import NOW, refusal_fixture
from test_satellite_stage import LIST, PRODUCT
from test_satellite_validation import offer_fixture


class LoopbackTests(CoordinatorFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "pc"
        self.sources["stores"]["tsukumo"]["seed_urls"] = [LIST]
        self.transports, self.robots_calls, self.inputs, self.outputs = [], [], [], []
        self.runners = []
        self.replies = None
        self.stage_reason = None
        self.echo_delay = 0
        self.after_echo = None
        self.result_delay = 0
        self.remote_stages = self.new_remote()
        self.agent = Agent(self.home, self.pc, deadline_clock=lambda: self.clock.now - NOW,
            wall_clock=self.clock, sleep=self.agent_sleep, stage_runner=self.run_stage, log=Mock())
        self.addCleanup(self.agent.close)
        # No live HTTP, browser or unbounded sleeps can hide behind the real Client.
        for name, value in (("time", self.clock), ("monotonic", self.clock), ("sleep", self.clock.sleep)):
            self.enterContext(patch("sale_monitor.runner.time." + name, value))
        for module in ("sale_monitor.runner", "sale_monitor.http"):
            self.enterContext(patch(module + ".iso", side_effect=lambda *a: protocol.time_string(self.clock.now)))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        # Simulate an installation without git metadata; keep the memory loopback
        # entirely git-free while letting the real agent construct its manifests.
        self.enterContext(patch.object(agent_module, "code_sha", return_value=None))

    def new_remote(self, run_id="run-1"):
        return RemoteStages(self.coord, self.disk, self.routes, run_id,
            sources_config=self.sources, clock=self.clock, monotonic=self.clock)

    def begin_stage(self, phase="discovery", raw=None, plan=None, discovery=None):
        now = self.clock.now
        return self.remote_stages.begin("tsukumo", phase, self.input if raw is None else raw,
            protocol.stage_deadlines(now + 5000, now, 590, 120, 300),
            encode(plan) if plan is not None else None, discovery)

    def agent_sleep(self, seconds):
        self.clock.sleep(seconds)
        self.remote_stages.service()
        if self.agent.waiting and any(h.audit.get("echo_commit") for h in self.remote_stages.handles):
            if self.echo_delay:
                delay, self.echo_delay = self.echo_delay, 0
                self.clock.sleep(delay)
            if self.after_echo:
                callback, self.after_echo = self.after_echo, None
                callback()

    def run_stage(self, work):
        self.runners.append(work.name)
        ticket = read_json(work / "ticket.json", {})
        self.inputs.append((work / "input/stores/tsukumo.json").read_bytes())
        replies = self.replies
        if replies is None:
            replies = [(200, {}, PRODUCT.body)] if ticket["phase"] == "verification" else [
                (200, {}, b"<html>list</html>"), (200, {}, PRODUCT.body)]
        transport = MemoryHTTPS(replies)
        self.transports.append(transport)

        def client_factory(**kwargs):
            client = Client(**kwargs)
            client.opener = build_opener(SafeRedirect(client._before_redirect, kwargs["guard"]), transport)
            return client

        def robots(url):
            self.robots_calls.append(url)
            return 200, {}, b"User-agent: *\nAllow: /"

        with patch("sale_monitor.runner.discover", return_value=([{"url": PRODUCT.url, "kind": "sale"}], [], [])):
            code = stage.run_stage(work, deadline_clock=lambda: self.clock.now - NOW,
                wall_clock=self.clock, sleep=self.clock.sleep, client_factory=client_factory,
                robots_transport=robots)
        if self.stage_reason:
            result = read_json(work / "stage_result.json", {})
            result["reason"] = self.stage_reason
            atomic_json(work / "stage_result.json", result)
            code = 1
        self.outputs.append((work / "state/stores/tsukumo.json").read_bytes())
        if self.result_delay:
            self.clock.sleep(self.result_delay)
            self.remote_stages.service()  # expire while the result is still on the PC
        process = FakeProcess(running=self.stage_reason == "killed")
        if self.stage_reason != "killed":
            process.returncode = code
        return process

    def finish_stage(self, handle, expected="completed", step=True):
        path = self.disk.root / "stores/tsukumo.json"
        previous = path.read_bytes() if path.exists() else None
        if step:
            self.clock.sleep(30)  # next agent poll, with no real wait
            self.agent.step()
        self.clock.sleep(15)  # next coordinator poll
        status, accepted = self.remote_stages.finish(handle, self.clock.sleep)
        self.assertEqual(status, expected)
        audit = self.disk.load("satellite/" + self.remote_stages.run_id + ".json", {})["phases"][handle.phase]
        self.assertEqual(audit, handle.audit)
        self.assertEqual(audit["outcome"], expected)
        if accepted is not None:
            posted = self.coord.read(audit["result_commit"], "stores/tsukumo.json")
            self.assertEqual(accepted, posted)
            self.assertEqual((self.disk.root / "stores/tsukumo.json").read_bytes(), posted)
            manifest = json.loads(self.coord.read(audit["result_commit"], "manifest.json"))
            validation.validate_result(handle.input_bytes, posted, handle.ticket, manifest,
                audit["received_at"], ticket_commit=audit["ticket_commit"])
        else:
            self.assertEqual(path.read_bytes() if path.exists() else None, previous)
        return accepted

    def assert_no_requests(self):
        self.assertEqual(self.runners, [])
        self.assertEqual(self.transports, [])
        self.assertEqual(self.robots_calls, [])

    def test_discovery_and_verification_real_stage(self):
        discovery = self.begin_stage()
        raw = self.finish_stage(discovery)
        observed = json.loads(raw)["journal"]
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.transports[0].calls, [LIST, PRODUCT.url])
        self.assertTrue(discovery.audit["ack_commit"])
        echo = json.loads(self.pc.read(discovery.audit["echo_commit"], "echo.json"))
        protocol.validate_echo(echo)
        self.assertEqual(echo["ticket_commit"], discovery.audit["ticket_commit"])
        ack = json.loads(self.coord.read(discovery.audit["ack_commit"], "manifest.json"))
        self.assertEqual(echo["nonce"], ack["nonce"])
        # PC restart and coordinator-side rewrite must not change verification input.
        self.agent.close()
        self.disk.save("stores/tsukumo.json", {**json.loads(raw), "journal": []})
        self.agent = Agent(self.home, self.pc, deadline_clock=lambda: self.clock.now - NOW,
            wall_clock=self.clock, sleep=self.agent_sleep, stage_runner=self.run_stage, log=Mock())
        self.addCleanup(self.agent.close)
        key, offer = next(iter(json.loads(raw)["offers"].items()))
        plan = {"run_id": "run-1", "confirm": {"tsukumo": [
            {"url": offer["url"], "offer_key": key, "identity": "fixture"}]}}
        verification = self.begin_stage("verification", raw, plan,
            {"satellite_commit": discovery.audit["result_commit"], "state_sha256": protocol.sha256_bytes(raw)})
        verified = json.loads(self.finish_stage(verification))
        self.assertEqual(self.inputs[1], raw)
        self.assertEqual(verified["journal"][:len(observed)], observed)
        self.assertEqual(len(verified["journal"]), 2)
        self.assertTrue(verified["verification"]["results"])

    def test_all_declines_cross_real_endpoints(self):
        reasons = ("refused", "unreachable", "intake_stopped", "code_mismatch", "dependency_mismatch",
                   "input_mismatch", "config_invalid", "retry_after", "clock_unverified", "clock_skew", "expired")
        for reason in reasons:
            with self.subTest(reason=reason):
                self.agent.close()
                self.setUp()
                control = ControlState(self.home)
                if reason == "refused":
                    control.add_refusal(refusal_fixture())
                elif reason == "unreachable":
                    for i in range(3):
                        control.record_run(str(i), 1, 0)
                elif reason == "intake_stopped":
                    control.stop_intake("fixture")
                elif reason == "retry_after":
                    control.set_retry_after("shop.tsukumo.co.jp", NOW + 300)
                if reason == "clock_unverified":
                    self.echo_delay = 121
                elif reason == "clock_skew":
                    self.agent.wall_clock = lambda: self.clock.now + 60
                handle = self.begin_stage()
                # Mutate the actual wire ticket/input, retaining valid schemas and hashes
                # except for the deliberately corrupt input_mismatch payload.
                commit, files = self.pc._read[0]
                if reason == "config_invalid":
                    cfg = json.loads(files["input/config.json"])
                    cfg["stores"]["tsukumo"]["browser_fallback"] = True
                    files["input/config.json"] = encode(cfg)
                    handle.ticket["input_sha256"]["input/config.json"] = protocol.sha256_bytes(files["input/config.json"])
                elif reason == "input_mismatch":
                    files["input/config.json"] += b" "
                elif reason == "code_mismatch":
                    handle.ticket["code_fingerprint"] = "0" * 64
                elif reason == "expired":
                    handle.ticket["collect_until"] = protocol.time_string(self.clock.now + 40)
                    self.echo_delay = 15
                files["ticket.json"] = encode(handle.ticket)
                before = (self.disk.root / "stores/tsukumo.json").read_bytes()
                if reason == "dependency_mismatch":
                    dependencies = {"ok": False, "declared": {}, "installed": {}, "mismatches": ["fixture"]}
                    with patch.object(agent_module.fingerprint, "dependency_status", return_value=dependencies):
                        self.finish_stage(handle, "satellite_declined:" + reason)
                else:
                    self.finish_stage(handle, "satellite_declined:" + reason)
                manifest = json.loads(self.coord.read(handle.audit["result_commit"], "manifest.json"))
                self.assertEqual(protocol.validate_manifest(manifest), manifest)
                self.assertIsNone(manifest["code_sha"])
                self.assertIsNone(manifest["output_sha256"])
                self.assertEqual((self.disk.root / "stores/tsukumo.json").read_bytes(), before)
                self.assert_no_requests()

    def test_unknown_positive_fingerprint_version_declines(self):
        handle = self.begin_stage()
        handle.ticket["fingerprint_version"] = 2
        protocol.validate_ticket(handle.ticket)
        self.pc._read[0][1]["ticket.json"] = encode(handle.ticket)
        self.finish_stage(handle, "satellite_declined:code_mismatch")
        self.assert_no_requests()

    def test_fingerprint_version_still_requires_positive_integer(self):
        handle = self.begin_stage()
        for value in (0, -1, True, "2", None):
            with self.subTest(value=value), self.assertRaises(protocol.ValidationError):
                protocol.validate_ticket({**handle.ticket, "fingerprint_version": value})

    def test_fresh_agent_examines_live_head_once_and_ignores_stale_head(self):
        # Mirror GitMailbox's fresh-start cursor contract, with old history present.
        stale = self.begin_stage()
        self.clock.sleep(800)
        current = self.begin_stage()
        self.pc = MemoryMailbox(self.pc._write, self.pc._read, start_after=None)
        self.agent.mailbox = self.pc
        self.finish_stage(current)
        self.assertEqual(self.runners, [current.audit["ticket_commit"]])
        self.clock.sleep(30)
        self.agent.step()
        self.assertEqual(len(self.runners), 1)
        # A fresh installation seeing only an expired ticket must do no work.
        self.agent.close()
        stale_head = self.begin_stage()
        self.clock.sleep(800)
        other = Agent(self.root / "fresh-pc", MemoryMailbox(self.pc._write, self.pc._read, start_after=None),
            deadline_clock=lambda: self.clock.now - NOW, wall_clock=self.clock,
            sleep=self.agent_sleep, stage_runner=self.run_stage, log=Mock())
        self.addCleanup(other.close)
        other.step()
        self.assertEqual(len(self.runners), 1)
        self.assertEqual(ControlState(self.root / "fresh-pc").data["processed"], {})
        self.assertEqual(other.state["relay_position"], stale_head.audit["ticket_commit"])
        self.assertNotIn(stale.audit["ticket_commit"], ControlState(self.home).data["processed"])

    def test_schema_vocabulary_does_not_allow_foreign_fetch_urls(self):
        raw = self.finish_stage(self.begin_stage())
        state = json.loads(raw)
        self.assertEqual(next(iter(state["offers"].values()))["evidence"][0]["fields"]["schema_availability"],
                         "https://schema.org/InStock")
        for value in ({"url": "https://schema.org/InStock"},
                      {"schema_availability": "https://example.com/InStock"},
                      {"schema_availability": "https://schema.org/unknown"}):
            with self.subTest(value=value), self.assertRaisesRegex(protocol.ValidationError, "foreign_url"):
                validation._walk_urls(value, "tsukumo")

    def test_stopped_states_are_accepted(self):
        for reason in sorted(stage.STOP_REASONS):
            with self.subTest(reason=reason):
                self.agent.close()
                self.setUp()
                # Exercise actual guard stops wherever HTTP/clock inputs suffice.
                if reason == "refused":
                    self.replies = [(403, {}, b"refused")]
                elif reason == "retry_after":
                    self.replies = [(429, {"Retry-After": "300"}, b"")]
                elif reason == "no_response":
                    self.sources["stores"]["tsukumo"]["seed_urls"] = [LIST, LIST + "second/"]
                    self.replies = [TimeoutError()] * 5
                elif reason in {"error", "killed"}:
                    # Process failures are injected at the runner boundary; the
                    # state itself is still produced by the real stage/Collector.
                    self.stage_reason = reason
                handle = self.begin_stage()
                if reason == "deadline":
                    handle.ticket["collect_until"] = protocol.time_string(self.clock.now + 38)
                    self.pc._read[0][1]["ticket.json"] = encode(handle.ticket)
                self.finish_stage(handle, "stopped:" + reason)
                if reason == "refused":
                    self.assertEqual(self.routes.status("tsukumo:direct", "sale_discovery"), "paused_refused")

    def test_refusal_blocks_next_run_until_ticket_clears_it(self):
        self.replies = [(403, {}, b"refused")]
        self.finish_stage(self.begin_stage(), "stopped:refused")
        refusal_id = ControlState(self.home).active_refusals[0]["refusal_id"]
        self.routes = RouteState(self.disk, self.config, self.clock.now)
        self.remote_stages = self.new_remote("run-2")
        count = len(self.pc._read)
        handle = self.begin_stage(raw=(self.disk.root / "stores/tsukumo.json").read_bytes())
        self.finish_stage(handle, "skipped:route_inactive", step=False)
        self.assertEqual(len(self.pc._read), count)
        self.config["routes"]["tsukumo:direct"]["cleared_refusals"] = [refusal_id]
        self.routes = RouteState(self.disk, self.config, self.clock.now)
        self.remote_stages = self.new_remote("run-3")
        self.replies = None
        handle = self.begin_stage(raw=(self.disk.root / "stores/tsukumo.json").read_bytes())
        self.finish_stage(handle)
        self.assertEqual(handle.ticket["cleared_refusals"], [refusal_id])
        self.assertEqual(ControlState(self.home).active_refusals, [])
        self.assertEqual(self.routes.status("tsukumo:direct", "sale_discovery"), "active")

    def test_absent_at_ack_window(self):
        handle = self.begin_stage()
        before = (self.disk.root / "stores/tsukumo.json").read_bytes()
        self.assertEqual(self.remote_stages.finish(handle, self.clock.sleep), ("satellite_absent", None))
        self.assertEqual(self.clock.now, NOW + 180)
        self.assertEqual((self.disk.root / "stores/tsukumo.json").read_bytes(), before)
        self.assertEqual(self.disk.load("satellite/run-1.json", {})["phases"]["discovery"]["outcome"], "satellite_absent")
        self.assert_no_requests()

    def test_slow_result_is_only_recorded_at_next_stage_zero(self):
        handle = self.begin_stage()
        self.result_delay = 800
        before = (self.disk.root / "stores/tsukumo.json").read_bytes()
        self.agent.step()  # ack/echo succeeds; actual stage output is posted too late
        self.finish_stage(handle, "satellite_deadline", step=False)
        self.assertEqual((self.disk.root / "stores/tsukumo.json").read_bytes(), before)
        self.assertFalse((self.disk.root / "satellite/late/run-1-discovery.json").exists())
        self.remote_stages = self.new_remote("run-2")
        records = self.disk.load("satellite/late/run-1-discovery.json", {})["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["manifest"]["status"], "completed")
        self.assertEqual(self.coord.read(records[0]["satellite_commit"], "stores/tsukumo.json"), self.outputs[0])
        self.assertEqual((self.disk.root / "stores/tsukumo.json").read_bytes(), before)

    def test_next_run_uses_rewritten_coordinator_state(self):
        first = json.loads(self.finish_stage(self.begin_stage()))
        self.assertTrue(first["journal"])
        first["journal"] = []  # stage D consumed the journal
        self.disk.save("stores/tsukumo.json", first)
        raw = (self.disk.root / "stores/tsukumo.json").read_bytes()
        self.remote_stages = self.new_remote("run-2")
        second = self.begin_stage(raw=raw)
        output = json.loads(self.finish_stage(second))
        self.assertEqual(self.inputs[-1], raw)
        self.assertEqual(output["journal"][:len(first["journal"])], first["journal"])
        self.assertEqual(len(output["journal"]), 1)
        self.assertEqual(output["journal"][0]["run_id"], "run-2")

    def test_real_repository_config_and_empty_initial_state(self):
        repo = Path(__file__).resolve().parents[1]
        paths = [repo / "config/sources.json", repo / "config/routes.json"]
        originals = [p.read_bytes() for p in paths]
        self.sources, self.config = [json.loads(raw) for raw in originals]
        route = self.config["routes"]["tsukumo:direct"]
        route["executor"] = "satellite"
        for name in ("sale_discovery", "product_verify"):
            route["functions"][name]["status"] = "active"
        route["functions"]["comparison_lookup"]["status"] = "paused_configuration"
        effective = validation.build_effective_config(self.sources, self.config, "tsukumo")
        self.assertEqual(validation.validate_effective_config(effective, "tsukumo"), 5)
        self.routes = RouteState(self.disk, self.config, self.clock.now)
        self.remote_stages = self.new_remote()
        # Exact fallback shape used by pipeline.run when the store file is absent.
        raw = encode({"schema_version": 1, "store": "tsukumo", "queue": {}, "done": [],
                      "offers": {}, "journal": [], "cycle_complete": False})
        handle = self.begin_stage(raw=raw)
        output = json.loads(self.finish_stage(handle))
        self.assertEqual(self.transports[0].calls[0], effective["stores"]["tsukumo"]["seed_urls"][0])
        self.assertTrue(output["journal"])
        self.assertEqual([p.read_bytes() for p in paths], originals)

    def test_real_git_both_endpoints_and_echo_between_tickets(self):
        self.agent.close()
        repo = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True, capture_output=True)
        self.coord = GitMailbox(str(repo), protocol.RELAY_BRANCH, protocol.SATELLITE_BRANCH,
                                self.root / "actions-mailbox", start_after="")
        # The factory used by Agent/__main__, with only the remote redirected locally.
        with patch.object(protocol, "REMOTE_URL", str(repo)):
            self.pc = make_mailbox(self.home)
        self.assertEqual(self.pc.poll(), [])  # missing branch
        self.remote_stages = self.new_remote()
        self.agent = Agent(self.home, self.pc, deadline_clock=lambda: self.clock.now - NOW,
            wall_clock=self.clock, sleep=self.agent_sleep, stage_runner=self.fake_git_stage, log=Mock())
        self.addCleanup(self.agent.close)
        first = self.begin_stage()
        # Simulate installation/restart after the live ticket was already posted.
        with patch.object(protocol, "REMOTE_URL", str(repo)):
            self.pc = make_mailbox(self.home)
        self.agent.mailbox = self.pc
        extra = []
        self.after_echo = lambda: extra.append(self.begin_stage())
        raw = self.finish_stage(first)
        self.finish_stage(extra[0], step=False)
        history = subprocess.run(["git", "--git-dir", str(repo), "rev-list", "--reverse", protocol.RELAY_BRANCH],
                                 check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(history[:3], [first.audit["ticket_commit"], first.audit["echo_commit"], extra[0].audit["ticket_commit"]])
        self.assertEqual(len(self.runners), 2)
        key, offer = next(iter(json.loads(raw)["offers"].items()))
        plan = {"run_id": "run-1", "confirm": {"tsukumo": [
            {"url": offer["url"], "offer_key": key, "identity": "fixture"}]}}
        verification = self.begin_stage("verification", raw, plan,
            {"satellite_commit": first.audit["result_commit"], "state_sha256": protocol.sha256_bytes(raw)})
        verified = json.loads(self.finish_stage(verification))
        self.assertEqual(verified["journal"][:1], json.loads(raw)["journal"])
        self.assertEqual(len(verified["journal"]), 2)

    def fake_git_stage(self, work):
        self.runners.append(work.name)
        ticket = read_json(work / "ticket.json", {})
        state = read_json(work / "input/stores/tsukumo.json", {})
        state.update(run_id=ticket["run_id"], phase=ticket["phase"])
        offer = offer_fixture(observed_at=protocol.time_string(self.clock.now), observed_run_id=ticket["run_id"])
        row = offer.to_dict()
        state["offers"][offer.key] = row
        state["journal"].append({"observation_id": digest([offer.key, offer.observed_at, row]),
                                 "run_id": ticket["run_id"], "offer": row})
        # Unusual whitespace proves that neither endpoint reserializes the result.
        (work / "state/stores/tsukumo.json").write_bytes(encode(state) + b" \r\n")
        atomic_json(work / "stage_result.json", {"reason": "completed", "counters": dict.fromkeys(stage.COUNTERS, 0)})
        return FakeProcess()
