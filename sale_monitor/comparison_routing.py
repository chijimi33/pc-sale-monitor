"""Known product addresses are fetch targets, never replacement observations."""
from __future__ import annotations

from copy import deepcopy

from .comparison_integrity import member_price_pending
from .models import STORES, Offer, allowed_url, digest, same_product, timestamp
from .parsing import canonical


SCOPE_FIELDS = ("store", "product_id", "url", "channel", "seller_id", "jan",
                "brand", "model", "condition", "variant", "warranty")


def scope(offer: Offer) -> dict:
    """Identity/compatibility only; no old price or inferred availability."""
    return {key: deepcopy(getattr(offer, key)) for key in SCOPE_FIELDS}


def request_key(request: dict) -> str:
    return digest([request.get("identity"), request.get("query"), request.get("candidates")])[:24]


def offers_by_identity(offers: dict) -> dict:
    indexed = {}
    for key, row in offers.items():
        if not isinstance(row, dict):
            continue
        try:
            identity = Offer.from_dict(row).identity
        except (TypeError, ValueError):
            continue
        if identity:
            indexed.setdefault(identity, {})[key] = row
    return indexed


def known_targets(request: dict, offers: dict, store: str, now) -> tuple[list[Offer], str]:
    specs = request.get("candidates")
    if not isinstance(specs, list) or not specs or not isinstance(request.get("query"), str) or not request["query"].strip():
        return [], "candidate_scope_unverified"
    candidates = []
    for spec in specs:
        if not isinstance(spec, dict):
            return [], "candidate_scope_unverified"
        try:
            candidate = Offer.from_dict(spec)
            valid = (candidate.store in STORES and candidate.store != store and
                     candidate.channel == "online" and allowed_url(candidate.url) and
                     candidate.identity == request.get("identity") and candidate.identity and
                     candidate.condition is not None and candidate.seller_id)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return [], "candidate_scope_unverified"
        candidates.append(candidate)
    targets, covered = [], set()
    for row in offers.values():
        if not isinstance(row, dict):
            continue
        try:
            other = Offer.from_dict(row)
            checked = timestamp(other.observed_at)
            if (other.store != store or other.channel != "online" or checked is None or
                    checked > now or not other.product_id):
                continue
            # Later transport failures do not erase a previously identified URL.
            # Its next response must still pass all current verification checks.
            source = deepcopy(other)
            source.issues = [x for x in source.issues if x != "latest_fetch_failed"]
            if not member_price_pending(source) and source.errors(checked):
                continue
            matches = {i for i, candidate in enumerate(candidates)
                       if candidate.seller_id != other.seller_id and same_product(candidate, other)}
            if matches:
                covered.update(matches)
                targets.append(other)
        except (TypeError, ValueError, KeyError):
            continue
    if len(covered) != len(candidates):
        return [], "compatible_known_url_missing"
    return sorted(targets, key=lambda o: (canonical(o.url), o.key)), "known_urls_first"


def current_target(offer: Offer, expected: dict, run_id: str, now, request=None) -> bool:
    try:
        source = Offer.from_dict(expected)
        if request is not None:
            candidates = [Offer.from_dict(spec) for spec in request.get("candidates", [])]
            covered = [candidate for candidate in candidates
                       if source.seller_id != candidate.seller_id and same_product(source, candidate)]
            # Warranty compatibility with an unknown value is not transitive.
            # Recheck the original candidate scopes as well as the old source.
            if not covered or not all(offer.seller_id != candidate.seller_id and same_product(offer, candidate)
                                      for candidate in covered):
                return False
        return bool(offer.observed_run_id == run_id and not offer.errors(now) and
                    offer.store == source.store and offer.key == source.key and
                    canonical(offer.url) == canonical(source.url) and same_product(offer, source))
    except (TypeError, ValueError, KeyError):
        return False
