"""Lab-only, conservative primary-product JAN evidence; no I/O or offer mutation.

``None`` means unsupported store/no override. For Sofmap the returned dict has
selected_value, status (observed/unknown/conflict), evidence, conflicts, reasons.
Only ``observed`` permits adoption. Unknown/conflict must NOT fall back to a
less-scoped JAN. The caller owns integration, capture verification and all gates.
Evidence hashes describe exactly page.body (including a saved/rendered input),
not an independently verified response or a new live observation. cfg is kept
for the normalizer API; it cannot broaden these selectors or validation rules.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urljoin, urlsplit

from lxml import etree, html

from sale_monitor.models import valid_jan
from sale_monitor.parsing import canonical, clean, product_id


MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_NODES = 50000
MAX_ROWS = 512
MAX_JSON_BYTES = 512 * 1024
MAX_JSON_NODES = 10000
MAX_JSON_DEPTH = 32
MAX_EVIDENCE = 256
_LABEL = re.compile(r"^(JAN|EAN|UPC)(?:コード|CODE)?[:：]?$", re.I)
_EXCLUDED = re.compile(r"recommend|related|ranking|carousel|recent|suggest|footer|header|sidebar", re.I)
_GTIN_KEYS = ("gtin", "gtin8", "gtin12", "gtin13", "gtin14")


class _Limit(Exception):
    pass


class _JsonObject(dict):
    """Keep duplicate properties as evidence instead of last-value-wins."""
    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = pairs


def _members(obj, key):
    return [value for name, value in obj.pairs if name == key]


def _normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).upper()


def _excluded(node):
    return any(p.tag in ("aside", "footer", "header", "nav", "template") or
               _EXCLUDED.search(p.get("id", "") + " " + p.get("class", ""))
               for p in [node, *node.iterancestors()] if isinstance(p.tag, str))


def _has_class(node, name):
    return name in node.get("class", "").split()


def _sofmap_id(url):
    try:
        parts = urlsplit(url)
        values = [v for k, v in parse_qsl(parts.query, keep_blank_values=True) if k == "sku"]
        if (parts.scheme not in ("https", "http") or parts.hostname not in ("www.sofmap.com", "sofmap.com")
                or parts.username or parts.password or parts.port not in (None, 80, 443)
                or parts.path != "/product_detail.aspx" or len(values) != 1
                or not re.fullmatch(r"[0-9]{1,32}", values[0]) or product_id(url) != values[0]):
            return None
        return values[0]
    except (ValueError, TypeError):
        return None


def _rows(table):
    rows = table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
    if len(rows) > MAX_ROWS:
        raise _Limit
    result = []
    for row in rows:
        if _excluded(row):
            continue
        cells = row.xpath("./th|./td")
        if cells:
            # Preserve malformed/multiple cells as a claim, never pluck digits
            # from a larger string or flatten nested recommendation tables.
            nested = row.xpath(".//table") or any(_excluded(n) for n in row.iterdescendants() if isinstance(n.tag, str))
            value = clean(cells[1]) if len(cells) == 2 and not nested else None
            result.append((row, clean(cells[0]), value,
                           " | ".join(clean(c) for c in cells[1:])))
    return result


def _primary_tables(tree, pid, add, reasons, conflicts):
    """The retained Sofmap layout has main and detail_area as siblings."""
    wrappers = tree.xpath('//*[@id="wrapper"]')
    mains = tree.xpath('//*[@id="main"]')
    details = tree.xpath('//*[@id="detail_area"]')
    tabs = tree.xpath('//*[@id="tab2"]')
    if any(len(nodes) > 1 for nodes in (wrappers, mains, details, tabs)):
        conflicts.append("jan_primary_scope_ambiguous")
        return [], False, []
    if not all(len(nodes) == 1 for nodes in (wrappers, mains, details, tabs)):
        reasons.append("primary_product_scope_missing")
        return [], False, []
    wrapper, main, detail, tab = wrappers[0], mains[0], details[0], tabs[0]
    infos = [p for p in main.xpath("./section") if _has_class(p, "infobox")]
    if (wrapper.tag != "div" or not _has_class(wrapper, "item") or _excluded(wrapper)
            or main.tag != "main" or main.getparent() is not wrapper or not _has_class(main, "item")
            or detail.tag != "section" or detail.getparent() is not wrapper
            or tab.tag != "section" or tab.getparent() is not detail or not _has_class(tab, "prod_detail")
            or len(infos) != 1 or len(infos[0].xpath("./h1")) != 1
            or any(_excluded(p) for p in (main, detail, tab, *infos))):
        reasons.append("primary_product_scope_unverified")
        return [], False, []
    info = infos[0]
    titles = [clean(info.xpath("./h1")[0])]
    verified = []
    for table in tab.xpath("./table"):
        rows = _rows(table)
        numbers = [(row, label, value, raw) for row, label, value, raw in rows
                   if _normalized(label).rstrip(":：") == "商品番号"]
        if not numbers:
            continue  # Other specification/accessory tables cannot inherit identity.
        matches = all(value == pid for _, _, value, _ in numbers)
        for row, label, _, raw in numbers:
            add("primary_product_number", label, raw, tree.getroottree().getpath(row),
                "identity_match" if matches else "identity_mismatch", None, product_id=pid)
        if not matches:
            conflicts.append("jan_primary_product_number_mismatch")
            continue
        verified.append(table)
        titles.extend(value for _, label, value, _ in rows if _normalized(label) == "商品名" and value)
    if not verified:
        reasons.append("primary_product_number_unverified")
        return [], False, titles
    # Only direct, known price/info tables can share the verified spec identity.
    tables = verified + [p for p in info.xpath("./table") if _has_class(p, "infotable")]
    for table in tables[len(verified):]:
        for row, label, value, raw in _rows(table):
            if _normalized(label).rstrip(":：") == "商品番号" and value != pid:
                add("primary_product_number", label, raw, tree.getroottree().getpath(row),
                    "identity_mismatch", None, product_id=pid)
                conflicts.append("jan_primary_product_number_mismatch")
    return tables, not conflicts, titles


def _products(tree, add, conflicts):
    """Walk bounded JSON without promoting ItemList/recommendation children."""
    products, total_bytes, visited = [], 0, 0
    for script in tree.xpath('//script[translate(@type,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="application/ld+json"]'):
        raw = script.text or ""
        total_bytes += len(raw.encode("utf-8"))
        if total_bytes > MAX_JSON_BYTES:
            raise _Limit
        location = tree.getroottree().getpath(script)
        try:
            obj = json.loads(raw, object_pairs_hook=_JsonObject)
        except (ValueError, TypeError, RecursionError):
            # Malformed site FAQ/breadcrumb JSON is not primary GTIN evidence.
            add("json_ld_document", None, None, location, "ignored", None, reason="unreadable_json_ld")
            if not _excluded(script) and re.search(r'"@type"\s*:\s*(?:\[\s*)?"(?:https?://schema.org/)?Product"', raw):
                # Cannot establish which primary claims survived a broken
                # Product document; do not hide it behind a good display label.
                conflicts.append("jan_json_ld_product_unreadable")
            continue
        stack = [(obj, "$", 0, not _excluded(script))]
        while stack:
            node, path, depth, eligible = stack.pop()
            visited += 1
            if visited > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
                raise _Limit
            if isinstance(node, list):
                stack.extend((v, f"{path}[{i}]", depth + 1, eligible) for i, v in reversed(list(enumerate(node))))
            elif isinstance(node, dict):
                types = [t for typ in _members(node, "@type") for t in (typ if isinstance(typ, list) else [typ])]
                is_product = any(t in ("Product", "https://schema.org/Product", "http://schema.org/Product") for t in types)
                if is_product:
                    products.append((node, location + ":" + path, eligible))
                for key, value in reversed(node.pairs):
                    if isinstance(value, (dict, list)):
                        allowed = eligible and not is_product and key in ("@graph", "mainEntity")
                        stack.append((value, path + "." + key, depth + 1, allowed))
    return products


def _url_claims(product):
    urls = _members(product, "url")
    # Fragment-only @id is an object identifier, not proof of a product URL.
    urls.extend(v for v in _members(product, "@id") if isinstance(v, str) and not v.startswith("#"))
    for offers in _members(product, "offers"):
        if isinstance(offers, dict):
            offers = [offers]
        if isinstance(offers, list):
            urls.extend(u for o in offers if isinstance(o, dict) for u in _members(o, "url"))
    return urls


def _same_url(value, url):
    if not isinstance(value, str) or not value.strip() or value.startswith(("#", "?")):
        return False
    resolved = urljoin(url, value)
    return _sofmap_id(resolved) is not None and canonical(resolved) == canonical(url)


def _validated(raw, label):
    # valid_jan deliberately strips non-digits, so constrain the *whole* claim
    # first. Numeric JSON can already have lost a zero and is not recoverable.
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw.strip()):
        return None, "malformed_identifier"
    value = raw.strip()
    lengths = {"gtin8": (8,), "gtin12": (12,), "gtin13": (13,), "gtin14": ()}.get(label, (8, 12, 13))
    if len(value) not in lengths:
        return None, "unsupported_identifier_length"
    result = valid_jan(value)
    return result, "valid" if result else "invalid_checksum"


def extract_primary_jan(store, page, cfg):
    """Return provenance-bearing Sofmap evidence, or None for other stores.

    URLs/SKUs only establish scope; neither they nor cfg/search/image metadata
    ever supply a JAN. GTIN-14 is retained as unsupported (models.valid_jan
    accepts only 8/12/13); no padding, prefix guessing or identifier conversion.
    """
    if store != "sofmap":
        return None
    result = {"selected_value": None, "status": "unknown", "evidence": [], "conflicts": [], "reasons": []}
    evidence, conflicts, reasons = result["evidence"], result["conflicts"], result["reasons"]
    if not isinstance(page.body, bytes) or len(page.body) > MAX_BODY_BYTES:
        reasons.append("identity_input_limit_or_invalid_body")
        return result
    provenance = {"url": page.url, "observed_at": page.observed_at,
                  "body_sha256": hashlib.sha256(page.body).hexdigest(), "method": page.method}

    def add(kind, label, raw, scope, status, value, **extra):
        if len(evidence) >= MAX_EVIDENCE:
            raise _Limit
        evidence.append({**provenance, "kind": kind, "raw_label": label, "raw_value": raw,
                         "scope": scope, "status": status, "value": value, **extra})

    def claim(kind, label, raw, scope, **extra):
        value, validation = _validated(raw, label)
        add(kind, label, raw, scope, "valid" if value is not None else "invalid", value,
            validation=validation, **extra)

    pid = _sofmap_id(page.url)
    if pid is None or page.status != 200:
        reasons.append("current_product_page_unverified")
        return result
    try:
        tree = html.fromstring(page.text, parser=html.HTMLParser(no_network=True), base_url=page.url)
        if sum(1 for _ in tree.iter()) > MAX_NODES:
            raise _Limit
        tables, verified, titles = _primary_tables(tree, pid, add, reasons, conflicts)
        for table in tables:
            for row, label, value, raw in _rows(table):
                if _LABEL.fullmatch(_normalized(label)):
                    claim("primary_product_label", label, raw if value is not None else None,
                          tree.getroottree().getpath(row), product_id=pid, raw_cell_text=raw)
        products = _products(tree, add, conflicts)
        eligible_count = sum(eligible for _, _, eligible in products)
        for product, scope, eligible in products:
            claims = [(key, value) for key, value in product.pairs if key in _GTIN_KEYS]
            if not claims:
                continue
            urls = _url_claims(product)
            matches = [_same_url(u, page.url) for u in urls]
            skus = _members(product, "sku")
            sku_matches = bool(skus) and all(isinstance(sku, str) and sku.strip() == pid or
                                           type(sku) is int and str(sku) == pid for sku in skus)
            names = _members(product, "name")
            named = bool(names) and all(isinstance(name, str) and _normalized(name) in {
                _normalized(t) for t in titles if t} for name in names)
            if not eligible:
                reason = "json_ld_non_primary_context"
            elif matches and any(matches) and not all(matches):
                conflicts.append("jan_json_ld_primary_url_conflict")
                reason = "json_ld_primary_url_conflict"
            elif urls and not all(matches):
                reason = "json_ld_wrong_product_url"
            elif skus and not sku_matches:
                reason = "json_ld_product_number_mismatch"
                if matches and all(matches):
                    conflicts.append("jan_json_ld_product_number_mismatch")
            elif matches and all(matches):
                reason = None
            elif eligible_count != 1:
                reason = "json_ld_ambiguous_primary"
            elif not verified or not (sku_matches or named):
                reason = "json_ld_primary_context_unverified"
            else:
                reason = None
            for key, raw in claims:
                if reason:
                    add("primary_product_json_ld", key, raw, scope, "ignored", None,
                        reason=reason, product_urls=urls)
                    reasons.append(reason)
                else:
                    claim("primary_product_json_ld", key, raw, scope, product_id=pid,
                          product_urls=urls, primary_basis="explicit_url" if matches else "unique_verified_context")
    except _Limit:
        reasons.append("identity_extraction_limit")
        result["conflicts"] = sorted(set(conflicts))
        return result  # Never adopt a prefix of a bounded/partially scanned input.
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        reasons.append("identity_input_unreadable")
        return result
    values = {e["value"] for e in evidence if e["status"] == "valid"}
    invalid = any(e["status"] == "invalid" for e in evidence)
    if len(values) > 1:
        conflicts.append("jan_conflict_review_needed")
    if invalid:
        conflicts.append("jan_invalid_explicit_claim")
    if conflicts:
        result["status"] = "conflict" if values else "unknown"
    elif len(values) == 1:
        result.update(selected_value=next(iter(values)), status="observed")
    else:
        reasons.append("primary_jan_missing")
    result["conflicts"] = sorted(set(conflicts))
    result["reasons"] = sorted(set(reasons))
    return result
