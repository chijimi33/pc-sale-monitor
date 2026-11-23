from __future__ import annotations

from copy import deepcopy
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sale_monitor.models import iso, timestamp
from .safety import atomic_bytes, digest, encode, guard, read, write


def publish(store, root, generation, payload, hook=lambda stage: None):
    """Immutable generation + single atomic pointer. Outbox survives a failed publish."""
    root = guard(root)
    if not generation.startswith("lab-") or "/" in generation or "\\" in generation:
        raise ValueError("Experimental generation required")
    target = root / "generations" / (generation + ".json")
    if target.exists() and read(target) != payload:
        raise ValueError("Immutable publication generation changed")
    if not target.exists():
        write(target, payload)
    hook("before_pointer")
    pointer = {"generation": generation, "sha256": digest(payload), "file": str(target.relative_to(root)).replace("\\", "/")}
    write(root / "current.json", pointer)
    hook("after_pointer")
    # No external delivery; retried publication changes neither event ID nor acknowledgement.
    store.commit("publication:" + generation, [("publications", generation, pointer)])
    return pointer


def restored_files(state):
    # Preserve every source file, history, audit and registry exactly. Lab observations
    # are exported separately and cannot acquire a production run ID through restore.
    files = {}
    for name, ref in state["records"].get("source_files", {}).items():
        body = Path(ref["input_path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != ref["sha256"]:
            raise ValueError("Restore source hash mismatch")
        files[name] = body
    return files


def schedule_report(runs, start, end, minute=17, step_hours=4, grace_minutes=90):
    """Infer missing slots, never invent GitHub's undisclosed intended schedule time."""
    start, end = timestamp(start), timestamp(end)
    if not start or not end or end < start:
        raise ValueError("Invalid schedule observation range")
    cursor = start.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    cursor = cursor.replace(hour=cursor.hour // step_hours * step_hours)
    while cursor < start:
        cursor += timedelta(hours=step_hours)
    slots = []
    while cursor <= end:
        slots.append({"expected_at": iso(cursor), "basis": "cron_inferred_not_provider_confirmed", "runs": []})
        cursor += timedelta(hours=step_hours)
    samples, seen = [], set()
    for run in runs:
        row = deepcopy(run)
        row["genuine_scheduled_sample"] = bool(row.get("verified") and row.get("event") == "schedule")
        created = timestamp(row.get("created_at"))
        identity = str(row.get("id"))
        if identity in seen:
            row["genuine_scheduled_sample"] = False
        seen.add(identity)
        row["scheduled_at"] = row.get("scheduled_at")  # Usually unknown in GitHub API.
        row["start_delay_seconds"] = None
        if row["genuine_scheduled_sample"] and created:
            eligible = [s for s in slots if 0 <= (created - timestamp(s["expected_at"])).total_seconds() < step_hours * 3600]
            if eligible:
                slot = eligible[-1]
                slot["runs"].append(identity)
                row["inferred_delay_seconds"] = (created - timestamp(slot["expected_at"])).total_seconds()
        samples.append(row)
    for slot in slots:
        slot["missing_after_grace"] = not slot["runs"] and timestamp(slot["expected_at"]) + timedelta(minutes=grace_minutes) <= end
        slot["backfill_suggestion"] = "manual_lab_only_not_a_scheduled_sample" if slot["missing_after_grace"] else None
    return {"runs": samples, "slots": slots, "missing_slots": sum(s["missing_after_grace"] for s in slots),
            "genuine_scheduled_runs": sum(r["genuine_scheduled_sample"] for r in samples),
            "windows_stopping_affects_actions": False, "scheduled_time_verified": False}


def capture_timing(inputs, output):
    """Read actual workflow phase times for the three pinned snapshots, never dispatch."""
    import json
    from urllib.request import Request, urlopen
    from .inputs import REPOSITORY, SNAPSHOTS
    output = guard(Path(output))
    rows = []
    def get(relative):
        url = f"https://api.github.com/repos/{REPOSITORY}/" + relative
        with urlopen(Request(url, headers={"User-Agent": "PCSaleMonitor-Lab/1.0"}), timeout=30) as response:
            return json.load(response)
    for label, sha in SNAPSHOTS.items():
        latest = read(Path(inputs) / label / "public/latest.json")
        identity = latest["run_id"].split("-")[0]
        run = get("actions/runs/" + identity)
        if str(run["id"]) != identity or run["repository"]["full_name"] != REPOSITORY or run["path"] != ".github/workflows/monitor.yml":
            raise ValueError("Unexpected run provenance")
        jobs = get("actions/runs/" + identity + "/jobs?per_page=100")
        if jobs["total_count"] > len(jobs["jobs"]):
            raise ValueError("Incomplete job timing data")
        commit = get("git/commits/" + sha)
        collection_steps = [{"job": job["name"], "started_at": step.get("started_at"),
                             "completed_at": step.get("completed_at"), "conclusion": step.get("conclusion")}
                            for job in jobs["jobs"] for step in job.get("steps", [])
                            if step["name"] == "Collect with durable checkpoints"]
        publish_job = next((j for j in jobs["jobs"] if j["name"] == "publish"), {})
        row = {"snapshot": label, "run_id": identity, "event": run["event"], "head_sha": run["head_sha"],
               "scheduled_at": None, "scheduled_time_source": "not_exposed_by_provider",
               "workflow_created_at": run["created_at"], "actual_started_at": run.get("run_started_at"),
               "collection_steps": collection_steps,
               "collection_finished_at": max((x["completed_at"] for x in collection_steps if x["completed_at"]), default=None),
               "aggregate_generated_at": latest["generated_at"], "publication_commit_at": commit["committer"]["date"],
               "publish_job_finished_at": publish_job.get("completed_at"),
               "public_endpoint_first_available_at": None,
               "checked_at": iso(), "source": "github_actions_api_and_pinned_git_commit"}
        rows.append(row)
        write(output / (label + "-source.json"), {"run": run, "jobs": jobs, "commit": commit})
    write(output / "timing.json", rows)
    return rows
