from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from statistics import median

from .models import JST, Offer, allowed_url, digest, fresh, iso, same_product, timestamp


def amount(offer: Offer, points: bool) -> int | None:
    if offer.payment is None or points and (type(offer.points_yen) is not int or offer.points_yen < 0):
        return None
    return offer.payment - (offer.points_yen if points else 0)


def evaluate(offer: Offer, others: list[Offer], history: list[dict], now: datetime, points: bool = False) -> dict:
    decision = {"offer_key": offer.key, "status": "insufficient", "basis": "points" if points else "payment", "rule": None, "reasons": [], "comparisons": [], "history": {}}
    errors = offer.errors(now)
    if errors:
        decision["reasons"] = errors
        return decision
    price = amount(offer, points)
    if price is None:
        decision["reasons"] = ["points_unknown"]
        return decision
    # Every verified independent offer is considered, not two expensive examples.
    sellers = {}
    for other in others:
        if other.seller_id == offer.seller_id or not same_product(offer, other) or other.errors(now):
            continue
        payment = amount(other, points)
        if payment is None:
            continue
        if other.seller_id not in sellers or payment < sellers[other.seller_id][0]:
            sellers[other.seller_id] = (payment, other)
    compared = sorted(sellers.values(), key=lambda pair: pair[0])
    decision["comparisons"] = [{"seller_id": o.seller_id, "price_yen": p, "url": o.url, "observed_at": o.observed_at, "evidence": o.evidence} for p, o in compared]
    if compared and compared[0][0] < price:
        decision.update(status="rejected", reasons=["cheaper_current_offer"])
        return decision
    if len(compared) >= 2:
        reference = compared[0][0]
        difference = reference - price
        decision.update(reference_yen=reference, difference_yen=difference, difference_percent=round(100 * difference / reference, 4))
        if difference >= 500 and difference * 10 >= reference:
            decision.update(status="accepted", rule="A", reasons=[])
            return decision
        decision["reasons"].append("A_price_gap_below_threshold")
    else:
        decision["reasons"].append("A_independent_sellers_insufficient")

    # Current comparison must have actually happened, even for history-based B.
    if not compared:
        decision["reasons"].append("B_current_comparison_missing")
        return decision
    records = []
    for row in history:
        try:
            past = Offer.from_dict(row["offer"])
            checked = timestamp(past.observed_at)
            # Historical validity is tested at observation time; do not require old prices to be fresh now.
            if checked is None or checked >= now or not same_product(offer, past) or past.errors(checked):
                continue
            value = amount(past, points)
            if value is not None and allowed_url(past.url):
                records.append((checked, value, past.url))
        except (KeyError, TypeError, ValueError):
            continue
    year = [r for r in records if r[0].astimezone(JST).year == now.astimezone(JST).year]
    if year:
        minimum = min(r[1] for r in year)
        if minimum - price >= 500:
            decision.update(status="accepted", rule="B_observed_year_low", reasons=[], history={"minimum_yen": minimum, "from": iso(min(r[0] for r in year)), "to": iso(max(r[0] for r in year)), "samples": len(year), "scope": "observed_period_only", "sources": sorted(set(r[2] for r in year))})
            return decision
    # Use one market minimum per day, never count repeat observations as distinct days.
    recent = [r for r in records if now - timedelta(days=90) <= r[0] < now]
    daily = {}
    for date, value, url in recent:
        key = date.astimezone(JST).date().isoformat()
        daily[key] = min(daily.get(key, value), value)
    if len(daily) >= 3 and (now - min(r[0] for r in recent)).days >= 30:
        baseline = Fraction(median([Fraction(v) for v in daily.values()]))
        delta = baseline - price
        decision["history"] = {"median_yen": float(baseline), "minimum_yen": min(daily.values()), "from": min(daily), "to": max(daily), "days": len(daily), "samples": len(recent), "sources": sorted(set(r[2] for r in recent)), "scope": "observed_days_only"}
        if delta >= 500 and delta * 10 >= baseline and price <= min(daily.values()):
            decision.update(status="accepted", rule="B_recent_median", reasons=[])
            return decision
        decision["reasons"].append("B_history_gap_below_threshold")
    else:
        decision["reasons"].append("B_history_insufficient")
    if len(compared) >= 2 and "B_history_gap_below_threshold" in decision["reasons"]:
        decision["status"] = "rejected"
    return decision


def update_events(offer: Offer, decision: dict, prior: dict | None, now: datetime) -> tuple[list[dict], dict]:
    prior = prior or {}
    errors = set(offer.errors(now))
    # Missing comparisons are not a product change; retrieval failure must not
    # reset accepted state and trigger a duplicate alert on recovery.
    terminal_errors = {"stock_out_of_stock", "coupon_unavailable", "expired"}
    if errors - terminal_errors or (decision["status"] == "insufficient" and not errors):
        return [], prior
    accepted = decision["status"] == "accepted"
    current = {"payment": offer.payment, "points": offer.points_yen, "stock": offer.stock, "coupon_remaining": offer.coupon.get("remaining"), "coupon_verified": offer.coupon.get("verified"), "expires_at": offer.expires_at, "discount": offer.discount_yen, "accepted": accepted, "basis": decision["basis"]}
    previous = prior.get("facts", {})
    generation = prior.get("generation", 0) + (current != previous)
    ever_accepted = prior.get("ever_accepted", False) or accepted
    events = []
    types = []
    if accepted and not previous.get("accepted"):
        types.append("restocked" if prior.get("ever_accepted") and previous.get("stock") == "out_of_stock" else "qualified")
    if accepted and previous.get("accepted") and current != previous:
        if current["payment"] < previous["payment"]:
            types.append("price_down")
        if current["points"] is not None and previous.get("points") is not None and current["points"] > previous["points"]:
            types.append("points_improved")
        remaining, before = current["coupon_remaining"], previous.get("coupon_remaining")
        if type(remaining) is int and type(before) is int and any(remaining <= threshold < before for threshold in (5, 1)):
            types.append("remaining_threshold")
    # Failed/stale fetches never prove a sale ended.
    directly_observed = offer.verified and fresh(offer.observed_at, now) and not offer.issues
    explicit_end = offer.stock == "out_of_stock" or offer.coupon.get("remaining") == 0 or (offer.expires_at and timestamp(offer.expires_at) and timestamp(offer.expires_at) <= now)
    if ever_accepted and directly_observed and explicit_end and not prior.get("ended"):
        types.append("ended")
    expiry_buckets = prior.get("expiry_buckets", {})
    expires = timestamp(offer.expires_at)
    if accepted and expires:
        hours = (expires - now).total_seconds() / 3600
        for threshold in (24, 4):
            marker = f"{offer.expires_at}:{threshold}"
            if 0 < hours <= threshold and marker not in expiry_buckets:
                types.append("expires_" + str(threshold) + "h")
                expiry_buckets[marker] = iso(now)
    for kind in types:
        event_id = digest([offer.key, kind, generation, offer.expires_at])[:32]
        events.append({"event_id": event_id, "kind": kind, "occurred_at": iso(now), "offer_key": offer.key, "offer": offer.to_dict(), "decision": decision, "previous": previous, "published": True, "delivery_status": "unconfirmed"})
    state = {"facts": current, "generation": generation, "ever_accepted": ever_accepted, "ended": bool(explicit_end and directly_observed) if directly_observed else prior.get("ended", False), "expiry_buckets": expiry_buckets}
    return events, state
