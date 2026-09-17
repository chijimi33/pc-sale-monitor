"""Codex-facing integrity check and review receipt. Not exposed to Qwen."""
import argparse
from pathlib import Path

from common import ROOT, atomic, digest, inside, now, read, require_root
from net import get_json
from worker import API, report_index


def verify(job):
    job = Path(job)
    manifest = read(job / "manifest.json")
    if not manifest: raise ValueError("unfinished_job")
    for name, expected in manifest["files"].items():
        path = inside(job, name)
        if not path.exists() or digest(path.read_bytes()) != expected["sha256"]:
            raise ValueError("artifact_changed:" + name)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--decision", choices=["accepted", "changes_requested", "no_change", "failed_acknowledged"])
    parser.add_argument("--note")
    args = parser.parse_args(); require_root()
    job = inside(ROOT / "jobs", args.job_id)
    manifest = verify(job)
    print(manifest)
    if args.decision:
        if not args.note: raise ValueError("review_note_required")
        if args.decision == "accepted":
            if manifest["status"] != "awaiting_codex_review": raise ValueError("failed_job_cannot_be_accepted")
            current_sha = get_json(API + "/commits/main")["sha"]
            if manifest["base_sha"] != current_sha: raise ValueError("stale_base_retest_before_acceptance")
            if read(job / "controller-tests.json", {}).get("exit_code") != 0: raise ValueError("tests_not_passed")
        atomic(ROOT / "reviews" / (args.job_id + ".json"), {
            "reviewer": "Codex", "reviewed_at": now(), "decision": args.decision, "note": args.note,
            "manifest_sha256": digest((job / "manifest.json").read_bytes()), "deployed": False})
        report_index()


if __name__ == "__main__": main()
