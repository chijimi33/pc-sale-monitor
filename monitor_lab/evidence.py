from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re

from sale_monitor.engine import evaluate
from sale_monitor.models import Offer, iso, same_product, timestamp
from sale_monitor.parsing import canonical, clean, document, fields, field, first, integer, json_products, parse_product, stock_status

FIELDS = ("jan", "model", "price_yen", "shipping_yen", "stock", "condition", "variant", "warranty", "expires_at")


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
                                   "body_sha256": receipt.get("body_sha256"), "parser_evidence": offer.evidence}]}
               for name in FIELDS}
    if offer.condition and not schema.get("itemCondition"):
        records["condition"]["status"] = "catalog_inference"
    conflicts, conditional = list(offer.issues), []
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
    return Observation(offer, records, conditional, sorted(set(conflicts)), receipt)


def decide(candidate, comparators, history, now, run_id):
    """Reuse unchanged A/B maths; enforce same-run and unresolved comparator holds."""
    if candidate.observed_run_id != run_id:
        return {"offer_key": candidate.key, "status": "insufficient", "basis": "payment", "rule": None,
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
            if errors and other.stock != "out_of_stock":
                holds.extend("comparator_" + error for error in errors)
            else:
                usable.append(other)
    decision = evaluate(candidate, usable, history, now)
    if holds:
        decision.update(status="insufficient", rule=None, reasons=sorted(set(decision["reasons"] + holds)))
    return decision


def compare_baseline(candidate, comparators, history, now, run_id):
    baseline = evaluate(candidate, [o for o in comparators if o.observed_run_id == run_id], history, now)
    proposed = decide(candidate, comparators, history, now, run_id)
    return {"baseline": baseline, "proposed": proposed,
            "changed": baseline != proposed,
            "explanation": proposed["reasons"] if baseline != proposed else []}
