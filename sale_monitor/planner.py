"""Plan comparison requests without fetching or persisting any data."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from math import floor

from .engine import history_basis
from .models import Offer, iso, same_product, timestamp


def _backoff(entry: dict, now: datetime) -> bool:
    failures = entry.get("consecutive_failures", 0)
    attempted = timestamp(entry.get("last_attempt_at"))
    if failures <= 0 or attempted is None:
        return False
    interval = min(timedelta(hours=4 * 2 ** min(failures - 1, 6)), timedelta(days=7))
    return now - attempted < interval


def plan(candidates: list[Offer], current: list[Offer], known: dict[str, list[Offer]],
         history_by_identity: dict[str, list[dict]], scope_stores: list[str],
         lookup_stores: list[str], budgets: dict[str, int], tracking: dict,
         changes: dict[str, str], now: datetime, *, run_id: str, scope_version: int,
         exploration_share: float = 0.2, verifiable_stores: list[str] | None = None) -> dict:
    """Reserve complete comparison bundles and bounded searches for this run."""
    if not 0 <= exploration_share <= 1:
        raise ValueError("exploration_share must be between zero and one")
    if any(type(value) is not int or value < 0 for value in budgets.values()):
        raise ValueError("budgets must be nonnegative integers")
    scope = set(scope_stores)
    verifiable = scope if verifiable_stores is None else set(verifiable_stores) & scope
    stores = sorted(verifiable)
    lookup = sorted(set(lookup_stores) & verifiable)
    confirm_budget = {s: floor(budgets.get(s, 0) * (1 - exploration_share)) for s in stores}
    explore_budget = {s: budgets.get(s, 0) - confirm_budget[s] for s in stores}
    confirmed = {(o.store, o.key) for o in current if o.observed_run_id == run_id}
    confirmed_urls = {(o.store, o.url) for o in current if o.observed_run_id == run_id}
    rows = []
    for candidate in candidates:
        identity = candidate.identity
        errors = candidate.errors(now)
        basis = history_basis(candidate, history_by_identity.get(identity, []), now)
        peers = {}
        # The index normally contains the latest offer; choose it deterministically
        # if callers supply duplicate snapshots for the same store and key.
        for other in sorted(known.get(identity, []), key=lambda o: (iso(timestamp(o.observed_at)) if timestamp(o.observed_at) else "", o.url)):
            if (same_product(candidate, other) and other.seller_id != candidate.seller_id
                    and other.store in scope and other.channel == "online"):
                peers[(other.store, other.key)] = other
        bundle = []
        successes = []
        for key, other in sorted(peers.items()):
            entry = tracking.get(identity, {}).get(other.store, {})
            success = timestamp(entry.get("last_success_at"))
            successes.append(iso(success) if success else "")
            state = ("confirmed_this_run" if key in confirmed else "unverifiable" if other.store not in verifiable
                     else "backoff" if _backoff(entry, now) else "to_verify")
            bundle.append({"store": other.store, "offer_key": other.key, "url": other.url, "state": state})
        bundled_urls = {(item["store"], item["url"]) for item in bundle}
        for store, entry in sorted(tracking.get(identity, {}).items()):
            if store not in scope or store == candidate.store:
                continue
            for url in sorted(set(entry.get("unverified_urls", []))):
                if (store, url) in bundled_urls:
                    continue
                success = timestamp(entry.get("last_success_at"))
                successes.append(iso(success) if success else "")
                state = ("confirmed_this_run" if (store, url) in confirmed_urls else "unverifiable" if store not in verifiable
                         else "backoff" if _backoff(entry, now) else "to_verify")
                bundle.append({"store": store, "offer_key": None, "url": url, "state": state})
        payments = [o.payment for o in peers.values() if o.payment is not None]
        reference = min(payments) if payments else None
        price = candidate.payment
        if reference is None or price is None:
            price_rank = 2
        elif reference - price >= 500 and (reference - price) * 10 >= reference:
            price_rank = 0
        else:
            price_rank = 1 if price <= reference else 3
        priority = [{"met": 0, "insufficient": 1, "not_met": 2}[basis["status"]],
                    {"new": 0, "price_down": 1, "restocked": 2}.get(changes.get(candidate.key), 3),
                    price_rank, min(successes, default=""), candidate.key]
        if errors:
            status, reasons = "candidate_ineligible", errors
        elif not bundle:
            status, reasons = "no_known_comparator", ["no_known_comparator"]
        elif any(item["state"] == "unverifiable" for item in bundle):
            status, reasons = "comparator_unverifiable", ["comparator_unverifiable"]
        elif any(item["state"] == "backoff" for item in bundle):
            status, reasons = "comparator_backoff", ["comparator_backoff"]
        elif all(item["state"] == "confirmed_this_run" for item in bundle):
            status, reasons = "already_confirmed", []
        else:
            status, reasons = "pending", []
        rows.append((candidate, {"offer_key": candidate.key, "store": candidate.store, "identity": identity,
                                "status": status, "reasons": reasons, "history_status": basis["status"],
                                "priority": priority, "bundle": bundle}))
    rows.sort(key=lambda pair: (pair[1]["priority"], pair[0].store))
    confirm = {s: [] for s in stores}
    explore = {s: [] for s in stores}
    reserved = {s: 0 for s in stores}
    explore_requests = {s: 0 for s in stores}
    requests = {}
    for candidate, row in rows:
        if row["status"] != "pending":
            continue
        required = {(item["store"], item["url"]): item for item in row["bundle"] if item["state"] == "to_verify"}
        extra = Counter(store for store, url in required if (store, url) not in requests)
        if any(reserved[store] + count > confirm_budget[store] for store, count in extra.items()):
            row.update(status="skipped_budget", reasons=["skipped_budget"])
            continue
        row["status"] = "planned"
        for key, item in sorted(required.items()):
            store, url = key
            if key not in requests:
                request = {"url": url, "offer_key": item["offer_key"], "identity": candidate.identity, "for_candidates": []}
                requests[key] = request
                confirm[store].append(request)
                reserved[store] += 1
            if candidate.key not in requests[key]["for_candidates"]:
                requests[key]["for_candidates"].append(candidate.key)
    searches = {}
    for candidate, row in rows:
        if row["status"] != "no_known_comparator":
            continue
        known_stores = {item["store"] for item in row["bundle"]}
        for store in lookup:
            if store == candidate.store or store in known_stores:
                continue
            entry = tracking.get(candidate.identity, {}).get(store, {})
            searched_at = timestamp(entry.get("last_search_at"))
            if searched_at is not None and now - searched_at <= timedelta(hours=24):
                continue
            key = (store, candidate.identity)
            if key not in searches:
                if explore_requests[store] + 2 > explore_budget[store]:
                    continue
                request = {"query": candidate.jan or candidate.brand + " " + candidate.model,
                           "identity": candidate.identity, "for_candidates": []}
                searches[key] = request
                explore[store].append(request)
                explore_requests[store] += 2
            if candidate.key not in searches[key]["for_candidates"]:
                searches[key]["for_candidates"].append(candidate.key)
    candidate_rows = [row for candidate, row in rows]
    return {"run_id": run_id, "scope_version": scope_version, "generated_at": iso(now),
            "confirm": confirm, "explore": explore, "candidates": candidate_rows,
            "summary": {"candidates": len(candidate_rows), "by_status": dict(sorted(Counter(row["status"] for row in candidate_rows).items())),
                        "reserved_requests": reserved, "confirm_budget": confirm_budget,
                        "explore_budget": explore_budget, "explore_requests": explore_requests}}


def update_tracking(tracking: dict, plan: dict, current: list[Offer], failed: dict[str, set[str]],
                    searched: dict[str, set[str]], now: datetime, discoveries: dict | None = None) -> dict:
    """Apply reached requests only, keeping the supplied tracking unchanged."""
    result = deepcopy(tracking)
    observed = {(o.store, o.url) for o in current if o.observed_run_id == plan["run_id"]}
    observed_keys = {(o.store, o.key) for o in current if o.observed_run_id == plan["run_id"]}
    updated_at = iso(now)

    def entry(identity, store):
        return result.setdefault(identity, {}).setdefault(store, {"last_attempt_at": None, "last_success_at": None,
                                                                 "consecutive_failures": 0, "last_search_at": None})

    outcomes = {}
    for store, requests in plan["confirm"].items():
        for request in requests:
            fetched = {r["url"] if isinstance(r, dict) else r for r in (discoveries or {}).get(store, {}).get("verified", [])}
            success = ((store, request["url"]) in observed or request["url"] in fetched or
                       (store, request.get("offer_key")) in observed_keys)
            failure = request["url"] in failed.get(store, set())
            if success or failure:
                values = outcomes.setdefault((request["identity"], store), [False, 0])
                values[0] |= success
                values[1] += int(failure and not success)
    for (identity, store), (success, failures) in sorted(outcomes.items()):
        values = entry(identity, store)
        values["last_attempt_at"] = updated_at
        if success:
            values["last_success_at"] = updated_at
            values["consecutive_failures"] = 0
        # A failed URL still leaves an incomplete bundle when another URL succeeded.
        if failures:
            values["consecutive_failures"] = values.get("consecutive_failures", 0) + failures
    for store, identities in sorted(searched.items()):
        for identity in sorted(identities):
            entry(identity, store)["last_search_at"] = updated_at
    # A discovered URL stays an open obligation until evidence settles it.
    # Fetching the page is not enough: an unidentified product, a different
    # kind of identifier, or unknown purchase conditions all remain open.
    observations = [o for o in current if o.observed_run_id == plan["run_id"]]
    by_url = {(o.store, o.url): o for o in observations}

    def settled(identity, store, url):
        offer = by_url.get((store, url))
        if offer is None or not offer.identity:
            return None
        if offer.identity != identity:
            same_kind = offer.identity.split(":", 1)[0] == identity.split(":", 1)[0]
            return {"reason": "identity_mismatch", "observed_identity": offer.identity} if same_kind else None
        # The same identifier does not prove the same configuration. Hand the
        # obligation to the known-comparator rule only when every current sale
        # candidate is the same product; otherwise that rule would skip it.
        candidates = [c for c in observations if c.identity == identity and c.store != store
                      and c.discovery_kind != "comparison"]
        if not candidates or not all(same_product(c, offer) for c in candidates):
            return None
        if not offer.errors(now):
            return {"reason": "became_known_comparator", "offer_key": offer.key}
        # An explicit end is evidence, exactly as for known comparators.
        ended = offer.stock == "out_of_stock" and offer.verified and offer.evidence and not offer.issues
        return {"reason": "ended", "offer_key": offer.key} if ended else None

    for store, discovery in sorted((discoveries or {}).items()):
        for row in list(discovery.get("unverified", [])) + [r for r in discovery.get("verified", []) if isinstance(r, dict)]:
            values = entry(row["identity"], store)
            values.setdefault("unverified_urls", []).append(row["url"])
    for identity, stores in result.items():
        for store, values in stores.items():
            if "unverified_urls" not in values:
                continue
            remaining = []
            for url in sorted(set(values["unverified_urls"])):
                outcome = settled(identity, store, url)
                if outcome is None:
                    remaining.append(url)
                else:
                    log = values.setdefault("settled_urls", [])
                    log.append({"url": url, "at": updated_at, **outcome})
                    del log[:-20]
            values["unverified_urls"] = remaining
    return result
