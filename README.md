# Breuninger Full-Catalogue Scraper · DEU · E0005r

> End-to-end Python pipeline that collects every product listing from [Breuninger.com](https://www.breuninger.com) across all brands, normalises the data to the **DataBoutique E0005r schema**, and delivers the dataset to an S3 bucket for purchase and download.

**Live dataset:** [Breuninger.com — Full — DEU — E0005r](https://anzywiz.github.io/)  
**Delivery path:** `s3://databoutique.com/sellers/.../YYYY-MM-DD/data_file.txt`  
**Schema:** E0005r · **Country:** DEU · **Currency:** EUR  

---

## What it does

Breuninger is one of Germany's largest premium department stores, stocking 1,500+ brands across fashion, beauty, sport, and lifestyle. This scraper:

1. Discovers every brand listed on the `/de/marken/` directory
2. Paginates through each brand's full product catalogue (up to 2,600+ products per brand)
3. Enriches each product with full detail-page data — material, care, sizes, gallery images, categories, delivery info
4. Deduplicates, cleans, and converts the dataset to a tab-separated `data_file.txt` conforming to the DataBoutique E0005r format
5. Validates the output and uploads it to S3 via `boto3`; any schema violations are surfaced before delivery

---

## Pipeline overview

```
phase1_get_brands.py        →   output/brands.json
phase2_scrape_products.py   →   output/products.csv
phase3_enrich.py            →   output/products_enriched.csv
phase4_to_txt.py            →   output/data_file.txt
phase5_dbq_upload.py        →   s3://databoutique.com/.../data_file.txt
```

Run the full pipeline in one command:

```bash
python main.py --all
```

Or run individual phases:

```bash
python main.py --phase1
python main.py --phase2 --workers 5
python main.py --phase3
python main.py --phase4
python main.py --phase5
```

---

## Phase details

### Phase 1 — Brand discovery
Fetches `https://www.breuninger.com/de/marken/` and extracts every brand's name, slug, and URL from the alphabetical brand directory. Saves to `output/brands.json`.

### Phase 2 — Product catalogue scraping
For each brand, paginates through all listing pages using Breuninger's internal API:

```
Page 1:  /de/marken/{slug}/
Page N:  /de/marken/{slug}/?page=N&mehr-laden=true
```

Parses each `<suchen-produkt>` web component to extract: product ID (SYAN), color variant ID, title, brand, price (regular and sale), badges, main image, all gallery images, available sizes, and color options.

- **Concurrency:** configurable worker threads (`--workers N` or `config.json`)
- **Resume:** every scraped page URL is checkpointed to `output/phase2_progress.json`; a restart picks up exactly where it left off and skips completed brands with a single `SKIP` log line
- **Proxies:** rotating or list-based proxies loaded from `.env`

### Phase 3 — Product detail enrichment
Visits each unique product URL and extracts the full detail page:

- Full description bullet points
- Material composition (e.g. `100% polyester`, `Leder`)
- Care instructions
- Available sizes with per-size stock status
- Complete image gallery (up to 2244×3072px)
- Breadcrumb categories and department
- Delivery options and pricing
- Dimensions and fit info
- Webcode → stored as `additional_code_3`
- Tracking JSON embedded in the page (source of truth for categories, price in cents, sale status)

Deduplicates by `itemurl` before processing so each product is only visited once regardless of how many brand pages it appeared on.

### Phase 4 — Format conversion
Reads `products_enriched.csv`, deduplicates again, sanitises all fields (price validation, field length caps, pipe-delimited image lists), sets `competence_date` to today, and writes a tab-separated `data_file.txt` in the E0005r schema.

### Phase 5 — Validation and S3 upload
Validates the output file against the DataBoutique E0005r schema requirements. If validation passes, uploads to:

```
s3://databoutique.com/sellers/{SELLER_ID}/{CONTRACT_ID}/YYYY-MM-DD/data_file.txt
```

Logs success with a delivery confirmation. If any validator errors are found, they are printed with the specific field and row so they can be fixed before re-uploading. No partial or invalid files are sent.

---

## Tech stack

| Concern | Library |
|---|---|
| HTTP requests | `requests` (plain, no session) |
| HTML parsing | `BeautifulSoup4` + `lxml` |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` |
| Data processing | `pandas` |
| S3 upload | `boto3` |
| Config / secrets | `python-dotenv` |

---

## Project structure

```
.
├── main.py                   # Orchestrator — runs any combination of phases
├── phase1_get_brands.py      # Brand directory scraper
├── phase2_scrape_products.py # Brand catalogue scraper with pagination + resume
├── phase3_enrich.py          # Product detail page enricher
├── phase4_to_txt.py          # E0005r format converter
├── phase5_dbq_upload.py      # Validator + S3 uploader
├── utils.py                  # Shared logging, config loader
├── config.json.example       # Config template (copy to config.json)
├── .env.example              # Secrets template (copy to .env)
├── requirements.txt
└── output/                   # Generated at runtime, gitignored
    ├── brands.json
    ├── products.csv
    ├── products_enriched.csv
    ├── data_file.txt
    ├── phase2_progress.json
    └── phase3_progress.json
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Anzywiz/breuninger-scraper.git
cd breuninger-scraper
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config.json.example config.json
cp .env.example .env
```

Edit `config.json` to set worker counts, delays, and S3 / DataBoutique credentials:

```json
{
  "phase2_workers": 3,
  "phase2_delay": 3,
  "phase3_workers": 3,
  "phase3_delay": 2,
  "fixed_values": {
    "contract_id": "YOUR_CONTRACT_ID",
    "seller_id":   "YOUR_SELLER_ID"
  }
}
```

Edit `.env` to add proxies (optional but recommended):

```env
# Rotating proxy — single endpoint that rotates IP on every request
ROTATING_PROXY=http://user:pass@proxy.provider.com:8080

# OR a comma-separated list (each worker picks one at random)
# PROXY_LIST=http://user:pass@proxy1.com:8080,http://user:pass@proxy2.com:8080

# AWS credentials for S3 upload
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
```

### 3. Run

```bash
# Full pipeline
python main.py --all

# Resume after interruption (phase2 and phase3 continue from last checkpoint)
python main.py --phase2 --phase3 --phase4 --phase5

# Scrape a single brand for testing
python main.py --phase2 --brand adidas

# Override worker count at runtime
python main.py --phase2 --workers 5
```

---

## Resume behaviour

Both phase 2 and phase 3 write a progress file after every page/product. On restart:

- **Phase 2:** brands whose page-1 URL is already in the progress file are printed as `SKIP [N/1597] BrandName — X products` and bypassed entirely. Brands that were mid-pagination resume from the last completed page.
- **Phase 3:** products whose `itemurl` is already done are skipped silently. Only the remaining URLs are fetched.

Pass `--fresh` to any phase to ignore saved progress and start from scratch.

---

## Output schema (E0005r — key fields)

| Field | Description |
|---|---|
| `product_code` | Breuninger SYAN (internal product ID) |
| `additional_code_1` | Color variant ID (`farb_id`) |
| `additional_code_3` | Breuninger web code |
| `product_title` | Full product name including sub-name |
| `brand` | Brand name |
| `color_info` | Pipe-separated color names |
| `size_info` | Pipe-separated available sizes |
| `material_info` | Material composition |
| `specifications` | Care instructions |
| `price` / `full_price` | Sale price / original price in EUR |
| `promotion_type` | `sale` if reduced |
| `main_image_url` | Primary product image |
| `all_images` | Pipe-separated full gallery URLs |
| `category1–5` | Hierarchical categories from breadcrumb and tracking JSON |
| `delivery` | Delivery options and pricing |
| `in_stock` | `1` = in stock, `0` = sold out |
| `itemurl` | Canonical product page URL |
| `competence_date` | Date the data was collected |

---

## Data compliance

- **Data access:** Web scraping (independent access)
- **Public data:** No MNPI
- **PII:** None collected
- **Proxy usage:** Configurable via `.env`

---

## Requirements

```
requests>=2.31.0
beautifulsoup4>=4.12.0
lxml>=5.0.0
python-dotenv>=1.0.0
pandas>=2.0.0
boto3>=1.34.0
```

Python 3.10+