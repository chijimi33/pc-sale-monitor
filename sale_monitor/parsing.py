"""Conservative, product-scoped HTML extraction; unknown values stay unknown."""
from __future__ import annotations

from datetime import timedelta
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lxml import html

from .http import Page
from .models import JST, Offer, allowed_url, digest, iso, timestamp, valid_jan

SALE = re.compile(r"特価|セール|タイムセール|値下げ|お買い得|在庫限り|数量限定|限定価格|処分|sale|clearance", re.I)
PC = re.compile(r"パソコン|PC|CPU|GPU|SSD|HDD|DDR[345]|メモリ|マザーボード|グラフィック|電源|クーラー|モニター|ディスプレイ|キーボード|マウス|ゲーミング|ルーター|USB|Ryzen|GeForce|Radeon|Core\s+i[3579]|CORELIQUID|Samsung|SanDisk|Crucial", re.I)
TRACKING = re.compile(r"^(utm_|pre$|ref$|srsltid$|fbclid$|gclid$)")


def canonical(url: str) -> str:
    p = urlsplit(url)
    if (p.hostname or "").endswith("amazon.co.jp"):
        asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", p.path)
        if asin:
            return "https://www.amazon.co.jp/dp/" + asin[1]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not TRACKING.search(k)))
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", query, ""))


def document(page: Page):
    return html.fromstring(page.text, base_url=page.url)


def clean(element) -> str:
    if element is None:
        return ""
    return " ".join(" ".join(element.xpath('.//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript)]')).split())


def first(tree, xpath: str) -> str | None:
    matches = tree.xpath(xpath)
    if not matches:
        return None
    result = matches[0]
    return (clean(result) if hasattr(result, "text_content") else str(result)).strip() or None


def integer(value) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*[¥￥]?\s*\d[\d,]*(?:\.0+)?\s*(?:円)?\s*", value):
        return int(value.strip().replace("¥", "").replace("￥", "").replace(",", "").replace("円", "").split(".")[0])
    return None


def fields(tree) -> dict:
    result = {}
    for row in tree.xpath("//tr"):
        cells = row.xpath("./th|./td")
        if len(cells) == 2 and len(clean(cells[0])) < 35:
            result[clean(cells[0])] = clean(cells[1])
    for label in tree.xpath("//dt"):
        sibling = label.getnext()
        if sibling is not None and sibling.tag == "dd":
            result[clean(label)] = clean(sibling)
    return result


def field(data: dict, pattern: str) -> str | None:
    return next((v for k, v in data.items() if re.search(pattern, k)), None)


def stock_status(value: str | None) -> str:
    if not value:
        return "unknown"
    if re.search(r"OutOfStock|SoldOut|在庫なし|在庫切れ|売り切れ|完売|販売終了|品切れ", value, re.I):
        return "out_of_stock"
    if re.search(r"PreOrder|予約|取寄|取り寄せ|受注", value, re.I):
        return "preorder"
    # Schema.org explicitly declares availability, but gives no stock count.
    # A shop's unqualified "在庫限り" label alone still stays unknown.
    if re.fullmatch(r"(?:https?://schema\.org/)?LimitedAvailability", value.strip(), re.I):
        return "in_stock"
    if re.search(r"InStock|在庫あり|在庫有|即納|即日出荷|\d+時間以内に出荷|通常\d+.*出荷", value, re.I):
        return "in_stock"
    return "unknown"


def product_id(url: str) -> str:
    p = urlsplit(url)
    q = dict(parse_qsl(p.query))
    return q.get("product_id") or q.get("pc_id") or q.get("sku") or p.path.strip("/").split("/")[-1].replace(".html", "")


def json_products(tree) -> list[dict]:
    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                yield from walk(item)
        elif isinstance(obj, dict):
            typ = obj.get("@type", [])
            if typ == "Product" or isinstance(typ, list) and "Product" in typ:
                yield obj
            else:
                for key in ("@graph", "mainEntity", "itemListElement", "item"):
                    yield from walk(obj.get(key))
    products = []
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            products.extend(walk(json.loads(script.text or "")))
        except (ValueError, TypeError):
            continue
    return products


def parse_product(store: str, page: Page, cfg: dict, discovery: dict | None = None) -> Offer:
    tree = document(page)
    data = fields(tree)
    offer = Offer(store, product_id(page.url), canonical(page.url), seller_id=store,
                  observed_at=page.observed_at, discovery_url=(discovery or {}).get("source"),
                  discovery_kind=(discovery or {}).get("kind", "sale"))
    products = json_products(tree)
    primary = [p for p in products if canonical(p.get("url") or (p.get("offers", {}).get("url") if isinstance(p.get("offers"), dict) else "") or page.url) == canonical(page.url)]
    item = primary[0] if len(primary) == 1 else {}
    offer.title = item.get("name") or first(tree, '//h1') or first(tree, '//meta[@property="og:title"]/@content') or ""
    offer.jan = valid_jan(field(data, r"JAN|EAN|UPC|商品番号")) or next((valid_jan(item.get(k)) for k in ("gtin13", "gtin12", "gtin8", "gtin", "sku") if valid_jan(item.get(k))), None)
    offer.model = field(data, r"^(商品)?型番$|^メーカー型番$")
    if not offer.model and item.get("mpn") and item["mpn"] != offer.product_id:
        offer.model = str(item["mpn"])
    brand = item.get("brand")
    offer.brand = (brand.get("name") if isinstance(brand, dict) else brand) or field(data, r"^メーカー(名)?$|^ブランド(名)?$")
    offer.warranty = field(data, r"^保証期間$")
    offer.variant = field(data, r"^商品構成$|^セット内容$")
    schema_offer = item.get("offers", {})
    if isinstance(schema_offer, list):
        schema_offer = schema_offer[0] if len(schema_offer) == 1 else {}
    if not isinstance(schema_offer, dict) or schema_offer.get("@type") == "AggregateOffer":
        schema_offer = {}
    if schema_offer.get("priceCurrency") == "JPY":
        offer.price_yen = integer(schema_offer.get("price"))
    offer.stock = stock_status(schema_offer.get("availability"))
    condition = str(schema_offer.get("itemCondition", ""))
    offer.condition = "new" if condition.endswith("NewCondition") else "used" if condition.endswith("UsedCondition") else None
    shipping = schema_offer.get("shippingDetails", {})
    if isinstance(shipping, list):
        shipping = shipping[0] if len(shipping) == 1 else {}
    rate = shipping.get("shippingRate", {}) if isinstance(shipping, dict) else {}
    if rate.get("currency") == "JPY":
        offer.shipping_yen = integer(rate.get("value"))
    expiry = schema_offer.get("priceValidUntil") or (discovery or {}).get("expires_at")
    if expiry:
        date = timestamp(expiry)
        if date:
            offer.expires_at = iso(date + timedelta(days=1) - timedelta(seconds=1)) if len(expiry) == 10 else iso(date)
    evidence_fields = {"json_ld": bool(item), "schema_availability": schema_offer.get("availability"), "specifications": data}
    # Scope selectors to the current product, never scrape a page-wide lowest price.
    if store == "ark":
        price_blocks = tree.xpath('//*[@id=$id]/ancestor::li[contains(concat(" ",normalize-space(@class)," ")," itemprice ")][1]', id="item-" + offer.product_id)
        if len(price_blocks) == 1:
            displayed = first(price_blocks[0], './div[contains(concat(" ",normalize-space(@class)," ")," date-diff2 ")]')
            if displayed:
                evidence_fields["expiry"] = {"schema": schema_offer.get("priceValidUntil"), "discovery": (discovery or {}).get("expires_at"), "product_display": displayed}
                match = re.fullmatch(r"開催期間:\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})まで", displayed)
                date = timestamp(offer.expires_at)
                # A date without a year cannot establish a new deadline. Compare
                # it with the claimed deadline, retaining both sources on conflict.
                if match and date:
                    date = date.astimezone(JST)
                    if tuple(map(int, match.groups())) != (date.month, date.day, date.hour, date.minute):
                        offer.expires_at = None
                        offer.issues.append("expiry_conflict_review_needed")
    if store == "koubou":
        offer.price_yen = integer(first(tree, '//input[@id="priceIncTax"]/@value'))
        if field(data, r"^送料$") == "無料":
            offer.shipping_yen = 0
        raw = re.search(r"eccube\.classCategories\s*=\s*(\{.*?\});", page.text, re.S)
        if raw:
            try:
                variants = json.loads(raw.group(1))
                variants = [v for row in variants.values() for v in row.values() if isinstance(v, dict) and "stock_find" in v]
                matching = [v for v in variants if v.get("product_code") == offer.model and integer(v.get("price02")) == offer.price_yen]
                if matching and all(v.get("stock_find") is True for v in matching):
                    offer.stock = "in_stock"
                elif matching and all(v.get("stock_find") is False for v in matching):
                    offer.stock = "out_of_stock"
                if matching:
                    offer.points_yen = integer(matching[0].get("point"))
                    offer.purchase_limit = str(matching[0].get("limit") or "") or None
            except (ValueError, AttributeError, TypeError):
                offer.issues.append("variant_data_unreadable")
        # Matching stock_find describes the variant, not whether the shop has
        # opened purchasing. A preparing item can still have stock_find=true.
        buttons = tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," productDetail--main__right--price ")]//button[@disabled]')
        if any(clean(button) == "商品準備中" for button in buttons):
            evidence_fields["purchase_availability"] = {
                "source": "disabled_primary_purchase_button", "button_text": "商品準備中",
                "button_disabled": True, "variant_stock": offer.stock}
            offer.stock = "unknown"
            offer.issues.append("purchase_not_available")
        elif any(clean(button) == "在庫切れです" for button in buttons):
            evidence_fields["purchase_availability"] = {
                "source": "disabled_primary_purchase_button", "button_text": "在庫切れです",
                "button_disabled": True, "variant_stock": offer.stock}
            offer.stock = "out_of_stock"
        # BTO detail pages also expose priceIncTax, but it is only a starting
        # price when the primary PC price panel says 円～. Retain the printed
        # lower bound as evidence, not as the price of a confirmed configuration.
        pc_prices = tree.xpath('//input[@id="priceIncTax"]/parent::*[contains(concat(" ",normalize-space(@class)," ")," page_type_pc ")]/div[contains(concat(" ",normalize-space(@class)," ")," productDetail--bottom ")]/div[contains(concat(" ",normalize-space(@class)," ")," productDetail--bottom__contents ")]/dl[contains(concat(" ",normalize-space(@class)," ")," productDetail--bottom__contents--pirce ")]')
        if len(pc_prices) == 1:
            currency = first(pc_prices[0], './dd/span[contains(concat(" ",normalize-space(@class)," ")," currency ")]')
            if currency and re.fullmatch(r"円\s*[～〜~]", currency):
                evidence_fields["price_basis"] = "starting_price"
                evidence_fields["printed_price_from_yen"] = integer(first(pc_prices[0], './dd/span[contains(concat(" ",normalize-space(@class)," ")," value ")]'))
                evidence_fields["price_display"] = clean(pc_prices[0])
                offer.price_yen = None
                offer.issues.append("bto_configuration_review_needed")
    if store == "dospara":
        if first(tree, '//*[contains(concat(" ",normalize-space(@class)," ")," free_shipping ")]') == "送料無料":
            offer.shipping_yen = 0
        # Preserve the full displayed model string. A series name or inferred
        # abbreviated CPU name is never equated with a full part number.
        labels = tree.xpath('//h1[contains(@class,"product-show-detail")]')
        if len(labels) == 1 and clean(labels[0]) == " ".join(offer.title.split()):
            label = clean(labels[0])
            match = re.match(r"(AMD|Intel|ASRock|ASUS|MSI|GIGABYTE|CORSAIR|Corsair|Crucial|Samsung|SAMSUNG|Western Digital|玄人志向|CFD|ドスパラセレクト|Logicool|Thermaltake|Antec|NZXT)\s+(.+)", label)
            if not match:
                # The store's primary heading separates manufacturer and product
                # with two spaces. Preserve that delimiter before whitespace
                # normalization; unknown single-space labels stay unresolved.
                match = re.fullmatch(r"([^\r\n]+?)[ \t\u3000]{2,}(\S[^\r\n]+)", labels[0].text_content().strip())
            if match:
                offer.brand = offer.brand or " ".join(match[1].split())
                offer.model = offer.model or " ".join(match[2].split())
                evidence_fields["product_label"] = {"brand": offer.brand, "full_model_label": offer.model, "source": "primary_product_heading"}
    if store == "tsukumo" and first(tree, '//li[contains(concat(" ",normalize-space(@class)," ")," free-shipping ")]') == "送料無料":
        offer.shipping_yen = 0
    selectors = {
        "tsukumo": ('//*[@id="product_price"]|//*[@itemprop="price"]/@content', '//*[contains(@class,"stock-status")]|//*[@itemprop="availability"]/@href'),
        "sofmap": ('//*[@id="price"]|//*[@id="priceTax"]|//*[@itemprop="price"]/@content', '//*[@id="stock"]|//*[contains(@class,"productDelivery")]'),
        "bic": ('//*[@id="styleSelect" and @data-price]/@data-price|//*[@itemprop="price"]/@content', '//*[@itemprop="availability"]/@href'),
        "joshin": ('//*[@itemprop="price"]/@content', '//*[@itemprop="availability"]/@href'),
        "yodobashi": ('//*[@id="js_scl_unitPrice"]|//*[@itemprop="price"]/@content', '//*[@id="salesInfoTxt"]|//*[@id="js_scl_stockStatus"]'),
    }
    if store in selectors:
        price_path, stock_path = selectors[store]
        offer.price_yen = offer.price_yen or integer(first(tree, price_path))
        scoped_stock = stock_status(first(tree, stock_path))
        if scoped_stock != "unknown":
            offer.stock = scoped_stock
    if store == "sofmap":
        # OnlineOnly describes a sales channel. Read availability from the
        # primary product table, never recommendation tables or order limits.
        stock_rows = tree.xpath('//*[@id="main"]/section[contains(concat(" ",normalize-space(@class)," ")," infobox ")]/table[contains(concat(" ",normalize-space(@class)," ")," infotable ")]//tr[th[normalize-space(.)="在庫"]]/td')
        if len(stock_rows) == 1:
            stock_text = clean(stock_rows[0])
            scoped_stock = stock_status(stock_text)
            if scoped_stock != "unknown":
                offer.stock = scoped_stock
                evidence_fields["stock_status"] = {
                    "source": "primary_product_information", "text": stock_text,
                    "status": scoped_stock}
    if offer.condition is None and cfg.get("default_condition") and not re.search(r"中古|アウトレット|再生品|バルク", offer.title + " " + str(field(data, r"商品状態|コンディション") or "")):
        offer.condition = cfg["default_condition"]
    shipping_text = field(data, r"^送料$|^配送料$")
    if offer.shipping_yen is None and shipping_text:
        offer.shipping_yen = 0 if shipping_text in ("無料", "送料無料") else integer(shipping_text)
    points = field(data, r"^ポイント$")
    if offer.points_yen is None and points and re.fullmatch(r"[\d,]+\s*ポイント", points):
        offer.points_yen = integer(points.replace("ポイント", "").strip())
    declared = first(tree, '//link[@rel="canonical"]/@href')
    if declared and canonical(urljoin(page.url, declared)) != canonical(page.url):
        offer.issues.append("canonical_product_mismatch")
    if re.search(r"セット|[24]枚組|まとめ買い", offer.title) and not offer.variant:
        offer.issues.append("bundle_contents_review_needed")
    if "/bto/customizer/" in page.url:
        offer.issues.append("bto_configuration_review_needed")
    offer.verified = bool(offer.title and offer.price_yen and (offer.identity or offer.model) and not offer.issues)
    offer.evidence = [{"url": page.url, "checked_at": page.observed_at, "method": page.method, "content_hash": digest(page.text), "fields": evidence_fields}]
    return offer


def discover(page: Page, cfg: dict, *, sale_page: bool, comparison: bool = False) -> tuple[list[dict], list[str], list[str]]:
    tree = document(page)
    host = urlsplit(page.url).hostname
    products, pages, campaigns = {}, set(), set()
    pattern = re.compile("|".join(cfg["product_patterns"]))
    for a in tree.xpath('//a[@href]'):
        url = canonical(urljoin(page.url, a.get("href")))
        if not allowed_url(url) or urlsplit(url).hostname != host:
            continue
        label = clean(a) + " " + " ".join(a.xpath('.//img/@alt'))
        if pattern.search(url):
            card = a
            for ancestor in a.iterancestors():
                if ancestor.tag in ("li", "article") or "item_tbl" in ancestor.get("class", ""):
                    card = ancestor
                    break
                if ancestor.tag in ("body", "main"):
                    break
            context = clean(card)
            if comparison or ((sale_page or SALE.search(context)) and (PC.search(context + label) or cfg.get("pc_only"))):
                products[url] = {"url": url, "source": page.url, "title": label.strip(), "kind": "comparison" if comparison else "sale", "expires_at": card.get("e-date")}
        else:
            parent = a.getparent()
            nav = " ".join([a.get("rel", ""), a.get("class", ""), parent.get("id", ""), parent.get("class", ""), label])
            pagination = re.search(r"next|pagination|pager|listnavi|次へ|次の", nav, re.I)
            same_path = urlsplit(url).path == urlsplit(page.url).path
            if pagination and (same_path or "next" in a.get("rel", "")):
                pages.add(url)
            elif not comparison and any(re.search(p, url) for p in cfg.get("sale_patterns", [])) and SALE.search(label + " " + url):
                campaigns.add(url)
    return list(products.values()), sorted(pages - {canonical(page.url)}), sorted(campaigns - {canonical(page.url)})


def confirmed_empty_search(store: str, page: Page) -> str | None:
    parts = urlsplit(page.url)
    query = dict(parse_qsl(parts.query)).get("keyword", "").strip()
    if store != "tsukumo" or parts.hostname != "shop.tsukumo.co.jp" or not re.fullmatch(r"/search(?:/p\d+)?/?", parts.path) or not query or page.status not in (200, 404):
        return None
    tree = document(page)
    markers = tree.xpath('//*[@id="sli_noresult"]')
    if len(markers) != 1 or not any(clean(node) == "該当する商品がありませんでした。" for node in [markers[0], *markers[0].xpath('./div')]):
        return None
    if first(tree, '//input[@name="keyword"]/@value') != query:
        return None
    return query if (first(tree, '//title') or "").startswith("検索結果：" + query + "｜") else None


def search_form(page: Page, query: str) -> str | None:
    tree = document(page)
    for form in tree.xpath('//form[not(@method) or translate(@method,"GET","get")="get"]'):
        inputs = form.xpath('.//input[@name]')
        search = next((e for e in inputs if e.get("name") in ("keyword", "key", "search", "q", "s", "word")), None)
        if search is None:
            continue
        url = urljoin(page.url, form.get("action", ""))
        if urlsplit(url).hostname != urlsplit(page.url).hostname or not allowed_url(url):
            continue
        values = {e.get("name"): e.get("value", "") for e in inputs if e.get("type") == "hidden"}
        values[search.get("name")] = query
        return url + ("&" if "?" in url else "?") + urlencode(values)
    return None
