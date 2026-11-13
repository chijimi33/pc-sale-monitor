from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sale_monitor.satellite.control import ControlState
from sale_monitor.satellite.protocol import ValidationError
from test_satellite_protocol import COMMIT, refusal_fixture


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = ControlState(self.root)

    def reload(self):
        return ControlState(self.root)

    def test_refusal_survives_reload_and_only_ticket_clears(self):
        self.control.add_refusal(refusal_fixture())
        self.control.stop_intake("failed_update")
        self.control.clear_local()
        self.assertEqual(self.reload().decline_reason({"config_revision": 100, "executor": "runner"}), "refused")
        self.assertIsNone(self.control.decline_reason({"cleared_refusals": ["refusal-1"]}))
        self.control.add_refusal(refusal_fixture())
        self.assertFalse(self.reload().active_refusals)

    def test_decline_priority(self):
        self.control.stop_intake("failed_update")
        self.assertEqual(self.reload().decline_reason({}), "intake_stopped")
        for run in range(3):
            self.control.record_run(str(run), 1, 0)
        self.assertEqual(self.reload().decline_reason({}), "unreachable")
        self.control.add_refusal(refusal_fixture())
        self.assertEqual(self.reload().decline_reason({}), "refused")

    def test_run_count_and_latch(self):
        self.control.record_run("1", 5, 0)
        self.control.record_run("1", 5, 0)
        self.assertEqual(self.control.data["zero_success_runs"], 1)
        for name in ("mismatch", "expired", "retry_after", "clock", "resend"):
            self.control.record_run(name, 0, 0, excluded=True)
        self.control.record_run("429-after-attempt", 1, 0, excluded=True)
        self.control.record_run("robots-only", 0, 0)
        self.assertEqual(self.control.data["zero_success_runs"], 1)
        self.control.record_run("success", 2, 1)
        self.assertEqual(self.control.data["zero_success_runs"], 0)
        for run in ("2", "3", "4"):
            self.control.record_run(run, 5, 0)
        self.control.record_run("5", 1, 1)
        self.assertTrue(self.reload().data["unreachable"])
        self.control.clear_local()
        self.assertFalse(self.reload().data["unreachable"])

    def test_retry_and_last_request_survive(self):
        self.control.set_retry_after("host", 20)
        self.control.set_retry_after("host", 10)
        self.control.request_started("host", 123)
        self.assertEqual(self.reload().retry_until("host", 30), 30)
        self.assertEqual(self.reload().retry_until("host", 10), 20)
        self.assertEqual(self.reload().data["last_request_at"]["host"], 123)

    def test_claim_before_collect_and_unsent_results(self):
        self.assertTrue(self.control.claim_ticket(COMMIT))
        self.assertFalse(self.reload().claim_ticket(COMMIT))
        self.control.finish_ticket(COMMIT, "completed")
        data = {"manifest_path": "out/manifest.json", "state_path": "out/stores/tsukumo.json"}
        self.control.queue_result(COMMIT, data)
        data["state_path"] = "changed"
        reloaded = self.reload()
        self.assertEqual(reloaded.data["processed"][COMMIT], "completed")
        self.assertEqual(reloaded.data["unsent_results"][COMMIT]["state_path"], "out/stores/tsukumo.json")
        reloaded.result_sent(COMMIT)
        self.assertFalse(self.reload().data["unsent_results"])

    def test_atomic_file_and_corruption(self):
        self.control.save()
        self.assertTrue((self.root / "control.json").exists())
        self.assertFalse((self.root / "control.json.tmp").exists())
        (self.root / "control.json").write_text('{"broken": true}', encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.reload()

    def test_injected_io(self):
        saved = {}
        def write(path, value):
            saved[str(path)] = deepcopy(value)
        def read(path, default):
            return deepcopy(saved.get(str(path), default))
        first = ControlState("unused", read=read, write=write)
        first.stop_intake("failed_update")
        self.assertEqual(ControlState("unused", read=read, write=write).decline_reason({}), "intake_stopped")
