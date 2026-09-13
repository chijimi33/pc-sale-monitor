"""One shared edition, one extraction, six applicability records. No inferred stock."""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

from .http import Client
from .models import BRANCHES, Offer, allowed_url, digest, iso, timestamp, utcnow
from .parsing import clean, document
from .storage import Store


def extract_asset(body: bytes, content_type: str) -> dict:
    if body.startswith(b"%PDF"):
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(body)).pages)
        if text.strip():
            return {"text": text, "method": "pdf_text", "status": "review_needed"}
        converter = shutil.which("pdftoppm")
        if not converter or not shutil.which("tesseract"):
            return {"text": "", "method": "pdf_ocr", "status": "ocr_dependency_missing"}
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "flyer.pdf"
            pdf.write_bytes(body)
            subprocess.run([converter, "-png", "-r", "160", str(pdf), str(Path(folder)/"page")], check=True, capture_output=True, timeout=120)
            extracted = [extract_asset(p.read_bytes(), "image/png") for p in sorted(Path(folder).glob("page-*.png"))]
            return {"text": "\n".join(p["text"] for p in extracted), "method": "pdf_ocr", "status": "review_needed" if all(p["status"] == "review_needed" for p in extracted) else "ocr_failed"}
    executable = shutil.which("tesseract")
    if not executable:
        return {"text": "", "method": "ocr", "status": "ocr_dependency_missing"}
    from PIL import Image
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "flyer.png"
        Image.open(BytesIO(body)).convert("RGB").save(path)
        result = subprocess.run([executable, str(path), "stdout", "-l", "jpn+eng", "--psm", "11"], capture_output=True, timeout=120)
        if result.returncode:
            return {"text": "", "method": "ocr", "status": "ocr_failed"}
        return {"text": result.stdout.decode("utf-8", "replace"), "method": "ocr", "status": "review_needed"}


def extract_candidates(text: str) -> list[dict]:
    # Keep uncertain text and amounts together for ChatGPT's visual review. OCR
    # never becomes a verified offer without a hash-bound reviewed record.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    results = []
    for index, line in enumerate(lines):
        match = re.search(r"(?:[¥￥]\s*(\d[\d,]+)|(\d[\d,]+)\s*円)", line)
        if match:
            context = "\n".join(lines[max(0, index-3):index+4])
            quantity = re.search(r"(?:限定|数量)\s*(\d+)\s*(?:台|個|点)", context)
            limit = re.search(r"(?:お一人様|1人|一人)[^\n]{0,25}", context)
            results.append({"candidate_id": digest(context)[:24], "title": None, "model": None, "price_yen": int((match[1] or match[2]).replace(",", "")),
                            "sale_date": None, "listed_quantity": int(quantity[1]) if quantity else None,
                            "purchase_limit": limit[0] if limit else None, "raw_context": context, "needs_review": True})
    return results


def collect_flyer(client: Client, state: Store, cfg: dict, review_root: Path) -> tuple[list[Offer], dict]:
    page = client.get(cfg["index_url"])
    tree = document(page)
    links = [urljoin(page.url, a.get("href")) for a in tree.xpath('//a[@href]') if "sale_flyer.php" in a.get("href", "")]
    if links:
        page = client.get(links[0])
        tree = document(page)
    containers = tree.xpath('//main|//*[@id="main"]|//*[@id="contents"]|//*[@id="main_contents"]')
    scope = containers[0] if containers else tree
    urls = set()
    for node in scope.xpath('.//a[@href]|.//img[@src]'):
        value = node.get("href") or node.get("src")
        url = urljoin(page.url, value)
        if not allowed_url(url) or not re.search(r"\.(pdf|jpe?g|png|webp)(\?|$)", url, re.I):
            continue
        label = url + " " + node.get("alt", "")
        if re.search(r"flyer|chirashi|tirashi|higawari|日替|チラシ", label, re.I):
            urls.add(url)
    if not urls:
        return [], {"status": "flyer_asset_discovery_failed", "url": page.url, "checked_at": page.observed_at}
    assets = []
    for url in sorted(urls):
        asset = client.get(url)
        content_hash = sha256(asset.body).hexdigest()
        name = f"flyers/assets/{content_hash}.json"
        parsed = state.load(name, None)
        if parsed is None or parsed.get("status") in ("ocr_dependency_missing", "ocr_failed") and shutil.which("tesseract"):
            parsed = extract_asset(asset.body, asset.content_type)
            parsed.update(content_hash=content_hash, first_seen_at=page.observed_at, candidates=extract_candidates(parsed["text"]))
            state.save(name, parsed)
        assets.append({"url": url, "content_hash": content_hash, "extraction_path": name, "status": parsed["status"]})
    edition = digest(sorted(a["content_hash"] for a in assets))
    raw_text = clean(scope)
    record = {"edition": edition, "version": edition[:12], "url": page.url, "assets": assets, "checked_at": page.observed_at,
              "period": None, "period_text": raw_text[:6000], "branches": list(BRANCHES), "status": "review_needed"}
    reviewed = Store(review_root).load(edition + ".json", None)
    offers = []
    if reviewed and reviewed.get("edition") == edition and reviewed.get("reviewed_at") and reviewed.get("reviewer"):
        record["period"] = reviewed.get("period")
        record["status"] = "reviewed"
        for row in reviewed.get("products", []):
            key = row.get("jan") or row.get("model") or digest(row.get("title"))
            offer = Offer("koubou", digest([edition, key, row.get("sale_date")])[:24], page.url, channel="store_flyer", seller_id="koubou",
                          title=row.get("title", ""), model=row.get("model"), jan=row.get("jan"), brand=row.get("brand"), condition=row.get("condition"),
                          price_yen=row.get("price_yen"), shipping_yen=0, observed_at=page.observed_at, stock="unknown", verified=True,
                          expires_at=row.get("expires_at"), listed_quantity=row.get("listed_quantity"), purchase_limit=row.get("purchase_limit"), branches=list(BRANCHES),
                          branch_overrides=row.get("branch_overrides", {}), discovery_url=page.url)
            offer.issues = ["store_stock_confirmation_required"]
            offer.branches = [branch for branch in BRANCHES if not offer.branch_overrides.get(branch, {}).get("excluded")]
            if any(v.get("price_yen") is not None and v["price_yen"] != offer.price_yen or v.get("conditions") for v in offer.branch_overrides.values()):
                offer.issues.append("store_specific_conditions_review_needed")
            start = timestamp(row.get("sale_date"))
            if start is None or start > utcnow():
                offer.issues.append("sale_not_started_or_date_unknown")
            offer.evidence = [{"url": page.url, "checked_at": page.observed_at, "method": "reviewed_common_flyer", "fields": {"edition": edition, "assets": assets, "reviewed_at": reviewed["reviewed_at"], "sale_date": row.get("sale_date"), "listed_quantity_scope": "common_flyer_not_store_inventory"}}]
            # A store-specific confirmation is allowed only with its own timestamp
            # and evidence. Shared printed quantity cannot satisfy this condition.
            from .models import fresh
            for branch, override in offer.branch_overrides.items():
                if branch in BRANCHES and override.get("stock") == "in_stock" and override.get("source_url") and allowed_url(override["source_url"]) and fresh(override.get("checked_at"), utcnow()) and not override.get("excluded"):
                    offer.stock = "in_stock"
                    offer.issues.remove("store_stock_confirmation_required")
                    break
            offers.append(offer)
    state.save(f"flyers/editions/{edition}.json", record)
    state.save("flyers/latest.json", record)
    return offers, record
