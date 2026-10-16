from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
import re

from sale_monitor.engine import amount, evaluate
from sale_monitor.models import JST, Offer, iso, same_product, timestamp
from sale_monitor.parsing import canonical, clean, document, fields, field, first, integer, json_products, parse_product, stock_status

FIELDS = ("jan", "model", "price_yen", "shipping_yen", "discount_yen", "points_yen", "conditional_points",
          "stock", "condition", "variant", "warranty", "expires_at")


@dataclass
class Observation:
    offer: Offer
    fields: dict
    conditional_prices: list
    conflicts: list
    receipt: dict

    def to_dict(self):
        return {"offer": self.offer.to_dict(), "fields": self.fields,
                "conditional_prices": self.conditional_prices,
                "conflicts": self.conflicts, "receipt": self.receipt}


def normalize(store, page, cfg, run_id, receipt):
    from .capture import parser_representation
    representation = parser_representation(receipt)
    offer = parse_product(store, page, cfg)
    offer.observed_run_id = run_id
    tree = document(page)
    products = [p for p in json_products(tree) if canonical(p.get("url") or page.url) == canonical(page.url)]
    product = products[0] if len(products) == 1 else {}
    schema = product.get("offers", {})
    if isinstance(schema, list):
        schema = schema[0] if len(schema) == 1 else {}
    if not isinstance(schema, dict):
        schema = {}
    records = {name: {"value": getattr(offer, name), "status": "observed" if getattr(offer, name) is not None else "unknown",
                      "sources": [{"kind": "production_parser", "url": page.url, "observed_at": page.observed_at,
                                   "body_sha256": representation.get("body_sha256"),
                                   "body_kind": representation.get("body_kind", "http_response"),
                                   "representation_observed_at": representation.get("observed_at", page.observed_at),
                                   "parser_evidence": offer.evidence}]}
               for name in FIELDS}
    if offer.condition and not schema.get("itemCondition"):
        records["condition"]["status"] = "catalog_inference"
    conflicts, conditional = list(offer.issues), []
    from .identity import extract_primary_jan
    primary_jan = extract_primary_jan(store, page, cfg)
    if primary_jan is not None:
        # Preserve the baseline value while selecting only product-bound
        # evidence. An unknown/conflicting primary claim never falls back to
        # the original document-wide JAN/SKU lookup.
        records['jan']['status'] = primary_jan['status']
        records['jan']['sources'].extend(primary_jan['evidence'])
        records['jan']['primary_identity_reasons'] = primary_jan['reasons']
        offer.jan = primary_jan['selected_value'] if primary_jan['status'] == 'observed' else None
        conflicts.extend(primary_jan['conflicts'])
    point_conflicts = []
    if store == 'koubou':
        raw = re.search(r'eccube\.classCategories\s*=\s*(\{.*?\});', page.text, re.S)
        if raw:
            try:
                variants = [v for row in json.loads(raw.group(1)).values() for v in row.values()
                            if isinstance(v, dict) and 'stock_find' in v]
                matching = [v for v in variants if v.get('product_code') == offer.model
                            and integer(v.get('price02')) == offer.price_yen]
                if matching:
                    values = {integer(v.get('point')) for v in matching}
                    records['points_yen']['sources'].append({'kind': 'matching_product_variants',
                        'product_code': offer.model, 'price_yen': offer.price_yen,
                        'points': [v.get('point') for v in matching], 'url': page.url})
                    if len(values) != 1 or None in values:
                        point_conflicts.append('points_conflict_review_needed')
                        offer.points_yen = None
                        records['points_yen']['status'] = 'conflict'
            except (ValueError, AttributeError, TypeError):
                # The unchanged product parser already reports unreadable variant data.
                pass
    displayed_points = field(fields(tree), r'^ポイント$')
    if displayed_points:
        bare_points = bool(re.fullmatch(r'[\d,]+\s*ポイント', displayed_points))
        records['points_yen']['sources'].append({'kind': 'displayed_points', 'text': displayed_points,
                                               'url': page.url, 'observed_at': page.observed_at})
        if not bare_points:
            offer.conditional_points = deepcopy(offer.conditional_points) + [
                {'kind': 'displayed_points_conditions', 'text': displayed_points, 'status': 'unverified',
                 'url': page.url, 'observed_at': page.observed_at}]
            offer.points_yen = None
            records['points_yen']['status'] = 'conditions_unverified'
        elif offer.points_yen is not None and integer(displayed_points.replace('ポイント', '').strip()) != offer.points_yen:
            point_conflicts.append('points_conflict_review_needed')
            offer.points_yen = None
            records['points_yen']['status'] = 'conflict'
    records['conditional_points']['status'] = 'unverified' if offer.conditional_points else 'none_parsed'
    schema_stock = stock_status(schema.get("availability"))
    if schema_stock != "unknown" and offer.stock != "unknown" and schema_stock != offer.stock:
        conflicts.append("stock_conflict_review_needed")
        records["stock"]["sources"].append({"kind": "schema_and_product_display", "schema": schema.get("availability"), "display_selected": offer.stock})
    shipping = schema.get("shippingDetails", {})
    primary_free_shipping = False
    if store == "ark":
        boxes = tree.xpath('//*[@id=$id]/ancestor::div[contains(concat(" ",normalize-space(@class)," ")," item_detailbox ")][1]', id="item-" + offer.product_id)
        if len(boxes) == 1:
            marks = boxes[0].xpath('.//span[contains(concat(" ",normalize-space(@class)," ")," stat-panel-116 ")]')
            primary_free_shipping = any(clean(m) == "送料無料" for m in marks)
            if primary_free_shipping:
                records["shipping_yen"]["sources"].append({"kind": "primary_product_free_shipping", "text": "送料無料", "url": page.url})
                records["shipping_yen"]["status"] = "product_specific_display"
                offer.shipping_yen = 0
    if not primary_free_shipping and shipping and (isinstance(shipping, list) or isinstance(shipping, dict) and
                     (shipping.get("shippingDestination") or shipping.get("eligibleTransactionVolume"))):
        records["shipping_yen"]["status"] = "rule_scope_unverified"
        conflicts.append("shipping_destination_or_threshold_unverified")
        offer.shipping_yen = None
    displayed_shipping = field(fields(tree), r"^送料$|^配送料$")
    rate = shipping.get("shippingRate", {}) if isinstance(shipping, dict) else {}
    schema_shipping = integer(rate.get("value")) if rate.get("currency") == "JPY" else None
    visible_shipping = 0 if displayed_shipping in ("無料", "送料無料") else integer(displayed_shipping)
    if schema_shipping is not None and visible_shipping is not None and schema_shipping != visible_shipping:
        conflicts.append("shipping_conflict_review_needed")
        records["shipping_yen"]["sources"].append({"kind": "schema_and_display", "schema": schema_shipping, "display": displayed_shipping})
        offer.shipping_yen = None
    if store == "koubou":
        panels = tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," productDetail--main__right--price ")]')
        # Do not use sitewide login/footer/member registration text as price evidence.
        text = " ".join(clean(p) for p in panels)
        if re.search(r"(?:WEB\s*)?会員(?:限定|価格)|ログイン.{0,30}(?:価格|表示)|(?:価格|表示).{0,30}ログイン", text):
            conditional.append({"kind": "member", "price_yen": None, "status": "unverified",
                                "scope": "primary_product_price_panel", "text": text[:2000],
                                "url": page.url, "observed_at": page.observed_at})
            conflicts.append("member_price_not_verified")
        displayed_price = integer(first(tree, '//input[@id="priceIncTax"]/@value'))
        schema_price = integer(schema.get("price")) if schema.get("priceCurrency") == "JPY" else None
        if displayed_price is not None and schema_price is not None and displayed_price != schema_price:
            conflicts.append("price_conflict_review_needed")
            records["price_yen"]["sources"].append({"kind": "schema_and_display", "schema": schema_price, "display": displayed_price})
    for name in FIELDS:
        if any(reason.startswith(name.split("_")[0] + "_conflict") for reason in conflicts):
            records[name]["status"] = "conflict"
    for name in FIELDS:
        records[name]["selected_value"] = getattr(offer, name)
    offer.issues = sorted(set(offer.issues + conflicts))
    # Points ambiguity affects only the effective-price basis. Retain it in the
    # field evidence without invalidating an otherwise verified cash payment.
    return Observation(offer, records, conditional, sorted(set(conflicts + point_conflicts)), receipt)


def reconcile_discovery(observation, sources, role):
    """Reconcile every listing after collection, including late shared parents.

    The original product field value remains available separately from the
    selected value. No listing price can replace a product observation.
    """
    result = deepcopy(observation)
    offer = Offer.from_dict(result['offer'])
    field = result['fields']['expires_at']
    dates, invalid = set(), False
    if field['value']:
        dates.add(iso(timestamp(field['value'])))
    for source in sources:
        discovered = source['discovered']
        raw = discovered.get('expires_at')
        date = timestamp(raw)
        normalized = iso(date + timedelta(days=1) - timedelta(seconds=1)) if date and len(raw) == 10 else iso(date) if date else None
        if normalized:
            dates.add(normalized)
        elif raw:
            invalid = True
        evidence = {**deepcopy(source), 'kind': 'discovery_listing', 'value': raw, 'normalized_value': normalized}
        if evidence not in field['sources']:
            field['sources'].append(evidence)
    offer.discovery_kind = 'sale' if role == 'candidate' else 'comparison'
    preferred = [s for s in sources if s['discovered'].get('kind') == offer.discovery_kind] or sources
    offer.discovery_url = preferred[0]['url']
    conflicts = set(result['conflicts'])
    if len(dates) > 1:
        conflicts.add('expiry_conflict_review_needed')
    if invalid:
        conflicts.add('expiry_discovery_unverified')
    if len(dates) == 1:
        date = timestamp(next(iter(dates))).astimezone(JST)
        for evidence in offer.evidence:
            display = evidence.get('fields', {}).get('expiry', {}).get('product_display', '')
            match = re.fullmatch(r'開催期間:\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})まで', display)
            if match and tuple(map(int, match.groups())) != (date.month, date.day, date.hour, date.minute):
                conflicts.add('expiry_conflict_review_needed')
    if any(reason.startswith('expiry_') for reason in conflicts):
        offer.expires_at = None
        field['status'] = 'conflict' if 'expiry_conflict_review_needed' in conflicts else 'unverified'
    elif dates:
        offer.expires_at = next(iter(dates))
        field['status'] = 'observed' if field['value'] else 'discovery_listing_observed'
    field['selected_value'] = offer.expires_at
    offer.issues = sorted(set(offer.issues) | conflicts)
    result.update(offer=offer.to_dict(), conflicts=sorted(conflicts), discovery_sources=deepcopy(sources))
    return result


def decide(candidate, comparators, history, now, run_id, *, points=False):
    """Reuse unchanged A/B maths; enforce same-run and unresolved comparator holds."""
    if candidate.observed_run_id != run_id:
        return {"offer_key": candidate.key, "status": "insufficient", "basis": "points" if points else "payment", "rule": None,
                "reasons": ["candidate_not_observed_in_run"], "comparisons": []}
    holds = []
    usable = []
    for other in comparators:
        if other.key == candidate.key or other.seller_id == candidate.seller_id or other.identity != candidate.identity:
            continue
        if other.observed_run_id != run_id:
            holds.append("known_comparator_not_verified_this_run")
        elif "member_price_not_verified" in other.issues:
            holds.append("comparator_member_price_not_verified")
        elif same_product(candidate, other):
            errors = other.errors(now)
            if other.stock == 'out_of_stock':
                # Unavailability is an exclusion only when the rest of the
                # current product evidence is valid. It must not hide stale,
                # unverified or contradictory observations of a known seller.
                errors = [error for error in errors if error != 'stock_out_of_stock']
            if errors:
                holds.extend("comparator_" + error for error in errors)
            elif points and other.stock != 'out_of_stock' and amount(other, True) is None:
                holds.append('comparator_points_unknown')
            else:
                usable.append(other)
    decision = evaluate(candidate, usable, history, now, points=points)
    if holds:
        decision.update(status="insufficient", rule=None, reasons=sorted(set(decision["reasons"] + holds)))
    return decision


def compare_baseline(candidate, comparators, history, now, run_id):
    baseline = evaluate(candidate, [o for o in comparators if o.observed_run_id == run_id], history, now)
    proposed = decide(candidate, comparators, history, now, run_id)
    return {"baseline": baseline, "proposed": proposed,
            "changed": baseline != proposed,
            "explanation": proposed["reasons"] if baseline != proposed else []}
