from pathlib import Path
import hashlib
import os
import subprocess
import sys
import unittest
import uuid

from monitor_lab.capture import Capture, LEGACY_FORMAT, recover_capture, smoke_capture, verify_capture
from monitor_lab.safety import allowed_root, atomic_bytes, digest, read, write


class CaptureRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = allowed_root() / "tests" / ("capture-recovery-" + uuid.uuid4().hex)

    def test_hard_exit_during_append_or_completion_preserves_published_snapshot(self):
        code = r"""
from dataclasses import asdict
import hashlib,os,sys
from monitor_lab.acquire import Receipt
from monitor_lab.capture import Capture
c=Capture(sys.argv[1],{'experiment_id':'lab-fault','mode':'fixture'},{'stores':{'koubou':{}}})
def kill(stage):
    if stage==sys.argv[2]:os._exit(73)
for i in (1,2):
    body=('body '+str(i)).encode()
    r=Receipt('https://www.pc-koubou.jp/products/detail.php?product_id='+str(i),200,
      '2026-10-02T09:00:00+00:00',hashlib.sha256(body).hexdigest(),'fixture',{},body_bytes=len(body),evidence_mode='fixture')
    r.body_file=c.body(r,body)
    c.append({'store':'koubou',**asdict(r)}, {},hook=kill if i==2 and sys.argv[3]=='append' else lambda stage:None)
c.checkpoint(result={'experiment_id':'lab-fault','mode':'fixture','receipts':c.receipts},hook=kill)
"""
        for operation in ("append", "complete"):
            for stage in ("after_receipts_snapshot", "before_publish", "after_publish"):
                path = self.root / operation / stage
                child = subprocess.run([sys.executable, "-B", "-c", code, str(path), stage, operation],
                                       capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(73, child.returncode, child.stderr)
                state = verify_capture(path, allow_partial=True)
                count = 2 if operation == "complete" or stage == "after_publish" else 1
                self.assertEqual(count, len(state["receipts"]))
                self.assertEqual(operation == "complete" and stage == "after_publish", state["manifest"]["complete"])
                self.assertEqual(2, len(list((path / "bodies").glob("*.body"))))

    def test_convenience_exports_are_not_the_published_checkpoint(self):
        root = self.root / "complete"
        smoke_capture(root)
        (root / "receipts.partial.json").write_bytes(b"stale convenience export")
        (root / "result.json").unlink()
        state = verify_capture(root)
        self.assertTrue(state["manifest"]["complete"])
        self.assertEqual(2, len(state["receipts"]))

    def test_damaged_pointer_recovery_preserves_source_and_requires_partial_opt_in(self):
        source = self.root / "damaged"
        smoke_capture(source)
        (source / "capture-manifest.json").write_bytes(b"{broken")
        before = {str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in source.rglob("*") if f.is_file()}
        report = recover_capture(source, self.root / "restored")
        self.assertEqual(2, report["selected_receipt_count"])
        self.assertTrue(report["original_snapshot_complete"])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            verify_capture(self.root / "restored")
        state = verify_capture(self.root / "restored", allow_partial=True)
        self.assertFalse(state["manifest"]["complete"])
        self.assertEqual(2, len(state["receipts"]))
        after = {str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest()
                 for f in source.rglob("*") if f.is_file()}
        self.assertEqual(before, after)

    def test_corrupt_latest_body_recovers_older_valid_snapshot(self):
        source = self.root / "damaged"
        smoke_capture(source)
        latest = verify_capture(source)
        (source / latest["receipts"][-1]["body_file"]).write_bytes(b"corrupted second body")
        report = recover_capture(source, self.root / "restored")
        self.assertEqual(1, report["selected_receipt_count"])
        self.assertEqual(2, len(report["invalid_checkpoints"]))
        self.assertEqual(1, len(verify_capture(self.root / "restored", allow_partial=True)["receipts"]))

    def test_no_verified_generation_never_recovers_as_empty(self):
        source = self.root / "damaged"
        smoke_capture(source)
        (source / "source-settings.json").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "No valid checkpoint"):
            recover_capture(source, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_losing_all_bodies_does_not_fall_back_to_empty_initial_checkpoint(self):
        source = self.root / "damaged"
        smoke_capture(source)
        for body in (source / "bodies").glob("*.body"):
            body.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "No verified receipts"):
            recover_capture(source, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_recovery_cannot_write_inside_source(self):
        source = self.root / "damaged"
        smoke_capture(source)
        (source / "capture-manifest.json").write_bytes(b"{broken")
        with self.assertRaisesRegex(ValueError, "separate"):
            recover_capture(source, source / "restored")
        self.assertFalse((source / "restored").exists())

    def test_non_object_pointer_can_recover_from_valid_history(self):
        source = self.root / "damaged"
        smoke_capture(source)
        (source / "capture-manifest.json").write_bytes(b"[]")
        with self.assertRaisesRegex(ValueError, "Unsupported evidence manifest"):
            verify_capture(source, allow_partial=True)
        self.assertEqual(2, recover_capture(source, self.root / "restored")["selected_receipt_count"])

    def test_legacy_v1_complete_capture_remains_readable(self):
        source = self.root / "v2"
        smoke_capture(source)
        current = verify_capture(source)
        target = self.root / "v1"
        names = {"study.json", "source-settings.json", "receipts.partial.json", "hosts.json", "result.json"}
        names.update(r["body_file"] for r in current["receipts"])
        files = []
        for name in sorted(names):
            body = (source / name).read_bytes()
            atomic_bytes(target / name, body)
            files.append({"path": name, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        write(target / "capture-manifest.json", {"format": LEGACY_FORMAT, "experiment_id": "lab-fixture-archive-smoke",
              "mode": "fixture", "complete": True, "receipt_count": 2, "files": files, "files_sha256": digest(files)})
        self.assertEqual(current["receipts"], verify_capture(target)["receipts"])

    def test_completed_capture_cannot_be_appended_or_republished(self):
        capture = Capture(self.root / "capture", {"experiment_id": "lab-empty", "mode": "fixture"}, {})
        capture.checkpoint(result={"experiment_id": "lab-empty", "mode": "fixture", "receipts": []})
        before = (capture.root / "capture-manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "immutable"):
            capture.append({}, {})
        with self.assertRaisesRegex(ValueError, "immutable"):
            capture.checkpoint(failure={"type": "late"})
        self.assertEqual(before, (capture.root / "capture-manifest.json").read_bytes())
