"""Missing observations are uncertainty, never an absent competitor's price.

Past offers identify verification work only. They are not passed to the current
price evaluator, and missing work does not expire into successful verification.
"""
from __future__ import annotations

from .engine import amount
from .models import Offer, fresh, same_product, timestamp


def known_comparators(states: dict, history: list[dict], now) -> dict:
    latest = {}
    rows = (row for state in states.values() for row in state.get("offers", {}).values())
    for group in ((r.get("offer", {}) for r in history if isinstance(r, dict)), rows):
        for row in group:
            if not isinstance(row, dict):
                continue
            try:
                offer = Offer.from_dict(row)
            except (TypeError, ValueError):
                continue
            checked = timestamp(offer.observed_at)
            if offer.channel != "online" or checked is None or checked > now:
                continue
            # A subsequent failed attempt must not erase a previously verified
            # source from the missing-observation check. Keep the stored row intact.
            offer.issues = [x for x in offer.issues if x != "latest_fetch_failed"]
            if offer.errors(checked):
                continue
            for points in (False, True):
                if amount(offer, points) is None:
                    continue
                key = (offer.store, offer.key, points)
                prior = latest.get(key)
                if prior is None or timestamp(prior.observed_at) <= checked:
                    latest[key] = offer
    indexed = {}
    for offer in sorted(latest.values(), key=lambda o: timestamp(o.observed_at), reverse=True):
        indexed.setdefault(offer.identity, []).append(offer)
    return indexed


def missing_comparators(offer: Offer, known: dict, current: dict, now, *, points=False) -> list[dict]:
    missing = []
    seen = set()
    for other in known.get(offer.identity, []):
        if other.seller_id == offer.seller_id or not same_product(offer, other) or amount(other, points) is None:
            continue
        key = (other.store, other.key)
        if key in seen:
            continue
        seen.add(key)
        observed = current.get((other.store, other.key))
        if observed is not None and same_product(offer, observed):
            if not observed.errors(now) and amount(observed, points) is not None:
                continue
            # An explicit current end is evidence; an HTTP/parse failure or
            # unknown shipping/points is not. Never substitute the old price.
            ended = (observed.stock == "out_of_stock" or
                     observed.expires_at and timestamp(observed.expires_at) and timestamp(observed.expires_at) <= now or
                     observed.coupon.get("remaining") == 0 and observed.coupon.get("verified") is True)
            if ended and observed.verified and observed.evidence and not observed.issues and fresh(observed.observed_at, now):
                continue
        missing.append({"store": other.store, "offer_key": other.key, "seller_id": other.seller_id,
                        "url": other.url, "last_verified_at": other.observed_at,
                        "current_issues": (observed.errors(now) + (["points_unknown"] if points and amount(observed, True) is None else []))
                            if observed else ["not_observed_this_run"],
                        "basis": "points" if points else "payment"})
    return missing
