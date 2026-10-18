"""Anonymous Yahoo public HTML discovery and product receipts.

The search index supplies URLs only. Each offer needs its own product response;
regional postage, member prices, options and campaign totals are not inferred.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from urllib.parse import quote_plus, unquote_plus, urlsplit

from .http import FetchError, Page
from .models import Offer, UTC, iso, timestamp, valid_jan
from .parsing import SALE, document


SEARCH = "https://shopping.yahoo.co.jp"
STORE = "https://store.shopping.yahoo.co.jp"


def search_url(query: str, page: int = 1) -> str:
    if not isinstance(query, str) or not query.strip() or type(page) is not int or page < 1:
        raise FetchError("yahoo_public_request_unverified")
    return SEARCH + "/search/" + quote_plus(query, safe="") + "/0/" + (f"{page}/" if page > 1 else "")


def search_scope(url, query, page):
    parts = urlsplit(url)
    path = parts.path.split("/")
    return (parts.scheme == "https" and parts.netloc == "shopping.yahoo.co.jp"
            and not parts.query and not parts.fragment and len(path) == (6 if page > 1 else 5)
            and path[:2] == ["", "search"] and unquote_plus(path[2]) == query
            and path[3:] == (["0", str(page), ""] if page > 1 else ["0", ""]))


def product_scope(url):
    parts = urlsplit(url)
    match = re.fullmatch(r"/([a-z0-9-]+)/([a-zA-Z0-9_.-]+)\.html", parts.path)
    if (parts.scheme != "https" or parts.netloc != "store.shopping.yahoo.co.jp"
            or parts.query or parts.fragment or not match or match[2] in ("info", "guide")):
        raise ValueError("product_scope")
    return match[1], match[2].lower()


def payload(page):
    if page.status != 200:
        raise ValueError("http_status")
    tree = document(page)
    scripts = tree.xpath('//script[@id="__NEXT_DATA__"]/text()')
    if len(scripts) != 1:
        raise ValueError("page_payload")
    return tree, json.loads(scripts[0])


def parse_search(page: Page, query: str, number: int = 1, *, comparison: bool = False):
    try:
        search_url(query, number)
        if not search_scope(page.url, query, number):
            raise ValueError("search_scope")
        _, data = payload(page)
        expected = [query, "0"] + ([str(number)] if number > 1 else [])
        if data["page"] != "/search/[[...params]]" or data["query"]["params"] != expected:
            raise ValueError("search_query")
        bff = data["props"]["initialState"]["bff"]
        header, alt = bff["searchResultHeader"], bff["alternativePagination"]
        terms = header["title"]["main"]["taxonomies"]
        if (len(terms) != 1 or len(terms[0]["items"]) != 1
                or terms[0]["items"][0]["text"] != query
                or not search_scope(terms[0]["items"][0]["url"], query, 1)):
            raise ValueError("search_header")
        if bff.get("searchError") is not None:
            error = bff["searchError"]
            # Observed on a real first-page JAN search. A missing result shell,
            # another error, suggestions or a contradictory count is not proof
            # that the requested product has no listings.
            if (number == 1 and error["errorCode"] == "zeroMatch" and error["queryMessage"] == query
                    and error["reSearchInfo"] is None and error["reSearchSuggest"] is None
                    and bff["searchResults"] is None and alt is None
                    and type(header["hitCount"]) is int and header["hitCount"] == 0):
                return [], None, {
                    "method": "yahoo_public_search_html", "url": page.url, "query": query, "page": number,
                    "result": "no_results", "total": 0, "start": 0, "end": 0, "returned": 0, "selected": 0,
                    "provider_window_truncated": False, "observed_at": page.observed_at,
                    "http_status": page.status, "body_sha256": hashlib.sha256(page.body).hexdigest()}
            raise ValueError("search_error")
        sections = bff["searchResults"]["items"][str(number)]
        pagers = [s["content"] for s in sections if s["type"] == "PAGER"]
        results = [s["content"]["items"] for s in sections if s["type"] == "RESULT"]
        if len(pagers) != 1 or len(results) != 1:
            raise ValueError("search_sections")
        pager, rows = pagers[0], results[0]
        for key in ("current", "start", "end", "total", "totalPage"):
            if type(pager[key]) is not int or type(alt[key]) is not int or pager[key] != alt[key]:
                raise ValueError("pagination_conflict")
        total, start, end, last = (pager[k] for k in ("total", "start", "end", "totalPage"))
        # Empty result arrays still require the explicit zeroMatch shape above.
        if (pager["current"] != number or not isinstance(rows, list) or not rows
                or type(header["hitCount"]) is not int or header["hitCount"] != total
                or not 1 <= start <= end <= total or len(rows) != end - start + 1
                or not 1 <= number <= last or (number == 1 and start != 1)
                or (number > 1 and start <= 1)):
            raise ValueError("search_count")
        for p in (pager, alt):
            if not search_scope(p["baseNormalizedUrl"]["url"], query, 1):
                raise ValueError("pagination_scope")
        next_url = None
        if number < last:
            next_url = pager["nextUrl"]["url"]
            if (not search_scope(next_url, query, number + 1) or alt["nextUrl"]["url"] != next_url
                    or pager["next"]["url"] != next_url or pager["next"]["pageNumber"] != number + 1
                    or alt["next"] != next_url or end >= total):
                raise ValueError("next_page")
        elif any(p.get(k) is not None for p in (pager, alt) for k in ("next", "nextUrl")):
            raise ValueError("terminal_page")
        products, seen = [], set()
        for row in rows:
            seller, code = product_scope(row["url"])
            item_id = seller + "_" + code
            if (row["itemId"].lower() != item_id or row["storeId"] != seller
                    or row["store"]["url"] != STORE + "/" + seller + "/"
                    or item_id in seen or not isinstance(row["name"], str) or not row["name"].strip()):
                raise ValueError("search_product")
            seen.add(item_id)
            discounted = (type(row.get("discountRate")) in (int, float) and row["discountRate"] > 0
                          and type(row.get("originalPrice")) is int and type(row.get("price")) is int
                          and row["originalPrice"] > row["price"] > 0)
            if comparison or SALE.search(row["name"]) or discounted:
                products.append({"url": row["url"], "title": row["name"], "source": page.url,
                                 "kind": "comparison" if comparison else "sale"})
        return products, next_url, {
            "method": "yahoo_public_search_html", "url": page.url, "query": query, "page": number,
            "total": total, "start": start, "end": end, "returned": len(rows), "selected": len(products),
            "provider_window_truncated": next_url is None and end < total,
            "observed_at": page.observed_at, "http_status": page.status,
            "body_sha256": hashlib.sha256(page.body).hexdigest()}
    except (KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
        raise FetchError("yahoo_public_search_unverified", page=page) from exc


def plain_options(item):
    if (type(item.get("axisNum")) is not int or item["axisNum"] != 0 or item.get("multipleVariations")
            or item.get("individualItemList") or item.get("individualItemOptionList")
            or item.get("inscriptionOptionList")):
        return False
    options = item.get("selectOptionList")
    if not isinstance(options, list):
        return False
    for option in options:
        choices = option.get("choiceList", [])
        if (len(choices) != 1 or choices[0].get("isSelectable") is not True
                or choices[0].get("charge") not in (None, 0)):
            return False
    return True


def parse_product(page: Page, task: dict) -> Offer:
    try:
        seller, code = product_scope(task["url"])
        if product_scope(page.url) != (seller, code):
            raise ValueError("redirect_scope")
        tree, root = payload(page)
        data = root["props"]["pageProps"]
        if "user" not in data or data["user"] is not None:
            raise ValueError("anonymous_scope")
        item = data["item"]
        schemas = tree.xpath('//script[@id="item-json-ld"]/text()')
        if len(schemas) != 1:
            raise ValueError("product_schema")
        schema = json.loads(schemas[0])
        offer_data = schema["offers"]
        if (schema["@type"] != "Product" or data["seller"]["id"] != seller
                or str(item["sellerManagedItemId"]).lower() != code
                or product_scope(item["url"]) != (seller, code)
                or product_scope(offer_data["url"]) != (seller, code)
                or not isinstance(item["name"], str) or not item["name"].strip()):
            raise ValueError("product_identity")
        canonicals = tree.xpath('//link[@rel="canonical"]/@href')
        if len(canonicals) != 1 or product_scope(canonicals[0]) != (seller, code):
            raise ValueError("canonical_scope")
        jan, schema_jan = valid_jan(item.get("janCode")), valid_jan(schema.get("gtin13"))
        if jan and schema_jan and jan.zfill(13) != schema_jan.zfill(13):
            raise ValueError("jan_conflict")
        offer = Offer("yahoo", seller + "_" + code, page.url, title=item["name"],
                      jan=schema_jan or jan, brand=item.get("brandName") if isinstance(item.get("brandName"), str) else None,
                      seller_id="yahoo:" + seller, observed_at=page.observed_at, verified=True,
                      discovery_kind=task.get("kind", "sale"), discovery_url=task.get("source"))
        condition = offer_data.get("itemCondition", "").rsplit("/", 1)[-1]
        if item.get("isUsed") is False and condition == "NewCondition":
            offer.condition = "new"
        elif item.get("isUsed") is True and condition == "UsedCondition":
            offer.condition = "used"
            offer.variant = item.get("usedConditionText") or None
            offer.issues.append("used_warranty_review_needed")
        table = item["priceTable"]
        active = [table[k] for k in ("price1", "price2") if isinstance(table.get(k), dict)
                  and table[k].get("isApplicablePrice") is True]
        selected = active[0] if len(active) == 1 else {}
        price = selected.get("price")
        general = (selected.get("isPremiumPrice") is False and selected.get("isYourTimeSalePrice") is False
                   and selected.get("isYouBadge") is False
                   and (selected.get("isRegularPrice") is True or selected.get("isBargainPrice") is True))
        if not general:
            offer.issues.append("member_price_not_verified")
        elif (type(price) is not int or price <= 0 or price != item.get("applicablePrice")
              or price != offer_data.get("price") or offer_data.get("priceCurrency") != "JPY"
              or item.get("isTaxable") is not True):
            offer.issues.append("yahoo_price_conflict")
        else:
            offer.price_yen = price
        if selected.get("isBargainPrice") is True:
            start, end = item.get("salePeriodStart"), item.get("salePeriodEnd")
            observed = timestamp(page.observed_at)
            if (type(start) is not int or type(end) is not int or not observed
                    or not start <= observed.timestamp() < end
                    or item.get("isBeforeSalePeriod") is not False or item.get("isAfterSalePeriod") is not False):
                offer.price_yen = None
                offer.issues.append("yahoo_sale_period_unverified")
            else:
                offer.expires_at = iso(datetime.fromtimestamp(end, UTC))
        simple = plain_options(item)
        if not simple:
            offer.price_yen = None
            offer.issues.append("yahoo_options_unverified")
        stock, delivery = item["stock"], item["delivery"]
        minimum, maximum = stock.get("minPurchaseQuantity"), stock.get("maxPurchaseQuantity")
        if minimum is not None and (type(minimum) is not int or minimum != 1):
            offer.price_yen = None
            offer.issues.append("yahoo_minimum_quantity_unverified")
        if type(maximum) is int and maximum > 0:
            offer.purchase_limit = f"1注文につき{maximum}点まで"
        availability = offer_data.get("availability", "").rsplit("/", 1)[-1]
        if (simple and stock.get("isStockVaried") is False and stock.get("isReserveOverdraft") is False
                and item.get("isNormalPurchaseDisable") is False and item.get("isOnSale") is True):
            if (stock.get("isAvailable") is True and availability == "InStock"
                    and delivery.get("shippingType") == "InStock" and delivery.get("isBackOrder") is False
                    and stock.get("quantity") != 0):
                offer.stock = "in_stock"
            elif stock.get("isAvailable") is False and availability in ("OutOfStock", "SoldOut"):
                offer.stock = "out_of_stock"
        quantity = stock.get("quantity")
        offer.listed_quantity = quantity if type(quantity) is int and quantity >= 0 else None
        # A product's apparent nationwide 0 can coexist with seller guide
        # surcharges and paid delivery methods. Retain that display as evidence
        # only until destination and method have a verified tariff.
        postage = data.get("postage") or {}
        offer.issues.append("yahoo_shipping_policy_unverified")
        parts = [p for group in data.get("point", {}).get("currentCampaignList", [])
                 for p in group.get("partsCampaignList", []) if p.get("title") == "ストアポイント"]
        if len(parts) == 1:
            point = parts[0]
            if (type(point.get("point")) is int and point["point"] >= 0 and point.get("entryText") is None
                    and point.get("isReachedLimit") is False and point.get("immediatePoint") == 0
                    and point.get("grantTiming") == "AfterPointOnly" and point.get("bonusType") == "LyLimited"):
                offer.points_yen = point["point"]
        offer.conditional_points = [{"kind": "coupon_or_campaign", "amount_yen": None,
            "conditions": "一般利用者向けの追加条件は要確認。LYP固有分は取得・計上対象外。"}]
        offer.evidence = [{"url": page.url, "checked_at": page.observed_at, "method": "yahoo_public_product_html",
            "body_sha256": hashlib.sha256(page.body).hexdigest(), "http_status": page.status,
            "fields": {"product_id": offer.product_id, "jan": offer.jan, "seller": seller,
                "general_price_yen": offer.price_yen, "condition": offer.condition, "stock": offer.stock,
                "base_points_yen": offer.points_yen, "displayed_postage": {k: postage.get(k)
                    for k in ("fee", "prefecture", "conditionFreeText", "notice", "isPostageFree")},
                "shipping_verified": False, "simple_options": simple}}]
        return offer
    except (KeyError, ValueError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        raise FetchError("yahoo_public_product_unverified", page=page) from exc
