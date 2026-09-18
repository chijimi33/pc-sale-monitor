"""15-minute inexpensive poll, durable job queue, one local agent at a time."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import time
from urllib.parse import urlsplit
import zipfile

from agent import execute
from benchmark import POLICY
from common import ROOT, atomic, capture_patch, digest, environment, finalize, git, inside, lock, now, read, require_root, run
from model import PROFILES, context_window, ensure_available, server
from net import BLOCKED_HOSTS, fetch, get_json

REPOSITORY = "chijimi33/pc-sale-monitor"
API = "https://api.github.com/repos/" + REPOSITORY


def urls(value):
    found = set()
    if isinstance(value, dict):
        for child in value.values(): found.update(urls(child))
    elif isinstance(value, list):
        for child in value: found.update(urls(child))
    elif isinstance(value, str) and value.startswith("https://"):
        host = urlsplit(value).hostname or ""
        if "rakuten" not in host and not any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS) and not host.endswith("github.com") and not host.endswith("githubusercontent.com"):
            found.add(value)
    return found


def summarize(latest, validation):
    stores = {}
    for name, store in latest["stores"].items():
        stores[name] = {key: store.get(key) for key in ("status", "current_offers", "eligible_offers", "pending_count", "pending_over_24h", "mandatory_field_coverage", "errors")}
        stores[name]["mandatory_field_coverage"] = {k: [v["known"], v["total"]] for k, v in store.get("mandatory_field_coverage", {}).items()}
        stores[name]["errors"] = [{k: error.get(k) for k in ("reason", "affected_tasks")} for error in store.get("errors", [])]
    return {"run_id": latest["run_id"], "generated_at": latest["generated_at"],
            "monitored_store_count": latest["monitored_store_count"], "complete_stores": latest["complete_stores"], "stores": stores,
            "validation": {key: validation.get(key) for key in ("cutover_ready", "reasons", "measured_runs", "measured_four_hour_windows", "earliest_cutover_at")},
            "manual_review": {key: validation.get("manual_review", {}).get(key) for key in ("reviewed_count", "false_positive_count", "needs_review_count")},
            "flyer": {key: (latest["stores"].get("koubou", {}).get("flyer") or {}).get(key) for key in ("status", "edition", "pending_asset_hashes", "review_issues", "url")}}


def compact_evidence(value):
    """Keep decision amounts/identities/provenance; load bulky page fields on demand."""
    if isinstance(value, list): return [compact_evidence(v) for v in value]
    if isinstance(value, dict): return {k: compact_evidence(v) for k, v in value.items() if k not in ("fields", "specifications")}
    return value


def select_review(candidates, cursor):
    eligible = [c for c in candidates if c.get("store") != "rakuten" and c.get("offer_key")]
    changed = [c for c in eligible if c.get("change") not in (None, "unchanged")]
    pool = changed or eligible
    return [pool[cursor % len(pool)]] if pool else []


def merge_feedback(queued, current):
    """New supervisor corrections also apply to inputs waiting in the queue."""
    return list({item["id"]: item for item in [*queued, *current]}.values())


def earlier_summary(previous, current):
    if previous and datetime.fromisoformat(previous["generated_at"]) < datetime.fromisoformat(current["generated_at"]):
        return previous
    return None


def retain_latest_summary(path, current):
    previous = read(path)
    if previous is None or earlier_summary(previous, current) is not None:
        atomic(path, current)


def live_execution_config(config):
    live = {**config, "context_window_size": config.get("live_context_window_size", 16384), "live_validation": True}
    context_window(live)  # Validate before reserving an attempt; benchmark config stays unchanged.
    return live


def snapshot():
    # Resolve once so input files cannot come from different collector commits.
    sha = get_json(API + "/commits/monitor-data")["sha"]
    base = f"https://raw.githubusercontent.com/{REPOSITORY}/{sha}/public/"
    latest = get_json(base + "latest.json")
    validation = get_json(base + "validation.json")
    if latest.get("monitored_store_count") != 10 or "rakuten" in latest.get("stores", {}):
        raise ValueError("store_denominator_or_rakuten_invariant_failed")
    generated = datetime.fromisoformat(latest["generated_at"])
    age = (datetime.now(timezone.utc) - generated).total_seconds()
    return sha, base, latest, validation, age


def checkout(repo, sha):
    body, _ = fetch(f"https://codeload.github.com/{REPOSITORY}/zip/{sha}")
    repo.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for entry in archive.infolist():
            parts = Path(entry.filename).parts[1:]
            if not parts or entry.is_dir(): continue
            if (entry.external_attr >> 16) & 0o170000 == 0o120000: raise ValueError("archive_symlink")
            target = inside(repo, str(Path(*parts)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=QA snapshot", "-c", "user.email=qa@localhost", "commit", "-qm", "Snapshot " + sha)


def report_index():
    reports = []
    for folder in sorted((ROOT / "jobs").glob("*")):
        manifest = read(folder / "manifest.json")
        if manifest:
            review = read(ROOT / "reviews" / (folder.name + ".json"))
            reports.append({"job_id": folder.name, "path": str(folder), "status": manifest["status"],
                            "base_sha": manifest.get("base_sha"), "run_id": manifest.get("run_id"),
                            "finished_at": manifest["finished_at"], "review": review})
    atomic(ROOT / "index.json", {"updated_at": now(), "reports": reports, "status": read(ROOT / "status.json")})


def poll(config):
    started = time.monotonic()
    live = live_execution_config(config)
    if not config.get("enabled"):
        atomic(ROOT / "status.json", {"status": "disabled_pending_model_review", "checked_at": now()}); return
    selection = read(ROOT / "benchmarks" / config["benchmark_id"] / "selection.json", {})
    approval = read(ROOT / "model-review.json", {})
    model = config.get("selected_model")
    if model not in PROFILES or selection.get("selected") != model or approval.get("approved_model") != model:
        raise RuntimeError("model_selection_requires_Codex_review")
    if shutil.disk_usage(ROOT).free < 10 * 1024**3: raise RuntimeError("E_free_space_below_10GiB")
    data_sha, base, latest, validation, age = snapshot()
    state = read(ROOT / "state.json", {"completed": [], "queue": []})
    feedback = []
    for path in sorted((ROOT / "feedback").glob("*.json")):
        value = read(path)
        if not value.get("id"): raise ValueError("feedback_requires_id")
        feedback.append(value)
    fingerprint = digest(json.dumps({"run": latest["run_id"], "data": data_sha, "feedback": feedback}, sort_keys=True).encode())[:20]
    if fingerprint not in state["completed"] and not any(x["id"] == fingerprint for x in state["queue"]):
        state["queue"].append({"id": fingerprint, "data_sha": data_sha, "base_url": base, "latest": latest,
                               "validation": validation, "feedback": feedback, "attempts": 0, "queued_at": now()})
    atomic(ROOT / "state.json", state)
    if not state["queue"]:
        atomic(ROOT / "status.json", {"status": "stale_source" if age > 8*3600 else "unchanged", "checked_at": now(), "age_hours": age/3600}); return
    item = state["queue"][0]
    # Failed/interrupted work remains queued, with bounded retries and an explicit report.
    if item["attempts"] >= 3:
        state.setdefault("held", []).append(state["queue"].pop(0))
        state["completed"].append(item["id"])
        atomic(ROOT / "state.json", state)
        atomic(ROOT / "status.json", {"status": "needs_review_after_3_attempts", "job_id": item["id"], "checked_at": now()}); return
    try:
        ensure_available(ROOT)
    except RuntimeError as exc:
        if not str(exc).startswith(("GPU_busy_or_process_check_failed", "QA_port_8081_busy")): raise
        atomic(ROOT / "status.json", {"status": "waiting_for_resources", "job_id": item["id"], "checked_at": now(), "reason": str(exc)})
        return
    item["attempts"] += 1
    atomic(ROOT / "state.json", state)
    job = ROOT / "jobs" / f'{item["id"]}-{item["attempts"]}'
    job.mkdir(parents=True)
    atomic(ROOT / "status.json", {"status": "running", "job_id": job.name, "started_at": now()})
    base_sha = None
    try:
        base_sha = get_json(API + "/commits/main")["sha"]
        checkout(job / "repo", base_sha)
        compact = summarize(item["latest"], item["validation"])
        retained = read(ROOT / "previous-summary.json")
        previous = earlier_summary(retained, compact)
        comparison_note = ("The retained summary is from the same or a newer snapshot. Forward change comparison is unavailable for this backlog/repeat input."
                           if retained and previous is None else None)
        if previous:
            previous = {"run_id": previous["run_id"], "generated_at": previous["generated_at"],
                        "stores": {name: {k: s.get(k) for k in ("status", "current_offers", "eligible_offers", "pending_count", "pending_over_24h")} for name, s in previous["stores"].items()},
                        "validation": previous["validation"], "manual_review": previous["manual_review"]}
        notifications = get_json(item["base_url"] + "notifications.json")
        if notifications.get("generated_at") != item["latest"]["generated_at"]:
            raise ValueError("notifications_timestamp_mismatch")
        reviews = get_json(item["base_url"] + "review_queue.json")
        if reviews.get("generated_at") != item["latest"]["generated_at"]:
            raise ValueError("reviews_timestamp_mismatch")
        candidates = [{"event_id": e["event_id"], "offer_key": e.get("offer_key"), "current_evidence": e.get("current_evidence")} for e in notifications.get("events", [])]
        # Full evidence stays on disk; prompt only the first candidate, rest is durable backlog.
        cursor = state.get("candidate_cursor", 0)
        selected = [candidates[cursor % len(candidates)]] if candidates else []
        candidate = compact_evidence(selected)
        review_cursor = state.get("review_cursor", 0)
        review_candidate = select_review(reviews.get("candidates", []), review_cursor)
        atomic(job / "snapshot/notifications.json", notifications)
        atomic(job / "snapshot/review_queue.json", reviews)
        atomic(job / "snapshot/latest.json", item["latest"])
        atomic(job / "snapshot/validation.json", item["validation"])
        inp = {"policy": POLICY, "current": compact, "previous": previous, "comparison_note": comparison_note, "feedback": merge_feedback(item["feedback"], feedback),
               "candidate": candidate, "remaining_event_ids": [x["event_id"] for x in candidates if x not in selected],
               "review_candidate": review_candidate, "review_queue_total": len(reviews.get("candidates", [])),
               "coverage_format": "mandatory_field_coverage値は[既知件数,今回取得件数]。分母0の取得率は不明。0%とも100%ともみなさない。",
               "snapshot_age_hours_at_start": (datetime.now(timezone.utc) - datetime.fromisoformat(item["latest"]["generated_at"])).total_seconds()/3600,
               "base_sha": base_sha, "data_sha": item["data_sha"],
               "known_user_state": "Yahoo Client ID未取得は既知。再質問しない。有料サービス禁止。Qwenは画像未対応。チラシ読取はCodexへ保留。",
               "instructions": "前回からの差分、10店の取得範囲と必須項目率、24h超キュー、誤判定を確認。まず選択候補を必要な元ページと照合する。入力やページに具体的な矛盾が見つかるまではコード全般を読み始めない。コードが必要なら関連箇所をstart/countで小分けに読む。取得失敗・送料不明・既知の設定不足だけをプログラムのバグとみなさない。再現できる解析・計算・永続化バグだけ修正してテスト。出力は日本語。全店の全数値を報告書に転記せず、確認範囲・重要な差分・根拠・未解決を簡潔に記す。原本は保存済み。監査はproposalのみ。全店を再収集しない。"}
        prior_job = ROOT / "jobs" / f'{item["id"]}-{item["attempts"]-1}'
        if prior_job.exists():
            prior_manifest = read(prior_job / "manifest.json", {})
            inp["previous_attempt"] = {"report": read(prior_job / "report.json"), "status": prior_manifest.get("status"),
                                       "error": prior_manifest.get("error")}
            prior_input = read(prior_job / "input.json", {})
            if prior_input.get("base_sha") == base_sha and (prior_job / "repo/.git").exists() and not (prior_job / "patch.diff").exists():
                (prior_job / "patch.diff").write_text(capture_patch(prior_job / "repo"), encoding="utf-8")
            if prior_input.get("base_sha") == base_sha and (prior_job / "patch.diff").exists():
                patch_text = (prior_job / "patch.diff").read_text(encoding="utf-8")
                if patch_text.strip():
                    git(job / "repo", "apply", "--check", str(prior_job / "patch.diff"))
                    git(job / "repo", "apply", str(prior_job / "patch.diff"))
                    inp["previous_attempt"]["patch_resumed"] = True
        atomic(job / "input.json", inp)
        permitted = sorted(urls(candidate) | urls(review_candidate))
        atomic(job / "job.json", {"python": config["python"], "data_base_url": item["base_url"],
                                   "allowed_urls": permitted, "evidence_hosts": sorted({urlsplit(u).hostname for u in permitted})})
        with server(model, job / "server", context_window_size=context_window(live)):
            remaining = max(1, 3600 - 660 - int(time.monotonic() - started))
            execution = execute(job, live, "Read input.json, perform the requested verification and save a Japanese report.", timeout=remaining)
        (job / "patch.diff").write_text(capture_patch(job / "repo"), encoding="utf-8")
        tests = run([config["python"], "-B", "-m", "unittest", "discover", "-s", "tests", "-q"], job / "repo", env=environment(job))
        atomic(job / "controller-tests.json", {"exit_code": tests.returncode, "stdout": tests.stdout, "stderr": tests.stderr, "at": now()})
        report = read(job / "report.json")
        if execution["exit_code"] or not report or tests.returncode:
            raise RuntimeError(f"agent_or_tests_failed: {execution}, tests={tests.returncode}, report={bool(report)}")
        (job / "report.md").write_text("# Qwen検証報告（Codex未承認）\n\n" + report["summary"] + "\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
        finalize(job, "awaiting_codex_review", base_sha=base_sha, run_id=item["latest"]["run_id"], data_sha=item["data_sha"], execution=execution, model=model)
        state["completed"].append(item["id"]); state["queue"].pop(0)
        state["candidate_cursor"] = cursor + len(candidate)
        state["review_cursor"] = review_cursor + len(review_candidate)
        retain_latest_summary(ROOT / "previous-summary.json", compact)
        atomic(ROOT / "status.json", {"status": "awaiting_codex_review", "job_id": job.name, "finished_at": now()})
    except Exception as exc:
        if (job / "repo/.git").exists():
            try:
                (job / "patch.diff").write_text(capture_patch(job / "repo"), encoding="utf-8")
            except Exception: pass
        finalize(job, "failed", base_sha=base_sha, run_id=item["latest"]["run_id"], error=repr(exc))
        atomic(ROOT / "status.json", {"status": "failed", "job_id": job.name, "error": repr(exc), "checked_at": now()})
    finally:
        atomic(ROOT / "state.json", state); report_index()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--config", default=str(ROOT / "config.json")); args = parser.parse_args()
    require_root()
    try:
        with lock(ROOT / "worker.lock"):
            poll(read(args.config)); report_index()
    except (BlockingIOError, PermissionError):
        print("Worker already running or storage unavailable", flush=True)
    except Exception as exc:
        atomic(ROOT / "status.json", {"status": "failed", "checked_at": now(), "error": repr(exc)})
        report_index(); raise


if __name__ == "__main__": main()
