#!/usr/bin/env python3
"""Scrape Eventbrite's East-Cobb / Marietta listing for upcoming events
and save them to the Weekend Events Notion DB tagged East_Cobb_Connect.

Eventbrite filters by date right in the URL:

    https://www.eventbrite.com/d/ga--marietta/east-cobb/
        ?page=N&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

We compute the window dynamically (upcoming Friday → +14 days) and
paginate via ?page=N using `__SERVER_DATA__.page_count` as the stop
condition.

Events come from a JSON blob embedded in the HTML:

    window.__SERVER_DATA__ = { … search_data: { events: { results: [...] } } … };

Each result has name, summary, full_description, url, start_date,
start_time, end_date, end_time, primary_venue (with address), image,
is_cancelled, is_online_event. We skip cancelled events; online events
are kept (Featured Event's Claude eval can decide relevance).

Shared helpers (_clean_html, _parse_iso_date, _normalize_title,
existing_source_urls, save_event) are imported from the sibling
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
)
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

# Base URL fragment up to (but not including) the query string. The
# locality slug controls geographic filtering; if a different newsletter
# ever uses this scraper, set EVENTBRITE_LOCALITY env var to its slug
# (e.g. "ga--atlanta/perimeter").
EVENTBRITE_LOCALITY = os.environ.get(
    "EVENTBRITE_LOCALITY", "ga--marietta/east-cobb"
).strip("/")
BASE_URL = f"https://www.eventbrite.com/d/{EVENTBRITE_LOCALITY}/"

USER_AGENT      = "Mozilla/5.0"
END_WINDOW_DAYS = 14
PAGE_SLEEP_SEC  = 0.4   # Be kind to Eventbrite between paginated requests
# Hard cap on pages walked, in case Eventbrite ever returns a bad
# page_count or we miss the stop signal. Real queries top out at ~50.
MAX_PAGES_HARD_CAP = 100


# ---------------------------------------------------------------------------
# HTTP fetch with retry
# ---------------------------------------------------------------------------
def _fetch(url: str) -> str:
    headers = {
        "User-Agent":      USER_AGENT,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20, headers=headers, allow_redirects=True)
        except Exception as e:
            print(f"    fetch error (attempt {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200 and r.text:
            return r.text
        if r.status_code in (202, 429, 503) and attempt < 2:
            wait = 3 * (attempt + 1)
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


def fetch_page(start: date, end: date, page: int) -> tuple[list[dict], int]:
    """Return (events_on_page, page_count) for a single Eventbrite results page.
    page_count is the total number of pages Eventbrite reports for this query."""
    url = (f"{BASE_URL}?page={page}"
           f"&start_date={start.isoformat()}&end_date={end.isoformat()}")
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
def _venue_fields(venue) -> tuple[str, str]:
    if not isinstance(venue, dict):
        return "", ""
    name = _clean_html(venue.get("name", "") or "")
    addr = venue.get("address") or {}
    if isinstance(addr, dict):
        display = addr.get("localized_address_display", "") or ""
        if not display:
            display = ", ".join(p for p in (
                addr.get("address_1", ""),
                addr.get("city", ""),
                addr.get("region", ""),
                addr.get("postal_code", ""),
            ) if p)
        return name, _clean_html(display)
    return name, ""


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
    venue_name, address = _venue_fields(raw.get("primary_venue"))
    description = _clean_html(
        raw.get("full_description") or raw.get("summary") or ""
    )[:2000]
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
    print(f"  Locality:       {EVENTBRITE_LOCALITY}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date filter:  {start} → {end}  (sent to Eventbrite directly)")
    print(f"  → URL pattern:  {BASE_URL}?page=N&start_date={start}&end_date={end}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    # Collect: best occurrence per URL (Eventbrite returns recurring events
    # as separate results, each with its own date — keep the earliest).
    by_url: dict[str, dict] = {}
    skipped_cancelled = 0
    skipped_no_data   = 0

    # First page also tells us page_count.
    events, page_count = fetch_page(start, end, 1)
    if not events and page_count == 0:
        print("  ✗ No events on page 1 (or __SERVER_DATA__ missing) — aborting")
        return 0
    print(f"  Eventbrite reports {page_count} page(s)")
    total_pages = min(page_count or 1, MAX_PAGES_HARD_CAP)
    if page_count > MAX_PAGES_HARD_CAP:
        print(f"  ⚠ Capping at MAX_PAGES_HARD_CAP={MAX_PAGES_HARD_CAP}")

    for page in range(1, total_pages + 1):
        if page > 1:
            time.sleep(PAGE_SLEEP_SEC)
            events, _ = fetch_page(start, end, page)
            if not events:
                print(f"  [page {page}] no events returned — stopping early")
                break
        print(f"  [page {page}/{total_pages}] {len(events)} events")
        for raw in events:
            if raw.get("is_cancelled"):
                skipped_cancelled += 1
                continue
            ev = normalize_event(raw)
            if not ev:
                skipped_no_data += 1
                continue
            url = ev["source_url"]
            prior = by_url.get(url)
            if prior is None or ev["start_date"] < prior["start_date"]:
                by_url[url] = ev

    # Name-level dedup (Eventbrite occasionally lists the same event
    # under multiple URLs when an organizer reposts).
    candidates = sorted(by_url.values(), key=lambda e: e["start_date"])
    seen_names: set[str] = set()
    deduped: list[dict] = []
    name_dupes = 0
    for ev in candidates:
        key = _normalize_title(ev["event_name"])
        if not key:
            continue
        if key in seen_names:
            name_dupes += 1
            continue
        seen_names.add(key)
        deduped.append(ev)
    if name_dupes:
        print(f"  ↓ Removed {name_dupes} same-name duplicate(s)")
    candidates = deduped

    inserted = 0
    skipped_existing = 0
    print(f"\n━━ Saving {len(candidates)} in-window candidate(s) ━━")
    for ev in candidates:
        if ev["source_url"] in existing:
            skipped_existing += 1
            continue
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER):
            inserted += 1
            print(f"  ✓ {ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, "
          f"skipped {skipped_existing} existing, "
          f"{skipped_cancelled} cancelled, "
          f"{skipped_no_data} unparseable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
