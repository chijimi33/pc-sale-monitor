from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit

from sale_monitor.engine import update_events
from sale_monitor.http import Page
from sale_monitor.models import Offer, STORES, iso, same_product, timestamp
from sale_monitor.parsing import confirmed_empty_search
from .acquire import Coordinator, Receipt, TRANSPORTS
from .benchmark import process_counters
from .coverage import coverage_report
from .evidence import decide, normalize, reconcile_discovery
from .discovery import Dispatcher, activate_search, load_bundle, root_changes
from .inputs import import_state, verify
from .history import HistoryIndex, profile
from .events import load_registry, project
from .queueing import task_age_key
from .operations import publish
from .safety import digest, environment, experiment_id, guard, implementation_hash, read, write
from .scheduler import collect
from .stores import BACKENDS, SQLite


def task_id(store, url):
    return "fixture:" + digest([store, url])[:24]


def existing_task_id(tasks, store, url):
    matches = [k for k, t in tasks.items() if t.get("lab_store") == store and t.get("url") == url
               and t.get("type", "product") == "product"]
    return min(matches, key=lambda k: (task_age_key(tasks[k]), k)) if matches else task_id(store, url)


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
            if (candidate.get('lab_identity_profile') and known.get('profile')
                    and not same_product(Offer.from_dict(candidate['lab_identity_profile']), Offer.from_dict(known['profile']))):
                continue
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


def run(inputs, label, architecture, mode, output, *, backend=None, transport="urllib", budget=2100, cycles=1, max_tasks=20,
        discovery_input=None):
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
    discovery = load_bundle(discovery_input, manifest['input_hash'], cfg['stores']) if discovery_input else None
    resources = {p['url']: {**p, '_root': Path(inputs), 'kind': 'product'} for p in pages}
    if discovery:
        for resource in discovery['resources']:
            if resource['url'] in resources:
                raise ValueError('Discovery input must not replace pinned primary-page fixtures')
            resources[resource['url']] = {**resource, '_root': Path(discovery_input)}
    allowed_urls = set(resources)
    replay_now = max(timestamp(p['observed_at']) for p in resources.values())
    start = time.perf_counter()
    counters_before = process_counters()
    source_files, source_tasks, source_offers = import_state(Path(inputs), label)
    source_event_states, source_events, event_input = load_registry(source_files)
    history_started = time.perf_counter()
    historical = HistoryIndex(source_files, replay_now if mode == 'replay' else datetime.now(timezone.utc))
    history_index_seconds = time.perf_counter() - history_started
    chosen = backend or ("json" if architecture == "A" else "sqlite")
    meta_path = output / "experiment.json"
    conditions = {"input_hash": manifest["input_hash"], "snapshot": label, "architecture": architecture,
                  "mode": mode, "backend": chosen, "transport": transport, "budget": budget, "cycles": cycles,
                  "max_tasks": max_tasks, "source_base": manifest["source_base"]}
    conditions["implementation_hash"] = implementation_hash()
    if discovery:
        conditions['discovery_input_hash'] = discovery['input_hash']
    if meta_path.exists():
        meta = read(meta_path)
        if meta["conditions"] != conditions:
            raise ValueError("Cannot resume with changed experimental conditions")
    else:
        meta = {"experiment_id": experiment_id(), "conditions": conditions, "environment": environment(),
                "started_at": iso(), "production_run_id": None}
        write(meta_path, meta)
    run_id = meta["experiment_id"]
    with BACKENDS[chosen](output / "store") as store:
        state = store.snapshot()
        if not state["transactions"]:
            files, originals, known_offers = source_files, source_tasks, source_offers
            changes = [("source_files", k, v) for k, v in files.items()] + [("tasks", k, v) for k, v in originals.items()]
            changes += [("identity_catalog", key, {"identity": Offer.from_dict(row).identity,
                         'profile': profile(Offer.from_dict(row)),
                         **{field: row.get(field) for field in ("store", "url", "condition", "variant", "warranty")}})
                        for key, row in known_offers.items()]
            changes += [('identity_catalog', key, row) for key, row in historical.catalog().items()]
            changes += [('history_inputs', 'snapshot', historical.summary())]
            changes += [('event_inputs', 'snapshot', event_input)]
            changes += [('event_state', key, value) for key, value in source_event_states.items()]
            changes += [('events', key, value) for key, value in source_events.items()]
            for page in pages:
                if page["name"] not in {"koubou-b550", "tsukumo-capture"}:
                    continue
                key = existing_task_id(originals, page["store"], page["url"])
                original = originals.get(key)
                task = make_task(page["store"], page["url"], page["observed_at"], group=page["group"], role="candidate", original=original)
                changes.append(("tasks", key, task))
            if discovery:
                changes += root_changes(discovery, originals, cfg['stores'], allowed_urls)
            store.commit("import:" + manifest["snapshots"][label]["data_sha"], changes)
        state = store.snapshot()
        hosts = deepcopy(state["records"].get("hosts", {}))
        client = Coordinator(TRANSPORTS[transport](), budget=budget, hosts=hosts)
        def verified_not_found(page):
            fixture = resources.get(page.url, {})
            return fixture.get('kind') == 'list' and bool(confirmed_empty_search(fixture['store'], page))
        client.inspect_not_found = verified_not_found if discovery else None
        dispatcher = Dispatcher(cfg['stores'], allowed_urls)

        if mode == "replay":
            def saved_page(url):
                fixture = resources[url]
                body = (fixture['_root'] / fixture["path"]).read_bytes()
                if hashlib.sha256(body).hexdigest() != fixture['body_sha256']:
                    raise ValueError('Saved resource changed during collection')
                method = 'saved_primary_html' if fixture['_root'] == Path(inputs) else 'saved_discovery_html'
                page = Page(url, body, fixture["observed_at"], method, fixture.get('content_type', ''), status=fixture['status'])
                receipt = Receipt(url, fixture["status"], fixture["observed_at"], fixture["body_sha256"],
                                  method, environment(), evidence_mode='fixture_replay' if fixture.get('evidence_mode') == 'fixture' else 'replay',
                                  body_bytes=len(body), content_type=page.content_type)
                if fixture['status'] != 200:
                    receipt.error = 'http_' + str(fixture['status'])
                    if fixture['status'] != 404 or not verified_not_found(page):
                        page = None
                return page, receipt
            client.fetch = saved_page

        def plan_candidate(task, offer, tasks, cycle, records):
            task['lab_identity'] = offer.identity
            task['lab_identity_profile'] = profile(offer)
            task['lab_query'] = offer.jan or offer.model
            task['lab_comparisons_planned'] = True
            added, _ = dependencies(task, pages, tasks, offer.observed_at, records.get('identity_catalog', {}))
            for _, key, value in added:
                if discovery:
                    if value['lab_kind'] == 'search':
                        activate_search(value, cfg['stores'], allowed_urls)
                    elif value.get('url') in allowed_urls:
                        value['lab_selected'] = True
                if not tasks.get(key, {}).get('lab_selected'):
                    value['lab_ready_cycle'] = cycle + (architecture == 'A')
            return added

        def on_page(task, page, receipt_dict, tasks, cycle, records):
            if task['lab_kind'] in {'search', 'list'}:
                changes = dispatcher.expand(task, page, receipt_dict, tasks)
                staged = {**tasks, **{key: value for ns, key, value in changes if ns == 'tasks'}}
                for key in dict.fromkeys(k for ns, k, _ in changes if ns == 'tasks'):
                    child = staged[key]
                    if (child.get('lab_kind') == 'product' and child.get('lab_status') == 'complete'
                            and child.get('lab_role') == 'candidate' and not child.get('lab_comparisons_planned')):
                        prior = next((o for o in records.get('observations', {}).values() if
                                      o['offer']['store'] == child['lab_store'] and o['offer']['url'] == child['url']), None)
                        if prior is not None:
                            added = plan_candidate(child, Offer.from_dict(prior['offer']), staged, cycle, records)
                            changes += [('tasks', key, child)] + added
                            staged.update({k: v for _, k, v in added})
                return changes
            observation = normalize(task["lab_store"], page, cfg["stores"][task["lab_store"]], run_id, receipt_dict)
            changes = [("observations", observation.offer.key, observation.to_dict())]
            if task["lab_role"] == "candidate":
                changes += plan_candidate(task, observation.offer, tasks, cycle, records)
            return changes

        try:
            collection = collect(store, client, run_id, allowed_urls, on_page,
                                 architecture=architecture, cycles=cycles, max_tasks=max_tasks, budget=budget,
                                 elapsed_before_collection=time.perf_counter() - start)
        finally:
            client.transport.close()
        receipts = collection['receipts']
        completed_times = collection['completed_task_seconds']
        state = store.snapshot()
        if discovery:
            reconciled = []
            for key, observation in state['records'].get('observations', {}).items():
                matches = [t for t in state['records']['tasks'].values() if
                           t.get('lab_store') == observation['offer']['store'] and t.get('url') == observation['offer']['url']]
                sources = []
                for task in matches:
                    for source in task.get('lab_discovery_evidence', []):
                        if source not in sources:
                            sources.append(source)
                if sources:
                    role = 'candidate' if any(t.get('lab_role') == 'candidate' for t in matches) else 'comparison'
                    value = reconcile_discovery(observation, sources, role)
                    if value != observation:
                        reconciled.append(('observations', key, value))
            if reconciled:
                store.commit('discovery-evidence:' + digest(reconciled), reconciled)
                state = store.snapshot()
        observations = sorted(state["records"].get("observations", {}).values(),
                              key=lambda row: (row['offer']['store'], row['offer']['url']))
        offers = [Offer.from_dict(o["offer"]) for o in observations]
        decisions = []
        basis_decisions = []
        changes = []
        decided = set()
        now = replay_now if mode == "replay" else datetime.now(timezone.utc)
        for task in sorted(state["records"]["tasks"].values(), key=lambda row: (row.get('lab_store', ''), row.get('url', ''))):
            if task.get("lab_role") != "candidate":
                continue
            candidate = next((o for o in offers if o.url == task["url"] and o.store == task['lab_store']), None)
            if not candidate or candidate.key in decided:
                continue
            decided.add(candidate.key)
            missing = [p for p in pages if p["group"] == task["lab_group"] and p["store"] != task["lab_store"] and not any(o.url == p["url"] for o in offers)]
            missing += [t for t in state["records"]["tasks"].values() if t.get("lab_kind") == "product"
                        and task["url"] in t.get("lab_dependencies", []) and t["lab_status"] != "complete"]
            discovery_pending = [t for t in state['records']['tasks'].values() if discovery
                                 and t.get('lab_kind') in {'search', 'list'} and (t.get('lab_selected') or t.get('lab_parent_keys'))
                                 and candidate.url in t.get('lab_dependencies', []) and t.get('lab_status') != 'complete']
            pair = {}
            for points in (False, True):
                history_rows, history_evidence = historical.select(candidate, now, run_id, points=points)
                decision = decide(candidate, offers, history_rows, now, run_id, points=points)
                if missing:
                    decision.update(status='insufficient', rule=None,
                                    reasons=sorted(set(decision['reasons'] + ['known_comparator_not_verified_this_run'])))
                if discovery_pending:
                    decision.update(status='insufficient', rule=None,
                                    reasons=sorted(set(decision['reasons'] + ['comparison_discovery_incomplete'])))
                decision['candidate_url'] = candidate.url
                decision['history_scope'] = 'pinned_history_B_only_current_comparisons_same_run'
                decision['history_evidence'] = history_evidence
                pair[decision['basis']] = decision
            # Match production selection: prefer an accepted payment basis,
            # otherwise use points only when that basis actually qualifies.
            decision = pair['payment'] if pair['payment']['status'] == 'accepted' or pair['points']['status'] != 'accepted' else pair['points']
            basis_record = {'offer_key': candidate.key, 'candidate_url': candidate.url,
                            **pair, 'selected_basis': decision['basis']}
            basis_decisions.append(basis_record)
            decisions.append(decision)
            prior = state["records"].get("event_state", {}).get(candidate.key)
            event_decision = decision
            prior_basis = (prior or {}).get('facts', {}).get('basis')
            if decision['status'] != 'accepted' and prior_basis in pair and pair[prior_basis]['status'] == 'insufficient':
                # Losing evidence for the previously accepted basis is not a
                # qualification reset, even if the other basis is rejected.
                event_decision = pair[prior_basis]
            events, event_state = update_events(candidate, event_decision, deepcopy(prior), now)
            event_state = {**deepcopy(prior or {}), **event_state}
            changes.append(("decisions", candidate.key, decision))
            changes.append(('basis_decisions', candidate.key, basis_record))
            changes.append(("event_state", candidate.key, event_state))
            for event in events:
                # A fixed ID may already have a delivery acknowledgement. Never
                # replace its immutable original payload with replay output.
                if event['event_id'] in state['records'].get('events', {}):
                    existing = state['records']['events'][event['event_id']]
                    if any(existing.get(k) != event[k] for k in ('offer_key', 'kind')):
                        raise ValueError('Conflicting existing event identity')
                    continue
                event.update(published=False, delivery_status="not_sent_lab_only", experiment_id=run_id,
                             evidence_mode=mode)
                changes.append(("events", event["event_id"], event))
        changes = [(ns, key, value) for ns, key, value in changes
                   if key not in state["records"].get(ns, {}) or state["records"][ns][key] != value]
        if changes:
            store.commit("decision:" + digest(changes), changes)
        state = store.snapshot()
        notifications, event_projection = project(state['records'].get('events', {}),
                                                   state['records'].get('observations', {}),
                                                   state['records'].get('decisions', {}), now, run_id,
                                                   basis_decisions=state['records'].get('basis_decisions', {}))
        tasks = state["records"]["tasks"]
        originals = {k: t for k, t in tasks.items() if "lab_key" in t}
        pending = [t for t in tasks.values() if t["lab_status"] != "complete"]
        pending_age_now = timestamp(read(Path(inputs) / label / "public/latest.json")["generated_at"])
        coverage = coverage_report(resources, state['records'], source_tasks, cfg['stores'], mode, run_id)
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
                   "scheduling": collection['scheduling'],
                   "logical_mutation_bytes": store.logical_bytes,
                   "scope": "Two products, six saved primary pages from October 1 used against each pinned backlog. Page fixtures are supplemental controlled inputs, not a reconstruction of the snapshot's original HTTP outcomes. Whole-store throughput and full coverage not measured.",
                   "scheduled_stability_samples": 0, "formal_audits_added": 0, "cutover": False}
        summary['coverage'] = coverage
        summary['history_input'] = historical.summary()
        summary['basis_decisions'] = basis_decisions
        summary['basis_counts'] = {basis: {status: sum(row[basis]['status'] == status for row in basis_decisions)
                                          for status in ('accepted', 'rejected', 'insufficient')}
                                   for basis in ('payment', 'points')}
        summary['history_index_seconds'] = history_index_seconds
        if discovery:
            summary['all_selected_comparators_observed'] = (all(any(o.url == p['url'] for o in offers) for p in pages)
                and all(t['lab_status'] == 'complete' for t in tasks.values() if t.get('lab_selected') and t.get('lab_role') == 'comparison'))
            discovery_tasks = [t for t in tasks.values() if t.get('lab_kind') in {'search', 'list'} and
                               (t.get('lab_selected') or t.get('lab_root_ids') or t.get('lab_parent_keys'))]
            summary['discovery'] = {'input_hash': discovery['input_hash'], 'root_requests': len(discovery['roots']),
                                   'resources': len(discovery['resources']), 'tasks': len(discovery_tasks),
                                   'completed': sum(t['lab_status'] == 'complete' for t in discovery_tasks),
                                   'confirmed_empty': sum(t.get('lab_resolution', {}).get('result') == 'confirmed_empty' for t in discovery_tasks),
                                   'outside_selected_resources': sum(t.get('lab_reason') == 'discovered_url_outside_selected_resources' for t in tasks.values()),
                                   'discovered_products': sum(t.get('lab_kind') == 'product' and bool(t.get('lab_parent_keys')) for t in tasks.values())}
            summary['scope'] += ' An explicit discovery bundle adds bounded home/list/product resources; all other discovered URLs stay pending with their lineage. This does not measure whole-store completion.'
        counters = process_counters()
        summary["peak_rss_bytes"] = counters["peak_rss_bytes"]
        summary["process_write_bytes_delta"] = counters.get("process_write_bytes", 0) - counters_before.get("process_write_bytes", 0)
        summary["timing_scope"] = "History preparation, collection/planning, decisions and checkpoints; fixed-input hash verification and final publication/export excluded. Run in a fresh process for comparable peak RSS."
        summary['event_input'] = event_input
        summary['event_projection'] = {k: v for k, v in event_projection.items() if k != 'events'}
        write(output / 'event-projection.json', event_projection)
        write(output / 'coverage.json', coverage)
        write(output / "receipts.json", receipts)
        write(output / "result.json", summary)
        payload = {"schema_version": 1, "mode": "isolated_lab",
                "monitored_store_count": 10, "excluded_stores": ["rakuten"], "offers": [o.to_dict() for o in offers],
                "decisions": decisions, 'basis_decisions': basis_decisions, "notifications": notifications}
        publish(store, output / "publication", run_id + '-' + digest(payload)[:24], payload)
        store.export(output / "state-export.json")
        if isinstance(store, SQLite):
            store.backup(output / "backup.sqlite3")
        return summary
