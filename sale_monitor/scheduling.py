"""Bounded waiting between discovery, sale refresh and comparison work.

These are processing shares, not request limits. All queued work is retained.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from .models import Offer, timestamp


LANES = ("discovery", "sale", "comparison", "sale")


def plan_comparison_refresh(offers: dict, requests, candidate_identities=()) -> tuple[set[str], dict]:
    """Select new refresh work, without resolving or editing existing work.

    A broad model search may discover many other catalog products. Once their
    identities are known, refresh only those needed by the current comparison
    requests. Unknown identities remain work to verify, never implicit no-hits.
    Missing/legacy request metadata cannot prove that an item is unnecessary.
    """
    usable = isinstance(requests, list) and all(
        isinstance(r, dict) and isinstance(r.get("identity"), str) and r["identity"].strip()
        for r in requests)
    identities = {r["identity"] for r in requests} if usable else None
    candidates = set(candidate_identities)
    selected, counts = set(), Counter()
    for key, row in offers.items():
        if row.get("channel") != "online" or row.get("discovery_kind") != "comparison":
            continue
        identity = Offer.from_dict(row).identity
        reason = ("request_plan_unverified" if identities is None else
                  "identity_unresolved" if not identity else
                  "requested_identity" if identity in identities else
                  "sale_candidate_identity" if identity in candidates else "not_requested_identity")
        counts[reason] += 1
        if reason != "not_requested_identity":
            selected.add(key)
    return selected, {
        "policy": "requested_or_known_sale_candidate_identity_or_unresolved",
        "request_plan_verified": usable,
        "requested_identity_count": len(identities) if identities is not None else None,
        "sale_candidate_identity_count": len(candidates),
        "known_comparison_offers": sum(counts.values()),
        "selected_for_refresh": len(selected),
        "not_newly_refreshed": counts["not_requested_identity"],
        "reasons": dict(counts),
        "existing_queue_policy": "retained_with_original_age_and_attempts",
        "historical_offer_policy": "retained_not_current_without_new_observation",
    }


def lane(task: dict) -> str:
    if task.get("kind") == "comparison":
        return "comparison"
    return "discovery" if task["type"] in ("list", "dospara_list", "amazon_discovery") else "sale"


def select_task(pending: list[tuple[str, dict]], scheduler: dict, order, now, attempted=None):
    """A saved cursor prevents short runs from always restarting in one lane."""
    cursor = scheduler.get("cursor", 0) % len(LANES)
    for offset in range(len(LANES)):
        index = (cursor + offset) % len(LANES)
        candidates = [pair for pair in pending if lane(pair[1]) == LANES[index]]
        if not candidates:
            continue

        group = LANES[index]
        if LANES[index] == "comparison":
            # Three opportunities for currently needed work, then one for old
            # work. Persist the cursor so interruptions cannot starve either.
            needed = [p for p in candidates if p[1].get("requested")]
            other = [p for p in candidates if not p[1].get("requested")]
            comparison_cursor = scheduler.get("comparison_cursor", 0) % 4
            candidates = (needed if comparison_cursor < 3 else other) or needed or other
            scheduler["comparison_cursor"] = (comparison_cursor + 1) % 4
            group += ":requested" if candidates[0][1].get("requested") else ":other"

        # Cooling down makes a failed task runnable again, but does not give it
        # every opportunity ahead of equally runnable work that has not been
        # attempted this run. Retain retry opportunities as well as coverage.
        attempted = attempted or set()
        untried = [pair for pair in candidates if pair[0] not in attempted]
        retries = [pair for pair in candidates if pair[0] in attempted]
        retry_cursors = scheduler.setdefault("retry_cursor_by_group", {})
        retry_cursor = retry_cursors.get(group, 0) % 4
        candidates = (untried if retry_cursor < 3 else retries) or untried or retries
        retry_cursors[group] = (retry_cursor + 1) % 4

        def ranked(pair):
            created = timestamp(pair[1].get("created_at"))
            overdue = created is not None and now - created >= timedelta(hours=24)
            # Overdue work cannot be displaced forever by newly promoted tasks.
            return (not overdue, order(pair[1]))

        selected = min(candidates, key=ranked)
        scheduler["cursor"] = (index + 1) % len(LANES)
        counts = scheduler.setdefault("selected_by_lane", {})
        counts[LANES[index]] = counts.get(LANES[index], 0) + 1
        attempt_kind = "retry_same_run" if selected[0] in attempted else "first_attempt_this_run"
        counts = scheduler.setdefault("selected_by_attempt_kind", {})
        counts[attempt_kind] = counts.get(attempt_kind, 0) + 1
        return selected
    return None


def plan_comparisons(current: list[Offer], changes: dict, stores, now):
    """Plan new cross-shop searches only after the candidate itself is usable.

    This never edits queues or history. Missing fields/out-of-stock candidates
    remain visible in the review feed and here, and are reconsidered next run.
    """
    eligible, held = [], []
    for offer in current:
        if offer.discovery_kind == "comparison":
            continue
        reasons = offer.errors(now)
        if reasons:
            held.append({"offer_key": offer.key, "store": offer.store,
                         "identity": offer.identity, "reasons": reasons})
        else:
            eligible.append(offer)
    planned = {}
    for name in stores:
        requests = {}
        for offer in eligible:
            if offer.store == name:
                continue
            query = offer.jan or " ".join(filter(None, (offer.brand, offer.model)))
            priority = {"new": 0, "price_down": 1, "restocked": 2}.get(changes.get(offer.key), 3)
            previous = requests.get(offer.identity)
            if previous is None or priority < previous["priority"]:
                requests[offer.identity] = {"query": query, "identity": offer.identity, "priority": priority}
        planned[name] = sorted(requests.values(), key=lambda q: (q["priority"], q["identity"]))
    summary = {"policy": "eligible_sale_candidates_only", "eligible_sale_offers": len(eligible),
               "held_sale_offers": len(held), "held_reasons": dict(Counter(r for h in held for r in h["reasons"])),
               "new_requests_by_store": {name: len(items) for name, items in planned.items()},
               "existing_queue_policy": "retained_with_original_age_and_attempts"}
    return planned, {"summary": summary, "held_candidates": held}
