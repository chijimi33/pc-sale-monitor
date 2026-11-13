"""Bounded routing tallies for the index; full records remain in the detail file."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .comparison_integrity import missing_comparators, out_of_scope_comparators
from .engine import amount
from .models import Offer, allowed_url, iso, same_product, timestamp
from .storage import read_json


REQUEST_REASONS = frozenset(("known_urls_first", "candidate_scope_unverified",
                             "compatible_known_url_missing"))
RECEIPT_RESULTS = frozenset(("verified_current_product", "search_requested",
                             "search_requested_after_unverified_done_task"))


def counts(records, field: str, allowed: frozenset) -> dict | None:
    if not isinstance(records, dict):
        return None
    result = Counter()
    for record in records.values():
        value = record.get(field) if isinstance(record, dict) else None
        key = value if isinstance(value, str) and value in allowed else "other_or_unrecognized"
        result[key] += 1
    return dict(sorted(result.items()))


def summary(state: dict) -> dict:
    graph = state.get("comparison_routing")
    result = {"status": "not_recorded", "run_id": None, "request_count": None,
              "receipt_count": None, "request_reasons": None, "receipt_results": None}
    if graph is None or graph == {}:
        return result
    if not isinstance(graph, dict):
        return {**result, "status": "invalid"}
    run_id = graph.get("run_id")
    if isinstance(run_id, str) and run_id:
        result["run_id"] = run_id
    for container, field, allowed, count_field, tally_field in (
            ("requests", "reason", REQUEST_REASONS, "request_count", "request_reasons"),
            ("receipts", "result", RECEIPT_RESULTS, "receipt_count", "receipt_results")):
        records = graph.get(container)
        result[tally_field] = counts(records, field, allowed)
        if isinstance(records, dict):
            result[count_field] = len(records)
    if result["request_count"] is None or result["receipt_count"] is None:
        result["status"] = "invalid"
    elif not result["run_id"] or not isinstance(state.get("run_id"), str) or not state["run_id"]:
        result["status"] = "unverified"
    elif state.get("status") == "job_missing" or result["run_id"] != state["run_id"]:
        result["status"] = "stale"
    else:
        result["status"] = "current"
    return result


def scope_summary(scope: dict) -> dict:
    """Publish the fixed scope without copying the full route evidence."""
    return {"version": scope["version"], "in_scope": scope["in_scope"],
            "out_of_scope": {route: {"reason": row.get("reason")}
                             for route, row in scope.get("out_of_scope", {}).items()},
            **({"optional": scope["optional"], "label": scope_label(scope)} if scope.get("optional") else {})}


def scope_label(scope: dict) -> str:
    label = f"比較範囲 v{scope['version']}"
    if scope.get("optional"):
        sources = read_json(Path(__file__).resolve().parents[1] / "config/sources.json", {})["stores"]
        stores = list(dict.fromkeys(_route_store(scope, route) for route in scope["optional"]))
        names = "、".join(sources.get(store, {}).get("name", store) for store in stores)
        label += f"（必須{len(set(scope['in_scope_stores']))}店＋任意: {names}）"
    return label


def _route_store(scope, route):
    return scope.get("routes", {}).get(route, {}).get("store", route.split(":", 1)[0])


def decision_scope(offer, payment: dict, peers: list, known: dict, current: dict, now, scope: dict, *,
                   plan_row=None, tracking=None, states=None, history=()) -> dict:
    """Attach comparison provenance without changing the evaluator's inputs."""
    confirmed = []
    for row in payment["comparisons"]:
        other = next(o for o in peers if o.seller_id == row["seller_id"] and o.url == row["url"]
                     and o.observed_at == row["observed_at"] and amount(o, payment["basis"] == "points") == row["price_yen"]
                     and same_product(offer, o) and not o.errors(now))
        confirmed.append({"store": other.store, "seller_id": other.seller_id, "channel": other.channel,
                          "url": row["url"], "observed_at": row["observed_at"], "price_yen": row["price_yen"],
                          **({"scope": "optional"} if other.store in scope.get("optional_stores", []) else {})})
    stores = scope["in_scope_stores"]
    excluded = out_of_scope_comparators(offer, known, stores)
    unconfirmed = []
    for route, row in scope.get("out_of_scope", {}).items():
        store = scope.get("routes", {}).get(route, {}).get("store", route.split(":", 1)[0])
        if store == offer.store:
            continue
        statuses = row.get("status", {})
        reason = scope.get("paused_stores", {}).get(store)
        if reason is None and isinstance(statuses, dict):
            reason = next((s for s in statuses.values() if s != "active"), None)
        item = {"route": route, "store": store, "category": "out_of_scope",
                "reason": reason or row.get("reason", "out_of_scope")}
        other = next((o for o in excluded if o["store"] == store), None)
        if other:
            item.update(known_offer_url=other["url"], last_verified_at=other["last_verified_at"])
        unconfirmed.append(item)
    missing = missing_comparators(offer, known, current, now, in_scope_stores=stores)
    observed_stores = {o.store for o in peers if o.seller_id != offer.seller_id and
                       same_product(offer, o) and not o.errors(now) and o.payment is not None}
    for route in scope["in_scope"]:
        store = scope.get("routes", {}).get(route, {}).get("store", route.split(":", 1)[0])
        if store == offer.store or store in observed_stores:
            continue
        gaps = [m for m in missing if m["store"] == store]
        issues = list(dict.fromkeys(issue for gap in gaps for issue in gap["current_issues"]))
        if not issues:
            issues = list(dict.fromkeys(issue for o in peers if o.store == store and same_product(offer, o)
                                       for issue in o.errors(now)))
        item = {"route": route, "store": store, "category": "in_scope_not_confirmed",
                "reason": issues[0] if issues else "no_known_offer"}
        if issues:
            item["current_issues"] = issues
        unconfirmed.append(item)
    if scope.get("optional"):
        unconfirmed.extend(_optional_unconfirmed(offer, payment, peers, known, now, scope,
            plan_row or {}, tracking or {}, states or {}, history))
    return {"version": scope["version"], "confirmed": confirmed, "unconfirmed": unconfirmed,
            "label": scope_label(scope)}


def _optional_unconfirmed(offer, basis, peers, known, now, scope, plan_row, tracking, states, history):
    points = basis["basis"] == "points"
    planned = {(item["store"], item["url"]): item for item in plan_row.get("optional", [])}
    result = []
    for route in scope["optional"]:
        store = _route_store(scope, route)
        if store == offer.store:
            continue
        entry = tracking.get(offer.identity, {}).get(store, {})
        urls = {o.url for o in known.get(offer.identity, []) if o.store == store
                and o.seller_id != offer.seller_id and same_product(offer, o)}
        urls.update(entry.get("unverified_urls", []))
        urls.update(url for s, url in planned if s == store)
        state = states.get(store, {})
        verification = state.get("verification", {})
        if verification.get("run_id") != offer.observed_run_id:
            verification = {}
        stage_reason = None
        stages = scope.get("run", {}).get("stage_results", {}).get(store, {})
        for phase in ("discovery", "verification"):
            status = stages.get(phase, {})
            status = status.get("status") if isinstance(status, dict) else status
            if status and status != "completed":
                stage_reason = status
                break
        if not urls:
            result.append({"route": route, "store": store, "category": "optional_not_confirmed",
                           "reason": "not_searched", "last_verified": None, "used_in_comparison": False})
        for url in sorted(urls):
            item = planned.get((store, url), {})
            keys = set()
            for receipt in verification.get("results", {}).values():
                if receipt.get("url") == url and receipt.get("observed_offer_key"):
                    keys.add(receipt["observed_offer_key"])
            observed = [o for o in peers if o.store == store and (o.url == url or o.key in keys)
                        and o.seller_id != offer.seller_id and same_product(offer, o)]
            if any(not o.errors(now) and amount(o, points) is not None for o in observed):
                continue
            reason = stage_reason
            if reason is None and item.get("state") in ("mandatory_not_ready", "backoff", "skipped_budget"):
                reason = item["state"]
            if reason is None and item.get("state") == "unverifiable":
                function = scope.get("routes", {}).get(route, {}).get("functions", {}).get("product_verify", "unverifiable")
                reason = function.get("status", "unverifiable") if isinstance(function, dict) else function
            if reason is None:
                issues = [issue for other in observed for issue in other.errors(now)]
                reason = ("fetch_failed" if url in verification.get("failed_urls", []) else
                          issues[0] if issues else "points_unknown" if observed and points else "not_reached")
            past, newer = _optional_evidence(offer, store, url, keys, points, now, history, state, entry, verification)
            row = {"route": route, "store": store, "url": url, "offer_key": item.get("offer_key"),
                   "category": "optional_not_confirmed", "reason": reason,
                   "last_verified": past, "used_in_comparison": False}
            if newer:
                row["newer_evidence"] = newer
            result.append(row)
    return result


def _optional_evidence(candidate, store, url, keys, points, now, history, state, entry, verification):
    observations = []
    failed_snapshot = False
    for row in [*(r.get("offer", {}) for r in history), *state.get("offers", {}).values(),
                *(r.get("offer", {}) for r in state.get("journal", []))]:
        try:
            other = Offer.from_dict(row)
        except (TypeError, ValueError):
            continue
        checked = timestamp(other.observed_at)
        if (other.store == store and (other.url == url or other.key in keys) and checked and checked <= now
                and other.seller_id != candidate.seller_id and same_product(candidate, other) and allowed_url(other.url)):
            failed_snapshot |= "latest_fetch_failed" in other.issues
            other.issues = [issue for issue in other.issues if issue != "latest_fetch_failed"]
            observations.append(other)
    valid = [o for o in observations if not o.errors(timestamp(o.observed_at)) and amount(o, points) is not None]
    latest = max(valid, key=lambda o: timestamp(o.observed_at), default=None)
    past = {"observed_at": latest.observed_at, "yen": amount(latest, points), "stock": latest.stock} if latest else None
    newer = []
    ended = [o for o in observations if o.stock == "out_of_stock" and o.verified and o.evidence and not o.issues
             and (latest is None or timestamp(o.observed_at) > timestamp(latest.observed_at))]
    if ended:
        other = max(ended, key=lambda o: timestamp(o.observed_at))
        newer.append({"observed_at": other.observed_at, "yen": amount(other, points), "stock": other.stock})
    attempts = [task for queue in ("queue", "verification_queue") for task in state.get(queue, {}).values()
                if task.get("url") == url and task.get("last_error")]
    failed_at = [timestamp(task.get("last_attempt_at")) for task in attempts]
    if failed_snapshot:
        failed_at.append(timestamp(entry.get("last_attempt_at")) or timestamp(state.get("checkpoint_at")))
    if url in verification.get("failed_urls", []):
        failed_at.append(timestamp(state.get("checkpoint_at")) or timestamp(entry.get("last_attempt_at")))
    failed_at = [date for date in failed_at if date and date <= now and
                 (latest is None or date > timestamp(latest.observed_at))]
    if failed_at or url in verification.get("failed_urls", []):
        newer.append({"observed_at": iso(max(failed_at)) if failed_at else None, "reason": "fetch_failed"})
    return past, newer


def plan_summary(disk, run_id: str, now, scope: dict) -> dict:
    """Read this run's plan; never create legacy comparison requests."""
    plan = disk.load(f"plans/{run_id}.json", None)
    if plan is None:
        return {"run_id": run_id, "status": "no_plan_for_run"}
    candidates = plan.get("candidates", [])
    if isinstance(candidates, dict):
        candidates = candidates.values()
    return {"run_id": run_id, "generated_at": plan.get("generated_at", iso(now)),
            "scope_version": scope["version"], "summary": plan.get("summary", {}),
            "candidate_status_counts": dict(Counter(row.get("status", "unknown") for row in candidates))}


def run_outcome(states: dict, current: list, run_id: str, scope: dict) -> dict:
    """Count current collection evidence, including confirmed empty lists."""
    paused = scope.get("paused_stores", {})
    stages = scope.get("run", {}).get("stage_results", {})

    def status(name):
        stage = stages.get(name, {}).get("discovery", "completed")
        return stage.get("status") if isinstance(stage, dict) else stage

    unavailable = {name for name in stages if status(name).startswith(("satellite_", "relay_failed", "skipped:"))}
    results = {name: state for name, state in states.items()
               if name not in paused and name not in unavailable
               and state.get("run_id") == run_id and state.get("status") != "job_missing"}
    observations = [o for o in current if o.store in results]
    empty = sum(state.get("list_pages", 0) > 0 and state.get("listed_candidates") == 0
                and not state.get("errors") and not state.get("discovery_gaps")
                and state.get("status") not in ("failed", "error", "interrupted")
                and status(name) == "completed"
                for name, state in results.items())
    successful = bool(results and (observations or empty))
    reasons = [] if successful else ["no_current_store_results" if not results else "no_current_observations_or_confirmed_empty_results"]
    return {"successful": successful, "reasons": reasons, "current_observations": len(observations),
            "stores_with_current_results": list(results), "confirmed_empty_results": empty}


def route_counts(scope: dict) -> dict:
    """Count stores once even when several routes share a seller."""
    active = set()
    for route in scope.get("routes", {}).values():
        functions = route.get("functions", {})
        if route.get("channel") == "direct" and any(
                (value.get("status") if isinstance(value, dict) else value) == "active"
                for value in functions.values()):
            active.add(route["store"])
    return {"active_direct_stores": len(active), "paused_stores": len(scope.get("paused_stores", {}))}
