"""Read the public search response used by Koubou's own search page.

Search rows discover URLs only. Prices and availability still come from a fresh
product-page verification; an empty HTML/JS shell is never evidence of no hits.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .http import FetchError, Page
from .models import digest


ORIGIN = "https://www.pc-koubou.jp"
ENDPOINT = ORIGIN + "/search/npsearch.php"
PAGE_SIZE = 20


def search_url(query: str, offset: int = 0) -> str:
    if not isinstance(query, str) or not query.strip() or type(offset) is not int or offset < 0 or offset % PAGE_SIZE:
        raise FetchError("invalid_comparison_search_request")
    return ENDPOINT + "?" + urlencode({"searchbox": 1, "q": query, "limit": PAGE_SIZE,
        "o": offset, "sort": "Score", "s5[]": "", "cache_t": 1, "fmt": "json", "s2b": "通常"})


def legacy_query(url: str) -> str | None:
    parts = urlsplit(url)
    params = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme != "https" or parts.netloc != "www.pc-koubou.jp"
            or parts.path != "/user_data/search.php" or parts.fragment
            or set(params) - {"q", "cache_t"} or len(params.get("q", [])) != 1):
        return None
    return params["q"][0] or None


def parse_search(page: Page, query: str, offset: int = 0):
    """Fail closed on an incomplete, mismatched or changed provider response."""
    try:
        parts = urlsplit(page.url)
        if page.status != 200 or parts.scheme != "https" or parts.netloc != "www.pc-koubou.jp" or parts.path != "/search/npsearch.php":
            raise ValueError()
        root = json.loads(page.text)["kotohaco"]
        request, result = root["request"], root["result"]
        params, info, items = request["param"], result["info"], result["items"]
        if (request["accountid"] != "pckoubou" or params["q"] != query
                or params["o"] != offset or params["limit"] != PAGE_SIZE
                or params["s2b"] != "通常" or params["s5"] != [""]
                or params["fmt"] != "json" or params["sort"] != "Score"):
            raise ValueError()
        hits = info["hitnum"]
        if (type(hits) is not int or hits < 0 or type(info["status"]) is not int or info["status"] != 0
                or any(type(info[k]) is not int for k in ("offset", "current_page", "last_page"))
                or info["offset"] != offset or info["current_page"] != offset // PAGE_SIZE + 1
                or info["last_page"] != max(1, (hits + PAGE_SIZE - 1) // PAGE_SIZE)
                or not isinstance(items, list) or (offset and offset >= hits)
                or len(items) != min(PAGE_SIZE, hits - offset)):
            raise ValueError()
        products, seen = [], set()
        for item in items:
            product_id = str(item["itemid"])
            url = urljoin(ORIGIN, item["url"])
            target = urlsplit(url)
            if (not re.fullmatch(r"\d+", product_id) or product_id in seen
                    or target.scheme != "https" or target.netloc != "www.pc-koubou.jp"
                    or target.path != "/products/detail.php" or target.fragment
                    or parse_qs(target.query).get("product_id") != [product_id]
                    or not isinstance(item.get("title", ""), str)):
                raise ValueError()
            seen.add(product_id)
            products.append({"url": url, "title": item.get("title", ""), "source": page.url,
                             "kind": "comparison"})
        next_offset = offset + PAGE_SIZE if offset + len(items) < hits else None
        evidence = {"query": query, "offset": offset, "total_hits": hits, "returned_count": len(items),
                    "next_offset": next_offset, "result": "no_results" if hits == 0 else "results",
                    "observed_at": page.observed_at, "url": page.url, "http_status": page.status,
                    "content_hash": digest(page.text), "method": "koubou_public_search_json"}
        return products, next_offset, evidence
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FetchError("comparison_search_response_unverified") from exc
