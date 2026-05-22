#!/usr/bin/env python3
"""Scrape Eventbrite's East-Cobb / Marietta listings for upcoming
events and save them to the Weekend Events Notion DB tagged
East_Cobb_Connect.

We walk six category-scoped result pages — each filters Eventbrite's
catalog to one interest area so generic noise (yoga drop-ins, pricing
seminars, etc.) doesn't drown out real local events:

    https://www.eventbrite.com/d/ga--marietta/{CATEGORY}/east-cobb/
        ?page=N&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

CATEGORY slugs: hobbies--events, charity-and-causes--events,
sports-and-fitness--events, community--events, performances, networking.

City filter: Eventbrite's geo filter is loose — a "East Cobb" search
still returns Atlanta, Kennesaw, Powder Springs, Stone Mountain, etc.
We exclude anything whose venue city isn't Marietta or Sandy Springs
(case-insensitive). Online-only events (no venue city) are also
excluded for this reason.

Window: upcoming_friday(today) → +14 days. Sent to Eventbrite directly
via the URL so we don't paginate through anything outside the window.

Events come from a JSON blob embedded in the HTML:

    window.__SERVER_DATA__ = { … search_data: { events: { results: [...] } } … };

Each result has name, summary, full_description, url, start_date,
start_time, end_date, end_time, primary_venue (with address), image,
is_cancelled, is_online_event. Cancelled events are skipped.

Shared helpers (_clean_html, _normalize_title, existing_source_urls,
save_event, format_dates_human) are imported from the sibling
ecc_event_webscraper module so they stay in sync.
"""
import os
import re
import sys
import json
import time
from datetime import date, datetime, timedelta

import requests

# Sibling-folder import of shared helpers from the travelcobb/visitmariettaga
# scraper. Same NewsletterCreation/Code path for the Notion helper +
# upcoming_friday.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from ecc_event_webscraper import (  # noqa: E402
    _clean_html,
    _normalize_title,
    existing_source_urls,
    save_event,
    format_dates_human,
)
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import is_cancelled_event  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

# Category-scoped result pages. Each is one URL of the form
# /d/{LOCALITY_PREFIX}/{CATEGORY}/{LOCALITY_SUFFIX}/?page=N&start=…&end=…
LOCALITY_PREFIX = "ga--marietta"
LOCALITY_SUFFIX = "east-cobb"
EVENTBRITE_CATEGORIES = [
    "hobbies--events",
    "charity-and-causes--events",
    "sports-and-fitness--events",
    "community--events",
    "performances",
    "networking",
]

# Venue city allow-list. Eventbrite's geo filter is loose — the East
# Cobb URL still surfaces Atlanta / Kennesaw / Powder Springs events —
# so we hard-filter on venue.address.city. Case-insensitive match.
# "East Cobb" is technically part of Marietta but some venues set their
# city to "East Cobb" directly, so include it explicitly.
ALLOWED_CITIES = {"marietta", "sandy springs", "east cobb"}

USER_AGENT      = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
END_WINDOW_DAYS = 14
PAGE_SLEEP_SEC      = 0.6   # between paginated requests within a category
CATEGORY_SLEEP_SEC  = 2.0   # between category transitions (heavier pause)
# Hard cap on pages walked PER CATEGORY, in case Eventbrite ever returns
# a bad page_count or we miss the stop signal.
MAX_PAGES_HARD_CAP = 100


# ---------------------------------------------------------------------------
# HTTP fetch with retry
# ---------------------------------------------------------------------------
def _fetch(url: str) -> str:
    """Fetch one Eventbrite category page. Uses curl_cffi with Chrome TLS
    fingerprint impersonation when available — Eventbrite blocks data-center
    IPs (e.g. GitHub Actions) with HTTP 405 unless the request looks like
    a real browser at the TLS layer. Falls back to plain `requests` if
    curl_cffi isn't installed (local dev without it)."""
    headers = {
        "User-Agent":      USER_AGENT,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    # Prefer curl_cffi for browser TLS impersonation
    cffi_get = None
    try:
        from curl_cffi import requests as _cffi
        cffi_get = lambda u: _cffi.get(u, impersonate="chrome120",
                                       timeout=20, allow_redirects=True)
    except ImportError:
        pass

    for attempt in range(3):
        try:
            if cffi_get is not None:
                r = cffi_get(url)
            else:
                r = requests.get(url, timeout=20, headers=headers, allow_redirects=True)
        except Exception as e:
            print(f"    fetch error (attempt {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200 and r.text:
            return r.text
        # 202/429/503 are transient. 405 is Eventbrite's bot-detection
        # block — also retryable since the impersonated TLS fingerprint
        # sometimes gets through on a second attempt.
        if r.status_code in (202, 405, 429, 503) and attempt < 2:
            wait = 5 * (attempt + 1)
            print(f"    HTTP {r.status_code} — retry {attempt + 1}/3 in {wait}s")
            time.sleep(wait)
            continue
        print(f"    HTTP {r.status_code} from {url}")
        return ""
    return ""


# ---------------------------------------------------------------------------
# Parse the __SERVER_DATA__ JSON blob out of a page's HTML
# ---------------------------------------------------------------------------
_SERVER_DATA_RE = re.compile(
    r"window\.__SERVER_DATA__\s*=\s*(\{.+?\});\s*\n", re.DOTALL,
)


def _parse_server_data(html: str) -> dict:
    m = _SERVER_DATA_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"    __SERVER_DATA__ parse failed: {e}")
        return {}


def _build_url(category: str, start: date, end: date, page: int) -> str:
    return (f"https://www.eventbrite.com/d/"
            f"{LOCALITY_PREFIX}/{category}/{LOCALITY_SUFFIX}/"
            f"?page={page}&start_date={start.isoformat()}&end_date={end.isoformat()}")


def fetch_page(category: str, start: date, end: date,
               page: int) -> tuple[list[dict], int]:
    """Return (events_on_page, page_count) for one category's Eventbrite
    results page. page_count is what Eventbrite reports for that
    category in this date window."""
    url = _build_url(category, start, end, page)
    html = _fetch(url)
    if not html:
        return [], 0
    sd = _parse_server_data(html)
    if not sd:
        return [], 0
    page_count = int(sd.get("page_count", 0) or 0)
    events = (sd.get("search_data", {}).get("events", {}).get("results", [])) or []
    return events, page_count


# ---------------------------------------------------------------------------
# Normalize an Eventbrite event dict → our standard event shape
# ---------------------------------------------------------------------------
def _venue_fields(venue) -> tuple[str, str, str]:
    """Return (venue_name, address_display, city) from an Eventbrite
    primary_venue dict. Empty strings if any field is missing."""
    if not isinstance(venue, dict):
        return "", "", ""
    name = _clean_html(venue.get("name", "") or "")
    addr = venue.get("address") or {}
    if not isinstance(addr, dict):
        return name, "", ""
    city = (addr.get("city") or "").strip()
    display = addr.get("localized_address_display", "") or ""
    if not display:
        display = ", ".join(p for p in (
            addr.get("address_1", ""),
            city,
            addr.get("region", ""),
            addr.get("postal_code", ""),
        ) if p)
    return name, _clean_html(display), city


def _format_time(start_time: str, end_time: str) -> str:
    """'18:00' + '20:00' → '6:00 PM – 8:00 PM'. Empty inputs return ''."""
    def _fmt(t: str) -> str:
        if not t or ":" not in t:
            return ""
        try:
            h, m = (int(x) for x in t.split(":")[:2])
        except ValueError:
            return ""
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"
    s, e = _fmt(start_time), _fmt(end_time)
    if s and e:
        return f"{s} – {e}"
    return s or e


def _image_url(image) -> str:
    if isinstance(image, dict):
        return image.get("url", "") or ""
    if isinstance(image, str):
        return image
    return ""


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def normalize_event(raw: dict) -> dict | None:
    """Convert a raw Eventbrite search result into our standard event
    dict. Returns None if the event is cancelled or missing required
    fields (url, name, start_date)."""
    if raw.get("is_cancelled"):
        return None
    url   = raw.get("url", "") or ""
    name  = _clean_html(raw.get("name", "") or "")
    start = _parse_date(raw.get("start_date", ""))
    if not url or not name or not start:
        return None
    end = _parse_date(raw.get("end_date", "")) or start
    venue_name, address, city = _venue_fields(raw.get("primary_venue"))
    description = _clean_html(
        raw.get("full_description") or raw.get("summary") or ""
    )[:2000]
    # Text-based check in addition to the structured `is_cancelled` flag —
    # organizers sometimes update the title/description to mark a
    # cancellation without flipping the API field.
    if is_cancelled_event(name, description):
        return None
    return {
        "event_name":  name,
        "description": description,
        "source_url":  url,
        "image_url":   _image_url(raw.get("image")),
        "start_date":  start,
        "end_date":    end,
        "time":        _format_time(raw.get("start_time", ""), raw.get("end_time", "")),
        "location":    venue_name,
        "address":     address,
        "city":        city,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today = date.today()
    start = _upcoming_friday(today)
    end   = start + timedelta(days=END_WINDOW_DAYS)

    print("Eventbrite scraper")
    print(f"  → Categories:   {len(EVENTBRITE_CATEGORIES)}  ({', '.join(EVENTBRITE_CATEGORIES)})")
    print(f"  → City filter:  {sorted(ALLOWED_CITIES)}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date filter:  {start} → {end}  (sent to Eventbrite directly)")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    # Group by normalized title across ALL categories. Eventbrite lists
    # each occurrence of a recurring event as a separate result with its
    # own URL and date — same name across them. Grouping on name
    # collapses them into one Notion row with `all_dates` = every
    # in-window occurrence.
    by_name: dict[str, dict] = {}
    skipped_cancelled = 0
    skipped_no_data   = 0
    skipped_city      = 0

    for cat_idx, category in enumerate(EVENTBRITE_CATEGORIES):
        if cat_idx > 0:
            # Cool-down between categories — Eventbrite's rate-limiter
            # responds with HTTP 405 to burst traffic across categories.
            time.sleep(CATEGORY_SLEEP_SEC)
        print(f"━━ category: {category} ━━")
        events, page_count = fetch_page(category, start, end, 1)
        if not events and page_count == 0:
            print(f"  · no events / __SERVER_DATA__ missing — skipping category")
            print()
            continue
        total_pages = min(page_count or 1, MAX_PAGES_HARD_CAP)
        if page_count > MAX_PAGES_HARD_CAP:
            print(f"  ⚠ {page_count} pages reported, capping at {MAX_PAGES_HARD_CAP}")
        print(f"  Eventbrite reports {page_count} page(s)")

        for page in range(1, total_pages + 1):
            if page > 1:
                time.sleep(PAGE_SLEEP_SEC)
                events, _ = fetch_page(category, start, end, page)
                if not events:
                    print(f"  [page {page}] no events returned — stopping early")
                    break
            kept_this_page = 0
            for raw in events:
                if raw.get("is_cancelled"):
                    skipped_cancelled += 1
                    continue
                ev = normalize_event(raw)
                if not ev:
                    skipped_no_data += 1
                    continue
                if (ev.get("city") or "").strip().lower() not in ALLOWED_CITIES:
                    skipped_city += 1
                    continue
                name_key = _normalize_title(ev["event_name"])
                if not name_key:
                    continue
                kept_this_page += 1
                entry = by_name.get(name_key)
                if entry is None:
                    ev["all_dates"] = {ev["start_date"]}
                    by_name[name_key] = ev
                else:
                    entry["all_dates"].add(ev["start_date"])
                    if ev["start_date"] < entry["start_date"]:
                        entry["start_date"] = ev["start_date"]
            print(f"  [page {page}/{total_pages}] {len(events)} events  "
                  f"({kept_this_page} after city filter)")
        print()

    candidates = sorted(by_name.values(), key=lambda e: e["start_date"])

    # Backfill: Eventbrite's search API usually returns a CDN image,
    # but a small number slip through with image=None (private/draft
    # events, image still uploading, etc.). Scrape each detail page
    # for an og:image fallback so we don't ship blank cards.
    import sys as _sys, os as _os
    _sys.path.append(_os.path.join(_os.path.dirname(__file__), "..", "..",
                                   "..", "NewsletterCreation", "Code"))
    from event_image_scraper import backfill_images  # noqa: E402
    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from event detail pages")

    inserted = 0
    skipped_existing = 0
    multi_date = 0
    print(f"━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        if len(ev.get("all_dates") or {}) > 1:
            multi_date += 1
        if ev["source_url"] in existing:
            skipped_existing += 1
            continue
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER):
            inserted += 1
            dates_disp = format_dates_human(ev.get("all_dates") or [])
            print(f"  ✓ {dates_disp or ev['start_date']}  {ev['event_name'][:60]}  ({ev.get('city','?')})")
    print()
    print(f"✓ Done. Inserted {inserted}, "
          f"skipped {skipped_existing} existing, "
          f"{skipped_city} wrong city, "
          f"{skipped_cancelled} cancelled, "
          f"{skipped_no_data} unparseable  "
          f"({multi_date} multi-date event(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
