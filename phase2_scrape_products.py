"""
Phase 2 — Scrape all products for every brand.

Pagination:
  Page 1 : GET /de/marken/SLUG/
  Page N : GET /de/marken/SLUG/?page=N&mehr-laden=true   (no offset needed)
  Stop   : no <button data-load-more-url="..."> in response

Product card: <suchen-produkt data-syan="SYAN" data-product-index="N">
  .suchen-produkt__marke          brand
  .suchen-produkt__name__text     product name
  .suchen-produkt__name__zusatz   sub-name / variant label
  a[data-product-link]            URL + data-farb-id (color_id)
  .suchen-preis--schwarzpreis     regular price
  .suchen-preis--rotpreis         sale price
  img[data-test-suchen-product-image]   main image
  .suchen-produkt-farbkachel      color swatches (data-image-*, data-sizes JSON)
  ul.suchen-produkt__sizes > li   available sizes
  [data-test-suchen-product-badge-neu]  "Neu" badge
  [data-sale-badge]               sale badge

  Total products: second <span class="...fortschritt__numbers">
  Next page btn : <button data-load-more-url="/de/marken/SLUG/?page=N&mehr-laden=true">
"""
from __future__ import annotations

import argparse, csv, json, os, random, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from utils import load_config, log_err, log_head, log_info, log_ok, log_warn

load_dotenv()
CFG           = load_config()
OUT_DIR       = Path(CFG["output_dir"])
BRANDS_FILE   = Path(CFG["phase1_brands_file"])
PRODUCTS_CSV  = Path(CFG["phase2_products_csv"])
PROGRESS_FILE = Path(CFG["phase2_progress_file"])
DELAY         = float(CFG.get("phase2_delay", 3))
PAGE_SIZE     = int(CFG.get("phase2_page_size", 96))
MAX_RETRIES   = int(CFG.get("phase2_max_retries", 5))
RETRY_DELAY   = float(CFG.get("phase2_retry_delay", 5))
WORKERS       = int(CFG.get("phase2_workers", 3))
BASE_URL      = "https://www.breuninger.com"
TODAY         = date.today().isoformat()
FV            = CFG.get("fixed_values", {})
OUT_DIR.mkdir(parents=True, exist_ok=True)

ALL_COLUMNS = [
    "dbq_prd_type","website_name","competence_date","country_code","currency_code",
    "store_id","store_name","store_address","store_zip","store_url",
    "merchant_id","merchant_name","merchant_url","merchant_image","merchant_descr",
    "product_code","additional_code_1","additional_code_1_type",
    "additional_code_2","additional_code_2_type",
    "additional_code_3","additional_code_3_type",
    "size_info","brand","color_info","material_info","variant_info",
    "description","product_title","specifications","delivery","in_stock","quantity",
    "category1","category2","category3","category4","category5",
    "category6","category7","category8","category9","category10",
    "full_price","price","ppu","unit_type","promotion_type","promotion_end_date",
    "package_desc","additional_tags","additional_content",
    "itemurl","main_image_url","all_images","rank",
    "contract_id","seller_id","delivery_id",
]

# ── Proxy ──────────────────────────────────────────────────────────────────
RAW_PROXY      = os.getenv("ROTATING_PROXY", "").strip()
PROXY_LIST_RAW = os.getenv("PROXY_LIST", "").strip()

def _proxies():
    if RAW_PROXY:
        return {"http": RAW_PROXY, "https": RAW_PROXY}
    if PROXY_LIST_RAW:
        p = random.choice([x.strip() for x in PROXY_LIST_RAW.split(",") if x.strip()])
        return {"http": p, "https": p}
    return None

# ── HTTP ───────────────────────────────────────────────────────────────────
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124","Google Chrome";v="124","Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_jar      = requests.cookies.RequestsCookieJar()
_jar_lock = Lock()

def fetch(url: str, referer: str | None = None) -> str | None:
    hdrs = dict(_HEADERS)
    if referer:
        hdrs["Referer"] = referer
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with _jar_lock:
                jar = _jar.copy()
            r = requests.get(url, headers=hdrs, proxies=_proxies(), cookies=jar, timeout=30, allow_redirects=True)
            with _jar_lock:
                _jar.update(r.cookies)
            if r.status_code == 200:
                return r.text
            log_warn(f"HTTP {r.status_code} [{attempt}] {url}")
        except Exception as e:
            log_warn(f"Error [{attempt}] {e} — {url}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY * attempt + random.uniform(0, 2))
    return None

# ── Progress ───────────────────────────────────────────────────────────────
_prog: dict  = {}
_prog_lock   = Lock()

def _load_prog():
    global _prog
    if PROGRESS_FILE.exists():
        _prog = json.loads(PROGRESS_FILE.read_text("utf-8"))

def _is_done(k: str) -> bool:
    return _prog.get(k) == "done"

def _mark_done(k: str):
    with _prog_lock:
        _prog[k] = "done"
        PROGRESS_FILE.write_text(json.dumps(_prog, indent=2), "utf-8")

# ── CSV ────────────────────────────────────────────────────────────────────
_csv_lock = Lock()

def _write(rows: list[dict]):
    if not rows:
        return
    new = not PRODUCTS_CSV.exists()
    with _csv_lock, open(PRODUCTS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ALL_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)

# ── Helpers ────────────────────────────────────────────────────────────────
def _t(el) -> str:
    return re.sub(r"\s+", " ", el.get_text()).strip() if el else ""

def _price(raw: str) -> str:
    raw = raw.replace(".", "").replace(",", ".")
    m = re.search(r"\d+\.?\d*", raw)
    return m.group(0) if m else ""

def _cap(s: str, n=1000) -> str:
    return s[:n] if s else ""

def _row() -> dict:
    return {c: "" for c in ALL_COLUMNS}

def _fixed(row: dict, brand: dict):
    row.update({
        "dbq_prd_type":    FV.get("dbq_prd_type", "E0005r"),
        "website_name":    FV.get("website_name", "Breuninger"),
        "competence_date": TODAY,
        "country_code":    FV.get("country_code", "DEU"),
        "currency_code":   FV.get("currency_code", "EUR"),
        "store_url":       FV.get("store_url", BASE_URL),
        "contract_id":     FV.get("contract_id", ""),
        "seller_id":       FV.get("seller_id", ""),
        "merchant_name":   brand["name"],
        "merchant_url":    brand["url"],
    })
    if not row["brand"]:
        row["brand"] = brand["name"]

# ── Parser ─────────────────────────────────────────────────────────────────
def parse_cards(html: str, brand: dict) -> tuple[list[dict], str | None, int]:
    """Returns (rows, next_url, total_on_site)."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    cards = soup.find_all("suchen-produkt")
    if not cards:
        cards = soup.select("li.suchen-produktliste__item--produkt")

    for card in cards:
        row = _row()

        # IDs
        row["product_code"] = card.get("data-syan", "")
        row["rank"]         = card.get("data-product-index", "")

        # Link + color id
        link = card.find("a", attrs={"data-product-link": True})
        if link:
            row["itemurl"]                = urljoin(BASE_URL, link.get("href", ""))
            row["additional_code_1"]      = link.get("data-farb-id", "")
            row["additional_code_1_type"] = "color_id"
            row["additional_code_2"]      = row["product_code"]
            row["additional_code_2_type"] = "syan"

        # Brand / title
        row["brand"]         = _t(card.find(class_="suchen-produkt__marke")) or brand["name"]
        name                 = _t(card.find(class_="suchen-produkt__name__text"))
        sub                  = _t(card.find(class_="suchen-produkt__name__zusatz"))
        row["product_title"] = f"{name} {sub}".strip() if sub else name
        row["variant_info"]  = sub

        # Price — schwarzpreis=regular, rotpreis=sale
        bp = card.find(class_=re.compile(r"suchen-preis--schwarzpreis"))
        rp = card.find(class_=re.compile(r"suchen-preis--rotpreis"))
        if rp and bp:
            row["full_price"]     = _price(_t(bp))
            row["price"]          = _price(_t(rp))
            row["promotion_type"] = "sale"
        elif bp:
            row["price"] = row["full_price"] = _price(_t(bp))

        # Badges
        tags = []
        if card.find(attrs={"data-test-suchen-product-badge-neu": True}):
            tags.append("Neu")
        if card.find(attrs={"data-sale-badge": True}):
            tags.append("Sale")
        row["additional_tags"] = "|".join(tags)

        # Images + colors + sizes from color swatches
        # <a class="suchen-produkt-farbkachel" data-image-standard="URL" data-image-mouseover="URL"
        #    data-image-weitere-bilder-0="URL" ... data-sizes='["S = 48", ...]'>
        imgs:   list[str] = []
        colors: list[str] = []
        sizes_done = False

        # Main image from picture/img
        mi = card.find(attrs={"data-test-suchen-product-image": True})
        if not mi:
            pic = card.find("picture")
            mi  = pic.find("img") if pic else None
        if mi:
            src = mi.get("src", "")
            if src and not src.startswith("data:"):
                imgs.append(src)

        for sw in card.find_all(class_="suchen-produkt-farbkachel"):
            si = sw.find("img", class_="suchen-produkt-farbkachel__bild")
            if si:
                alt = re.sub(r"\s+", " ", si.get("alt", "")).strip()
                if alt and alt not in colors:
                    colors.append(alt)
            # Collect only non-path, non-retina jpg URLs
            for attr, val in sw.attrs.items():
                if (attr.startswith("data-image-")
                        and not attr.endswith(("-path", "-retina"))
                        and isinstance(val, str)
                        and val.startswith("http")
                        and val not in imgs):
                    imgs.append(val)
            if not sizes_done:
                raw = sw.get("data-sizes", "")
                if raw:
                    try:
                        sz = json.loads(raw)
                        if sz:
                            row["size_info"] = _cap("|".join(str(s) for s in sz))
                            sizes_done = True
                    except Exception:
                        pass

        # Fallback sizes from visible list
        if not row["size_info"]:
            ul = card.find("ul", class_="suchen-produkt__sizes")
            if ul:
                sz = [_t(li) for li in ul.find_all("li") if _t(li)]
                row["size_info"] = _cap("|".join(sz))

        row["color_info"]     = _cap("|".join(colors))
        row["all_images"]     = _cap("|".join(imgs))
        row["main_image_url"] = imgs[0] if imgs else ""
        row["in_stock"]       = "1" if row["size_info"] else ""

        _fixed(row, brand)
        rows.append(row)

    # Pagination
    next_url = None
    btn = soup.find("button", attrs={"data-load-more-url": True})
    if btn:
        raw = btn.get("data-load-more-url", "")
        if raw:
            next_url = urljoin(BASE_URL, raw)

    # Total product count (second fortschritt__numbers span)
    total = 0
    nums = soup.find_all(class_=re.compile(r"fortschritt__numbers"))
    if len(nums) >= 2:
        try:
            total = int(re.sub(r"\D", "", _t(nums[1])))
        except ValueError:
            pass

    return rows, next_url, total

# ── Brand worker ───────────────────────────────────────────────────────────
def scrape_brand(brand: dict, brand_idx: int, brand_total: int) -> int:
    slug     = brand["slug"]
    base_url = brand["url"]
    scraped  = 0
    page     = 1
    next_url: str | None = base_url

    while next_url:
        url = next_url
        if _is_done(url):
            html = fetch(url, referer=base_url)
            if html:
                _, next_url, _ = parse_cards(html, brand)
            else:
                break
            page += 1
            continue

        html = fetch(url, referer=base_url if page > 1 else BASE_URL)
        if html is None:
            log_warn(f"[{brand['name']}] p{page} failed, stopping.")
            break

        rows, next_url, site_total = parse_cards(html, brand)
        if rows:
            _write(rows)
            scraped += len(rows)

        _mark_done(url)
        page += 1
        if next_url:
            time.sleep(DELAY + random.uniform(0, 1.5))

    status = f"{'✔' if scraped else '○'} [{brand_idx}/{brand_total}] {brand['name']} — {scraped} products"
    print(status)
    return scraped

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh",   action="store_true")
    ap.add_argument("--brand",   help="Only scrape this slug")
    ap.add_argument("--workers", type=int)
    args = ap.parse_args()

    log_head("Phase 2 — Breuninger Product Scraper")

    if not BRANDS_FILE.exists():
        log_err(f"Run phase1 first — {BRANDS_FILE} missing")
        return

    brands: list[dict] = json.loads(BRANDS_FILE.read_text("utf-8"))
    if args.brand:
        brands = [b for b in brands if b["slug"] == args.brand]
        if not brands:
            log_err(f"Brand not found: {args.brand}")
            return

    if not args.fresh:
        _load_prog()

    workers = args.workers or WORKERS
    log_info(f"{len(brands)} brands | {workers} workers | delay {DELAY}s")

    grand_total = 0

    if workers == 1:
        for i, b in enumerate(brands, 1):
            grand_total += scrape_brand(b, i, len(brands))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(scrape_brand, b, i, len(brands)): b
                    for i, b in enumerate(brands, 1)}
            for fut in as_completed(futs):
                try:
                    grand_total += fut.result()
                except Exception as e:
                    log_err(f"{futs[fut]['name']}: {e}")

    log_ok(f"Done. {grand_total} products → {PRODUCTS_CSV}")


if __name__ == "__main__":
    main()