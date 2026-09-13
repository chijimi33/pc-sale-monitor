from __future__ import annotations

from datetime import datetime
from dataclasses import asdict
import os
import re
from urllib.parse import urlencode

from .http import Client, FetchError
from .models import Offer, UTC, allowed_url, iso, timestamp, valid_jan
from .parsing import PC, SALE, document, field, fields, first, integer, parse_product, stock_status


def dospara_list(client: Client, url: str) -> tuple[list[dict], list[str]]:
    from .vendor import dospara_coupon_tool as vendor
    page = client.get(url)
    items, _ = vendor.parse_coupon_items_from_pages({url: page.text})
    links = [c.url for c in vendor.extract_campaign_candidates(page.text, url) if vendor.is_dospara_page_url(c.url)]
    return [asdict(i) for i in items], list(dict.fromkeys(links))


def dospara_product(client: Client, item_data: dict) -> Offer | None:
    from .vendor import dospara_coupon_tool as vendor
    item = vendor.CouponItem(**item_data)
    pages = {}

    def fetch(url, **kwargs):
        page = client.get(url, data=kwargs.get("data"), method=kwargs.get("method", "GET"), headers=kwargs.get("headers"))
        pages[url] = page
        return page.text

    items = [item]
    source = "https://www.dospara.co.jp/campaign-list"
    original = vendor.fetch_text
    vendor.fetch_text = fetch
    try:
        info = vendor.fetch_product_info([item.product_id], page_url=source)
        vendor.enrich_items(items, info, page_url=source)
        now = datetime.now(UTC)
        verified = vendor.verify_product_page_coupons(items, fetcher=fetch, now=now)
        vendor.verify_coupon_remaining(verified, fetcher=fetch, now=now)
    finally:
        vendor.fetch_text = original
    offers = []
    for item in items:
        row = item.to_record()
        url = row.get("product_url")
        if not url or not allowed_url(url):
            continue
        page = pages.get(url)
        offer = parse_product("dospara", page, {"default_condition": "new"}) if page else Offer("dospara", row["product_id"], url, seller_id="dospara")
        offer.product_id = row["product_id"]
        offer.title = row.get("product_page_product_name") or row.get("product_name") or offer.title
        offer.price_yen = row.get("product_page_regular_price_yen")
        offer.discount_yen = row.get("coupon_discount_yen") or 0
        offer.stock = stock_status(row.get("product_page_stock") or row.get("stock"))
        offer.observed_at = row.get("product_page_verified_at")
        offer.expires_at = row.get("coupon_expires_at")
        offer.coupon = {"code": row.get("coupon_code"), "verified": row.get("coupon_verified") is True and row.get("coupon_remaining_verified") is True,
                        "remaining": row.get("coupon_remaining"), "limited": True, "checked_at": row.get("coupon_remaining_checked_at") or row.get("remaining_checked_at")}
        offer.verified = bool(page and row.get("product_page_product_id") == row["product_id"])
        offer.discovery_url = row.get("source_url") or source
        if row.get("coupon_verification_error"):
            offer.issues.append("coupon_verification_failed")
        offer.evidence.append({"url": url, "checked_at": offer.observed_at, "method": "dospara_verified_coupon", "fields": row})
        offers.append(offer)
    return offers[0] if offers else None


def yahoo_page(client: Client, query: str, start: int = 1, *, comparison: bool = False) -> tuple[list[Offer], int | None, dict]:
    appid = os.environ.get("YAHOO_CLIENT_ID")
    if not appid:
        raise FetchError("configuration_needed:YAHOO_CLIENT_ID")
    endpoint = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    params = {"appid": appid, "query": query, "results": min(100, 1000-start), "start": start, "sort": "+price", "delivery_area": "27"}
    if not comparison:
        params["is_discounted"] = "true"
    payload = client.json(endpoint + "?" + urlencode(params))
    now = iso()
    offers = []
    for hit in payload.get("hits", []):
        label = hit.get("priceLabel") or {}
        if not comparison and not (label.get("discountedPrice") or SALE.search(hit.get("headLine", "") + hit.get("name", ""))):
            continue
        url = hit.get("url", "")
        if not allowed_url(url):
            continue
        seller = (hit.get("seller") or {}).get("sellerId")
        points = (hit.get("point") or {}).get("lyLimitedBonusAmount")
        end = label.get("periodEnd")
        offer = Offer("yahoo", str(hit["code"]), url, title=hit.get("name", ""), jan=valid_jan(hit.get("janCode")),
                      brand=(hit.get("brand") or {}).get("name"), seller_id="yahoo:" + seller if seller else None,
                      condition=hit.get("condition"), price_yen=integer(hit.get("price")),
                      shipping_yen=0 if (hit.get("shipping") or {}).get("code") == 2 else None,
                      points_yen=integer(points), stock="in_stock" if hit.get("inStock") is True else "out_of_stock" if hit.get("inStock") is False else "unknown",
                      observed_at=now, verified=True, discovery_url=endpoint, discovery_kind="comparison" if comparison else "sale",
                      expires_at=iso(datetime.fromtimestamp(end, UTC)) if isinstance(end, int) and end > 0 else None)
        if label.get("taxable") is False:
            offer.issues.append("tax_exclusive_price")
        offer.conditional_points = [{"kind": "coupon_or_campaign", "amount_yen": None, "conditions": "一般利用者向けキャンペーンの追加条件は要確認。LYP固有分は取得・計上対象外。"}]
        # Do not publish appid or premium fields in evidence.
        offer.evidence = [{"url": endpoint, "checked_at": now, "method": "yahoo_official_api", "fields": {
            "code": hit.get("code"), "seller": hit.get("seller"), "price": hit.get("price"), "inStock": hit.get("inStock"),
            "shipping": hit.get("shipping"), "janCode": hit.get("janCode"), "base_points_yen": points, "delivery": hit.get("delivery")}}]
        offers.append(offer)
    total = payload.get("totalResultsAvailable", 0)
    count = payload.get("totalResultsReturned", len(payload.get("hits", [])))
    next_start = start + count if count and start + count <= total else None
    # Yahoo's documented start+results window is a provider restriction, not our Web budget.
    truncated = next_start is not None and next_start >= 1000
    return offers, None if truncated else next_start, {"total": total, "returned": count, "provider_window_truncated": truncated}


def amazon_candidates(client: Client, source_url: str) -> tuple[list[dict], dict]:
    data = client.json(source_url)
    metadata = {"source_url": source_url, "transcribed_at": data.get("fetched_at"), "source_updated_at": (data.get("site") or {}).get("updated_at"), "read_at": iso()}
    candidates = []
    for item in data.get("items", []):
        asin = item.get("asin", "")
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or item.get("category") not in ("PCパーツ", "モニター", "グラボ", "SSD", "ミニPC", "ガジェット"):
            continue
        candidates.append({"url": "https://www.amazon.co.jp/dp/" + asin, "source": source_url, "kind": "sale", "title": item.get("title", ""), "chimolog": {
            "price_yen": item.get("price_yen"), "item_updated_at": item.get("fetched_at"), **metadata}})
    return candidates, metadata


def amazon_product(client: Client, candidate: dict) -> Offer:
    page = client.get(candidate["url"])
    tree = document(page)
    offer = parse_product("amazon", page, {}, candidate)
    offer.product_id = candidate["url"].rsplit("/", 1)[-1]
    offer.title = first(tree, '//*[@id="productTitle"]') or candidate.get("title", "")
    price = first(tree, '//*[@id="corePrice_feature_div"]//*[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]/*[contains(@class,"a-offscreen")]') or first(tree, '//*[@id="corePriceDisplay_desktop_feature_div"]//*[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]/*[contains(@class,"a-offscreen")]')
    offer.price_yen = integer(price)
    offer.stock = stock_status(first(tree, '//*[@id="availability"]'))
    merchant = first(tree, '//*[@id="merchantInfoFeature_feature_div"]') or first(tree, '//*[@id="merchant-info"]')
    shipper = first(tree, '//*[@id="fulfillerInfoFeature_feature_div"]')
    seller_link = first(tree, '//*[@id="sellerProfileTriggerId"]/@href')
    offer.seller_id = None
    if merchant and re.search(r"販売元\s*Amazon\.co\.jp|Amazon\.co\.jpが販売", merchant):
        offer.seller_id = "amazon"
    elif seller_link:
        match = re.search(r"[?&]seller=([A-Z0-9]+)", seller_link)
        if match:
            offer.seller_id = "amazon:" + match.group(1)
    shipping = first(tree, '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]')
    if shipping and "無料配送" in shipping and not re.search(r"以上|プライム|Prime|会員", shipping):
        offer.shipping_yen = 0
    condition = first(tree, '//*[@id="newAccordionRow"]') or first(tree, '//*[@id="conditionInfoFeature_feature_div"]')
    if condition and "新品" in condition:
        offer.condition = "new"
    offer.verified = bool(offer.price_yen and first(tree, '//*[@id="ASIN"]/@value') == offer.product_id)
    offer.issues.append("amazon_shipper_unverified") if not shipper and not (merchant and "出荷" in merchant) else None
    offer.evidence.append({"url": page.url, "checked_at": page.observed_at, "method": "amazon_buybox", "fields": {"seller": merchant, "shipper": shipper, "shipping": shipping, "chimolog_discovery": candidate.get("chimolog")}})
    # Chimolog is discovery only; its price is never substituted for an unavailable buy box.
    return offer
