from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

QA = Path(__file__).resolve().parents[1] / "scripts/qa"
sys.path.insert(0, str(QA))
from common import atomic, digest, finalize, lock, read
from broker import Broker
from net import validate
from worker import urls
import worker
from review import verify


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo/sale_monitor").mkdir(parents=True)
        (self.root / "repo/tests").mkdir()
        atomic(self.root / "input.json", {"immutable": True})
        atomic(self.root / "job.json", {"python": sys.executable, "allowed_urls": []})
        self.broker = Broker(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_path_escape_and_protected_files(self):
        for path in ("../input.json", str(self.root / "input.json"), ".git/config", "public/latest.json", "scripts/qa/worker.py"):
            with self.subTest(path=path), self.assertRaises(ValueError): self.broker.path(path, write=True)

    def test_exact_edit_refuses_ambiguous_match(self):
        path = self.root / "repo/sale_monitor/a.py"
        path.write_text("price = 1\nprice = 1\n")
        with self.assertRaises(ValueError): self.broker.invoke({"op": "replace", "path": "sale_monitor/a.py", "old": "price = 1", "new": "price = 2"})
        self.assertEqual(path.read_text(), "price = 1\nprice = 1\n")

    def test_new_file_and_report_remain_proposals(self):
        self.broker.invoke({"op": "replace", "path": "tests/test_new.py", "old": "", "new": "# test\n"})
        self.broker.invoke({"op": "report", "report": {"summary": "確認", "findings": [], "unresolved": [], "audit_status": "reviewed"}})
        self.assertEqual(read(self.root / "report.json")["audit_status"], "proposal_only")

    def test_no_evidence_no_finding(self):
        with self.assertRaises(ValueError): self.broker.invoke({"op": "report", "report": {"summary": "x", "findings": [{"claim": "sale"}], "unresolved": []}})

    def test_unselected_url_not_fetched(self):
        with self.assertRaises(ValueError): self.broker.invoke({"op": "evidence", "url": "https://example.com/"})

    def test_private_addresses_and_rakuten_blocked(self):
        for url, hosts in (("https://www.rakuten.co.jp/a", ["www.rakuten.co.jp"]), ("https://127.0.0.1/a", ["127.0.0.1"]), ("http://example.com/", ["example.com"]), ("https://user:pass@example.com/", ["example.com"])):
            with self.subTest(url=url), self.assertRaises(ValueError): validate(url, hosts)

    def test_finalize_detects_later_report_change(self):
        atomic(self.root / "report.json", {"summary": "a"})
        manifest = finalize(self.root, "awaiting_codex_review")
        atomic(self.root / "report.json", {"summary": "b"})
        self.assertNotEqual(manifest["files"]["report.json"]["sha256"], digest((self.root / "report.json").read_bytes()))

    def test_atomic_state_replaces_valid_json(self):
        path = self.root / "state.json"
        atomic(path, {"queue": [1]}); atomic(path, {"queue": [2]})
        self.assertEqual(read(path), {"queue": [2]})
        self.assertFalse(path.with_name("state.json.tmp").exists())

    def test_exclusive_worker_lock(self):
        with lock(self.root / "worker.lock"):
            with self.assertRaises(OSError):
                with lock(self.root / "worker.lock"): pass
        with lock(self.root / "worker.lock"): pass

    def test_source_url_discovery_excludes_rakuten(self):
        self.assertEqual(urls({"a": ["https://www.ark-pc.co.jp/i/1/", "https://item.rakuten.co.jp/x", "https://raw.githubusercontent.com/a"]}), {"https://www.ark-pc.co.jp/i/1/"})

    def test_review_detects_changed_artifact(self):
        atomic(self.root / "report.json", {"summary": "original"})
        finalize(self.root, "awaiting_codex_review")
        verify(self.root)
        atomic(self.root / "report.json", {"summary": "changed"})
        with self.assertRaisesRegex(ValueError, "artifact_changed"): verify(self.root)

    def test_disabled_worker_never_calls_network_or_model(self):
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "snapshot") as snapshot:
            worker.poll({"enabled": False})
            snapshot.assert_not_called()
            self.assertEqual(read(self.root / "status.json")["status"], "disabled_pending_model_review")

    def test_model_requires_review_before_network(self):
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "snapshot") as snapshot:
            with self.assertRaisesRegex(RuntimeError, "requires_Codex_review"):
                worker.poll({"enabled": True, "selected_model": "Q3_K_XL", "benchmark_id": "test"})
            snapshot.assert_not_called()

    def test_queued_input_survives_checkout_failure(self):
        atomic(self.root / "benchmarks/test/selection.json", {"selected": "Q3_K_XL"})
        atomic(self.root / "model-review.json", {"approved_model": "Q3_K_XL"})
        latest = {"run_id": "r", "generated_at": "2026-09-18T00:00:00+00:00"}
        with patch.object(worker, "ROOT", self.root), patch.object(worker, "snapshot", return_value=("data", "base/", latest, {}, 0)), patch.object(worker, "get_json", return_value={"sha": "base"}), patch.object(worker, "checkout", side_effect=RuntimeError("network unavailable")), patch.object(worker, "execute") as execute, patch.object(worker.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100*1024**3})()):
            for _ in range(4): worker.poll({"enabled": True, "selected_model": "Q3_K_XL", "benchmark_id": "test"})
            execute.assert_not_called()
            state = read(self.root / "state.json")
            self.assertEqual(len(state["held"]), 1)
            self.assertEqual(state["held"][0]["attempts"], 3)
            self.assertEqual(state["queue"], [])
            self.assertEqual(len(list((self.root / "jobs").glob("*/manifest.json"))), 3)


if __name__ == "__main__": unittest.main()
