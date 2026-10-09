"""Bounded routing tallies for the index; full records remain in the detail file."""
from __future__ import annotations

from collections import Counter


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
