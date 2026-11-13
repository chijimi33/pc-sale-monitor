from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from sale_monitor.satellite import agent as agent_module
from sale_monitor.satellite.agent import Agent, LockHeld, OSLock, encode
from sale_monitor.satellite.__main__ import main, status, update
from sale_monitor.satellite.control import ControlState
from sale_monitor.satellite.guard import Guard, CommunicationForbidden
from sale_monitor.satellite.deadline import Handshake
from sale_monitor.satellite.protocol import sha256_bytes, time_string
from sale_monitor.storage import atomic_json, read_json
from test_satellite_deadline import FakeClock
from test_satellite_protocol import COMMIT, NOW, URL, refusal_fixture, ticket_fixture
from test_satellite_validation import config_fixture


IDENTITY = {"fingerprint": "b" * 64, "fingerprint_version": 1, "files": 12}
DEPENDENCIES = {"ok": True, "declared": {}, "installed": {}, "mismatches": []}


def wire_status(kind, reason=None):
    return kind if reason is None else kind + ":" + reason


def inputs_fixture(config=None, state=None, plan=None):
    config = config or config_fixture()
    state = state or dict(schema_version=1, store="tsukumo", queue={}, done=[], offers={}, journal=[], cycle_complete=True)
    inputs = {"input/config.json": encode(config), "input/routes.json": encode({
        "schema_version": 1, "config_revision": 1, "routes": {"tsukumo:direct": config["route"]}}),
        "input/stores/tsukumo.json": encode(state)}
    if plan is not None:
        inputs["input/plan.json"] = encode(plan)
    return inputs


class MemoryMailbox:
    def __init__(self, clock):
        self.clock = clock
        self.commits = {}
        self.pending = []
        self.posts = []
        self.attempts = []
        self.last = None
        self.echo_mode = "normal"
        self.fail_results = self.fail_polls = 0
        self.echo_delay = 0
        self.fail_ack = False
        self.outbound = self.waiting = self.inbound = 0

    def add(self, commit=COMMIT, inputs=None, **changes):
        inputs = inputs or inputs_fixture()
        ticket = ticket_fixture(input_sha256={path: sha256_bytes(data) for path, data in inputs.items()}, **changes)
        self.commits[commit] = {"ticket.json": encode(ticket), **inputs}
        self.pending.append(commit)
        return ticket

    def poll(self):
        if self.fail_polls:
            self.fail_polls -= 1
            raise RuntimeError("secret URL must never be logged")
        pending, self.pending = self.pending, []
        if pending:
            self.last = pending[-1]
        return pending

    def read(self, commit, path):
        return self.commits[commit][path]

    def position(self):
        return self.last

    def post(self, files, message):
        self.attempts.append(dict(files))
        manifest = json.loads(files["manifest.json"])
        if manifest["kind"] == "result" and self.fail_results:
            self.fail_results -= 1
            raise RuntimeError("post failed")
        if manifest["kind"] == "ack" and self.fail_ack:
            raise RuntimeError("ack failed")
        self.posts.append(dict(files))
        if manifest["kind"] == "ack" and self.echo_mode != "missing":
            self.clock.advance(self.outbound + self.waiting)
            actions = self.clock.w
            self.clock.advance(self.echo_delay + self.inbound)
            nonce = manifest["nonce"] if self.echo_mode != "nonce" else "wrong_nonce_123456"
            commit = manifest["ticket_commit"] if self.echo_mode != "ticket" else "e" * 40
            self.commits["f" * 40] = {"echo.json": encode(dict(ticket_commit=commit, nonce=nonce,
                actions_time=time_string(actions - 60 if self.echo_mode == "skew" else actions)))}
            self.pending.append("f" * 40)
        return "c" * 40

    def manifests(self, kind="result"):
        return [json.loads(files["manifest.json"]) for files in self.posts
                if json.loads(files["manifest.json"])["kind"] == kind]


class FakeProcess:
    def __init__(self, running=False):
        self.returncode = None if running else 0
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.killed = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = FakeClock()
        self.mailbox = MemoryMailbox(self.clock)
        self.calls = []
        self.runner = self.stage_runner
        self.running = False
        self.stage_reason = "completed"
        self.stage_counts = dict(requests=1, ordinary_attempted=1, ordinary_succeeded=1, no_response=0, refusal=0)
        self.process = None
        for name, value in (("code_fingerprint", IDENTITY), ("dependency_status", DEPENDENCIES)):
            p = patch.object(agent_module.fingerprint, name, return_value=deepcopy(value))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(agent_module, "code_sha", return_value=COMMIT)
        p.start()
        self.addCleanup(p.stop)
        self.agent = self.make_agent()

    def make_agent(self):
        agent = Agent(self.home, self.mailbox, deadline_clock=lambda: self.clock.m,
            wall_clock=lambda: self.clock.w, sleep=self.clock.advance,
            stage_runner=lambda work: self.runner(work), log=Mock())
        self.addCleanup(agent.close)
        return agent

    def stage_runner(self, work):
        self.calls.append(work)
        handshake = Handshake.from_dict(read_json(work / "handshake.json", None),
            deadline_clock=lambda: self.clock.m, wall_clock=lambda: self.clock.w)
        self.assertTrue(handshake.may_request(NOW + 500))
        state = (work / "input/stores/tsukumo.json").read_bytes()
        self.output = state + b" "
        (work / "state/stores/tsukumo.json").write_bytes(self.output)
        atomic_json(work / "stage_result.json", {"reason": self.stage_reason, "counters": self.stage_counts})
        self.process = FakeProcess(self.running)
        return self.process

    def assert_declined(self, reason):
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("declined", reason))
        self.assertIn("active_refusals", self.mailbox.manifests()[-1])
        self.assertIsNone(self.mailbox.manifests()[-1]["output_sha256"])
        self.assertEqual(self.mailbox.manifests()[-1]["counters"]["requests"], 0)
        self.assertEqual(set(self.mailbox.posts[-1]), {"manifest.json"})
        self.assertFalse(self.calls)

    def test_success_exact_hashes_handshake_and_no_duplicate(self):
        ticket = self.mailbox.add()
        self.agent.step()
        result = self.mailbox.manifests()[-1]
        self.assertEqual(result["status"], wire_status("completed"))
        self.assertEqual(result["config_sha256"], ticket["input_sha256"]["input/config.json"])
        self.assertEqual(result["output_sha256"], sha256_bytes(self.output))
        self.assertEqual(self.mailbox.posts[-1]["stores/tsukumo.json"], self.output)
        self.assertEqual(self.mailbox.manifests("ack")[0]["active_refusals"], [])
        self.mailbox.pending.append(COMMIT)
        self.clock.advance(30)
        self.agent.step()
        self.assertEqual(len(self.calls), 1)
        self.agent.close()
        self.agent = self.make_agent()
        self.mailbox.pending.append(COMMIT)
        self.agent.step()
        self.assertEqual(len(self.calls), 1)

    def test_code_mismatch(self):
        self.mailbox.add(code_fingerprint="1" * 64)
        self.assert_declined("code_mismatch")

    def test_dependency_mismatch(self):
        self.mailbox.add()
        with patch.object(agent_module.fingerprint, "dependency_status", return_value={
                **DEPENDENCIES, "ok": False, "mismatches": ["lxml"]}):
            self.assert_declined("dependency_mismatch")

    def test_input_mismatch(self):
        self.mailbox.add()
        self.mailbox.commits[COMMIT]["input/config.json"] += b" "
        self.assert_declined("input_mismatch")

    def test_missing_input(self):
        self.mailbox.add()
        del self.mailbox.commits[COMMIT]["input/routes.json"]
        self.assert_declined("input_mismatch")

    def test_config_invalid(self):
        config = config_fixture()
        config["stores"]["tsukumo"]["browser_fallback"] = True
        self.mailbox.add(inputs=inputs_fixture(config))
        self.assert_declined("config_invalid")

    def test_foreign_queue_declined_before_stage_or_communication(self):
        state = json.loads(inputs_fixture()["input/stores/tsukumo.json"])
        state["queue"] = {"foreign": {"type": "product", "url": "https://example.com/item"}}
        self.mailbox.add(inputs=inputs_fixture(state=state))
        with patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
            self.assert_declined("config_invalid")
        self.assertFalse(self.mailbox.manifests("ack"))

    def test_route_inactive(self):
        inputs = inputs_fixture()
        routes = json.loads(inputs["input/routes.json"])
        routes["routes"]["tsukumo:direct"]["functions"]["sale_discovery"]["status"] = "paused_configuration"
        inputs["input/routes.json"] = encode(routes)
        self.mailbox.add(inputs=inputs)
        self.assert_declined("config_invalid")

    def test_refusal_unreachable_and_intake_declines(self):
        for reason in ("refused", "unreachable", "intake_stopped"):
            with self.subTest(reason=reason):
                self.agent.close()
                control = ControlState(self.home)
                control.clear_local()
                control.apply_ticket({"cleared_refusals": ["refusal-1"]} if reason != "refused" else {})
                if reason == "refused":
                    control.add_refusal(refusal_fixture())
                elif reason == "unreachable":
                    for run in range(3):
                        control.record_run(str(run), 1, 0)
                else:
                    control.stop_intake("test")
                commit = str(("refused", "unreachable", "intake_stopped").index(reason) + 1) * 40
                self.mailbox.add(commit)
                self.agent = self.make_agent()
                self.assert_declined(reason)
                self.assertEqual(bool(self.mailbox.manifests()[-1]["active_refusals"]), reason == "refused")

    def test_retry_after_control_and_input(self):
        host = "shop.tsukumo.co.jp"
        for source in ("control", "input"):
            with self.subTest(source=source):
                self.agent.close()
                state = json.loads(inputs_fixture()["input/stores/tsukumo.json"])
                if source == "control":
                    ControlState(self.home).set_retry_after(host, NOW + 300)
                else:
                    control = ControlState(self.home)
                    control.data["retry_after_until"] = {}
                    control.save()
                    state["retry_after"] = {host: NOW + 500}
                self.mailbox.add("1" * 40 if source == "control" else "2" * 40, inputs=inputs_fixture(state=state))
                self.agent = self.make_agent()
                self.assert_declined("retry_after")

    def test_stale_and_foreign_ticket_ignored(self):
        self.mailbox.add()
        self.clock.advance(501)
        self.agent.step()
        self.assertFalse(self.mailbox.posts)
        self.assertFalse(self.calls)
        self.mailbox.commits["b" * 40] = {"ticket.json": encode({"store": "ark"})}
        self.mailbox.pending.append("b" * 40)
        self.clock.advance(30)
        self.agent.step()
        self.assertFalse(self.mailbox.posts)

    def test_echo_failures_and_suspend(self):
        for mode in ("missing", "nonce", "ticket", "late", "suspend"):
            with self.subTest(mode=mode):
                self.agent.close()
                self.mailbox.echo_mode = mode
                self.mailbox.echo_delay = 121 if mode in ("late", "suspend") else 0
                self.mailbox.add(str(("missing", "nonce", "ticket", "late", "suspend").index(mode) + 1) * 40)
                self.agent = self.make_agent()
                self.assert_declined("clock_unverified")

    def test_definite_clock_skew(self):
        self.mailbox.add()
        self.mailbox.echo_mode = "skew"
        self.assert_declined("clock_skew")

    def test_expired_after_echo(self):
        self.mailbox.add(collect_until=time_string(NOW + 20))
        self.mailbox.echo_delay = 21
        self.assert_declined("expired")

    def test_child_killed_saved_state_posted(self):
        self.mailbox.add(collect_until=time_string(NOW + 2))
        self.running = True
        self.agent.step()
        self.assertTrue(self.process.killed)
        self.assertEqual(self.clock.m, 32)
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", "killed"))
        self.assertEqual(self.mailbox.posts[-1]["stores/tsukumo.json"], self.output)

    def test_child_exiting_during_kill_check_is_not_killed(self):
        self.mailbox.add(collect_until=time_string(NOW + 2))
        self.running = True
        original = Handshake.must_kill
        def must_kill(handshake, until):
            expired = original(handshake, until)
            if expired:
                self.process.returncode = 0
            return expired
        with patch.object(Handshake, "must_kill", must_kill):
            self.agent.step()
        self.assertFalse(self.process.killed)
        self.assertEqual(self.mailbox.manifests()[-1]["status"], "completed")

    def test_child_cannot_report_killed_without_parent_killing_it(self):
        self.mailbox.add()
        self.stage_reason = "killed"
        self.agent.step()
        self.assertFalse(self.process.killed)
        self.assertEqual(self.mailbox.manifests()[-1]["status"], "stopped:error")

    def test_new_ticket_during_collection_is_queued(self):
        self.mailbox.add(collect_until=time_string(NOW + 2))
        self.running = True
        original = self.runner
        def run(work):
            process = original(work)
            self.mailbox.add("b" * 40, run_id="next", collect_until=time_string(NOW + 10))
            return process
        self.runner = run
        self.agent.step()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.mailbox.manifests()), 1)

    def test_post_retry_identical_bytes_survives_restart(self):
        self.mailbox.add()
        self.mailbox.fail_results = 1
        self.agent.step()
        failed = self.mailbox.attempts[-1]
        self.assertTrue(ControlState(self.home).data["unsent_results"])
        self.agent.close()
        self.agent = self.make_agent()
        self.clock.advance(30)
        self.agent.step()
        self.assertEqual(self.mailbox.posts[-1], failed)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(ControlState(self.home).data["unsent_results"])

    def test_expired_queued_result_retained(self):
        self.mailbox.add()
        self.mailbox.fail_results = 10
        self.agent.step()
        self.clock.advance(86400 + 711)
        attempts = len(self.mailbox.attempts)
        self.agent.step()
        self.assertEqual(len(self.mailbox.attempts), attempts)
        queued = ControlState(self.home).data["unsent_results"][COMMIT]
        self.assertTrue(queued["expired"])
        self.assertTrue((self.home / queued["files"]["manifest.json"]).exists())

    def test_claimed_ticket_after_crash_is_not_collected(self):
        self.mailbox.add()
        ControlState(self.home).claim_ticket(COMMIT)
        self.agent.step()
        self.assertFalse(self.calls)
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", "error"))
        self.assertIn("stores/tsukumo.json", self.mailbox.posts[-1])

    def test_refusal_during_stage_latches_and_only_ticket_clears(self):
        original = self.runner
        def refused(work):
            process = original(work)
            control = ControlState(self.home)
            handshake = Handshake.from_dict(read_json(work / "handshake.json", None),
                deadline_clock=lambda: self.clock.m, wall_clock=lambda: self.clock.w)
            guard = Guard("tsukumo", control, handshake, NOW + 500,
                wall_clock=lambda: self.clock.w, sleep=self.clock.advance, refusal_id=lambda: "refusal-1")
            guard.before_request(URL)
            with self.assertRaises(CommunicationForbidden):
                guard.after_response(URL, 403, {}, b"no")
            atomic_json(work / "stage_result.json", {"reason": guard.reason, "counters": guard.counters})
            return process
        self.runner = refused
        self.mailbox.add()
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", "refused"))
        self.assertEqual(self.mailbox.manifests()[-1]["active_refusals"], [refusal_fixture()])
        self.mailbox.add("b" * 40, config_revision=2)
        self.clock.advance(30)
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("declined", "refused"))
        self.assertEqual(len(self.calls), 1)
        self.runner = self.stage_runner
        self.mailbox.add("c" * 40, cleared_refusals=["refusal-1"])
        self.clock.advance(30)
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("completed"))
        self.assertEqual(self.mailbox.manifests()[-1]["active_refusals"], [])

    def test_unreachable_accounted_once_per_run_and_clear_local(self):
        self.stage_counts.update(ordinary_succeeded=0, no_response=1)
        self.stage_reason = "no_response"
        for index, run in enumerate(("one", "one", "two", "three", "four")):
            self.mailbox.add(str(index + 1) * 40, run_id=run)
            self.clock.advance(30)
            self.agent.step()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("declined", "unreachable"))
        self.assertEqual(ControlState(self.home).data["zero_success_runs"], 3)
        self.agent.close()
        with patch("builtins.print") as output:
            self.assertEqual(main(["clear-local", "--home", str(self.home)]), 0)
        self.assertIn("cleared_refusals", output.call_args.args[0])
        self.assertFalse(ControlState(self.home).data["unreachable"])

    def test_accounting_timeout_and_excluded_declines(self):
        self.stage_counts.update(ordinary_succeeded=0)
        self.mailbox.add()
        self.agent.step()
        self.clock.advance(710 + 3601)
        self.agent.step()
        self.assertEqual(ControlState(self.home).data["zero_success_runs"], 1)
        self.mailbox.add("b" * 40, run_id="mismatch", code_fingerprint="1" * 64,
            issued_at=time_string(self.clock.w), collect_until=time_string(self.clock.w + 100),
            accept_until=time_string(self.clock.w + 200))
        self.clock.advance(30)
        self.agent.step()
        self.clock.advance(3801)
        self.agent.step()
        self.assertEqual(ControlState(self.home).data["zero_success_runs"], 1)

    def test_stale_new_run_finalizes_previous_without_counting_stale(self):
        self.stage_counts.update(ordinary_succeeded=0)
        self.mailbox.add()
        self.agent.step()
        self.mailbox.add("b" * 40, run_id="stale", collect_until=time_string(NOW + 1))
        self.clock.advance(30)
        self.agent.step()
        control = ControlState(self.home)
        self.assertEqual(control.data["zero_success_runs"], 1)
        self.assertEqual(set(control.data["runs"]), {"run-1"})
        self.assertEqual(len(self.mailbox.manifests()), 1)

    def test_clock_stop_does_not_count_as_unreachable_run(self):
        self.stage_counts.update(ordinary_succeeded=0)
        self.stage_reason = "error"
        original = self.runner
        def run(work):
            process = original(work)
            atomic_json(work / "stage_result.json", {"reason": "error", "guard_reason": "clock_unverified",
                        "counters": self.stage_counts})
            return process
        self.runner = run
        self.mailbox.add()
        self.agent.step()
        self.clock.advance(710 + 3601)
        self.agent.step()
        control = ControlState(self.home)
        self.assertEqual(control.data["zero_success_runs"], 0)
        self.assertTrue(control.data["runs"]["run-1"]["excluded"])

    def test_verification_requires_accepted_input_hash(self):
        inputs = inputs_fixture(plan={"run_id": "run-1", "confirm": {"tsukumo": []}})
        self.mailbox.add(inputs=inputs, phase="verification",
            accepted_discovery={"satellite_commit": COMMIT, "state_sha256": "1" * 64})
        self.assert_declined("input_mismatch")

    def test_transport_backoff_caps_and_logs_only_type(self):
        self.mailbox.fail_polls = 9
        self.agent.step()
        for delay in (30, 60, 120, 240, 300, 300):
            self.assertEqual(self.agent.next_io - self.clock.m, delay)
            self.clock.advance(delay)
            self.agent.step()
        self.assertNotIn("secret URL", str(self.agent.log.mock_calls))

    def test_second_agent_and_update_share_lock(self):
        self.agent.step()
        second = self.make_agent()
        with self.assertRaisesRegex(LockHeld, "already running"):
            second.step()
        command = Mock()
        with self.assertRaises(LockHeld):
            update(self.home, command=command)
        command.assert_not_called()
        self.agent.close()
        second.step()

    def test_stage_start_failure_posts_saved_input(self):
        self.mailbox.add()
        self.runner = Mock(side_effect=OSError("launch failed"))
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", "error"))
        self.assertEqual(self.mailbox.posts[-1]["stores/tsukumo.json"], inputs_fixture()["input/stores/tsukumo.json"])

    def test_ticket_and_input_bytes_reach_child_unchanged(self):
        ticket = self.mailbox.add()
        raw = json.dumps(ticket, indent=3).encode() + b"  \n"
        self.mailbox.commits[COMMIT]["ticket.json"] = raw
        original = self.runner
        def run(work):
            self.assertEqual((work / "ticket.json").read_bytes(), raw)
            for path in ticket["input_sha256"]:
                self.assertEqual((work / path).read_bytes(), self.mailbox.read(COMMIT, path))
            return original(work)
        self.runner = run
        self.agent.step()

    def test_claim_and_clear_happen_before_fingerprint(self):
        ControlState(self.home).add_refusal(refusal_fixture())
        self.mailbox.add(cleared_refusals=["refusal-1"])
        def inspect(*args):
            control = ControlState(self.home)
            self.assertEqual(control.data["processed"][COMMIT], "started")
            self.assertEqual(control.active_refusals, [])
            return IDENTITY
        with patch.object(agent_module.fingerprint, "code_fingerprint", side_effect=inspect):
            self.agent.step()
        self.assertEqual(len(self.calls), 1)

    def test_outbound_wait_and_return_delays_bound_first_request(self):
        for index, delays in enumerate(((2, 7, 11), (20, 30, 60), (0, 0, 120))):
            with self.subTest(delays=delays):
                self.agent.close()
                self.clock = FakeClock()
                self.mailbox = MemoryMailbox(self.clock)
                self.mailbox.outbound, self.mailbox.waiting, self.mailbox.inbound = delays
                self.mailbox.add(str(index + 1) * 40)
                original = self.stage_runner
                def run(work):
                    handshake = Handshake.from_dict(read_json(work / "handshake.json", None),
                        deadline_clock=lambda: self.clock.m, wall_clock=lambda: self.clock.w)
                    actual_actions_now = NOW + self.clock.m
                    self.assertGreaterEqual(handshake.actions_now_upper(), actual_actions_now)
                    self.assertLess(actual_actions_now, NOW + 500)
                    return original(work)
                self.runner = run
                self.agent = self.make_agent()
                self.agent.step()
                self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("completed"))

    def test_slow_poll_cannot_delay_kill(self):
        self.mailbox.add(collect_until=time_string(NOW + 2))
        self.running = True
        release = threading.Event()
        entered = threading.Event()
        original_poll = self.mailbox.poll
        original_runner = self.runner
        def slow_poll():
            entered.set()
            release.wait(5)
            return original_poll()
        def runner(work):
            process = original_runner(work)
            self.mailbox.poll = slow_poll
            original_terminate = process.terminate
            def terminate():
                self.assertEqual(self.clock.m, 32)
                self.assertTrue(entered.wait(1))
                original_terminate()
                release.set()
            process.terminate = terminate
            return process
        self.runner = runner
        try:
            self.agent.step()
            self.assertTrue(self.process.killed)
            if self.agent.poll_thread is not None:
                self.agent.poll_thread.join(2)
            self.agent.step()
            self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", "killed"))
        finally:
            release.set()

    def test_corrupt_child_result_posts_state_as_error(self):
        self.mailbox.add()
        original = self.runner
        def run(work):
            process = original(work)
            (work / "stage_result.json").write_bytes(b"{broken")
            return process
        self.runner = run
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", "error"))
        self.assertEqual(self.mailbox.posts[-1]["stores/tsukumo.json"], self.output)

    def test_child_exit_reasons_use_coordinator_status_vocabulary(self):
        for index, reason in enumerate(("deadline", "retry_after", "no_response", "error")):
            with self.subTest(reason=reason):
                self.stage_reason = reason
                self.mailbox.add(str(index + 1) * 40)
                self.clock.advance(30)
                self.agent.step()
                self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("stopped", reason))

    def test_mailbox_initialization_error_backs_off_with_lock_retained(self):
        self.agent.mailbox = None
        with patch.object(agent_module, "make_mailbox", side_effect=RuntimeError("secret")) as factory:
            self.agent.step()
            self.agent.step()
            factory.assert_called_once()
            self.assertIsNotNone(self.agent.lock)
            self.clock.advance(30)
            self.agent.step()
            self.assertEqual(factory.call_count, 2)
        self.agent.mailbox = self.mailbox

    def test_ack_post_failure_declines_without_collection(self):
        self.mailbox.add()
        self.mailbox.fail_ack = True
        self.agent.step()
        self.assertFalse(self.calls)
        self.clock.advance(30)
        self.agent.step()
        self.assertEqual(self.mailbox.manifests()[-1]["status"], wire_status("declined", "clock_unverified"))

    def test_null_code_sha_and_status_summary(self):
        self.mailbox.add()
        with patch.object(agent_module, "code_sha", return_value=None):
            self.agent.step()
        self.assertIsNone(self.mailbox.manifests()[-1]["code_sha"])
        summary = status(self.home)
        self.assertIn((COMMIT, "completed"), summary["last_tickets"])
        self.assertFalse(summary["queued_results"])


class StartupTests(unittest.TestCase):
    def test_mailbox_builder_uses_fixed_branches_and_persisted_cursor(self):
        module = types.ModuleType("sale_monitor.satellite.transport")
        module.GitMailbox = Mock()
        with tempfile.TemporaryDirectory() as home, patch.dict(sys.modules, {module.__name__: module}):
            atomic_json(Path(home) / "agent.json", {"relay_position": COMMIT})
            agent_module.make_mailbox(home)
        args, kwargs = module.GitMailbox.call_args
        self.assertEqual(args[:3], (agent_module.protocol.REMOTE_URL,
            agent_module.protocol.SATELLITE_BRANCH, agent_module.protocol.RELAY_BRANCH))
        self.assertEqual(kwargs["start_after"], COMMIT)
        self.assertEqual(kwargs["timeout"], 60)

    def test_windows_child_contained_before_resume(self):
        process = FakeProcess()
        calls = []
        with patch.object(agent_module.os, "name", "nt"), \
                patch.object(agent_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
                patch.object(agent_module.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(agent_module, "_kill_on_close", side_effect=lambda p: calls.append("contain")), \
                patch.object(agent_module, "_attach_job", side_effect=lambda p: calls.append("resume")):
            self.assertIs(agent_module.start_stage("work"), process)
        self.assertEqual(calls, ["contain", "resume"])
        self.assertTrue(spawn.call_args.kwargs["creationflags"] & 4)
        self.assertEqual(spawn.call_args.args[0][1:], ["-m", "sale_monitor.satellite", "stage", "--work", "work"])

    def test_windows_parent_job_configured_before_assignment(self):
        kernel = Mock()
        kernel.CreateJobObjectW.return_value = 123
        kernel.SetInformationJobObject.return_value = 1
        kernel.AssignProcessToJobObject.return_value = 1
        process = Mock(_handle=456)
        with patch.object(agent_module, "_job_api", return_value=kernel):
            agent_module._kill_on_close(process)
            self.assertEqual(process._satellite_job, 123)
            args = kernel.SetInformationJobObject.call_args.args
            self.assertEqual(args[0:2], (123, 9))
            self.assertEqual(args[2]._obj.basic.flags, 0x2000)
            self.assertEqual([call[0] for call in kernel.mock_calls], ["CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject"])
            agent_module._close_parent_job(process)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_posix_child_owns_new_session(self):
        process = FakeProcess()
        with patch.object(agent_module.os, "name", "posix"), \
                patch.object(agent_module.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(agent_module, "_attach_job"):
            agent_module.start_stage("work")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertTrue(process._pipeline_owned_group)


class UpdateTests(unittest.TestCase):
    def test_update_failure_stops_intake_without_remote_leak(self):
        for failure in ("origin", "fetch", "merge", "pip"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as home:
                calls = []
                def command(args, **kwargs):
                    calls.append(args)
                    self.assertTrue(kwargs["check"])
                    self.assertIn("timeout", kwargs)
                    if failure == "origin":
                        return Mock(stdout="https://wrong.invalid/secret")
                    if failure in args:
                        raise subprocess.CalledProcessError(1, args)
                    return Mock(stdout=agent_module.protocol.REMOTE_URL)
                output = Mock()
                self.assertEqual(update(home, command=command, output=output), 1)
                self.assertTrue(ControlState(home).data["intake_stopped"])
                self.assertNotIn("secret", str(output.call_args))
                self.assertEqual(len(calls), {"origin": 1, "fetch": 2, "merge": 3, "pip": 4}[failure])

    def test_update_success_only_clears_intake(self):
        with tempfile.TemporaryDirectory() as home:
            control = ControlState(home)
            control.stop_intake("failed")
            control.add_refusal(refusal_fixture())
            control.data["unreachable"] = True
            control.save()
            command = Mock(return_value=Mock(stdout=agent_module.protocol.REMOTE_URL))
            with patch.object(agent_module.fingerprint, "code_fingerprint", return_value=IDENTITY), \
                    patch.object(agent_module.fingerprint, "dependency_status", return_value=DEPENDENCIES):
                self.assertEqual(update(home, command=command, output=Mock()), 0)
            updated = ControlState(home)
            self.assertFalse(updated.data["intake_stopped"])
            self.assertTrue(updated.data["unreachable"])
            self.assertEqual(updated.active_refusals, [refusal_fixture()])
            arguments = [call.args[0] for call in command.call_args_list]
            self.assertEqual(arguments[-1], [sys.executable, "-m", "pip", "install", "-e", str(Path(agent_module.__file__).parents[2])])
            self.assertIn("--ff-only", arguments[2])
