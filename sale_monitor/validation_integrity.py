"""Evidence-backed validation counts; no inference from a run's timestamp alone."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import re
from urllib.request import Request, urlopen

from .models import STORES, allowed_url, iso, timestamp, utcnow
from .storage import Store

REPOSITORY = "chijimi33/pc-sale-monitor"
RUN_ID = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)\Z")
HASH = re.compile(r"[a-fA-F0-9]{64}\Z")


def verified_provenance(run_id, catalog):
    match = RUN_ID.fullmatch(str(run_id))
    if not match:
        return None
    runs = catalog.get("runs", {}) if isinstance(catalog, dict) else {}
    row = runs.get(match[1], {}) if isinstance(runs, dict) else {}
    if not isinstance(row, dict):
        return None
    if (row.get("source") != "github_actions_api" or row.get("repository") != REPOSITORY
            or str(row.get("run_id")) != match[1]
            or row.get("workflow_path") != ".github/workflows/monitor.yml"
            or row.get("api_url") != f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{match[1]}"
            or type(row.get("max_attempt")) is not int or row["max_attempt"] < int(match[2])
            or not timestamp(row.get("created_at")) or not timestamp(row.get("checked_at"))
            or not isinstance(row.get("event"), str) or not row["event"]):
        return None
    return {**row, "attempt": int(match[2])}


def capture_provenance(root, run_id, *, token=None, fetch=None, now=None):
    """Backfill actual Actions events once, retaining old verified records on failure."""
    import json
    now = now or utcnow()
    disk = Store(root)
    catalog = disk.load("validation/run_provenance.json", {"runs": {}})
    targets = {p.stem for p in (root / "metrics").glob("*.json")} | {run_id}
    unresolved = {s for s in targets if RUN_ID.fullmatch(s) and not verified_provenance(s, catalog)}
    requests = 0
    errors = []

    def get(url):
        nonlocal requests
        requests += 1
        if fetch:
            return fetch(url)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "PCSaleMonitor-validation"}
        if token:
            headers["Authorization"] = "Bearer " + token
        with urlopen(Request(url, headers=headers), timeout=25) as response:
            return json.load(response)

    page = 1
    try:
        while unresolved:
            response = get(f"https://api.github.com/repos/{REPOSITORY}/actions/runs?per_page=100&page={page}")
            runs = response["workflow_runs"]
            if not runs:
                break
            needed_ids = {RUN_ID.fullmatch(s)[1] for s in unresolved}
            for run in runs:
                key = str(run.get("id"))
                if key not in needed_ids:
                    continue
                row = {"source": "github_actions_api", "repository": run.get("repository", {}).get("full_name"),
                       "run_id": key, "max_attempt": run.get("run_attempt"), "event": run.get("event"),
                       "created_at": run.get("created_at"), "head_sha": run.get("head_sha"),
                       "workflow_path": run.get("path"), "checked_at": iso(now),
                       "api_url": f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{key}"}
                candidate = {"runs": {key: row}}
                if verified_provenance(key + "-1", candidate):
                    catalog.setdefault("runs", {})[key] = row
            unresolved = {s for s in unresolved if not verified_provenance(s, catalog)}
            if len(runs) < 100:
                break
            page += 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not log authorization headers, discard old records, or count an
        # unknown event as schedule when GitHub is temporarily unavailable.
        code = getattr(exc, "code", None)
        errors.append(type(exc).__name__ + (f":{code}" if type(code) is int else ""))
    status = {"checked_at": iso(now), "api_requests": requests,
              "unresolved_run_ids": sorted(s for s in targets if not verified_provenance(s, catalog)),
              "errors": errors}
    catalog["last_sync"] = status
    disk.save("validation/run_provenance.json", catalog)
    return status


def scheduled_samples(recent, catalog, now):
    origins = {}
    counts = Counter()
    unknown = []
    for snapshot in recent:
        run_id = snapshot.get("run_id")
        row = verified_provenance(run_id, catalog)
        if row is None:
            counts["unknown"] += 1
            unknown.append(run_id)
            continue
        counts[row["event"]] += 1
        if row["event"] != "schedule":
            continue
        created = timestamp(row["created_at"])
        if not timedelta(0) <= now - created <= timedelta(days=7) or created > timestamp(snapshot["generated_at"]):
            continue
        # All rerun attempts share the original Actions run creation slot.
        # A later manual retry of a scheduled run cannot add a new window.
        key = row["run_id"]
        previous = origins.get(key)
        if previous is None or timestamp(previous["generated_at"]) < timestamp(snapshot["generated_at"]):
            origins[key] = snapshot
    windows = {}
    for snapshot in sorted(origins.values(), key=lambda s: timestamp(s["generated_at"])):
        row = verified_provenance(snapshot["run_id"], catalog)
        slot = int(timestamp(row["created_at"]).timestamp()) // (4 * 3600)
        windows[slot] = snapshot
    return list(windows.values()), {"policy": "github_api_schedule_original_created_at_four_hour_windows",
            "event_counts": dict(counts), "unknown_run_ids": unknown,
            "unique_scheduled_runs": len(origins),
            "sampled_run_ids": [s["run_id"] for s in windows.values()]}


def audit_counts(audit, now):
    """Count distinct finalized records, never a declared aggregate on its own."""
    audit = audit if isinstance(audit, dict) else {}
    records = audit.get("audits", [])
    records = records if isinstance(records, list) else []
    event_counts = Counter(r.get("event_id") for r in records if isinstance(r, dict) and isinstance(r.get("event_id"), str))
    duplicates = sorted(k for k, v in event_counts.items() if v > 1)
    passed, unresolved, false_positives = set(), set(), set()
    invalid = []
    proposal = lambda r: (r.get("proposal_only") is True or r.get("mode") == "proposal_only"
                          or r.get("status") == "proposal_only" or r.get("audit_status") == "proposal_only")
    for i, row in enumerate(records):
        if not isinstance(row, dict):
            invalid.append({"index": i, "reasons": ["not_an_object"]})
            continue
        event = row.get("event_id")
        key = event if isinstance(event, str) and event else f"invalid-record-{i}"
        reasons = []
        if not isinstance(event, str) or not event:
            reasons.append("event_id_missing")
        elif event in duplicates:
            reasons.append("duplicate_event_id")
        status = row.get("status")
        if status not in ("passed", "needs_review", "false_positive"):
            reasons.append("status_not_finalized")
        findings = row.get("findings", [])
        open_findings = not isinstance(findings, list) or any(not isinstance(f, dict) or f.get("status") != "resolved" for f in findings)
        if status == "needs_review" or open_findings:
            unresolved.add(key)
        if status == "false_positive":
            false_positives.add(key)
        reviewer = row.get("reviewer", audit.get("reviewer"))
        if proposal(audit) or proposal(row) or not isinstance(reviewer, str) or not reviewer.strip() or "qwen" in reviewer.lower():
            reasons.append("formal_reviewer_unconfirmed")
        reviewed = timestamp(row.get("reviewed_at"))
        if not reviewed or reviewed > now or not row.get("source_run_id") or not row.get("offer_key"):
            reasons.append("review_provenance_missing")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            reasons.append("evidence_missing")
        else:
            for ev in evidence:
                if not isinstance(ev, dict):
                    reasons.append("evidence_invalid")
                    continue
                observed = timestamp(ev.get("observed_at"))
                try:
                    valid_url = isinstance(ev.get("url"), str) and allowed_url(ev["url"])
                except ValueError:
                    valid_url = False
                if (ev.get("store") not in STORES or not isinstance(ev.get("url"), str)
                        or not valid_url or not observed or observed > now or (reviewed and observed > reviewed)
                        or not any(HASH.fullmatch(str(ev.get(k, ""))) for k in ("body_sha256", "sha256", "content_hash"))):
                    reasons.append("evidence_provenance_missing")
        if status == "passed" and (not isinstance(row.get("price_check"), dict)
                                   or row["price_check"].get("status") != "supported"
                                   or row["price_check"].get("rule") not in ("A", "B")):
            reasons.append("price_check_missing")
        if reasons:
            invalid.append({"index": i, "event_id": event, "reasons": sorted(set(reasons))})
        elif status == "passed" and not open_findings:
            passed.add(key)
    effective = {"reviewed_count": len(passed), "needs_review_count": len(unresolved),
                 "false_positive_count": len(false_positives)}
    mismatches = [k for k, v in effective.items() if type(audit.get(k)) is not int or audit[k] != v]
    if "audited_event_count" in audit and (type(audit["audited_event_count"]) is not int or audit["audited_event_count"] != len(event_counts)):
        mismatches.append("audited_event_count")
    return {**effective, "policy": "distinct_final_records_with_source_evidence",
            "duplicate_event_ids": duplicates, "invalid_records": invalid,
            "declared_count_mismatches": mismatches,
            "passed_event_ids": sorted(passed)}
