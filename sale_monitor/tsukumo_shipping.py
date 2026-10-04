"""The current standard delivery tariff, limited to checked ordinary goods."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import re
from urllib.parse import urlsplit

from lxml.etree import ParserError

from .http import Page
from .models import Offer, timestamp, valid_jan
from .parsing import clean, document, integer

POLICY_URL = "https://shop.tsukumo.co.jp/shopping-help/service/souryo.html"


def _class(name: str) -> str:
    return f'contains(concat(" ",normalize-space(@class)," ")," {name} ")'


def standard_product(page: Page, offer: Offer) -> bool:
    """Require the primary price and product identity; reject special terms."""
    location = urlsplit(page.url)
    match = re.fullmatch(r"/goods/(\d{8,13})/", location.path)
    if (page.status != 200 or location.scheme != "https" or location.netloc != "shop.tsukumo.co.jp"
            or location.query or not match or not valid_jan(match[1])
            or offer.store != "tsukumo" or offer.seller_id != "tsukumo"
            or offer.jan != match[1] or not offer.verified or offer.issues
            or offer.condition != "new" or offer.shipping_yen is not None
            or type(offer.price_yen) is not int or offer.price_yen <= 0):
        return False
    try:
        tree = document(page)
    except (ParserError, ValueError):
        return False
    articles = tree.xpath('//main/article')
    specs = tree.xpath('//main/section[@id="spec-contents"]')
    if len(articles) != 1 or len(specs) != 1:
        return False
    names = articles[0].xpath(f'.//h2[{_class("goods-name")}]')
    prices = articles[0].xpath(f'./div[{_class("goods-main-right")}]/div/p[{_class("price")}]')
    if len(names) != 1 or clean(names[0]) != " ".join(offer.title.split()) or len(prices) != 1:
        return False
    amounts = prices[0].xpath('./strong')
    taxes = prices[0].xpath(f'./span[{_class("including-tax-text")}]')
    if (len(amounts) != 1 or integer(clean(amounts[0])) != offer.price_yen
            or len(taxes) != 1 or clean(taxes[0]) != "（税込）"):
        return False
    # These areas include the product's bespoke notes and full specification,
    # but exclude site navigation, recommendations and unrelated reviews.
    text = " ".join(clean(node) for node in [articles[0], specs[0]])
    text += " " + " ".join(articles[0].xpath('.//img/@alt') + specs[0].xpath('.//img/@alt'))
    text = text.replace("送料無料まであと少し、オススメ商品", "")
    return not re.search(r"送料|配送料|配送費|運賃|別途料金|別途費用|着払い|大型配送|eX[.\s-]*computer|G[\s-]*GEAR|\bBTO\b", text, re.I)


def parse_policy(page: Page) -> dict | None:
    if page.status != 200 or page.url != POLICY_URL:
        return None
    try:
        tree = document(page)
    except (ParserError, ValueError):
        return None
    areas = tree.xpath(f'//main/article/div[{_class("MainArea")} and {_class("souryo")}]')
    if len(areas) != 1:
        return None
    area = areas[0]
    headers = area.xpath('./h2[normalize-space(.)="送料について"]')
    if len(headers) != 1:
        return None
    section = []
    for node in headers[0].itersiblings():
        if node.tag == "h2":
            break
        section.append(node)
    tables = [node for node in section if node.tag == "table" and node.get("id") == "soryo"]
    limits = [re.fullmatch(r"税込([\d,]+)円未満ご購入の場合", clean(node))
              for node in section if node.tag == "h4"]
    limits = [int(match[1].replace(",", "")) for match in limits if match]
    if (len(tables) != 1 or len(limits) != 1 or limits[0] <= 0
            or sum(len(node.xpath('descendant-or-self::table')) for node in section) != 1):
        return None
    table = tables[0]
    rows = table.xpath('./tr|./tbody/tr')
    if len(rows) != 2:
        return None
    regions = [clean(node) for node in rows[0].xpath('./th')]
    if regions != ["地域", "北海道", "本州 (関東以外)",
                   "関東 (東京、茨城県、群馬県、埼玉県、千葉県、神奈川県、山梨県、栃木県)",
                   "四国・九州・沖縄", "離島"]:
        return None
    cells = rows[1].xpath('./td')
    if len(cells) != 2 or clean(cells[0]) != "送料 （税込）" or cells[1].get("colspan") != "5":
        return None
    fee = re.fullmatch(r"([\d,]+)円（内消費税[\d,]+円）", clean(cells[1]))
    text = " ".join(clean(node) for node in section)
    if (not fee or int(fee[1].replace(",", "")) <= 0
            or f"税込{limits[0]:,}円以上で日本全国どこでも送料無料" not in text
            or "eX.computer、G-GEAR 製品を除く" not in text
            or "一部対象外商品は、商品ページへ別途記載" not in text
            or re.search(r"別途送料|追加送料|送料加算|会員限定|クーポン", text)):
        return None
    return {"below_yen": limits[0], "shipping_yen": int(fee[1].replace(",", "")),
            "scope": "standard_goods_single_item_nationwide", "tax_included": True}


def apply_policy(offer: Offer, product: Page, policy: Page) -> bool:
    if not standard_product(product, offer):
        return False
    product_at, policy_at = timestamp(product.observed_at), timestamp(policy.observed_at)
    if (product_at is None or policy_at is None
            or abs(product_at - policy_at) > timedelta(hours=1)):
        return False
    terms = parse_policy(policy)
    # Above the threshold still requires a product-specific free-shipping
    # indication, handled by the existing parser. Do not infer it here.
    if terms is None or offer.price_yen >= terms["below_yen"]:
        return False
    offer.shipping_yen = terms["shipping_yen"]
    offer.evidence.append({"url": policy.url, "checked_at": policy.observed_at,
        "method": policy.method, "body_sha256": hashlib.sha256(policy.body).hexdigest(),
        "fields": {"shipping_policy": {**terms, "product_url": product.url,
                    "product_price_yen": offer.price_yen, "quantity": 1}}})
    return True
