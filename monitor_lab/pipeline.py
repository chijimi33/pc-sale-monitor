from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit

from sale_monitor.engine import update_events
from sale_monitor.http import Page
from sale_monitor.models import Offer, STORES, iso, timestamp
from .acquire import Coordinator, Receipt, TRANSPORTS
from .benchmark import process_counters
from .evidence import decide, normalize
from .inputs import import_state, verify
from .operations import publish
from .safety import digest, environment, experiment_id, guard, implementation_hash, read, write
from .stores import BACKENDS, SQLite


def task_id(store, url):
    return "fixture:" + digest([store, url])[:24]


def existing_task_id(tasks, store, url):
    matches = [k for k, t in tasks.items() if t.get("lab_store") == store and t.get("url") == url
               and t.get("type", "product") == "product"]
    return min(matches, key=lambda k: (tasks[k].get("created_at", ""), k)) if matches else task_id(store, url)


def make_task(store, url, created, *, group=None, role="comparison", original=None):
    return {**deepcopy(original or {}), "lab_store": store, "url": url,
            "created_at": (original or {}).get("created_at") or created,
            "lab_status": "pending", "lab_role": role, "lab_group": group,
            "lab_attempts": deepcopy((original or {}).get("lab_attempts", [])),
            "lab_dependencies": deepcopy((original or {}).get("lab_dependencies", [])),
            "lab_kind": "product", "lab_selected": True}


def dependencies(candidate, pages, tasks, created, known_catalog=None):
    """Known product URLs first; only stores with no known URL get a search request."""
    group = candidate["lab_group"]
    targets = [p for p in pages if p["group"] == group and p["store"] != candidate["lab_store"]]
    allowed_urls = {p["url"] for p in pages}
    for known in (known_catalog or {}).values():
        if (candidate.get("lab_identity") and known.get("identity") == candidate["lab_identity"]
                and known["store"] != candidate["lab_store"] and known["url"] not in {p["url"] for p in targets}):
            targets.append({**known, "group": group})
    known_stores = {p["store"] for p in targets} | {candidate["lab_store"]}
    changes, keys = [], []
    for page in targets:
        key = existing_task_id(tasks, page["store"], page["url"])
        old = tasks.get(key)
        task = deepcopy(old) if old and old.get("lab_selected") else make_task(page["store"], page["url"], created, group=group, original=old)
        task["lab_selected"] = page["url"] in allowed_urls
        if not task["lab_selected"]:
            task["lab_reason"] = "known_url_outside_selected_fixture_scope"
        task["lab_dependencies"] = sorted(set(task.get("lab_dependencies", []) + [candidate["url"]]))
        changes.append(("tasks", key, task))
        keys.append(key)
    for store in STORES:
        if store in known_stores:
            continue
        key = "search:" + digest([store, group])[:24]
        task = deepcopy(tasks.get(key)) if key in tasks else make_task(store, "", created, group=group)
        task.update(lab_kind="search", lab_status="external_wait" if store == "yahoo" else "pending",
                    lab_reason="yahoo_client_id_unconfigured" if store == "yahoo" else "search_not_in_selected_fixture_scope",
                    query=candidate.get("lab_query") or group)
        task["lab_dependencies"] = sorted(set(task.get("lab_dependencies", []) + [candidate["url"]]))
        changes.append(("tasks", key, task))
        keys.append(key)
    return changes, keys


def run(inputs, label, architecture, mode, output, *, backend=None, transport="urllib", budget=2100, cycles=1, max_tasks=20):
    if architecture not in {"A", "B", "C"} or mode not in {"replay", "live"}:
        raise ValueError("Invalid architecture/mode")
    if architecture == "C" and environment()["system"] != "Windows":
        raise ValueError("C requires a real Windows process")
    if not 1 <= max_tasks <= 20 or budget <= 0 or budget > 2100 or cycles not in (1, 2):
        raise ValueError("Experiment limit exceeded")
    output = guard(Path(output))
    manifest = verify(Path(inputs))
    if label not in manifest["snapshots"]:
        raise ValueError("Unknown pinned input snapshot")
    pages = manifest["pages"]
    if len(pages) != 6:
        raise ValueError("The six primary-page fixtures are required")
    cfg = read(Path(__file__).resolve().parents[1] / "config/sources.json")
    chosen = backend or ("json" if architecture == "A" else "sqlite")
    meta_path = output / "experiment.json"
    conditions = {"input_hash": manifest["input_hash"], "snapshot": label, "architecture": architecture,
                  "mode": mode, "backend": chosen, "transport": transport, "budget": budget, "cycles": cycles,
                  "max_tasks": max_tasks, "source_base": manifest["source_base"]}
    conditions["implementation_hash"] = implementation_hash()
    if meta_path.exists():
        meta = read(meta_path)
        if meta["conditions"] != conditions:
            raise ValueError("Cannot resume with changed experimental conditions")
    else:
        meta = {"experiment_id": experiment_id(), "conditions": conditions, "environment": environment(),
                "started_at": iso(), "production_run_id": None}
        write(meta_path, meta)
    run_id = meta["experiment_id"]
    start = time.perf_counter()
    counters_before = process_counters()
    shared_deadline = time.monotonic() + budget
    with BACKENDS[chosen](output / "store") as store:
        state = store.snapshot()
        if not state["transactions"]:
            files, originals, known_offers = import_state(Path(inputs), label)
            changes = [("source_files", k, v) for k, v in files.items()] + [("tasks", k, v) for k, v in originals.items()]
            changes += [("identity_catalog", key, {"identity": Offer.from_dict(row).identity,
                         **{field: row.get(field) for field in ("store", "url", "condition", "variant", "warranty")}})
                        for key, row in known_offers.items()]
            for page in pages:
                if page["name"] not in {"koubou-b550", "tsukumo-capture"}:
                    continue
                key = existing_task_id(originals, page["store"], page["url"])
                original = originals.get(key)
                task = make_task(page["store"], page["url"], page["observed_at"], group=page["group"], role="candidate", original=original)
                changes.append(("tasks", key, task))
            store.commit("import:" + manifest["snapshots"][label]["data_sha"], changes)
        state = store.snapshot()
        hosts = deepcopy(state["records"].get("hosts", {}))
        client = Coordinator(TRANSPORTS[transport](), budget=max(.001, shared_deadline - time.monotonic()), hosts=hosts)
        client.deadline = shared_deadline
        receipts, completed_times = [], {}
        processed = 0
        replay_now = max(timestamp(p["observed_at"]) for p in pages)
        try:
            for cycle in range(cycles):
                state = store.snapshot()
                tasks = state["records"]["tasks"]
                selected = [k for k, t in tasks.items() if t.get("lab_selected") and t.get("lab_kind") == "product" and t["lab_status"] != "complete"]
                selected.sort(key=lambda k: (tasks[k]["lab_role"] != "candidate", tasks[k]["created_at"], k))
                seen = set()
                while selected and processed < max_tasks and time.monotonic() < client.deadline:
                    key = selected.pop(0)
                    if key in seen:
                        continue
                    seen.add(key)
                    task = deepcopy(tasks[key])
                    if task["lab_status"] == "complete":
                        continue
                    fixture = next((p for p in pages if p["url"] == task["url"]), None)
                    if fixture is None:
                        raise ValueError("Live request outside the fixed allowlist")
                    processed += 1
                    if mode == "replay":
                        body = (Path(inputs) / fixture["path"]).read_bytes()
                        page = Page(task["url"], body, fixture["observed_at"], "saved_primary_html")
                        receipt = Receipt(task["url"], fixture["status"], fixture["observed_at"], fixture["body_sha256"],
                                          "saved_primary_html", environment(), evidence_mode="replay")
                    else:
                        page, receipt = client.fetch(task["url"])
                    receipt_dict = asdict(receipt)
                    receipts.append(receipt_dict)
                    task["lab_attempts"].append(receipt_dict)
                    task["lab_last_error"] = receipt.error
                    changes = [("hosts", h, v) for h, v in client.hosts.items()]
                    if page:
                        observation = normalize(task["lab_store"], page, cfg["stores"][task["lab_store"]], run_id, receipt_dict)
                        task["lab_status"] = "complete"
                        changes.append(("observations", observation.offer.key, observation.to_dict()))
                        if task["lab_role"] == "candidate":
                            task["lab_identity"] = observation.offer.identity
                            task["lab_query"] = observation.offer.jan or observation.offer.model
                            added, keys = dependencies(task, pages, tasks, page.observed_at, state["records"].get("identity_catalog", {}))
                            changes += added
                            tasks.update({k: v for _, k, v in added})
                            if architecture != "A":
                                selected.extend(k for k in keys if tasks[k].get("lab_kind") == "product" and tasks[k].get("lab_selected") and k not in seen)
                        completed_times[key] = time.perf_counter() - start
                    else:
                        task["lab_status"] = "waiting"
                    tasks[key] = task
                    changes.append(("tasks", key, task))
                    # Observation and completion are a single durable transaction.
                    store.commit(f"acquire:{run_id}:{cycle}:{key}:{len(task['lab_attempts'])}", changes)
        finally:
            client.transport.close()
        state = store.snapshot()
        observations = list(state["records"].get("observations", {}).values())
        offers = [Offer.from_dict(o["offer"]) for o in observations]
        decisions = []
        changes = []
        for task in state["records"]["tasks"].values():
            if task.get("lab_role") != "candidate":
                continue
            candidate = next((o for o in offers if o.url == task["url"]), None)
            if not candidate:
                continue
            missing = [p for p in pages if p["group"] == task["lab_group"] and p["store"] != task["lab_store"] and not any(o.url == p["url"] for o in offers)]
            missing += [t for t in state["records"]["tasks"].values() if t.get("lab_kind") == "product"
                        and task["url"] in t.get("lab_dependencies", []) and t["lab_status"] != "complete"]
            now = replay_now if mode == "replay" else datetime.now(timezone.utc)
            decision = decide(candidate, offers, [], now, run_id)
            if missing:
                decision.update(status="insufficient", rule=None,
                                reasons=sorted(set(decision["reasons"] + ["known_comparator_not_verified_this_run"])))
            decision["candidate_url"] = candidate.url
            decision["history_scope"] = "history_retained_but_not_loaded_for_this_two_product_A_rule_experiment"
            decisions.append(decision)
            prior = state["records"].get("event_state", {}).get(candidate.key)
            events, event_state = update_events(candidate, decision, deepcopy(prior), now)
            changes.append(("decisions", candidate.key, decision))
            changes.append(("event_state", candidate.key, event_state))
            for event in events:
                event.update(published=False, delivery_status="not_sent_lab_only", experiment_id=run_id,
                             evidence_mode=mode)
                changes.append(("events", event["event_id"], event))
        changes = [(ns, key, value) for ns, key, value in changes
                   if key not in state["records"].get(ns, {}) or state["records"][ns][key] != value]
        if changes:
            store.commit("decision:" + digest(changes), changes)
        state = store.snapshot()
        tasks = state["records"]["tasks"]
        originals = {k: t for k, t in tasks.items() if "lab_key" in t}
        pending = [t for t in tasks.values() if t["lab_status"] != "complete"]
        pending_age_now = timestamp(read(Path(inputs) / label / "public/latest.json")["generated_at"])
        summary = {"experiment_id": run_id, **conditions, "environment": environment(),
                   "wall_seconds": time.perf_counter() - start, "http_navigation_attempts": client.requests,
                   "replayed_pages": len(receipts) if mode == "replay" else 0,
                   "observations": len(offers), "decisions": decisions,
                   "decidable_candidates": sum(d["status"] != "insufficient" for d in decisions),
                   "accepted_candidates": sum(d["status"] == "accepted" for d in decisions),
                   "original_pending_retained": len(originals), "all_pending_including_external_wait": len(pending),
                   "original_tasks_completed_by_replay_only": sum(t["lab_status"] == "complete" for t in originals.values()) if mode == "replay" else 0,
                   "original_pending_over_24h": sum(bool(timestamp(t.get("created_at"))) and (pending_age_now - timestamp(t["created_at"])).total_seconds() > 86400 for t in originals.values()),
                   "same_cycle_planner_enabled": architecture != "A",
                   "all_selected_comparators_observed": len(offers) == 6,
                   "completed_task_seconds": completed_times, "emitted_storage_bytes": store.emitted_bytes,
                   "logical_mutation_bytes": store.logical_bytes,
                   "scope": "Two products, six saved primary pages from October 1 used against each pinned backlog. Page fixtures are supplemental controlled inputs, not a reconstruction of the snapshot's original HTTP outcomes. Whole-store throughput and full coverage not measured.",
                   "scheduled_stability_samples": 0, "formal_audits_added": 0, "cutover": False}
        counters = process_counters()
        summary["peak_rss_bytes"] = counters["peak_rss_bytes"]
        summary["process_write_bytes_delta"] = counters.get("process_write_bytes", 0) - counters_before.get("process_write_bytes", 0)
        summary["timing_scope"] = "Collection/planning/checkpoints only; fixed-input hash verification and final publication/export excluded. Run in a fresh process for comparable peak RSS."
        write(output / "receipts.json", receipts)
        write(output / "result.json", summary)
        store.export(output / "state-export.json")
        if isinstance(store, SQLite):
            store.backup(output / "backup.sqlite3")
        publish(store, output / "publication", run_id, {"schema_version": 1, "mode": "isolated_lab",
                "monitored_store_count": 10, "excluded_stores": ["rakuten"], "offers": [o.to_dict() for o in offers],
                "decisions": decisions, "notifications": list(state["records"].get("events", {}).values())})
        return summary
