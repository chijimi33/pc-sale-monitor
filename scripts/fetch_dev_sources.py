"""Obtain upstream source and local HTML fixtures for adapter development."""
from pathlib import Path
import sys
from urllib.request import urlopen
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sale_monitor.http import Client

SAMPLES = {
    "ark-sale": "https://www.ark-pc.co.jp/t/c/760/",
    "ark-product": "https://www.ark-pc.co.jp/i/10401461/",
    "koubou-sale": "https://www.pc-koubou.jp/goods/parts_goods_tokusen.php",
    "koubou-product": "https://www.pc-koubou.jp/products/detail.php?product_id=1183984",
    "sofmap-sale": "https://www.sofmap.com/contents/?id=2959&sid=1",
    "tsukumo-sale": "https://shop.tsukumo.co.jp/",
    "joshin-sale": "https://joshinweb.jp/",
    "bic-sale": "https://www.biccamera.com/bc/main/",
    "yodobashi-sale": "https://www.yodobashi.com/",
    "flyer-index": "https://www.pc-koubou.jp/shopinfo/contents/sale_campaign.php",
}

def fetch_one(item):
    name, url = item
    try:
        page = Client().get(url)
        folder = ROOT / "scratch"
        folder.mkdir(exist_ok=True)
        (folder / (name + ".html")).write_text(page.text, encoding="utf-8")
        return name, len(page.body)
    except Exception as exc:
        return name, str(exc)

if __name__ == "__main__":
    folder = ROOT / "sale_monitor" / "vendor"
    folder.mkdir(exist_ok=True)
    (folder / "__init__.py").touch()
    for repo, filename in (("Dospara-coupon-price-tool", "dospara_coupon_tool.py"),):
        url = f"https://raw.githubusercontent.com/chijimi33/{repo}/a17c94d70c67b2ff633591e5161e1fbfc544e6ff/{filename}"
        (folder / filename).write_bytes(urlopen(url, timeout=30).read())
    with ThreadPoolExecutor(max_workers=5) as executor:
        for result in executor.map(fetch_one, SAMPLES.items()):
            print(result, flush=True)
