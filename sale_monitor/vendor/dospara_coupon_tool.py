#!/usr/bin/env python3
"""Fetch Dospara coupon products and return regular/coupon prices.

The target page stores coupon amounts in HTML classes such as
``coupon-wrapper coupon-5000`` and fills product prices through
``/s/dospara/api/getProducts``. This module joins those two sources.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


AUTO_PAGE_URL = "auto"
DEFAULT_PAGE_URL = "https://www.dospara.co.jp/event/diy_parts_accessory.html"
DEFAULT_DISCOVERY_START_URLS = [
    "https://www.dospara.co.jp/campaign-list",
]
DEFAULT_TIMEOUT = 20.0
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_DISCOVERY_CANDIDATES = 24
COUPON_REMAINS_URL = "https://www.dospara.co.jp/api/getCouponRemains"
DISCOVERY_MIN_SCORE = 35
COMMON_CA_BUNDLES = [
    "/etc/ssl/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/ca-certificates/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl/cert.pem",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)

CSV_FIELDS = [
    "campaign_source_url",
    "campaign_title",
    "campaign_end_text",
    "section",
    "product_id",
    "product_name",
    "api_regular_price_yen",
    "regular_price_yen",
    "coupon_discount_yen",
    "coupon_price_yen",
    "coupon_id",
    "coupon_code",
    "coupon_remaining",
    "coupon_remaining_verified",
    "coupon_remaining_checked_at",
    "coupon_verified",
    "coupon_verification_error",
    "product_page_regular_price_yen",
    "product_page_stock",
    "product_page_verified_at",
    "stock",
    "product_url",
]

DISCOVERY_KEYWORD_SCORES = [
    ("diy_parts_accessory", 80),
    ("PCパーツ", 45),
    ("周辺機器", 45),
    ("自作PC", 35),
    ("パーツ", 28),
    ("週替わり", 28),
    ("クーポン", 28),
    ("大決算", 24),
    ("強化祭り", 24),
    ("自作", 24),
    ("数量限定", 22),
    ("値引き", 18),
    ("セール", 18),
    ("SALE", 18),
    ("キャンペーン", 16),
    ("おすすめ", 10),
]

DISCOVERY_NEGATIVE_KEYWORD_SCORES = [
    ("中古買取", 80),
    ("買取", 70),
    ("サポート", 50),
    ("修理", 50),
    ("法人", 40),
]


@dataclass
class CouponItem:
    section: str | None
    product_id: str
    coupon_code: str | None
    coupon_discount_yen: int
    coupon_id: str | None = None
    coupon_remaining: int | None = None
    coupon_remaining_verified: bool | None = None
    coupon_remaining_checked_at: str | None = None
    api_regular_price_yen: int | None = None
    api_stock: str | None = None
    regular_price_yen: int | None = None
    coupon_price_yen: int | None = None
    product_name: str | None = None
    stock: str | None = None
    product_url: str | None = None
    image_url: str | None = None
    simple_spec: str | None = None
    campaign_source_url: str | None = None
    campaign_title: str | None = None
    campaign_updated_text: str | None = None
    campaign_end_text: str | None = None
    coupon_verified: bool | None = None
    coupon_verification_error: str | None = None
    coupon_verification_source_url: str | None = None
    product_page_coupon_expire_text: str | None = None
    coupon_expires_at: str | None = None
    product_page_product_id: str | None = None
    product_page_product_name: str | None = None
    product_page_regular_price_yen: int | None = None
    product_page_stock: str | None = None
    product_page_verified_at: str | None = None
    product_page_price_matches_api: bool | None = None
    product_page_stock_matches_api: bool | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "campaign_source_url": self.campaign_source_url,
            "campaign_title": self.campaign_title,
            "campaign_updated_text": self.campaign_updated_text,
            "campaign_end_text": self.campaign_end_text,
            "section": self.section,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "api_regular_price_yen": self.api_regular_price_yen,
            "api_stock": self.api_stock,
            "regular_price_yen": self.regular_price_yen,
            "coupon_discount_yen": self.coupon_discount_yen,
            "coupon_price_yen": self.coupon_price_yen,
            "coupon_id": self.coupon_id,
            "coupon_code": self.coupon_code,
            "coupon_remaining": self.coupon_remaining,
            "coupon_remaining_verified": self.coupon_remaining_verified,
            "coupon_remaining_checked_at": self.coupon_remaining_checked_at,
            "coupon_verified": self.coupon_verified,
            "coupon_verification_error": self.coupon_verification_error,
            "coupon_verification_source_url": self.coupon_verification_source_url,
            "product_page_coupon_expire_text": self.product_page_coupon_expire_text,
            "coupon_expires_at": self.coupon_expires_at,
            "product_page_product_id": self.product_page_product_id,
            "product_page_product_name": self.product_page_product_name,
            "product_page_regular_price_yen": self.product_page_regular_price_yen,
            "product_page_stock": self.product_page_stock,
            "product_page_verified_at": self.product_page_verified_at,
            "product_page_price_matches_api": self.product_page_price_matches_api,
            "product_page_stock_matches_api": self.product_page_stock_matches_api,
            "stock": self.stock,
            "product_url": self.product_url,
            "image_url": self.image_url,
            "simple_spec": self.simple_spec,
        }


@dataclass
class CampaignCandidate:
    url: str
    text: str | None
    score: int
    source_url: str
    item_count: int | None = None
    error: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "text": self.text,
            "score": self.score,
            "source_url": self.source_url,
            "item_count": self.item_count,
            "error": self.error,
        }


@dataclass
class ProductPageCoupon:
    product_id: str
    amount_yen: int
    coupon_code: str
    coupon_id: str | None = None
    campaign_key: str | None = None
    expire_text: str | None = None


@dataclass
class ProductPageSnapshot:
    product_id: str | None
    product_name: str | None
    regular_price_yen: int | None
    stock: str | None


@dataclass
class PageDiscovery:
    selected_url: str
    html_text: str
    start_urls: list[str]
    selected_candidate: CampaignCandidate | None
    selected_urls: list[str]
    selected_candidates: list[CampaignCandidate]
    inspected_candidates: list[CampaignCandidate]
    candidate_count: int
    page_html_by_url: dict[str, str]
    fallback_used: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "start_urls": self.start_urls,
            "selected_url": self.selected_url,
            "selected_urls": self.selected_urls,
            "selected_text": self.selected_candidate.text if self.selected_candidate else None,
            "selected_score": self.selected_candidate.score if self.selected_candidate else None,
            "selected_item_count": self.selected_candidate.item_count if self.selected_candidate else None,
            "coupon_page_count": len(self.selected_candidates),
            "selected_pages": [candidate.to_record() for candidate in self.selected_candidates],
            "candidate_count": self.candidate_count,
            "fallback_used": self.fallback_used,
            "inspected_candidates": [candidate.to_record() for candidate in self.inspected_candidates],
        }


def fetch_text(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    cafile: str | None = None,
    insecure_tls: bool = False,
) -> str:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    urlopen_kwargs: dict[str, Any] = {"timeout": timeout}
    if urllib.parse.urlparse(url).scheme == "https":
        urlopen_kwargs["context"] = make_ssl_context(cafile=cafile, insecure_tls=insecure_tls)

    with urllib.request.urlopen(request, **urlopen_kwargs) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def make_ssl_context(*, cafile: str | None = None, insecure_tls: bool = False) -> ssl.SSLContext:
    if insecure_tls:
        return ssl._create_unverified_context()

    ca_bundle = cafile or find_ca_bundle()
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


def find_ca_bundle() -> str | None:
    candidates = [
        os.environ.get("SSL_CERT_FILE"),
        ssl.get_default_verify_paths().cafile,
        *COMMON_CA_BUNDLES,
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def strip_html_comments(markup: str) -> str:
    return re.sub(r"<!--.*?-->", "", markup, flags=re.DOTALL)


def clean_text(markup: str) -> str:
    markup = re.sub(r"<br\b[^>]*>", "\n", markup, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_attrs(start_tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    attr_re = re.compile(
        r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
        flags=re.DOTALL,
    )
    for match in attr_re.finditer(start_tag):
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


def extract_section_title(markup_without_comments: str, position: int) -> str | None:
    window = markup_without_comments[max(0, position - 30_000) : position]
    patterns = [
        r'<h2\b[^>]*class=["\'][^"\']*section-title[^"\']*["\'][^>]*>(.*?)</h2>',
        r'<h3\b[^>]*class=["\'][^"\']*section-subtitle[^"\']*["\'][^>]*>(.*?)</h3>',
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, window, flags=re.IGNORECASE | re.DOTALL))
        if matches:
            title = clean_text(matches[-1].group(1))
            if title:
                return title
    return None


def extract_coupon_discount(card_html: str) -> int | None:
    wrapper_match = re.search(
        r'<div\b[^>]*class=["\'][^"\']*coupon-wrapper[^"\']*["\'][^>]*>',
        card_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if wrapper_match:
        class_match = re.search(r"\bcoupon-(\d+)\b", wrapper_match.group(0))
        if class_match:
            return int(class_match.group(1))

    text_match = re.search(r"(\d[\d,]*)\s*円\s*OFF", clean_text(card_html), flags=re.IGNORECASE)
    if text_match:
        return int(text_match.group(1).replace(",", ""))
    return None


def extract_coupon_code(card_html: str) -> str | None:
    code_match = re.search(
        r'<[^>]*class=["\'][^"\']*coupon-code__text[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        card_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not code_match:
        return None

    code = clean_text(code_match.group(1))
    return code or None


def extract_coupon_id(card_html: str) -> str | None:
    wrapper_match = re.search(
        r'<div\b[^>]*class=["\'][^"\']*coupon-wrapper[^"\']*["\'][^>]*>',
        card_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not wrapper_match:
        return None

    return blank_to_none(parse_attrs(wrapper_match.group(0)).get("data-coupon-id"))


def parse_coupon_items(page_html: str) -> list[CouponItem]:
    markup = strip_html_comments(page_html)
    items: list[CouponItem] = []
    seen: set[tuple[str, str | None, int]] = set()

    li_re = re.compile(r"(<li\b[^>]*>)(.*?)</li>", flags=re.IGNORECASE | re.DOTALL)
    for match in li_re.finditer(markup):
        start_tag = match.group(1)
        card_html = match.group(2)
        attrs = parse_attrs(start_tag)
        class_names = attrs.get("class", "")

        if "model-card" not in class_names or "get_product_data" not in class_names:
            continue
        if "coupon-wrapper" not in card_html:
            continue

        product_id = attrs.get("data-code")
        if not product_id:
            continue

        discount = extract_coupon_discount(card_html)
        if discount is None:
            continue

        coupon_code = extract_coupon_code(card_html)
        dedupe_key = (product_id, coupon_code, discount)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        items.append(
            CouponItem(
                section=extract_section_title(markup, match.start()),
                product_id=product_id,
                coupon_id=extract_coupon_id(card_html),
                coupon_code=coupon_code,
                coupon_discount_yen=discount,
            )
        )

    return items


def is_auto_page_url(page_url: str | None) -> bool:
    return page_url is None or page_url.strip().lower() == AUTO_PAGE_URL or not page_url.strip()


def extract_anchor_text(inner_html: str) -> str | None:
    text_parts = [clean_text(inner_html)]
    for image_match in re.finditer(r"<img\b([^>]*)>", inner_html, flags=re.IGNORECASE | re.DOTALL):
        attrs = parse_attrs(image_match.group(1))
        text_parts.extend([attrs.get("alt", ""), attrs.get("title", "")])

    text = clean_text(" ".join(part for part in text_parts if part))
    return text or None


def is_dospara_page_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host and host != "www.dospara.co.jp":
        return False

    lower_path = parsed.path.lower()
    if lower_path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".css", ".js", ".pdf", ".zip")):
        return False
    if lower_path.startswith("/s/dospara/api/"):
        return False

    return True


def normalize_campaign_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


def score_campaign_link(text: str | None, url: str) -> int:
    if not is_dospara_page_url(url):
        return 0

    parsed = urllib.parse.urlparse(url)
    haystack = f"{text or ''} {parsed.path} {parsed.query}".lower()
    score = 0
    for keyword, points in DISCOVERY_KEYWORD_SCORES:
        if keyword.lower() in haystack:
            score += points
    for keyword, points in DISCOVERY_NEGATIVE_KEYWORD_SCORES:
        if keyword.lower() in haystack:
            score -= points

    if parsed.path.startswith("/event/"):
        score += 12
    elif parsed.path.startswith("/contents/"):
        score += 4

    return max(score, 0)


def extract_campaign_candidates(page_html: str, base_url: str) -> list[CampaignCandidate]:
    markup = strip_html_comments(page_html)
    candidates: list[CampaignCandidate] = []
    anchor_re = re.compile(r"<a\b([^>]*)>(.*?)</a>", flags=re.IGNORECASE | re.DOTALL)

    for match in anchor_re.finditer(markup):
        attrs = parse_attrs(match.group(1))
        href = attrs.get("href")
        if not href:
            continue

        href = href.strip()
        if href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue

        url = absolute_url(href, base_url)
        if not url:
            continue
        url = normalize_campaign_url(url)

        text = extract_anchor_text(match.group(2))
        score = score_campaign_link(text, url)
        if score < DISCOVERY_MIN_SCORE:
            continue

        candidates.append(CampaignCandidate(url=url, text=text, score=score, source_url=base_url))

    return candidates


def merge_campaign_candidate(
    candidates_by_url: dict[str, CampaignCandidate],
    candidate: CampaignCandidate,
) -> None:
    candidate.url = normalize_campaign_url(candidate.url)
    existing = candidates_by_url.get(candidate.url)
    if existing is None:
        candidates_by_url[candidate.url] = candidate
        return

    if candidate.score > existing.score:
        existing.score = candidate.score
        existing.text = candidate.text or existing.text
        existing.source_url = candidate.source_url

    if existing.item_count is None and candidate.item_count is not None:
        existing.item_count = candidate.item_count


def fetch_with_options(
    fetcher: Callable[..., str],
    url: str,
    *,
    timeout: float,
    cafile: str | None,
    insecure_tls: bool,
) -> str:
    return fetcher(url, timeout=timeout, cafile=cafile, insecure_tls=insecure_tls)


def discover_coupon_page(
    *,
    start_urls: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    cafile: str | None = None,
    insecure_tls: bool = False,
    max_candidates: int = DEFAULT_MAX_DISCOVERY_CANDIDATES,
    fetcher: Callable[..., str] = fetch_text,
) -> PageDiscovery:
    resolved_start_urls = list(dict.fromkeys(start_urls or DEFAULT_DISCOVERY_START_URLS))
    candidates_by_url: dict[str, CampaignCandidate] = {}
    page_cache: dict[str, str] = {}

    for start_url in resolved_start_urls:
        try:
            start_html = fetch_with_options(
                fetcher,
                start_url,
                timeout=timeout,
                cafile=cafile,
                insecure_tls=insecure_tls,
            )
        except (OSError, urllib.error.URLError):
            continue

        page_cache[start_url] = start_html
        start_items = parse_coupon_items(start_html)
        if start_items:
            title = extract_page_meta(start_html).get("title")
            merge_campaign_candidate(
                candidates_by_url,
                CampaignCandidate(
                    url=start_url,
                    text=title,
                    score=max(score_campaign_link(title, start_url), DISCOVERY_MIN_SCORE),
                    source_url=start_url,
                    item_count=len(start_items),
                ),
            )

        for candidate in extract_campaign_candidates(start_html, start_url):
            merge_campaign_candidate(candidates_by_url, candidate)

    campaign_list_sources = {
        url for url in resolved_start_urls if urllib.parse.urlparse(url).path.rstrip("/") == "/campaign-list"
    }
    candidates = sorted(
        candidates_by_url.values(),
        key=lambda candidate: (
            candidate.source_url in campaign_list_sources and candidate.source_url != candidate.url,
            candidate.score,
            candidate.item_count or 0,
            candidate.url,
        ),
        reverse=True,
    )

    inspected: list[CampaignCandidate] = []
    selected_candidates: list[CampaignCandidate] = []
    page_html_by_url: dict[str, str] = {}
    for candidate in candidates[: max(1, max_candidates)]:
        try:
            page_html = page_cache.get(candidate.url)
            if page_html is None:
                page_html = fetch_with_options(
                    fetcher,
                    candidate.url,
                    timeout=timeout,
                    cafile=cafile,
                    insecure_tls=insecure_tls,
                )
                page_cache[candidate.url] = page_html
            candidate.item_count = len(parse_coupon_items(page_html))
        except (OSError, urllib.error.URLError) as exc:
            candidate.error = str(exc)
            inspected.append(candidate)
            continue

        inspected.append(candidate)
        if candidate.item_count:
            selected_candidates.append(candidate)
            page_html_by_url[candidate.url] = page_html

    if selected_candidates:
        primary_candidate = selected_candidates[0]
        return PageDiscovery(
            selected_url=primary_candidate.url,
            html_text=page_html_by_url[primary_candidate.url],
            start_urls=resolved_start_urls,
            selected_candidate=primary_candidate,
            selected_urls=[candidate.url for candidate in selected_candidates],
            selected_candidates=selected_candidates,
            inspected_candidates=inspected,
            candidate_count=len(candidates),
            page_html_by_url=page_html_by_url,
        )

    fallback_url = DEFAULT_PAGE_URL
    fallback_html = page_cache.get(fallback_url)
    if fallback_html is None:
        fallback_html = fetch_with_options(
            fetcher,
            fallback_url,
            timeout=timeout,
            cafile=cafile,
            insecure_tls=insecure_tls,
        )

    fallback_candidate = candidates_by_url.get(fallback_url) or CampaignCandidate(
        url=fallback_url,
        text="Fallback fixed Dospara coupon page",
        score=score_campaign_link("PCパーツ 周辺機器 クーポン", fallback_url),
        source_url="fallback",
    )
    fallback_candidate.item_count = len(parse_coupon_items(fallback_html))
    if fallback_candidate not in inspected:
        inspected.append(fallback_candidate)

    return PageDiscovery(
        selected_url=fallback_url,
        html_text=fallback_html,
        start_urls=resolved_start_urls,
        selected_candidate=fallback_candidate,
        selected_urls=[fallback_url],
        selected_candidates=[fallback_candidate] if fallback_candidate.item_count else [],
        inspected_candidates=inspected,
        candidate_count=len(candidates),
        page_html_by_url={fallback_url: fallback_html},
        fallback_used=True,
    )


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_product_info(
    product_ids: list[str],
    *,
    page_url: str,
    timeout: float = DEFAULT_TIMEOUT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cafile: str | None = None,
    insecure_tls: bool = False,
) -> dict[str, dict[str, Any]]:
    unique_ids = list(dict.fromkeys(product_ids))
    api_url = urllib.parse.urljoin(page_url, "/s/dospara/api/getProducts")
    product_info: dict[str, dict[str, Any]] = {}

    for batch in chunked(unique_ids, max(1, batch_size)):
        payload = {"paramList": [{"pid": product_id, "q": "", "kflg": ""} for product_id in batch]}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw = fetch_text(
            api_url,
            timeout=timeout,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": urllib.parse.urljoin(page_url, "/").rstrip("/"),
                "Referer": page_url,
            },
            cafile=cafile,
            insecure_tls=insecure_tls,
        )
        data = json.loads(raw)

        if data.get("returnCode") != "000000":
            message = data.get("errorMsg") or "Dospara product API returned an error"
            raise RuntimeError(message)

        for info in data.get("productInfoList", {}).values():
            product_id = info.get("productID")
            if product_id:
                product_info[str(product_id)] = info

    return product_info


def enrich_items(
    items: list[CouponItem],
    product_info: dict[str, dict[str, Any]],
    *,
    page_url: str,
) -> list[CouponItem]:
    for item in items:
        info = product_info.get(item.product_id)
        if not info:
            continue

        regular_price = parse_int(info.get("amttax"))
        item.api_regular_price_yen = regular_price
        item.api_stock = blank_to_none(info.get("stkname"))
        item.regular_price_yen = regular_price
        item.coupon_price_yen = (
            max(regular_price - item.coupon_discount_yen, 0) if regular_price is not None else None
        )
        item.product_name = blank_to_none(info.get("pname"))
        item.stock = item.api_stock
        item.product_url = absolute_url(info.get("url"), page_url)
        item.image_url = absolute_url(info.get("imgurl"), page_url)
        item.simple_spec = blank_to_none(info.get("simplespec"))

    return items


def strip_javascript_comments(script_text: str) -> str:
    without_block_comments = re.sub(r"/\*.*?\*/", "", script_text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", without_block_comments)


def extract_product_page_coupon(product_html: str, product_id: str) -> ProductPageCoupon | None:
    script_text = strip_javascript_comments(product_html)
    case_re = re.compile(
        rf"case\s+['\"]{re.escape(product_id)}['\"]\s*:(?P<body>.*?)(?:break\s*;)",
        flags=re.DOTALL,
    )

    for match in case_re.finditer(script_text):
        body = match.group("body")
        amount_match = re.search(r"productMap\.amount\s*=\s*['\"](\d+)['\"]", body)
        code_match = re.search(r"productMap\.couponcode\s*=\s*['\"]([^'\"]+)['\"]", body)
        if not amount_match or not code_match:
            continue

        campaign_key = extract_product_map_string(body, "campaign")
        return ProductPageCoupon(
            product_id=product_id,
            amount_yen=int(amount_match.group(1)),
            coupon_code=code_match.group(1).strip(),
            coupon_id=extract_product_map_string(body, "id"),
            campaign_key=campaign_key,
            expire_text=extract_campaign_expire_text(script_text, campaign_key),
        )

    return None


def extract_product_page_snapshot(product_html: str) -> ProductPageSnapshot | None:
    match = re.search(
        r"\b(?:var|let|const)\s+productJson\s*=\s*(\{.*?\})\s*;?\s*</script>",
        product_html,
        flags=re.DOTALL,
    )
    if not match:
        return None

    try:
        product = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    return ProductPageSnapshot(
        product_id=blank_to_none(product.get("productID")),
        product_name=blank_to_none(product.get("pname")),
        regular_price_yen=parse_int(product.get("amttax")),
        stock=blank_to_none(product.get("stkname")),
    )


def parse_japanese_coupon_expiry(expire_text: str | None) -> dt.datetime | None:
    if not expire_text:
        return None

    match = re.search(
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})",
        expire_text,
    )
    if not match:
        return None

    year, month, day, hour, minute = (int(value) for value in match.groups())
    jst = dt.timezone(dt.timedelta(hours=9))
    try:
        return dt.datetime(year, month, day, hour, minute, tzinfo=jst)
    except ValueError:
        return None


def extract_product_map_string(script_body: str, field_name: str) -> str | None:
    match = re.search(rf"productMap\.{re.escape(field_name)}\s*=\s*['\"]([^'\"]+)['\"]", script_body)
    return match.group(1).strip() if match else None


def extract_campaign_expire_text(script_text: str, campaign_key: str | None) -> str | None:
    if not campaign_key:
        return None

    match = re.search(rf"['\"]{re.escape(campaign_key)}['\"]\s*:\s*['\"]([^'\"]+)['\"]", script_text)
    return match.group(1).strip() if match else None


def verify_product_page_coupons(
    items: list[CouponItem],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cafile: str | None = None,
    insecure_tls: bool = False,
    fetcher: Callable[..., str] = fetch_text,
    now: dt.datetime | None = None,
) -> list[CouponItem]:
    verified_items: list[CouponItem] = []
    verification_time = now or dt.datetime.now(dt.timezone.utc)
    if verification_time.tzinfo is None:
        verification_time = verification_time.replace(tzinfo=dt.timezone.utc)

    for item in items:
        item.coupon_verified = False
        item.coupon_verification_source_url = item.product_url

        if not item.product_url:
            item.coupon_verification_error = "missing product_url"
            continue
        if not item.coupon_code:
            item.coupon_verification_error = "missing coupon_code"
            continue

        try:
            product_html = fetch_with_options(
                fetcher,
                item.product_url,
                timeout=timeout,
                cafile=cafile,
                insecure_tls=insecure_tls,
            )
        except (OSError, urllib.error.URLError) as exc:
            item.coupon_verification_error = f"product page fetch failed: {exc}"
            continue

        item.product_page_verified_at = verification_time.astimezone(dt.timezone.utc).isoformat()
        snapshot = extract_product_page_snapshot(product_html)
        if snapshot is None:
            item.coupon_verification_error = "productJson not found on product page"
            continue

        item.product_page_product_id = snapshot.product_id
        item.product_page_product_name = snapshot.product_name
        item.product_page_regular_price_yen = snapshot.regular_price_yen
        item.product_page_stock = snapshot.stock
        item.product_page_price_matches_api = (
            snapshot.regular_price_yen == item.api_regular_price_yen
            if snapshot.regular_price_yen is not None and item.api_regular_price_yen is not None
            else None
        )
        item.product_page_stock_matches_api = (
            snapshot.stock == item.api_stock
            if snapshot.stock is not None and item.api_stock is not None
            else None
        )

        if snapshot.product_id != item.product_id:
            item.coupon_verification_error = (
                f"product ID mismatch: campaign={item.product_id}, product_page={snapshot.product_id}"
            )
            continue
        if snapshot.regular_price_yen is None:
            item.coupon_verification_error = "regular price missing from product page"
            continue
        if snapshot.regular_price_yen < item.coupon_discount_yen:
            item.coupon_verification_error = (
                f"coupon discount exceeds product page price: price={snapshot.regular_price_yen}, "
                f"discount={item.coupon_discount_yen}"
            )
            continue
        if snapshot.stock is None:
            item.coupon_verification_error = "stock missing from product page"
            continue
        if "在庫なし" in snapshot.stock or "販売終了" in snapshot.stock:
            item.coupon_verification_error = f"product unavailable on product page: {snapshot.stock}"
            continue

        item.regular_price_yen = snapshot.regular_price_yen
        item.coupon_price_yen = snapshot.regular_price_yen - item.coupon_discount_yen
        item.stock = snapshot.stock
        if snapshot.product_name:
            item.product_name = snapshot.product_name

        product_coupon = extract_product_page_coupon(product_html, item.product_id)
        if product_coupon is None:
            item.coupon_verification_error = "coupon not active on product page"
            continue

        if product_coupon.amount_yen != item.coupon_discount_yen:
            item.coupon_verification_error = (
                f"coupon amount mismatch: campaign={item.coupon_discount_yen}, "
                f"product_page={product_coupon.amount_yen}"
            )
            continue

        if product_coupon.coupon_code != item.coupon_code:
            item.coupon_verification_error = (
                f"coupon code mismatch: campaign={item.coupon_code}, product_page={product_coupon.coupon_code}"
            )
            continue

        if item.coupon_id and product_coupon.coupon_id and product_coupon.coupon_id != item.coupon_id:
            item.coupon_verification_error = (
                f"coupon ID mismatch: campaign={item.coupon_id}, product_page={product_coupon.coupon_id}"
            )
            continue
        if product_coupon.coupon_id:
            item.coupon_id = product_coupon.coupon_id

        item.product_page_coupon_expire_text = product_coupon.expire_text
        coupon_expires_at = parse_japanese_coupon_expiry(product_coupon.expire_text)
        if coupon_expires_at is None:
            item.coupon_verification_error = (
                f"coupon expiry missing or invalid on product page: {product_coupon.expire_text}"
            )
            continue
        item.coupon_expires_at = coupon_expires_at.isoformat()
        if coupon_expires_at <= verification_time.astimezone(coupon_expires_at.tzinfo):
            item.coupon_verification_error = (
                f"coupon expired on product page: {product_coupon.expire_text}"
            )
            continue

        item.coupon_verified = True
        item.coupon_verification_error = None
        verified_items.append(item)

    return verified_items


def verify_coupon_remaining(
    items: list[CouponItem],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cafile: str | None = None,
    insecure_tls: bool = False,
    fetcher: Callable[..., str] = fetch_text,
    now: dt.datetime | None = None,
) -> tuple[list[CouponItem], dict[str, Any]]:
    verification_time = now or dt.datetime.now(dt.timezone.utc)
    if verification_time.tzinfo is None:
        verification_time = verification_time.replace(tzinfo=dt.timezone.utc)
    checked_at = verification_time.astimezone(dt.timezone.utc).isoformat()

    candidates: list[CouponItem] = []
    missing_id_count = 0
    for item in items:
        item.coupon_remaining_checked_at = checked_at
        if not item.coupon_id:
            item.coupon_verified = False
            item.coupon_remaining_verified = False
            item.coupon_verification_error = "missing coupon_id for remaining check"
            missing_id_count += 1
            continue
        candidates.append(item)

    requested = list(
        {
            (item.coupon_id, item.coupon_code): {
                "id": item.coupon_id,
                "code": item.coupon_code,
            }
            for item in candidates
        }.values()
    )

    response_by_coupon: dict[tuple[str, str], int] = {}
    if requested:
        payload = json.dumps({"coupons": requested}, ensure_ascii=False).encode("utf-8")
        try:
            raw = fetcher(
                COUPON_REMAINS_URL,
                timeout=timeout,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://www.dospara.co.jp",
                    "Referer": items[0].campaign_source_url or DEFAULT_PAGE_URL,
                },
                cafile=cafile,
                insecure_tls=insecure_tls,
            )
            response = json.loads(raw)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Dospara coupon remaining API failed: {exc}") from exc

        if response.get("returnCode") != "000000":
            message = response.get("errorMsg") or "unknown error"
            raise RuntimeError(f"Dospara coupon remaining API returned an error: {message}")

        for line in response.get("couponRemains") or []:
            coupon_id = blank_to_none(line.get("id"))
            coupon_code = blank_to_none(line.get("code"))
            remaining = parse_remaining_count(line.get("remain"))
            if coupon_id and coupon_code and remaining is not None:
                response_by_coupon[(coupon_id, coupon_code)] = remaining

    verified_items: list[CouponItem] = []
    ended_count = 0
    missing_response_count = 0
    for item in candidates:
        key = (item.coupon_id or "", item.coupon_code or "")
        remaining = response_by_coupon.get(key)
        item.coupon_remaining = remaining

        if remaining is None:
            item.coupon_verified = False
            item.coupon_remaining_verified = False
            item.coupon_verification_error = "coupon missing from remaining API response"
            missing_response_count += 1
            continue
        if remaining == 0:
            item.coupon_verified = False
            item.coupon_remaining_verified = False
            item.coupon_verification_error = f"coupon distribution ended: remain={remaining}"
            ended_count += 1
            continue

        item.coupon_remaining_verified = True
        verified_items.append(item)

    return verified_items, {
        "enabled": True,
        "source_url": COUPON_REMAINS_URL,
        "checked_at": checked_at,
        "requested_count": len(requested),
        "available_count": len(verified_items),
        "ended_count": ended_count,
        "missing_id_count": missing_id_count,
        "missing_response_count": missing_response_count,
    }


def parse_remaining_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value

    text = str(value).strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def blank_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def absolute_url(value: Any, base_url: str) -> str | None:
    text = blank_to_none(value)
    if not text:
        return None
    return urllib.parse.urljoin(base_url, text)


def extract_page_meta(page_html: str) -> dict[str, str | None]:
    title_match = re.search(r"<title\b[^>]*>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
    return {
        "title": clean_text(title_match.group(1)) if title_match else None,
        "updated_text": extract_first_class_text(page_html, "hero-image__date-upper"),
        "campaign_end_text": extract_first_class_text(page_html, "hero-image__date-bottom"),
    }


def extract_first_class_text(page_html: str, class_name: str) -> str | None:
    match = re.search(
        rf'<[^>]*class=["\'][^"\']*{re.escape(class_name)}[^"\']*["\'][^>]*>(.*?)</div>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    text = clean_text(match.group(1))
    return text or None


def apply_campaign_context(items: list[CouponItem], *, page_url: str, page_meta: dict[str, str | None]) -> None:
    for item in items:
        item.campaign_source_url = page_url
        item.campaign_title = page_meta.get("title")
        item.campaign_updated_text = page_meta.get("updated_text")
        item.campaign_end_text = page_meta.get("campaign_end_text")


def parse_coupon_items_from_pages(page_html_by_url: dict[str, str]) -> tuple[list[CouponItem], list[dict[str, Any]]]:
    items: list[CouponItem] = []
    pages: list[dict[str, Any]] = []

    for page_url, page_html in page_html_by_url.items():
        page_meta = extract_page_meta(page_html)
        page_items = parse_coupon_items(page_html)
        apply_campaign_context(page_items, page_url=page_url, page_meta=page_meta)
        items.extend(page_items)
        pages.append({"source_url": page_url, **page_meta, "count": len(page_items)})

    return dedupe_coupon_items(items), pages


def dedupe_coupon_items(items: list[CouponItem]) -> list[CouponItem]:
    deduped: list[CouponItem] = []
    seen: set[tuple[str, str | None, int]] = set()

    for item in items:
        dedupe_key = (item.product_id, item.coupon_code, item.coupon_discount_yen)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(item)

    return deduped


def update_page_counts_after_verification(
    pages: list[dict[str, Any]],
    verified_items: list[CouponItem],
    rejected_items: list[CouponItem],
    *,
    verification_enabled: bool,
) -> None:
    if not verification_enabled:
        for page in pages:
            page["parsed_count"] = page.get("count", 0)
            page["verified_count"] = page.get("count", 0)
            page["rejected_count"] = 0
        return

    verified_counts = count_items_by_campaign_source(verified_items)
    rejected_counts = count_items_by_campaign_source(rejected_items)
    for page in pages:
        source_url = page.get("source_url")
        parsed_count = page.get("count", 0)
        verified_count = verified_counts.get(source_url, 0)
        rejected_count = rejected_counts.get(source_url, 0)
        page["parsed_count"] = parsed_count
        page["verified_count"] = verified_count
        page["rejected_count"] = rejected_count
        page["count"] = verified_count


def count_items_by_campaign_source(items: list[CouponItem]) -> dict[str | None, int]:
    counts: dict[str | None, int] = {}
    for item in items:
        counts[item.campaign_source_url] = counts.get(item.campaign_source_url, 0) + 1
    return counts


def get_dospara_coupon_prices(
    page_url: str | None = AUTO_PAGE_URL,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    html_text: str | None = None,
    fetch_prices: bool = True,
    in_stock_only: bool = False,
    cafile: str | None = None,
    insecure_tls: bool = False,
    discovery_start_urls: list[str] | None = None,
    max_discovery_candidates: int = DEFAULT_MAX_DISCOVERY_CANDIDATES,
    verify_product_page_coupons_enabled: bool = True,
    fetcher: Callable[..., str] = fetch_text,
) -> dict[str, Any]:
    discovery: PageDiscovery | None = None
    resolved_page_url = DEFAULT_PAGE_URL if is_auto_page_url(page_url) else str(page_url)
    page_html_by_url: dict[str, str] = {}

    if html_text is None:
        if is_auto_page_url(page_url):
            discovery = discover_coupon_page(
                start_urls=discovery_start_urls,
                timeout=timeout,
                cafile=cafile,
                insecure_tls=insecure_tls,
                max_candidates=max_discovery_candidates,
                fetcher=fetcher,
            )
            resolved_page_url = discovery.selected_url
            html_text = discovery.html_text
            page_html_by_url = discovery.page_html_by_url
        else:
            html_text = fetch_with_options(
                fetcher,
                resolved_page_url,
                timeout=timeout,
                cafile=cafile,
                insecure_tls=insecure_tls,
            )
            page_html_by_url = {resolved_page_url: html_text}
    else:
        page_html_by_url = {resolved_page_url: html_text}

    items, pages = parse_coupon_items_from_pages(page_html_by_url)
    if fetch_prices and items:
        product_info = fetch_product_info(
            [item.product_id for item in items],
            page_url=resolved_page_url,
            timeout=timeout,
            batch_size=batch_size,
            cafile=cafile,
            insecure_tls=insecure_tls,
        )
        enrich_items(items, product_info, page_url=resolved_page_url)

    parsed_item_count = len(items)
    verification_enabled = bool(fetch_prices and verify_product_page_coupons_enabled and items)
    verified_item_count = 0
    rejected_items: list[CouponItem] = []
    remaining_verification: dict[str, Any] = {"enabled": False}
    if verification_enabled:
        parsed_items = items
        verification_time = dt.datetime.now(dt.timezone.utc)
        product_page_verified_items = verify_product_page_coupons(
            items,
            timeout=timeout,
            cafile=cafile,
            insecure_tls=insecure_tls,
            fetcher=fetcher,
            now=verification_time,
        )
        items, remaining_verification = verify_coupon_remaining(
            product_page_verified_items,
            timeout=timeout,
            cafile=cafile,
            insecure_tls=insecure_tls,
            fetcher=fetcher,
            now=verification_time,
        )
        verified_item_count = len(items)
        rejected_items = [item for item in parsed_items if item.coupon_verified is not True]

    update_page_counts_after_verification(
        pages,
        items,
        rejected_items,
        verification_enabled=verification_enabled,
    )

    if in_stock_only:
        items = [item for item in items if "在庫なし" not in (item.stock or "")]

    records = [item.to_record() for item in items]
    source_urls = list(page_html_by_url.keys())
    primary_page = pages[0] if pages else {"source_url": resolved_page_url, **extract_page_meta(html_text)}
    return {
        "source_url": resolved_page_url,
        "source_urls": source_urls,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "page": {key: value for key, value in primary_page.items() if key != "count"},
        "pages": pages,
        "discovery": discovery.to_record() if discovery else {"enabled": False},
        "coupon_verification": {
            "enabled": verification_enabled,
            "source": "product_page_and_coupon_remaining_api",
            "strict": verification_enabled,
            "checks": [
                "product_id",
                "regular_price_yen",
                "stock",
                "coupon_code",
                "coupon_discount_yen",
                "coupon_expiry",
                "coupon_remaining",
            ],
            "remaining": remaining_verification,
            "parsed_item_count": parsed_item_count,
            "verified_item_count": verified_item_count,
            "rejected_item_count": len(rejected_items),
            "rejected_items": [item.to_record() for item in rejected_items],
        },
        "count": len(records),
        "items": records,
    }


def render_json(result: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(result, ensure_ascii=False, indent=indent)


def render_csv(result: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result["items"])
    return output.getvalue()


def render_markdown(result: dict[str, Any]) -> str:
    rows = [CSV_FIELDS]
    for item in result["items"]:
        rows.append([format_markdown_cell(item.get(field)) for field in CSV_FIELDS])

    widths = [max(len(str(row[index])) for row in rows) for index in range(len(CSV_FIELDS))]
    header = "| " + " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(rows[0])) + " |"
    divider = "| " + " | ".join("-" * widths[index] for index in range(len(CSV_FIELDS))) + " |"
    body = [
        "| " + " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)) + " |"
        for row in rows[1:]
    ]
    return "\n".join([header, divider, *body])


def format_markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", r"\|").replace("\n", " ")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Dospara regular prices, coupon discounts, and after-coupon prices.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=AUTO_PAGE_URL,
        help="Dospara campaign page URL, or 'auto' to discover the current coupon page",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("json", "csv", "markdown"),
        default="json",
        help="Output format",
    )
    parser.add_argument("-o", "--output", help="Write output to this file instead of stdout")
    parser.add_argument("--html-file", help="Read campaign HTML from a local file instead of fetching it")
    parser.add_argument("--no-api", action="store_true", help="Do not call the product API")
    parser.add_argument(
        "--no-product-page-verification",
        action="store_true",
        help="Do not verify campaign coupons against each product page before output",
    )
    parser.add_argument("--in-stock-only", action="store_true", help="Drop items whose stock text contains '在庫なし'")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Product API batch size")
    parser.add_argument(
        "--discovery-url",
        action="append",
        dest="discovery_urls",
        help="Campaign index URL to scan when url is 'auto'. May be specified multiple times.",
    )
    parser.add_argument(
        "--max-discovery-candidates",
        type=int,
        default=DEFAULT_MAX_DISCOVERY_CANDIDATES,
        help="Maximum candidate campaign pages to inspect when url is 'auto'",
    )
    parser.add_argument("--cafile", help="CA bundle path for TLS verification")
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="Disable TLS certificate verification if this local Python cannot find a CA bundle",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation")
    return parser


def render_result(result: dict[str, Any], output_format: str, *, indent: int) -> str:
    if output_format == "json":
        return render_json(result, indent=indent)
    if output_format == "csv":
        return render_csv(result)
    if output_format == "markdown":
        return render_markdown(result)
    raise ValueError(f"Unsupported output format: {output_format}")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        html_text = None
        if args.html_file:
            with open(args.html_file, "r", encoding="utf-8") as file:
                html_text = file.read()

        result = get_dospara_coupon_prices(
            args.url,
            timeout=args.timeout,
            batch_size=args.batch_size,
            html_text=html_text,
            fetch_prices=not args.no_api,
            in_stock_only=args.in_stock_only,
            cafile=args.cafile,
            insecure_tls=args.insecure_tls,
            discovery_start_urls=args.discovery_urls,
            max_discovery_candidates=args.max_discovery_candidates,
            verify_product_page_coupons_enabled=not args.no_product_page_verification,
        )
        rendered = render_result(result, args.format, indent=args.indent)

        if args.output:
            with open(args.output, "w", encoding="utf-8", newline="") as file:
                file.write(rendered)
        else:
            print(rendered)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
