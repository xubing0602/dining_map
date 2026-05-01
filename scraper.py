"""
Michelin Guide Scraper
======================
Two-phase approach:
  Phase 1 (list pages):  ~397 pages × 48 items  → collects URLs + distinction
  Phase 2 (detail pages): one request per restaurant → full data

Run modes:
  python scraper.py --mode starred    # 1/2/3-star restaurants  (~4k, ~30-45 min @ 5 workers)
  python scraper.py --mode bib        # Bib Gourmand            (~3.6k, ~30 min @ 5 workers)
  python scraper.py --mode selected   # Selected (no award)     (~11.5k, ~90 min @ 5 workers)
  python scraper.py --mode sample     # 5 restaurants for testing

  --workers N     Concurrent detail-page workers (default 5). Each worker
                  sleeps 1-2.5s between requests, so N=5 → ~2-3 req/s.
  --max-hours H   Stop cleanly after H hours and save progress for resume.

Resume: if interrupted, re-run the same command — it skips already-scraped
URLs and continues from where it left off.
"""

import csv
import itertools
import re
import json
import time
import random
import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://guide.michelin.com"

# Per-mode list page configuration
# base_url = page 1;  page_url = page N (N >= 2)
# Pagination stops automatically when a page returns zero cards — no page count needed.
MODE_LIST_CONFIG = {
    "starred": {
        "base_url": "https://guide.michelin.com/us/en/restaurants/all-starred",
        "page_url": "https://guide.michelin.com/us/en/restaurants/all-starred/page/{page}",
        "filter":   None,         # dedicated URL — all cards are already starred
    },
    "bib": {
        "base_url": "https://guide.michelin.com/us/en/restaurants/bib-gourmand",
        "page_url": "https://guide.michelin.com/us/en/restaurants/bib-gourmand/page/{page}",
        "filter":   None,         # dedicated URL — all cards are already Bib Gourmand
    },
    "selected": {
        "base_url": "https://guide.michelin.com/us/en/restaurants",
        "page_url": "https://guide.michelin.com/us/en/restaurants/page/{page}",
        "filter":   {"Selected"}, # mixed main list — keep only Selected
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Delay range between requests (seconds)
DELAY_MIN = 1.0
DELAY_MAX = 2.5

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(OUTPUT_DIR / "scraper.log"),
    ],
)
log = logging.getLogger(__name__)

# Fixed column order for all output CSVs
COLUMNS = [
    "url", "name",
    "street_address", "city", "district", "state_region", "postal_code", "country", "country_code",
    "latitude", "longitude",
    "distinction", "green_star", "award_year",
    "cuisine", "full_genre",
    "price_symbol",
    "chef", "phone", "website",
    "description", "green_star_quote",
    "image_url",
    "facilities", "tags", "hotel",
    "accepts_reservations", "online_booking", "booking_partner",
    "review_date",
    "michelin_id", "restaurant_id",
]

PRICE_MAP = {
    "CAT_P01": "$",
    "CAT_P02": "$$",
    "CAT_P03": "$$$",
    "CAT_P04": "$$$$",
}

DISTINCTION_MAP = {
    "3 star": "3 Stars",
    "2 star": "2 Stars",
    "1 star": "1 Star",
    "bib gourmand": "Bib Gourmand",
    "plate": "Selected",
    "green star": "Green Star",
}

DISTINCTION_SETS = {
    "starred":  {"1 Star", "2 Stars", "3 Stars"},
    "bib":      {"Bib Gourmand"},
    "selected": {"Selected"},
}

MODE_PREFIX = {
    "starred":  "michelin_starred",
    "bib":      "michelin_bib",
    "selected": "michelin_selected",
    "sample":   "michelin_sample",
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Sentinel returned by fetch() when the server says the page does not exist.
# Callers that paginate should break on this; callers that fetch detail pages
# should treat it the same as None (skip the restaurant).
PAGE_NOT_FOUND = object()


def fetch(url: str, retries: int = 3) -> str | None:
    """
    Returns:
        str            — HTML body on success
        PAGE_NOT_FOUND — server returned 404 (page does not exist, do not retry)
        None           — all other failures after retries
    """
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:
                log.warning(f"HTTP 404 for {url}")
                return PAGE_NOT_FOUND  # don't retry; caller decides whether to stop
            if resp.status_code == 429:
                wait = 30 + attempt * 30
                log.warning(f"Rate limited (429). Waiting {wait}s…")
                time.sleep(wait)
            elif resp.status_code == 202:
                # Server accepted the request but hasn't finished — transient under load
                wait = 10 + attempt * 10
                log.warning(f"HTTP 202 (server busy). Waiting {wait}s and retrying…")
                time.sleep(wait)
            else:
                log.warning(f"HTTP {resp.status_code} for {url}")
                return None
        except requests.RequestException as e:
            log.warning(f"Request error ({attempt+1}/{retries}): {e}")
            time.sleep(5 * (attempt + 1))
    return None


def polite_sleep():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


# ---------------------------------------------------------------------------
# List page parser
# ---------------------------------------------------------------------------
def parse_list_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    all_cards = soup.find_all("div", attrs={"data-view": "restaurant"})
    # Exclude placeholder ghost cards Michelin injects to fill the last grid row.
    # Real cards live in js-restaurant__list_items; ghosts live in js-restaurants__empty_items.
    cards = [
        c for c in all_cards
        if not any(
            "js-restaurants__empty_items" in " ".join(p.get("class", []))
            for p in c.parents
        )
    ]
    restaurants = []

    for card in cards:
        r = {}
        r["michelin_id"] = card.get("data-id", "")
        r["latitude"] = card.get("data-lat", "")
        r["longitude"] = card.get("data-lng", "")
        map_pin = card.get("data-map-pin-name", "")

        link_tag = card.find("a", href=re.compile(r"/restaurant/"))
        r["url"] = BASE_URL + link_tag["href"].split("?")[0] if link_tag else ""

        h3 = card.find("h3")
        r["name"] = h3.get_text(strip=True) if h3 else ""

        love = card.find("div", class_="js-note-restaurant")
        if love:
            raw_dist = love.get("data-dtm-distinction", "").strip().lower()
            r["distinction"] = DISTINCTION_MAP.get(raw_dist, raw_dist)
            r["chef"] = love.get("data-dtm-chef", "").strip()
            r["city"] = love.get("data-dtm-city", "").strip()
            r["district"] = love.get("data-dtm-district", "").strip()
            r["region"] = love.get("data-dtm-region", "").strip()
            r["country_code"] = love.get("data-restaurant-country", "").strip().upper()
            r["online_booking"] = love.get("data-dtm-online-booking", "").strip()
            r["price_symbol"] = PRICE_MAP.get(love.get("data-dtm-price", ""), "")
        else:
            pin_dist = {
                "THREE_STARS": "3 Stars", "TWO_STARS": "2 Stars",
                "ONE_STAR": "1 Star", "BIB_GOURMAND": "Bib Gourmand",
            }
            r["distinction"] = pin_dist.get(map_pin, "Selected")
            r["chef"] = r["city"] = r["district"] = r["region"] = ""
            r["country_code"] = r["online_booking"] = r["price_symbol"] = ""

        full_text = card.get_text(separator="|", strip=True)
        parts = [p.strip() for p in full_text.split("|") if p.strip()]
        cuisine_candidates = [p for p in parts if p not in (r["name"],) and "·" not in p and "$" not in p]
        r["cuisine"] = cuisine_candidates[-1] if cuisine_candidates else ""

        if not r["price_symbol"]:
            m = re.search(r"\$+", full_text)
            r["price_symbol"] = m.group() if m else ""

        restaurants.append(r)

    return restaurants


# ---------------------------------------------------------------------------
# Detail page parser helpers
# ---------------------------------------------------------------------------
_WEBSITE_SKIP = {
    "michelin", "google", "facebook", "twitter", "instagram",
    "opentable", "resy", "apple", "branch", "youtube", "tripadvisor",
    "maps", "lafourchette", "thefork",
}


def _extract_website(soup: BeautifulSoup) -> str:
    a = soup.find("a", attrs={"data-event": "CTA_website"})
    if a and a.get("href", "").startswith("http"):
        return a["href"]
    for a in soup.find_all("a", href=re.compile(r"^https?://")):
        href = a["href"]
        if not any(s in href for s in _WEBSITE_SKIP):
            return href
    return ""


def _extract_description(soup: BeautifulSoup) -> str:
    el = soup.select_one(".data-sheet__description")
    return el.get_text(strip=True) if el else ""


def _extract_green_star_quote(soup: BeautifulSoup) -> str:
    bq = soup.find("blockquote")
    return bq.get_text(strip=True) if bq else ""


def _extract_facilities(soup: BeautifulSoup) -> str:
    for modal in soup.find_all("div", class_=lambda c: c and "modal__common" in c):
        if "Facilit" not in modal.get_text():
            continue
        items = [
            li.get_text(strip=True) for li in modal.find_all("li")
            if li.get_text(strip=True) and "Credit cards" not in li.get_text() and "Facilit" not in li.get_text()
        ]
        if items:
            return "|".join(items)
    return ""


def _extract_tags(soup: BeautifulSoup) -> str:
    container = soup.find(class_=re.compile(r"data-sheet__flex--container", re.I))
    if not container:
        return ""
    tags = [el.get_text(strip=True) for el in container.find_all(class_=re.compile(r"\btag\b"))]
    return "|".join(t for t in tags if t)


def _extract_hotel(soup: BeautifulSoup) -> str:
    header = soup.find(string=re.compile(r"Discover the hotel", re.I))
    if not header:
        return ""
    row = header.find_parent("div", class_=re.compile(r"\brow\b"))
    if not row:
        return ""
    name_el = row.select_one(".data-sheet__match-card--info_texts--name")
    return name_el.get_text(strip=True) if name_el else ""


def _extract_full_genre(soup: BeautifulSoup) -> str:
    for block in soup.select(".data-sheet__block--text"):
        text = block.get_text(separator=" ", strip=True)
        if re.search(r"[\$€£¥]{2,}", text):
            match = re.search(r"[·•]\s*(.+)", text)
            if match:
                return match.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------
def parse_detail_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    r: dict = {col: "" for col in COLUMNS}  # initialise all columns to empty
    r["url"] = url

    jsonld_tag = soup.find("script", type="application/ld+json")
    if jsonld_tag:
        try:
            data = json.loads(jsonld_tag.string)
            addr = data.get("address", {})
            r["name"] = data.get("name", "")
            r["street_address"] = addr.get("streetAddress", "")
            r["city"] = addr.get("addressLocality", "")
            r["state_region"] = addr.get("addressRegion", "")
            r["postal_code"] = addr.get("postalCode", "")
            r["country"] = addr.get("addressCountry", "")
            r["latitude"] = data.get("latitude", "")
            r["longitude"] = data.get("longitude", "")
            r["phone"] = data.get("telephone", "")
            r["cuisine"] = data.get("servesCuisine", "")
            r["accepts_reservations"] = data.get("acceptsReservations", "")
            r["image_url"] = data.get("image", "")
            r["review_date"] = data.get("review", {}).get("datePublished", "")
            r["award_year"] = data.get("award", {}).get("dateAwarded", "")
        except (json.JSONDecodeError, AttributeError):
            pass

    dlayer = dict(re.findall(r"dLayer\['([^']+)'\]\s*=\s*'([^']*)'", html))
    raw_dist = dlayer.get("distinction", "").strip().lower()
    r["distinction"] = DISTINCTION_MAP.get(raw_dist, raw_dist)
    r["green_star"] = dlayer.get("greenstar", "False").strip()
    r["chef"] = dlayer.get("chef", "").strip()
    r["district"] = dlayer.get("district", "").strip()
    r["price_symbol"] = PRICE_MAP.get(dlayer.get("price", ""), "")
    r["michelin_id"] = dlayer.get("id", "").strip()
    r["restaurant_id"] = dlayer.get("restaurant_id", "").strip()
    r["country_code"] = dlayer.get("restaurant_country", "").strip().upper()
    r["online_booking"] = dlayer.get("online_booking", "").strip()
    r["booking_partner"] = dlayer.get("partner", "").strip()

    r["description"] = _extract_description(soup)
    r["website"] = _extract_website(soup)
    r["full_genre"] = _extract_full_genre(soup)
    r["facilities"] = _extract_facilities(soup)
    r["tags"] = _extract_tags(soup)
    r["hotel"] = _extract_hotel(soup)
    r["green_star_quote"] = _extract_green_star_quote(soup) if r["green_star"] == "True" else ""

    return r


# ---------------------------------------------------------------------------
# Resumable list-page scraper
# ---------------------------------------------------------------------------
def scrape_list_pages(cfg: dict, page_range: range | None = None) -> list[dict]:
    """
    Walk paginated list pages defined by cfg (one of MODE_LIST_CONFIG values).

    Termination — whichever comes first:
      - A page returns zero cards  (natural end of the list)
      - page_range is exhausted    (only when --pages is passed explicitly for testing)

    Applies cfg['filter'] distinction set when set, otherwise keeps all cards.
    """
    pages = page_range if page_range is not None else itertools.count(1)
    all_restaurants = []

    for page in pages:
        url = cfg["base_url"] if page == 1 else cfg["page_url"].format(page=page)
        log.info(f"List page {page}: {url}")
        html = fetch(url)
        if html is PAGE_NOT_FOUND:
            log.info(f"  Page {page} does not exist — end of list")
            break
        if not html:
            log.error(f"  Failed — skipping page {page}")
            polite_sleep()
            continue
        items = parse_list_page(html)
        if not items:
            log.info(f"  No cards on page {page} — end of list")
            break
        if cfg["filter"]:
            items = [r for r in items if r.get("distinction") in cfg["filter"]]
        log.info(f"  → {len(items)} restaurants")
        all_restaurants.extend(items)
        polite_sleep()

    return all_restaurants


# ---------------------------------------------------------------------------
# Resumable + concurrent detail-page scraper
# ---------------------------------------------------------------------------
def scrape_detail_pages(
    urls: list[str],
    working_csv: Path,
    deadline: float | None = None,
    workers: int = 5,
) -> bool:
    """
    Scrape detail pages concurrently, appending each row immediately to
    working_csv. Skips URLs already present in working_csv (resume support).

    Args:
        urls:        Full list of URLs to scrape for this run.
        working_csv: Path to append results to (created if absent).
        deadline:    time.monotonic() value after which to stop cleanly.
        workers:     Number of concurrent HTTP workers.

    Returns:
        True if all URLs were processed, False if stopped early.
    """
    # Load already-done URLs from any previous partial run
    done_urls: set[str] = set()
    if working_csv.exists():
        try:
            existing = pd.read_csv(working_csv, usecols=["url"])
            done_urls = set(existing["url"].dropna())
        except Exception:
            pass

    remaining = [u for u in urls if u not in done_urls]
    total = len(urls)
    already = len(done_urls)

    if already:
        log.info(f"Resuming: {already} already done, {len(remaining)} remaining (of {total})")
    else:
        log.info(f"Starting: {len(remaining)} URLs to scrape  [workers={workers}]")

    if not remaining:
        return True

    # Shared state for thread-safe writing and progress tracking
    write_lock = threading.Lock()
    counter = already          # total rows written so far (including prior run)
    stopped = threading.Event()

    def worker(url: str) -> dict | None:
        """Fetch + parse one page; each worker sleeps after its own request."""
        if stopped.is_set():
            return None
        html = fetch(url)
        polite_sleep()         # each worker paces itself independently
        if not html:           # covers both PAGE_NOT_FOUND and other failures
            log.warning(f"  Skipped (fetch failed): {url}")
            return None
        return parse_detail_page(html, url)

    write_header = not working_csv.exists() or working_csv.stat().st_size == 0

    with open(working_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(worker, url): url for url in remaining}

            for future in as_completed(futures):
                # Deadline check — cancel queued (not yet started) futures and stop
                if deadline and time.monotonic() >= deadline:
                    stopped.set()
                    for pending in futures:
                        pending.cancel()
                    log.warning(
                        f"Approaching time limit — {counter} total rows saved. "
                        f"Re-run to resume from here."
                    )
                    return False

                url = futures[future]
                try:
                    row = future.result()
                except Exception as e:
                    log.warning(f"Worker error for {url}: {e}")
                    row = None

                if row:
                    with write_lock:
                        writer.writerow(row)
                        f.flush()   # survives a crash — at most one row lost
                        counter += 1
                        log.info(f"Detail {counter}/{total}: {url}")

    return True


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------
def run_mode(mode: str, page_range: range | None, max_hours: float | None, workers: int = 5) -> None:
    date_str = datetime.now().strftime("%Y%m%d")
    prefix = MODE_PREFIX[mode]

    # Working files (dot-prefixed so .gitignore can exclude them)
    url_list_file = OUTPUT_DIR / f".{prefix}_urls.txt"
    working_csv = OUTPUT_DIR / f".{prefix}_working.csv"

    deadline = (time.monotonic() + max_hours * 3600) if max_hours else None

    # ── SAMPLE mode: no resume needed ────────────────────────────────────────
    if mode == "sample":
        log.info("=== SAMPLE MODE ===")
        sample_cfg = MODE_LIST_CONFIG["starred"]   # borrow starred URL for sample
        stubs = scrape_list_pages(sample_cfg, range(1, 2))
        urls = [r["url"] for r in stubs if r["url"]][:5]
        sample_working = OUTPUT_DIR / f".{prefix}_working.csv"
        sample_working.unlink(missing_ok=True)
        scrape_detail_pages(urls, sample_working, deadline, workers)
        final = OUTPUT_DIR / f"{prefix}_{date_str}.csv"
        sample_working.rename(final)
        log.info(f"Saved → {final}")
        print(f"\n  {final.name}  ({sum(1 for _ in open(final)) - 1} restaurants)")
        return

    cfg = MODE_LIST_CONFIG[mode]

    # ── Phase 1: collect URLs (skip if already saved from a prior run) ────────
    if url_list_file.exists():
        urls = url_list_file.read_text(encoding="utf-8").splitlines()
        log.info(f"=== {mode.upper()} MODE (resumed) — loaded {len(urls)} URLs from prior run ===")
    else:
        log.info(f"=== {mode.upper()} MODE — collecting URLs from {cfg['base_url']} ===")
        stubs = scrape_list_pages(cfg, page_range)
        urls = [r["url"] for r in stubs if r.get("url")]
        url_list_file.write_text("\n".join(urls), encoding="utf-8")
        log.info(f"Saved {len(urls)} URLs → {url_list_file}")

    # ── Phase 2: scrape detail pages ─────────────────────────────────────────
    completed = scrape_detail_pages(urls, working_csv, deadline, workers)

    if not completed:
        # Hit the time limit — leave working files in place for next run
        log.info(
            f"Partial run saved to {working_csv}. "
            f"Re-run 'python scraper.py --mode {mode}' to resume."
        )
        return

    # ── All done: finalise output files ──────────────────────────────────────
    if mode == "starred":
        # Split into per-star-level CSVs + combined
        df = pd.read_csv(working_csv)
        for stars, label in [("3 Stars", "3star"), ("2 Stars", "2star"), ("1 Star", "1star")]:
            subset = df[df["distinction"] == stars]
            path = OUTPUT_DIR / f"michelin_{label}_{date_str}.csv"
            subset.to_csv(path, index=False)
            log.info(f"Saved {len(subset)} rows → {path}")
            print(f"\n  {path.name}  ({len(subset)} restaurants)")
        combined = OUTPUT_DIR / f"{prefix}_{date_str}.csv"
        df.to_csv(combined, index=False)
        log.info(f"Saved {len(df)} rows → {combined}")
        print(f"\n  {combined.name}  ({len(df)} restaurants)")
    else:
        final = OUTPUT_DIR / f"{prefix}_{date_str}.csv"
        working_csv.rename(final)
        n = sum(1 for _ in open(final)) - 1
        log.info(f"Saved {n} rows → {final}")
        print(f"\n  {final.name}  ({n} restaurants)")

    # Clean up working files
    url_list_file.unlink(missing_ok=True)
    working_csv.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Michelin Guide scraper")
    parser.add_argument(
        "--mode",
        choices=["starred", "bib", "selected", "sample"],
        default="sample",
        help="Which distinction tier to scrape",
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="List-page range override, e.g. 1-10 for testing. "
             "Defaults to the full range for each mode "
             "(starred: 1-80, bib: 1-76, selected: 1-397).",
    )
    parser.add_argument(
        "--max-hours",
        type=float,
        default=None,
        help="Stop cleanly after this many hours (for CI time limits). "
             "Working files are kept so the next run can resume.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Concurrent detail-page workers (default 5). "
             "Each worker sleeps 1-2.5s between requests.",
    )
    args = parser.parse_args()

    if args.pages:
        start, end = args.pages.split("-") if "-" in args.pages else (args.pages, args.pages)
        page_range = range(int(start), int(end) + 1)
    else:
        page_range = None  # auto-detect: stop when page returns no cards

    run_mode(args.mode, page_range, args.max_hours, args.workers)
    print("\nDone.")


if __name__ == "__main__":
    main()
