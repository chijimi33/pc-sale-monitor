from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import re
import unicodedata
from urllib.parse import urlsplit

UTC = timezone.utc
JST = timezone(timedelta(hours=9))
STORES = ("dospara", "ark", "tsukumo", "sofmap", "joshin", "bic", "koubou", "yodobashi", "yahoo", "amazon")
BRANCHES = ("枚方", "大阪日本橋", "東大阪", "なんばアウトレット別館", "堺", "岸和田")
BLOCKED_HOSTS = ("rakuten.co.jp", "rakuten.ne.jp", "rakuten.com", "rakuten.jp", "r10.to")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).isoformat()


def timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("/", "-"))
        return result.replace(tzinfo=JST) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def fresh(value: str | None, now: datetime, hours: int = 12) -> bool:
    date = timestamp(value)
    return date is not None and timedelta(0) <= now - date <= timedelta(hours=hours)


def digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def allowed_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    return parts.scheme in ("http", "https") and bool(host) and not any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).upper()).strip()


def valid_jan(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in (8, 12, 13):
        return None
    total = sum(int(n) * (3 if i % 2 == 0 else 1) for i, n in enumerate(reversed(digits[:-1])))
    return digits if (10 - total % 10) % 10 == int(digits[-1]) else None


@dataclass
class Offer:
    store: str
    product_id: str
    url: str
    title: str = ""
    model: str | None = None
    jan: str | None = None
    brand: str | None = None
    seller_id: str | None = None
    condition: str | None = None
    variant: str | None = None
    warranty: str | None = None
    channel: str = "online"
    price_yen: int | None = None
    shipping_yen: int | None = None
    discount_yen: int = 0
    points_yen: int | None = None
    conditional_points: list = field(default_factory=list)
    stock: str = "unknown"
    coupon: dict = field(default_factory=dict)
    expires_at: str | None = None
    observed_at: str | None = None
    observed_run_id: str | None = None
    source_updated_at: str | None = None
    verified: bool = False
    discovery_url: str | None = None
    discovery_kind: str = "sale"
    evidence: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    branch_overrides: dict = field(default_factory=dict)
    listed_quantity: int | None = None
    purchase_limit: str | None = None

    @property
    def key(self) -> str:
        return digest([self.store, self.seller_id, self.channel, self.product_id])[:24]

    @property
    def identity(self) -> str | None:
        jan = valid_jan(self.jan)
        if jan:
            return "jan:" + jan
        if self.model and self.brand:
            return "model:" + normalize(self.brand) + ":" + normalize(self.model)
        return None

    @property
    def payment(self) -> int | None:
        if any(type(n) is not int for n in (self.price_yen, self.shipping_yen, self.discount_yen)):
            return None
        return self.price_yen + self.shipping_yen - self.discount_yen

    def errors(self, now: datetime) -> list[str]:
        result = list(self.issues)
        if self.store not in STORES or not allowed_url(self.url):
            result.append("excluded_source")
        if not self.verified or not self.evidence:
            result.append("unverified_product")
        if not self.identity:
            result.append("identity_missing")
        if not self.seller_id:
            result.append("seller_unknown")
        if self.condition is None:
            result.append("condition_unknown")
        if self.stock != "in_stock":
            result.append("stock_" + self.stock)
        if not fresh(self.observed_at, now):
            result.append("stale_observation")
        if self.source_updated_at and not fresh(self.source_updated_at, now):
            result.append("stale_source")
        if self.price_yen is None or type(self.price_yen) is not int or self.price_yen <= 0:
            result.append("price_unknown")
        if self.shipping_yen is None or type(self.shipping_yen) is not int or self.shipping_yen < 0:
            result.append("shipping_unknown")
        if type(self.discount_yen) is not int or self.discount_yen < 0 or (self.payment is not None and self.payment <= 0):
            result.append("invalid_payment")
        if self.points_yen is not None and (type(self.points_yen) is not int or self.points_yen < 0 or self.payment is not None and self.points_yen >= self.payment):
            result.append("invalid_points")
        if self.expires_at and (timestamp(self.expires_at) is None or timestamp(self.expires_at) <= now):
            result.append("expired")
        if self.coupon:
            remaining = self.coupon.get("remaining")
            if self.coupon.get("verified") is not True:
                result.append("coupon_unverified")
            if self.coupon.get("limited") and (type(remaining) is not int or remaining not in (-1,) and remaining <= 0):
                result.append("coupon_unavailable")
            if not fresh(self.coupon.get("checked_at"), now):
                result.append("coupon_stale")
        return sorted(set(result))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Offer:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def same_product(a: Offer, b: Offer) -> bool:
    if not a.identity or a.identity != b.identity or a.condition != b.condition:
        return False
    # Different known variants or guarantee conditions must not be merged.
    return not any(x and y and normalize(x) != normalize(y) for x, y in ((a.variant, b.variant), (a.warranty, b.warranty)))
