"""
Phase 3 — Enrich each product by visiting its detail page (PDP).

Real PDP HTML selectors (verified from live snippets):

  Brand/title:
    h1 or .ents-product-summary__heading      brand link
    .ents-product-summary__name               product name
    .ents-product-summary__namesub            sub-name
    [data-ents-colors-tracking-json-content]  tracking JSON with categories, color, department, group[]

  Price:
    [data-test-ents-regular-price]            regular price
    .bdk-badge-price                          same

  Color:
    .ents-colors__active span.ents-copy-small-bold  active color name

  Sizes (from size layer):
    label[data-test-ents-size-item] input[value]    size values
    brtn-size-layer-inline-recommendation[in-stock]  stock per size

  Stock:
    .ents-stock-information__availability  text: "Sofort lieferbar" / "Ausverkauft"

  Images (from ents-image-view__entry):
    .ents-image-view__entry img[src]         all gallery images (highest res jpg)
    ents-zoomable-image[zoom-src]            full-res webp (use as main if present)

  Description bullets:
    [data-test-ents-description-details] ul li   description bullet points

  Webcode:
    [data-test-ents-description-webcode]   "Web-Code: 3077475"

  Fit / dimensions:
    brtn-product-details-accordion   contains model-dimension slot text
    .dimension li                    "Rückenlänge: 74 cm" etc.

  Material:
    [data-test-ents-accordion-material-grooming-content] p   e.g. "100% polyester"

  Care:
    .ents-product-detail-accordion__care-instructions li div.ents-copy   care text

  Delivery:
    [data-test-ents-standard-delivery-message] span  "DHL Standardversand für €X"
    .ents-stock-information__availability            "Sofort lieferbar"

  Categories (breadcrumb):
    [data-test-suchen-breadcrumb-item-text]   breadcrumb texts in order
"""
from __future__ import annotations

import argparse, csv, json, os, random, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
PRODUCTS_CSV  = Path(CFG["phase2_products_csv"])
ENRICHED_CSV  = Path(CFG.get("phase3_enriched_csv", "output/products_enriched.csv"))
PROGRESS_FILE = Path(CFG.get("phase3_progress_file", "output/phase3_progress.json"))
DELAY         = float(CFG.get("phase3_delay", 2))
MAX_RETRIES   = int(CFG.get("phase3_max_retries", 4))
WORKERS       = int(CFG.get("phase3_workers", 3))
BASE_URL      = "https://www.breuninger.com"
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

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
_jar      = requests.cookies.RequestsCookieJar()
_jar_lock = Lock()

def fetch(url: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with _jar_lock:
                jar = _jar.copy()
            r = requests.get(url, headers={**_HEADERS, "Referer": BASE_URL},
                             proxies=_proxies(), cookies=jar, timeout=30, allow_redirects=True)
            with _jar_lock:
                _jar.update(r.cookies)
            if r.status_code == 200:
                return r.text
            log_warn(f"HTTP {r.status_code} [{attempt}] {url}")
        except Exception as e:
            log_warn(f"Error [{attempt}] {e}")
        if attempt < MAX_RETRIES:
            time.sleep(2 * attempt + random.uniform(0, 2))
    return None

# ── Progress / CSV ─────────────────────────────────────────────────────────
_prog: dict  = {}
_prog_lock   = Lock()
_csv_lock    = Lock()

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

def _write(row: dict):
    new = not ENRICHED_CSV.exists()
    with _csv_lock, open(ENRICHED_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ALL_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)

# ── Helpers ────────────────────────────────────────────────────────────────
def _t(el) -> str:
    return re.sub(r"\s+", " ", el.get_text()).strip() if el else ""

def _price(raw: str) -> str:
    raw = raw.replace("\xa0", "").replace(".", "").replace(",", ".")
    m = re.search(r"\d+\.?\d*", raw)
    return m.group(0) if m else ""

def _cap(s, n=1000) -> str:
    return str(s)[:n] if s else ""

# ── PDP enricher ───────────────────────────────────────────────────────────
def enrich(base: dict) -> dict:
    url = base.get("itemurl", "")
    if not url:
        return base

    html = fetch(url)
    if not html:
        return base

    soup = BeautifulSoup(html, "html.parser")
    row  = dict(base)

    # ── Tracking JSON (richest source — has categories, department, color, price) ─
    tracking_el = soup.find(attrs={"data-ents-colors-tracking-json-content": True})
    if tracking_el:
        try:
            tracking = json.loads(tracking_el["data-ents-colors-tracking-json-content"])
            # tracking is {COLOR_ID: {products: [...], event: "product.view"}}
            for color_data in tracking.values():
                for prod in color_data.get("products", []):
                    # Categories from group[] array
                    group = prod.get("group", [])
                    for i, g in enumerate(group[:10], 1):
                        if g and not row.get(f"category{i}"):
                            row[f"category{i}"] = g
                    # Department → category if not set
                    dept = prod.get("department", "")
                    if dept and not row.get("category1"):
                        row["category1"] = dept
                    # Target group
                    tg = prod.get("target_group", "")
                    if tg and not row.get("additional_content"):
                        row["additional_content"] = tg
                    # Color
                    color_arr = prod.get("color", [])
                    if color_arr and not row.get("color_info"):
                        row["color_info"] = color_arr[1] if len(color_arr) > 1 else color_arr[0]
                    # Price (stored as cents int, e.g. 11000 = 110.00€)
                    if not row.get("price"):
                        p = prod.get("price", 0)
                        if p:
                            row["price"] = f"{p / 100:.2f}"
                    if not row.get("full_price"):
                        p = prod.get("price_original", 0)
                        if p:
                            row["full_price"] = f"{p / 100:.2f}"
                    # Promo
                    if prod.get("sale") and not row.get("promotion_type"):
                        row["promotion_type"] = "sale"
                    break  # first product is enough
                break
        except Exception:
            pass

    # ── Title / brand ──────────────────────────────────────────────────────
    if not row.get("product_title"):
        name = _t(soup.find(class_="ents-product-summary__name"))
        sub  = _t(soup.find(class_="ents-product-summary__namesub"))
        row["product_title"] = f"{name} {sub}".strip() if sub else name
    if not row.get("brand"):
        brand_link = soup.find("a", class_=re.compile(r"bdk-link-brand"))
        row["brand"] = _t(brand_link).strip() if brand_link else ""

    # ── Price ──────────────────────────────────────────────────────────────
    if not row.get("price"):
        pe = soup.find(attrs={"data-test-ents-regular-price": True}) \
          or soup.find(class_="bdk-badge-price")
        if pe:
            row["price"] = _price(_t(pe))
    if not row.get("full_price"):
        row["full_price"] = row.get("price", "")

    # ── Color ──────────────────────────────────────────────────────────────
    if not row.get("color_info"):
        ce = soup.find(class_="ents-colors__active")
        if ce:
            bold = ce.find(class_=re.compile(r"copy-small-bold"))
            row["color_info"] = _t(bold) if bold else _t(ce)

    # ── Sizes + per-size stock ─────────────────────────────────────────────
    if not row.get("size_info"):
        size_items = soup.find_all(attrs={"data-test-ents-size-item": True})
        sizes = []
        oos   = []
        for si in size_items:
            inp = si.find("input")
            val = inp.get("value", "") if inp else ""
            if not val:
                val = _t(si.find(class_="ents-size-information"))
            if val:
                sizes.append(val)
            rec = si.find("brtn-size-layer-inline-recommendation")
            if rec and rec.get("in-stock", "").lower() == "false":
                oos.append(val)
        if sizes:
            row["size_info"] = _cap("|".join(sizes))

    # ── Stock ──────────────────────────────────────────────────────────────
    if not row.get("in_stock"):
        avail = soup.find(class_="ents-stock-information__availability")
        if avail:
            txt = _t(avail).lower()
            row["in_stock"] = "0" if "ausverkauft" in txt or "sold out" in txt else "1"
        else:
            row["in_stock"] = "1" if row.get("size_info") else ""

    # ── Description bullets ────────────────────────────────────────────────
    if not row.get("description"):
        desc_el = soup.find(attrs={"data-test-ents-description-details": True})
        if desc_el:
            bullets = [_t(li) for li in desc_el.find_all("li") if _t(li)]
            row["description"] = _cap("|".join(bullets))

    # ── Webcode → additional_code_3 ────────────────────────────────────────
    if not row.get("additional_code_3"):
        wc_el = soup.find(attrs={"data-test-ents-description-webcode": True})
        if wc_el:
            wc = re.search(r"\d{5,}", _t(wc_el))
            if wc:
                row["additional_code_3"]      = wc.group(0)
                row["additional_code_3_type"] = "webcode"

    # ── Material ───────────────────────────────────────────────────────────
    if not row.get("material_info"):
        mat_el = soup.find(attrs={"data-test-ents-accordion-material-grooming-content": True})
        if mat_el:
            row["material_info"] = _cap(_t(mat_el))

    # ── Care instructions ──────────────────────────────────────────────────
    if not row.get("specifications"):
        care_list = soup.select(".ents-product-detail-accordion__care-instructions li div.ents-copy")
        if care_list:
            care = [_t(c) for c in care_list if _t(c)]
            row["specifications"] = _cap("|".join(care))

    # ── Fit / dimensions ───────────────────────────────────────────────────
    if not row.get("package_desc"):
        # <div class="dimension"> inside brtn-product-details-accordion shadow
        # The slot content is rendered server-side in the <template> tag
        dim_el = soup.find(class_="dimension")
        if not dim_el:
            # Fallback: look inside <brtn-product-details-accordion> for dimension text
            brtn = soup.find("brtn-product-details-accordion")
            if brtn:
                tmpl = brtn.find("template")
                if tmpl:
                    inner = BeautifulSoup(str(tmpl), "html.parser")
                    dim_el = inner.find(class_="dimension")
        if dim_el:
            row["package_desc"] = _cap(_t(dim_el))

    # ── Delivery info ──────────────────────────────────────────────────────
    if not row.get("delivery"):
        del_msgs = soup.find_all(attrs={"data-test-ents-standard-delivery-message": True})
        if del_msgs:
            row["delivery"] = _cap(" | ".join(_t(d) for d in del_msgs if _t(d)))

    # ── Images (gallery) ──────────────────────────────────────────────────
    if not row.get("all_images"):
        imgs = []
        # ents-image-view__entry contains the gallery slides
        for entry in soup.select(".ents-image-view__entry"):
            img = entry.find("img", attrs={"data-test-ents-image": True})
            if not img:
                img = entry.find("img")
            if img:
                src = img.get("src", "")
                if src and not src.startswith("data:") and src not in imgs:
                    imgs.append(src)
        if imgs:
            row["all_images"]     = _cap("|".join(imgs))
            row["main_image_url"] = row.get("main_image_url") or imgs[0]

    # ── Breadcrumb categories (fallback if tracking JSON had none) ─────────
    if not row.get("category1"):
        crumbs = soup.find_all(attrs={"data-test-suchen-breadcrumb-item-text": True})
        for i, c in enumerate(crumbs[:10], 1):
            row[f"category{i}"] = _t(c)

    return row

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh",   action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--limit",   type=int)
    args = ap.parse_args()

    log_head("Phase 3 — Breuninger PDP Enricher")

    if not PRODUCTS_CSV.exists():
        log_err(f"Run phase2 first — {PRODUCTS_CSV} missing")
        return

    with open(PRODUCTS_CSV, newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Deduplicate by itemurl
    seen: set[str] = set()
    unique = []
    for r in all_rows:
        u = r.get("itemurl", "").strip()
        if u and u not in seen:
            seen.add(u)
            unique.append(r)

    log_info(f"{len(all_rows)} total rows → {len(unique)} unique URLs")

    if not args.fresh:
        _load_prog()

    todo = [r for r in unique if not _is_done(r.get("itemurl", ""))]
    if args.limit:
        todo = todo[:args.limit]

    log_info(f"{len(todo)} to enrich | {len(unique) - len(todo)} already done")

    workers  = args.workers or WORKERS
    done_n   = [0]
    total    = len(todo)
    lock     = Lock()

    def worker(row: dict):
        enriched = enrich(row)
        _mark_done(row.get("itemurl", ""))
        _write(enriched)
        with lock:
            done_n[0] += 1
            title = enriched.get("product_title", "")[:40]
            print(f"  [{done_n[0]}/{total}] {title}", end="\r")
        time.sleep(DELAY + random.uniform(0, 1))

    if workers == 1:
        for r in todo:
            worker(r)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(worker, r) for r in todo]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    log_err(f"Worker error: {e}")

    print()
    log_ok(f"Done. {done_n[0]} enriched → {ENRICHED_CSV}")


if __name__ == "__main__":
    main()