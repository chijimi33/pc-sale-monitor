from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import sys

from sale_monitor.engine import evaluate
from sale_monitor.http import Page
from sale_monitor.models import Offer, timestamp
from sale_monitor.parsing import parse_product
from sale_monitor.validation_integrity import audit_counts, verified_provenance
from .evidence import decide, normalize
from .inputs import SNAPSHOTS, import_state, verify
from .operations import schedule_report
from .safety import digest, environment, guard, read, write
from .stores import BACKENDS


def invoke(inputs, label, arch, cycles, output, backend):
    command = [sys.executable, "-m", "monitor_lab", "run", "--input", str(inputs), "--snapshot", label,
               "--architecture", arch, "--cycles", str(cycles), "--mode", "replay", "--backend", backend, "--output", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    return read(output / "result.json")


def normalized_decisions(result):
    return {d["candidate_url"]: (d["status"], d["rule"], d["reasons"], d.get("reference_yen")) for d in result["decisions"]}


def suite(inputs, output, repeats=2):
    inputs, output = Path(inputs), guard(Path(output))
    manifest = verify(inputs)
    results, checks = [], []
    configurations = [("A", 1, "json"), ("A", 2, "json"), ("B", 1, "sqlite")]
    if environment()["system"] == "Windows":
        configurations.append(("C", 1, "sqlite"))
    for label in SNAPSHOTS:
        files, original_tasks, _ = import_state(inputs, label)
        for repeat in range(repeats):
            same = []
            for arch, cycles, backend in configurations:
                target = output / f"{label}-{arch}-{cycles}-{repeat+1}"
                result = invoke(inputs, label, arch, cycles, target, backend)
                result["output"] = str(target)
                state = read(target / "state-export.json")
                tasks = state["records"]["tasks"]
                assert set(original_tasks) <= set(tasks), "Original tasks disappeared"
                for key, original in original_tasks.items():
                    for field, value in original.items():
                        if not field.startswith("lab_"):
                            assert tasks[key][field] == value, f"Original task {field} changed"
                assert state["records"]["source_files"] == files, "Source references changed"
                assert all(v["delivery_status"] == "not_sent_lab_only" for v in state["records"].get("events", {}).values())
                assert result["formal_audits_added"] == result["scheduled_stability_samples"] == 0
                if arch == "A" and cycles == 1:
                    assert result["observations"] == 2 and result["accepted_candidates"] == 0
                else:
                    assert result["observations"] == 6 and result["accepted_candidates"] == 1
                    assert any("comparator_member_price_not_verified" in d["reasons"] for d in result["decisions"])
                    same.append(normalized_decisions(result))
                results.append(result)
            assert all(d == same[0] for d in same)
            checks.append({"snapshot": label, "repeat": repeat+1, "original_tasks_retained": len(original_tasks),
                           "original_non_lab_fields_equal": True, "A_two_cycles_B_C_decisions_equal": True})
    # Isolate scheduler effect while holding the storage backend constant.
    only_scheduler = []
    for arch in ("A", "B"):
        target = output / ("scheduler-only-" + arch)
        only_scheduler.append(invoke(inputs, "ark_repaired", arch, 1, target, "sqlite"))
    assert [r["observations"] for r in only_scheduler] == [2, 6]
    # Real resume: a second CLI process with unchanged conditions must not fetch or
    # duplicate an accepted event. Preserve the first result before resuming.
    resume_target = output / "ark_repaired-B-1-1"
    first = read(resume_target / "result.json")
    write(resume_target / "first-invocation-result.json", first)
    with BACKENDS["sqlite"](resume_target / "store") as store:
        before = store.snapshot()
    resumed = invoke(inputs, "ark_repaired", "B", 1, resume_target, "sqlite")
    with BACKENDS["sqlite"](resume_target / "store") as store:
        after = store.snapshot()
    assert resumed["replayed_pages"] == 0
    assert before == after, "Resume changed committed state or event identities"
    # Formal audit reference is checked read-only; test passes never become audits.
    audit_file = inputs / "ark_repaired/state/validation/manual_review.json"
    audit = read(audit_file)
    audit_result = audit_counts(audit, datetime.now(timezone.utc))
    assert audit_result["reviewed_count"] == 4 and audit_result["needs_review_count"] == 2
    assert not audit_result["invalid_records"] and not audit_result["declared_count_mismatches"]
    audit_result["reference_records"] = [{k: r.get(k) for k in ("event_id", "status", "source_run_id", "findings", "price_check")} for r in audit["audits"]]
    audit_result["content_evaluation_scope"] = "Reference status/provenance validation; does not newly verify all six original primary pages or create audits"
    write(output / "formal-audit-reference.json", audit_result)
    # Same six response bodies into existing and proposed parsers, one variable.
    cfg = read(Path(__file__).resolve().parents[1] / "config/sources.json")
    baseline, proposed, by_group = [], [], {}
    run_id = "lab-parser-control"
    for fixture in manifest["pages"]:
        page = Page(fixture["url"], (inputs / fixture["path"]).read_bytes(), fixture["observed_at"])
        old = parse_product(fixture["store"], page, cfg["stores"][fixture["store"]])
        old.observed_run_id = run_id
        baseline.append(old)
        proposed.append(normalize(fixture["store"], page, cfg["stores"][fixture["store"]], run_id, fixture).offer)
        by_group[fixture["url"]] = fixture["group"]
    now = max(timestamp(o.observed_at) for o in baseline)
    parser_comparison = []
    for url in ("https://www.pc-koubou.jp/products/detail.php?product_id=1051336", "https://shop.tsukumo.co.jp/goods/4711289500124/"):
        old = next(o for o in baseline if o.url == url)
        new = next(o for o in proposed if o.url == url)
        parser_comparison.append({"url": url, "baseline": evaluate(old, baseline, [], now),
                                  "proposed": decide(new, proposed, [], now, run_id)})
    assert [r["baseline"]["status"] for r in parser_comparison] == ["accepted", "accepted"]
    assert [r["proposed"]["status"] for r in parser_comparison] == ["accepted", "insufficient"]
    write(output / "parser-comparison.json", parser_comparison)
    # Verified original schedule provenance remains distinct from inferred slots.
    catalog = read(inputs / "ark_repaired/state/validation/run_provenance.json")
    latest = read(inputs / "ark_repaired/public/latest.json")
    runs = []
    for identity, row in catalog["runs"].items():
        if row["created_at"] < "2026-09-30":
            continue
        check = verified_provenance(identity + "-1", catalog)
        runs.append({**row, "id": identity, "verified": check is not None,
                     "actual_started_at": None, "collection_finished_at": None, "published_at": None})
    schedule = schedule_report(sorted(runs, key=lambda r: r["created_at"]), "2026-09-30T00:00:00Z", latest["generated_at"])
    write(output / "schedule-observation.json", schedule)
    summary = {"input_hash": manifest["input_hash"], "repeats": repeats, "results": results, "checks": checks,
               "scheduler_only": only_scheduler, "resume_state_equal": True, "resumed_replay_requests": 0,
               "formal_audits_added": 0, "scheduled_stability_samples": 0, "production_written": False,
               "environment": environment(), "all_assertions_passed": True}
    write(output / "comparison.json", summary)
    return summary
