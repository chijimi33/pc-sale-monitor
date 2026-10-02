from __future__ import annotations

from dataclasses import asdict
import hashlib
from http.client import IncompleteRead
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid
import zipfile

from monitor_lab.acquire import Coordinator
from monitor_lab.capture import Capture, replay_capture, smoke_capture, verify_capture
from monitor_lab.safety import allowed_root, digest, guard, read, write
from monitor_lab.study import study
from monitor_lab.tests.test_lab import FakeClock, FakeTransport


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / "tests" / ("capture-" + uuid.uuid4().hex)
        self.settings = {"stores": {"koubou": {"default_condition": "new"}}}

    def capture(self, name="capture"):
        return Capture(self.root / name, {"experiment_id": "lab-fixture", "mode": "fixture"}, self.settings)

    def coordinator(self, capture, responses):
        clock = FakeClock()
        self.transport = FakeTransport(responses)
        return Coordinator(self.transport, clock=clock, monotonic=clock, sleep=clock.sleep, capture=capture.body)

    def record(self, capture, coordinator, url="https://www.pc-koubou.jp/products/detail.php?product_id=1"):
        page, receipt = coordinator.fetch(url)
        capture.append({"store": "koubou", **asdict(receipt)}, coordinator.hosts)
        return page, receipt

    def complete(self, capture):
        capture.checkpoint(result={"experiment_id": "lab-fixture", "mode": "fixture", "receipts": capture.receipts})

    def test_success_and_404_bodies_survive_zip_move_and_offline_replay(self):
        original = self.root / "original"
        smoke_capture(original)
        archive = self.root / "evidence.zip"
        with zipfile.ZipFile(archive, "w") as target:
            for file in original.rglob("*"):
                if file.is_file():
                    target.write(file, str(file.relative_to(original)))
        moved = self.root / "downloaded"
        with zipfile.ZipFile(archive) as source:
            source.extractall(moved)  # Entries are from the just-created local bundle.
        original, retired = guard(original), guard(self.root / "original-unavailable")
        shutil.move(str(original), str(retired))
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Replay must not fetch")):
            verified = verify_capture(moved)
            result = replay_capture(moved, self.root / "replay")
        self.assertEqual([200, 404], [r["status"] for r in verified["receipts"]])
        self.assertTrue(all(not Path(r["body_file"]).is_absolute() for r in verified["receipts"]))
        self.assertEqual(0, result["http_requests"])
        self.assertEqual(1, result["parsed_pages"])
        self.assertEqual("2026-10-01T00:00:00+00:00", result["observations"][0]["observation"]["offer"]["observed_at"])

    def test_challenge_body_retained_without_retrying_second_task(self):
        capture = self.capture()
        client = self.coordinator(capture, [(403, {}, b"Access denied")])
        _, first = self.record(capture, client)
        _, second = self.record(capture, client, "https://www.pc-koubou.jp/products/detail.php?product_id=2")
        self.complete(capture)
        result = verify_capture(capture.root)
        self.assertEqual(b"Access denied", (capture.root / first.body_file).read_bytes())
        self.assertEqual("shared_host_wait", second.error)
        self.assertIsNone(second.body_file)
        self.assertEqual(1, len(self.transport.calls))
        self.assertEqual(2, len(result["receipts"]))

    def test_incomplete_read_preserves_partial_bytes_but_does_not_parse(self):
        capture = self.capture()
        client = self.coordinator(capture, [IncompleteRead(b"partial response", 100)])
        page, receipt = self.record(capture, client)
        self.complete(capture)
        self.assertIsNone(page)
        self.assertTrue(receipt.body_incomplete)
        self.assertEqual(b"partial response", (capture.root / receipt.body_file).read_bytes())
        self.assertEqual("transport:IncompleteRead", receipt.error)
        self.assertEqual(0, replay_capture(capture.root, self.root / "replay")["parsed_pages"])

    def test_evidence_write_failure_is_not_reported_as_a_site_error(self):
        client = self.coordinator(self.capture(), [(200, {}, b"ok")])
        client.capture = lambda *args: (_ for _ in ()).throw(OSError("local disk full"))
        with self.assertRaisesRegex(OSError, "local disk full"):
            client.fetch("https://www.pc-koubou.jp/products/detail.php?product_id=1")
        self.assertEqual({}, client.hosts)

    def test_partial_capture_requires_opt_in_and_cannot_be_overwritten(self):
        capture = self.capture()
        client = self.coordinator(capture, [(200, {}, b"<h1>partial study</h1>")])
        self.record(capture, client)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            verify_capture(capture.root)
        self.assertEqual(1, len(verify_capture(capture.root, allow_partial=True)["receipts"]))
        before = (capture.root / "capture-manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.capture()
        self.assertEqual(before, (capture.root / "capture-manifest.json").read_bytes())

    def test_missing_and_changed_bodies_fail_before_replay_output(self):
        for failure in ("missing", "changed"):
            root = self.root / failure
            smoke_capture(root)
            receipt = verify_capture(root)["receipts"][0]
            file = root / receipt["body_file"]
            if failure == "missing":
                file.unlink()
            else:
                file.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                replay_capture(root, self.root / (failure + "-replay"))
            self.assertFalse((self.root / (failure + "-replay")).exists())

    def test_escaping_duplicate_and_modified_indexes_are_rejected(self):
        for failure in ("escape", "duplicate", "checksum"):
            root = self.root / failure
            smoke_capture(root)
            index = read(root / "capture-manifest.json")
            if failure == "escape":
                index["files"][0]["path"] = "../outside.json"
            elif failure == "duplicate":
                index["files"].append(dict(index["files"][0]))
            else:
                index["files"][0]["bytes"] += 1
            if failure != "checksum":
                index["files_sha256"] = digest(index["files"])
            write(root / "capture-manifest.json", index)
            with self.assertRaises(ValueError):
                verify_capture(root)

    def test_receipt_mismatch_fails_even_if_index_is_recomputed(self):
        root = self.root / "mismatch"
        smoke_capture(root)
        receipts_file = read(root / "capture-manifest.json")["records"]["receipts"]
        receipts = read(root / receipts_file)
        receipts[0]["body_sha256"] = "0" * 64
        write(root / receipts_file, receipts)
        index = read(root / "capture-manifest.json")
        for row in index["files"]:
            body = (root / row["path"]).read_bytes()
            row.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        index["files_sha256"] = digest(index["files"])
        write(root / "capture-manifest.json", index)
        with self.assertRaisesRegex(ValueError, "disagree"):
            verify_capture(root)

    def test_study_interruption_keeps_verified_partial_capture(self):
        class Transport(FakeTransport):
            method = "fake"
            def __init__(self):
                super().__init__([(200, {}, b"<h1>one</h1>")])
            def close(self):
                pass
        output = self.root / "interrupted"
        with patch("monitor_lab.study.TRANSPORTS", {"fake": Transport}):
            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                study(output, methods=["fake"], emit=lambda row: (_ for _ in ()).throw(RuntimeError("interrupt")))
        result = verify_capture(output, allow_partial=True)
        self.assertFalse(result["manifest"]["complete"])
        self.assertEqual(1, len(result["receipts"]))
        self.assertEqual("RuntimeError", read(output / "failure.json")["type"])
        self.assertFalse((output / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
