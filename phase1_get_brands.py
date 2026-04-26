"""
Phase 1 — Scrape all brands from https://www.breuninger.com/de/marken/
Saves output/brands.json  →  [{ name, slug, url }, ...]
Uses plain requests (no session) + optional proxy from .env
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils import log_err, log_head, log_info, log_ok, log_warn, load_config

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────
CFG           = load_config()
OUT_DIR       = Path(CFG["output_dir"])
BRANDS_FILE   = Path(CFG["phase1_brands_file"])
DELAY         = float(CFG.get("phase1_delay", 2.0))
BASE_URL      = "https://www.breuninger.com"
BRANDS_URL    = f"{BASE_URL}/de/marken/"

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Proxy ────────────────────────────────────────────────────────────────
RAW_PROXY = os.getenv("ROTATING_PROXY", "").strip()
PROXY_LIST_RAW = os.getenv("PROXY_LIST", "").strip()

def get_proxies() -> dict | None:
    if RAW_PROXY:
        return {"http": RAW_PROXY, "https": RAW_PROXY}
    if PROXY_LIST_RAW:
        proxies = [p.strip() for p in PROXY_LIST_RAW.split(",") if p.strip()]
        chosen = random.choice(proxies)
        return {"http": chosen, "https": chosen}
    return None


# ── HTTP helpers ─────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# Shared cookie jar — carries session cookies across calls
_cookie_jar = requests.cookies.RequestsCookieJar()

def fetch(url: str, retries: int = 4) -> requests.Response | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                url,
                headers=HEADERS,
                proxies=get_proxies(),
                cookies=_cookie_jar,
                timeout=30,
                allow_redirects=True,
            )
            _cookie_jar.update(r.cookies)
            if r.status_code == 200:
                return r
            log_warn(f"[{attempt}] HTTP {r.status_code} for {url}")
        except Exception as exc:
            log_warn(f"[{attempt}] Error fetching {url}: {exc}")
        if attempt < retries:
            wait = DELAY * attempt + random.uniform(0, 2)
            time.sleep(wait)
    return None


# ── Parsers ──────────────────────────────────────────────────────────────
def parse_brands(html: str) -> list[dict]:
    """
    Breuninger /de/marken/ lists brands alphabetically in anchor tags.
    The structure is:  <a href="/de/marken/SLUG/">Brand Name</a>
    """
    soup = BeautifulSoup(html, "html.parser")
    brands: list[dict] = []
    seen: set[str] = set()

    # Strategy 1: look for links that match /de/marken/<slug>/
    pattern = re.compile(r"^/de/marken/([^/]+)/?$")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # Normalise relative → absolute
        if href.startswith("/"):
            full_url = BASE_URL + href
        elif href.startswith(BASE_URL):
            full_url = href
        else:
            continue

        m = pattern.match(href)
        if not m:
            # also try the absolute form
            rel = href.replace(BASE_URL, "")
            m = pattern.match(rel)
            if not m:
                continue

        slug = m.group(1)
        if slug in seen:
            continue

        # Brand name — prefer text content; fall back to slug
        name = a.get_text(strip=True) or slug.replace("-", " ").title()
        if not name:
            continue

        brands.append({
            "name": name,
            "slug": slug,
            "url": f"{BASE_URL}/de/marken/{slug}/",
        })
        seen.add(slug)

    return brands


def slug_from_url(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else ""


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    log_head("Phase 1 — Breuninger Brand Scraper")
    log_info(f"Fetching brand directory: {BRANDS_URL}")

    r = fetch(BRANDS_URL)
    if r is None:
        log_err("Failed to fetch brands page. Check connectivity / proxy.")
        return

    brands = parse_brands(r.text)

    if not brands:
        log_warn("No brands found via link pattern — trying JSON-LD / script tags …")
        # Fallback: look for JSON inside <script> blocks
        soup = BeautifulSoup(r.text, "html.parser")
        for script in soup.find_all("script"):
            text = script.get_text()
            if '"brands"' in text or '"marken"' in text:
                try:
                    data = json.loads(text)
                    # try to find a list of brand objects
                    for v in data.values() if isinstance(data, dict) else [data]:
                        if isinstance(v, list) and v and "url" in str(v[0]):
                            for item in v:
                                url = item.get("url", "")
                                slug = slug_from_url(url)
                                if slug:
                                    brands.append({
                                        "name": item.get("name", slug),
                                        "slug": slug,
                                        "url": url if url.startswith("http") else BASE_URL + url,
                                    })
                except Exception:
                    pass

    if not brands:
        log_err("Could not find any brands. The site structure may have changed.")
        log_info("Saving raw HTML to output/brands_debug.html for inspection.")
        Path("output/brands_debug.html").write_text(r.text, encoding="utf-8")
        return

    # Deduplicate by slug
    seen: set[str] = set()
    unique = []
    for b in brands:
        if b["slug"] not in seen:
            unique.append(b)
            seen.add(b["slug"])

    BRANDS_FILE.write_text(json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8")
    log_ok(f"Saved {len(unique)} brands → {BRANDS_FILE}")


if __name__ == "__main__":
    main()
