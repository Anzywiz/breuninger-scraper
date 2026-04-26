"""
convert_to_txt.py
─────────────────
Reads a product CSV, applies DBQ upload rules, and writes a
semicolon-delimited .txt file (all fields quoted, no header row).

Rules applied:
  • all_images capped at 1000 characters
  • all string columns capped at 1000 characters
  • numeric price fields sanitised (max 99 999.99)
  • competence_date set to latest date in file
  • promotion_end_date formatted to YYYY-MM-DD
  • fixed_values from config.json stamped on every row
  • rank column dropped when dbq_prd_type == E0005
  • Exact duplicates on product_code removed (keep last)

Outputs:
  • data_file.txt          — DBQ upload file
  • brand_counts.csv       — per-brand product counts
  • fill_rate_report.csv   — per-column fill rates (non-constant cols only)

Usage:
    python convert_to_txt.py                        # uses config.json defaults
    python convert_to_txt.py --input my.csv         # override input file
    python convert_to_txt.py --input my.csv --output out/upload.txt
"""

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

import pandas as pd

# ── Coloured logging ──────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
COLOURS = {
    logging.DEBUG:    "\033[36m",   # cyan
    logging.INFO:     "\033[32m",   # green
    logging.WARNING:  "\033[33m",   # yellow
    logging.ERROR:    "\033[31m",   # red
    logging.CRITICAL: "\033[35m",   # magenta
}

class ColourFormatter(logging.Formatter):
    def format(self, record):
        colour = COLOURS.get(record.levelno, RESET)
        record.levelname = f"{colour}{BOLD}{record.levelname:<7}{RESET}"
        record.msg       = f"{colour}{record.msg}{RESET}"
        return super().format(record)

def _setup_logger(name: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColourFormatter(
        fmt="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    return logger

log = _setup_logger("convert_to_txt")

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG = "config.json"

NUMERIC_COLS = {"full_price", "price", "ppu", "rank", "in_stock", "quantity", "delivery_id"}
PRICE_COLS   = ["full_price", "price", "ppu"]
MAX_PRICE    = 99_999.99

# Columns that are constant per upload — excluded from fill-rate analysis
CONSTANT_COLS = {
    "dbq_prd_type", "website_name", "competence_date",
    "country_code", "currency_code",
    "contract_id", "seller_id", "delivery_id",
}

ALL_COLUMNS = [
    "dbq_prd_type", "website_name", "competence_date", "country_code", "currency_code",
    "store_id", "store_name", "store_address", "store_zip", "store_url",
    "merchant_id", "merchant_name", "merchant_url", "merchant_image", "merchant_descr",
    "product_code", "additional_code_1", "additional_code_1_type",
    "additional_code_2", "additional_code_2_type",
    "additional_code_3", "additional_code_3_type",
    "size_info", "brand", "color_info", "material_info", "variant_info",
    "description", "product_title", "specifications", "delivery", "in_stock", "quantity",
    "category1", "category2", "category3", "category4", "category5",
    "category6", "category7", "category8", "category9", "category10",
    "full_price", "price", "ppu", "unit_type", "promotion_type", "promotion_end_date",
    "package_desc", "additional_tags", "additional_content",
    "itemurl", "main_image_url", "all_images", "rank",
    "contract_id", "seller_id", "delivery_id",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    if not Path(path).exists():
        log.warning(f"Config not found at '{path}' — using empty config.")
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cap_images(val, max_len=1000):
    """Keep as many pipe-separated image URLs as fit within max_len chars."""
    if not val or pd.isna(val):
        return None
    urls = re.findall(r"https?://[^\s\",|]+", str(val))
    result = ""
    for url in urls:
        candidate = result + ("|" if result else "") + url
        if len(candidate) > max_len:
            break
        result = candidate
    return result or None


def fill_rate(series: pd.Series) -> float:
    """Fraction of non-empty, non-null values."""
    filled = series.replace("", pd.NA).dropna()
    return len(filled) / len(series) if len(series) else 0.0


def section(title: str):
    log.info(f"── {title} {'─' * max(0, 55 - len(title))}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Convert product CSV to DBQ upload txt.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.json")
    parser.add_argument("--input",  help="Override input CSV path")
    parser.add_argument("--output", help="Override output TXT path")
    args = parser.parse_args()

    cfg = load_config(args.config)

    in_csv  = Path(args.input  or cfg.get("input_csv",  "output/products_enriched.csv"))
    out_txt = Path(args.output or cfg.get("phase4_output_txt", "output/data_file.txt"))
    out_dir = out_txt.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    fixed_values  = cfg.get("fixed_values", {})
    dbq_prd_type  = fixed_values.get("dbq_prd_type", cfg.get("dbq_prd_type", ""))

    # ── Load ──────────────────────────────────────────────────────────────────
    section("Loading data")
    if not in_csv.exists():
        log.error(f"Input file not found: {in_csv}")
        sys.exit(1)

    df = pd.read_csv(in_csv, dtype=str, keep_default_na=False)
    log.info(f"  Loaded  {len(df):,} rows × {len(df.columns)} columns  ←  {in_csv}")

    # ── Image URL cap ─────────────────────────────────────────────────────────
    section("Capping image URLs")
    if "all_images" in df.columns:
        df["all_images"] = df["all_images"].apply(cap_images)
        log.info("  all_images capped to 1 000 chars")

    # ── Cap string columns ────────────────────────────────────────────────────
    section("Capping string columns")
    for col in df.columns:
        if col not in NUMERIC_COLS and col != "all_images":
            df[col] = df[col].astype(str).str[:1000]
    log.info("  All string columns capped at 1 000 chars")

    # ── Sanitise price columns ────────────────────────────────────────────────
    section("Sanitising price columns")
    for col in PRICE_COLS:
        if col not in df.columns:
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        bad = int((numeric > MAX_PRICE).sum())
        if bad:
            log.warning(f"  {col}: {bad:,} value(s) exceed {MAX_PRICE:,.2f} → nulled")
        df[col] = numeric.where(numeric <= MAX_PRICE).round(2).astype(str).replace("nan", "")
    log.info("  Price columns sanitised")

    # ── Competence date ───────────────────────────────────────────────────────
    section("Setting competence date")
    if "competence_date" in df.columns:
        df["competence_date"] = pd.to_datetime(df["competence_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        latest = df["competence_date"].dropna().max()
        df["competence_date"] = latest
        log.info(f"  competence_date = {latest}")

    # ── Promotion end date ─────────────────────────────────────────────────────
    section("Formatting promotion end date")
    if "promotion_end_date" in df.columns:
        df["promotion_end_date"] = pd.to_datetime(df["promotion_end_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    # ── Stamp fixed values ────────────────────────────────────────────────────
    section("Stamping fixed values")
    for key, val in fixed_values.items():
        if key in df.columns:
            df[key] = val
    log.info(f"  Applied: {list(fixed_values.keys()) or '(none)'}")

    # ── Drop rank for E0005 ───────────────────────────────────────────────────
    if dbq_prd_type == "E0005" and "rank" in df.columns:
        df.drop(columns=["rank"], inplace=True)
        log.info("  dbq_prd_type=E0005 → rank column dropped")

    # ── Deduplicate ───────────────────────────────────────────────────────────
    section("Deduplicating")
    before = len(df)
    if "product_code" in df.columns:
        df.drop_duplicates(subset=["product_code", "brand", "itemurl"], keep="last", inplace=True)
    removed = before - len(df)
    log.info(f"  {before:,} → {len(df):,} rows  ({removed:,} duplicates removed)")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Summary")
    unique_brands = df["brand"].nunique() if "brand" in df.columns else "n/a"
    log.info(f"  Total rows      : {len(df):,}")
    log.info(f"  Unique brands   : {unique_brands:,}" if isinstance(unique_brands, int) else f"  Unique brands   : {unique_brands}")
    if "product_code" in df.columns:
        log.info(f"  Unique prod codes: {df['product_code'].nunique():,}")

    # ── Brand counts ──────────────────────────────────────────────────────────
    section("Brand counts")
    if "brand" in df.columns:
        brand_counts = (
            df.drop_duplicates(subset=["product_code"])
            .groupby("brand")["product_code"].count()
            .reset_index(name="product_count")
            .sort_values("product_count", ascending=False)
        )
        counts_path = out_dir / "brand_counts.csv"
        brand_counts.to_csv(counts_path, index=False, encoding="utf-8")

        n          = len(brand_counts)
        max_count  = brand_counts["product_count"].max() or 1

        def print_brands(rows, label):
            log.info(f"  {label}:")
            for _, row in rows.iterrows():
                bar = "█" * min(int(row["product_count"] / max_count * 20), 20)
                print(f"    {row['brand'][:35]:35s}  {row['product_count']:>6}  {bar}")

        mid = max(0, n // 2 - 2)
        print_brands(brand_counts.head(5),             "Top 5 brands")
        print_brands(brand_counts.iloc[mid:mid + 5],   "Mid brands")
        print_brands(brand_counts.tail(5),             "Bottom 5 brands")
        log.info(f"  brand_counts.csv saved  ({n} brands)  →  {counts_path}")

    # ── Fill rate report ──────────────────────────────────────────────────────
    section("Fill rate report")
    analyse_cols = [c for c in ALL_COLUMNS if c in df.columns and c not in CONSTANT_COLS and c != "rank"]
    fill_rows = []
    print(f"\n  {'Column':<30}  {'Fill rate':>10}  {'Filled':>8}  {'Total':>8}")
    print(f"  {'─'*30}  {'─'*10}  {'─'*8}  {'─'*8}")
    for col in analyse_cols:
        rate    = fill_rate(df[col])
        filled  = int(df[col].replace("", pd.NA).notna().sum())
        total   = len(df)
        pct_str = f"{rate * 100:.1f}%"

        if rate >= 0.95:
            colour, flag = "\033[32m", ""       # green
        elif rate >= 0.5:
            colour, flag = "\033[33m", "  ⚠"   # yellow
        else:
            colour, flag = "\033[31m", "  ✗"   # red

        if rate > 0:  # 0% columns hidden from table — still written to CSV
            print(f"  {colour}{col:<30}  {pct_str:>10}  {filled:>8,}  {total:>8,}{flag}{RESET}")
        fill_rows.append({"column": col, "fill_rate_pct": round(rate * 100, 2),
                          "filled": filled, "total": total})

    fill_path = out_dir / "fill_rate_report.csv"
    pd.DataFrame(fill_rows).to_csv(fill_path, index=False, encoding="utf-8")
    log.info(f"\n  fill_rate_report.csv saved  →  {fill_path}")

    # ── Write output ──────────────────────────────────────────────────────────
    section("Writing output")
    df.to_csv(out_txt, sep=";", index=False, header=False,
              encoding="utf-8", quoting=csv.QUOTE_ALL)
    log.info(f"  ✓  {len(df):,} rows  →  {out_txt}")


if __name__ == "__main__":
    main()
    