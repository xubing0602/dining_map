# Michelin Guide Scraper

Scrapes restaurant data from the [Michelin Guide](https://guide.michelin.com/us/en/restaurants) and saves it as dated CSVs. Designed for monthly runs via GitHub Actions with built-in resume support and concurrent fetching.

---

## Overview

The scraper covers ~19,036 restaurants across all Michelin Guide regions worldwide, split into three tiers:

| Tier | Distinction | Count |
|---|---|---|
| Starred | 1 Star / 2 Stars / 3 Stars | ~3,800 |
| Bib Gourmand | Good quality, good value | ~3,600 |
| Selected | Listed but no award | ~11,600 |

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Test with 5 sample restaurants
python scraper.py --mode sample

# Scrape all starred restaurants
python scraper.py --mode starred

# Scrape Bib Gourmand
python scraper.py --mode bib
```

---

## How It Works

### Two-phase scraping

Every run goes through two phases:

**Phase 1 — List pages**

Each mode uses a dedicated Michelin filtered URL, so no post-filtering is needed for starred and bib:

| Mode | List URL |
|---|---|
| `starred` | `guide.michelin.com/us/en/restaurants/all-starred` |
| `bib` | `guide.michelin.com/us/en/restaurants/bib-gourmand` |
| `selected` | `guide.michelin.com/us/en/restaurants` (main list, filtered to Selected only) |

Each page holds ~48 restaurant cards. The scraper uses `itertools.count(1)` — an infinite page counter — and stops as soon as a page returns zero cards. This means no hardcoded page count anywhere: the scraper self-terminates correctly regardless of how many restaurants Michelin adds or removes over time. Each card exposes rich metadata via HTML `data-*` attributes — enough to collect detail-page URLs and distinction info without visiting individual pages. This phase takes ~5–10 minutes.

**Phase 2 — Detail pages** (one request per restaurant)

The scraper visits each restaurant's individual page and extracts the full dataset from three sources:

1. **JSON-LD structured data** (`<script type="application/ld+json">`) — the cleanest source for address, coordinates, phone, cuisine, reservation info, image, and the Michelin award block.
2. **`dLayer` JavaScript variables** — Michelin's analytics data layer, injected inline into every page. Contains distinction level, green star flag, chef name, price code, booking partner, and internal IDs.
3. **HTML parsing** — used for fields not present in JSON-LD or dLayer: full inspector description, restaurant website, multi-cuisine genre string, facilities list, experience tags, hotel association, and green star chef quote.

---

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--mode` | `sample` | Which tier to scrape. See modes below. |
| `--workers` | `5` | Number of concurrent HTTP workers for detail pages. |
| `--pages` | _(auto)_ | Override the list-page range for Phase 1, e.g. `1-5`. By default the scraper runs until a page returns zero cards. Only needed for testing a subset. |
| `--max-hours` | _(none)_ | Stop cleanly after N hours and preserve progress for resume. |

### `--mode`

| Value | What it scrapes | Output files |
|---|---|---|
| `sample` | 5 restaurants from the top of the list | `michelin_sample_YYYYMMDD.csv` |
| `starred` | All 1-star, 2-star, 3-star restaurants | `michelin_starred_YYYYMMDD.csv` + split files per star level |
| `bib` | All Bib Gourmand restaurants | `michelin_bib_YYYYMMDD.csv` |
| `selected` | All selected (no award) restaurants | `michelin_selected_YYYYMMDD.csv` |

The `starred` mode also produces three additional split files:

```
output/
  michelin_starred_20260601.csv   ← all stars combined
  michelin_3star_20260601.csv     ← 3-star only
  michelin_2star_20260601.csv     ← 2-star only
  michelin_1star_20260601.csv     ← 1-star only
```

### `--workers`

Controls how many detail pages are fetched concurrently. Each worker independently fetches one page, then sleeps for 1–2.5 seconds before its next request. Because web scraping is I/O-bound (almost all time is spent waiting on network), threading gives near-linear speedup regardless of CPU count.

```
workers=1  →  ~0.5 req/s  →  starred takes ~2.5 hrs
workers=5  →  ~2-3 req/s  →  starred takes ~35-45 min   ← default
workers=10 →  ~4-5 req/s  →  starred takes ~20 min      (watch for 429s)
```

Recommended ceiling is **10**. Above that, Michelin may return HTTP 429 (rate limited). The scraper backs off automatically on 429 (waits 30–90s and retries up to 3 times), but sustained high concurrency risks a temporary IP block.

### `--pages`

Limits Phase 1 to a page range. Useful for smoke-testing or scraping a geographic subset if you know which pages cover a region.

```bash
# Only scrape the first 5 list pages (~240 restaurants)
python scraper.py --mode starred --pages 1-5
```

### `--max-hours`

Tells the scraper to stop cleanly after N hours. When the deadline is reached, the current batch of in-flight requests completes, then the process exits, leaving working files on disk for the next run to resume from. Without this flag the scraper runs until complete.

This is primarily used in GitHub Actions to stop before the 6-hour job timeout, allowing the `if: always()` commit step to save progress.

```bash
# Stop after 2.5 hours — used by the workflow for starred mode
python scraper.py --mode starred --workers 5 --max-hours 2.5
```

---

## Resume Mechanism

If a run is interrupted (timeout, network failure, machine restart), re-running the **exact same command** resumes from where it left off — no data is re-fetched.

### How it works

Two working files are written to `output/` at the start of each run:

| File | Purpose |
|---|---|
| `output/.michelin_starred_urls.txt` | All URLs to scrape for this run (written after Phase 1) |
| `output/.michelin_starred_working.csv` | Partial results, appended row-by-row as pages are scraped |

On the next run:

1. **Phase 1 is skipped** if the URL list file exists — no need to re-crawl 397 list pages.
2. **Phase 2 reads the working CSV**, builds a set of already-scraped URLs, and only fetches the remaining ones.

Each row is written and `flush()`-ed to disk immediately after scraping, so at most one restaurant is lost in a hard crash.

On successful completion, the working CSV is renamed to the final dated output, and both working files are deleted.

### Resume in GitHub Actions

The workflow uses `if: always()` on the commit step, so working files are committed to the repo even if the scrape step hit the time limit. The next workflow run does `git pull` at the start to retrieve them.

```
Run 1 (hits --max-hours limit at restaurant 1,800/4,000):
  → exits cleanly, leaves .michelin_starred_urls.txt + .michelin_starred_working.csv
  → if: always() commits both files to git

Run 2 (re-triggered manually):
  → git pull retrieves the working files
  → logs "Resuming: 1800 already done, 2200 remaining"
  → scrapes only the 2,200 remaining, appends to working CSV
  → renames to michelin_starred_20260601.csv, deletes working files
  → commits final CSV
```

---

## Output CSV Fields

All CSVs share the same 32-column schema in this fixed order:

### Identity & Location

| Column | Source | Example |
|---|---|---|
| `url` | List page | `https://guide.michelin.com/us/en/…/san-laurel` |
| `name` | JSON-LD | `San Laurel` |
| `street_address` | JSON-LD | `Conrad Los Angeles, 100 S. Grand Ave.` |
| `city` | JSON-LD | `Los Angeles` |
| `district` | dLayer | `Los Angeles` |
| `state_region` | JSON-LD | `California` |
| `postal_code` | JSON-LD | `90012` |
| `country` | JSON-LD | `USA` |
| `country_code` | dLayer | `US` |
| `latitude` | JSON-LD | `34.0557521` |
| `longitude` | JSON-LD | `-118.2480445` |

### Michelin Awards

| Column | Source | Example |
|---|---|---|
| `distinction` | dLayer | `3 Stars` / `2 Stars` / `1 Star` / `Bib Gourmand` / `Selected` |
| `green_star` | dLayer | `True` / `False` |
| `award_year` | JSON-LD | `2026` |

### Food & Price

| Column | Source | Example |
|---|---|---|
| `cuisine` | JSON-LD | `Mediterranean Cuisine` (primary cuisine) |
| `full_genre` | HTML price block | `Mediterranean Cuisine, Contemporary` (all cuisines, comma-separated) |
| `price_symbol` | dLayer | `$` / `$$` / `$$$` / `$$$$` |

### Contact & Booking

| Column | Source | Example |
|---|---|---|
| `chef` | dLayer | `Richard EKKEBUS` |
| `phone` | JSON-LD | `+1 213-349-8585` |
| `website` | HTML `data-event="CTA_website"` | `https://www.mandarinoriental.com/…` |
| `accepts_reservations` | JSON-LD | `Yes` |
| `online_booking` | dLayer | `True` / `False` |
| `booking_partner` | dLayer | `opentable` / `resy` / `mozrest` |

### Content

| Column | Source | Example |
|---|---|---|
| `description` | `.data-sheet__description` | Full Michelin inspector note (no truncation) |
| `green_star_quote` | `<blockquote>` | Chef's sustainability statement (green star restaurants only) |
| `image_url` | JSON-LD | CDN JPEG URL |

### Details

| Column | Source | Example | Notes |
|---|---|---|---|
| `facilities` | Modal `<li>` items | `Air conditioning\|Car park\|Terrace` | Pipe-separated |
| `tags` | `.tag` in flex container | `Date night\|Iconic` | Pipe-separated |
| `hotel` | "Discover the hotel" section | `Il San Pietro di Positano` | Only when Michelin cross-links a hotel |

### Metadata

| Column | Source | Example |
|---|---|---|
| `review_date` | JSON-LD | `2025-06-25T12:38` |
| `michelin_id` | dLayer | `121103` |
| `restaurant_id` | dLayer | `1203497` |

### Multi-value fields

`facilities`, `tags`, and `full_genre` (when multiple cuisines exist) use `|` as the separator so they can be split easily:

```python
import pandas as pd
df = pd.read_csv("output/michelin_starred_20260601.csv")
df["facilities_list"] = df["facilities"].str.split("|")
df["tags_list"] = df["tags"].str.split("|")
```

---

## GitHub Actions Setup

The workflow runs automatically on the 1st of every month (03:00 UTC) and can also be triggered manually.

### First-time setup

1. Push the project to a GitHub repository.
2. Go to **Settings → Actions → General → Workflow permissions** and select **"Read and write permissions"**.

That's it. The workflow installs dependencies, runs the scraper, and commits dated CSVs back to the repo automatically.

### Scheduled run (monthly)

Runs `starred` + `bib` sequentially in one job. With 5 workers, both complete in ~75–90 minutes total, well within the 6-hour GitHub Actions job limit.

### Manual trigger

Go to **Actions → Monthly Michelin Scrape → Run workflow** and choose a mode:

| Mode input | What runs |
|---|---|
| `all` | `starred` + `bib` (same as monthly schedule) |
| `starred` | Starred restaurants only |
| `bib` | Bib Gourmand only |
| `selected` | Selected restaurants (~90 min with 5 workers) — runs as a separate job |
| `sample` | 5 restaurants — use to verify the scraper is working after changes |

### Time limits in the workflow

| Job | Modes | `--max-hours` | Job `timeout-minutes` |
|---|---|---|---|
| `scrape-starred-bib` | starred, bib | 2.5 each | 360 (6 hrs) |
| `scrape-selected` | selected | 5.5 | 360 (6 hrs) |

The `--max-hours` values are set conservatively lower than the job timeout. This ensures the scraper exits cleanly (rather than being killed) so the `if: always()` commit step can save partial progress.

---

## Project Structure

```
260501_fine_dining_map/
├── scraper.py                  # Main scraper
├── requirements.txt            # Python dependencies
├── .gitignore
├── .github/
│   └── workflows/
│       └── scrape.yml          # Monthly GitHub Actions workflow
└── output/
    ├── michelin_starred_YYYYMMDD.csv
    ├── michelin_3star_YYYYMMDD.csv
    ├── michelin_2star_YYYYMMDD.csv
    ├── michelin_1star_YYYYMMDD.csv
    ├── michelin_bib_YYYYMMDD.csv
    ├── michelin_selected_YYYYMMDD.csv
    ├── scraper.log             # Full run log (not committed)
    │
    │   Working files — present only during/after a partial run:
    ├── .michelin_starred_urls.txt
    ├── .michelin_starred_working.csv
    ├── .michelin_bib_urls.txt
    └── .michelin_bib_working.csv
```

---

## Dependencies

```
requests==2.32.5        # HTTP
beautifulsoup4==4.14.3  # HTML parsing
lxml==6.0.2             # Fast HTML parser backend for BeautifulSoup
pandas==3.0.1           # CSV reading/writing and final splits
```
