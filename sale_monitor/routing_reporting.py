"""Bounded routing tallies for the index; full records remain in the detail file."""
from __future__ import annotations

from collections import Counter

from .comparison_integrity import missing_comparators, out_of_scope_comparators
from .models import iso, same_product


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
                             for route, row in scope.get("out_of_scope", {}).items()}}


def decision_scope(offer, payment: dict, peers: list, known: dict, current: dict, now, scope: dict) -> dict:
    """Attach comparison provenance without changing the evaluator's inputs."""
    confirmed = []
    for row in payment["comparisons"]:
        other = next(o for o in peers if o.seller_id == row["seller_id"] and o.url == row["url"]
                     and o.observed_at == row["observed_at"] and o.payment == row["price_yen"]
                     and same_product(offer, o) and not o.errors(now))
        confirmed.append({"store": other.store, "seller_id": other.seller_id, "channel": other.channel,
                          "url": row["url"], "observed_at": row["observed_at"], "price_yen": row["price_yen"]})
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
    return {"version": scope["version"], "confirmed": confirmed, "unconfirmed": unconfirmed,
            "label": f"比較範囲 v{scope['version']}"}


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
    results = {name: state for name, state in states.items()
               if name not in paused and state.get("run_id") == run_id and state.get("status") != "job_missing"}
    observations = [o for o in current if o.store in results]
    stages = scope.get("run", {}).get("stage_results", {})
    empty = sum(state.get("list_pages", 0) > 0 and state.get("listed_candidates") == 0
                and not state.get("errors") and not state.get("discovery_gaps")
                and state.get("status") not in ("failed", "error", "interrupted")
                and stages.get(name, {}).get("discovery", "completed") == "completed"
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
