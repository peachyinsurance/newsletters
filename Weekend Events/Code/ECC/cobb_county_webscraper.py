#!/usr/bin/env python3
"""Scrape cobbcounty.gov events and save them to the Weekend Events
Notion DB tagged East_Cobb_Connect.

The cobbcounty.gov site is a Next.js SPA — the public /events page is
JS-rendered with no JSON-LD. The page's JS bundle calls a same-origin
GraphQL-backed REST endpoint that takes a date range and returns the
full event list in one shot:

    GET /api/search/events
        ?fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD
        &search=&department=&category=&age=&location=
        &pageSize=500

Response JSON shape:
    { "graphqlEventsSearchWww": {
        "results": [ {id, title, path, summary, location, eventAddress,
                      startDate.time, endDate.time, department, ...}, ... ],
        "pageInfo": {...}
    } }

Date window: today → upcoming_friday + 14 days (matches the other ECC
scrapers and the Featured Event picker).

Many of these are free (Parks & Recreation, Library, civic events) —
Free Events' text-scan on Event Name + Description will pick them up
automatically via the keyword 'free' / 'no charge' / etc., so we don't
do any special tagging here.

Shared helpers (_clean_html, _normalize_title, existing_source_urls,
save_event, format_dates_human) imported from the sibling
ecc_event_webscraper module.
"""
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests

# Sibling-folder helper imports (same path setup the other ECC-folder
# scrapers use). NewsletterCreation/Code is on path for event_date_filter.
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

# The Next.js SPA fetches this relative URL from its bundle — works
# server-to-server too since it's a public REST endpoint.
API_BASE        = "https://www.cobbcounty.gov"
API_PATH        = "/api/search/events"
SITE_BASE       = "https://www.cobbcounty.gov"
END_WINDOW_DAYS = 14
PAGE_SIZE       = 500   # API returns up to ~500 in one shot; 2-week window
                        # typically yields ~400 events for Cobb County.

USER_AGENT = "Mozilla/5.0"
HEADERS = {
    "User-Agent":      USER_AGENT,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
}


# ---------------------------------------------------------------------------
# Fetch with retry on transient codes
# ---------------------------------------------------------------------------
def _fetch_json(url: str) -> dict:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except Exception as e:
            print(f"    fetch error (attempt {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                print(f"    JSON parse failed: {e}")
                return {}
        if r.status_code in (429, 503) and attempt < 2:
            wait = 3 * (attempt + 1)
            print(f"    HTTP {r.status_code} — retry {attempt + 1}/3 in {wait}s")
            time.sleep(wait)
            continue
        print(f"    HTTP {r.status_code} from {url}")
        return {}
    return {}


def fetch_events(from_date: date, to_date: date) -> list[dict]:
    """Pull all events the API returns for [from_date, to_date]."""
    from urllib.parse import urlencode
    params = {
        "fromDate":   from_date.isoformat(),
        "toDate":     to_date.isoformat(),
        "search":     "",
        "department": "",
        "category":   "",
        "age":        "",
        "location":   "",
        "pageSize":   str(PAGE_SIZE),
    }
    url = f"{API_BASE}{API_PATH}?{urlencode(params)}"
    print(f"  Fetching {url[:100]}…")
    data = _fetch_json(url)
    block = data.get("graphqlEventsSearchWww") or {}
    return block.get("results") or []


# ---------------------------------------------------------------------------
# Normalize one API event → our standard event dict
# ---------------------------------------------------------------------------
def _parse_iso(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _format_time_range(start_str: str, end_str: str) -> str:
    """ISO datetimes → '1:00 PM – 3:00 PM'. Empty if both are midnight."""
    try:
        sdt = datetime.fromisoformat((start_str or "").replace("Z", "+00:00"))
        edt = datetime.fromisoformat((end_str or "").replace("Z", "+00:00")) \
              if end_str else None
    except Exception:
        return ""
    if not (sdt.hour or sdt.minute):
        return ""
    out = sdt.strftime("%-I:%M %p")
    if edt and (edt.hour or edt.minute):
        out += " – " + edt.strftime("%-I:%M %p")
    return out


def _build_address(event_address: dict) -> str:
    if not isinstance(event_address, dict):
        return ""
    parts = [
        event_address.get("addressLine1"),
        event_address.get("locality"),
        event_address.get("administrativeArea"),
        event_address.get("postalCode"),
    ]
    return ", ".join(p for p in parts if p)


def normalize_event(api_ev: dict) -> dict | None:
    """Map one API event into the standard scraper event dict."""
    title = _clean_html(api_ev.get("title") or "")
    path  = api_ev.get("path") or ""
    if not title or not path:
        return None
    description = _clean_html(api_ev.get("summary") or "")[:2000]
    if is_cancelled_event(title, description):
        return None
    url = path if path.startswith("http") else f"{SITE_BASE}{path}"
    start = _parse_iso((api_ev.get("startDate") or {}).get("time", ""))
    end   = _parse_iso((api_ev.get("endDate")   or {}).get("time", ""))
    location_name = _clean_html((api_ev.get("location") or {}).get("title") or "")
    address       = _build_address(api_ev.get("eventAddress") or {})
    time_str      = _format_time_range(
        (api_ev.get("startDate") or {}).get("time", ""),
        (api_ev.get("endDate")   or {}).get("time", ""),
    )
    return {
        "event_name":  title,
        "description": description,
        "source_url":  url,
        "image_url":   "",   # listing API doesn't expose images
        "start_date":  start,
        "end_date":    end,
        "time":        time_str,
        "location":    location_name,
        "address":     address,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print("Cobb County scraper")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    raw_events = fetch_events(today, window_end)
    print(f"  API returned {len(raw_events)} raw event listing(s)\n")
    if not raw_events:
        return 0

    # Group by normalized title so recurring events / cross-source dupes
    # collapse into one row with all in-window dates collected — same
    # pattern as the JSON-LD scrapers.
    by_name: dict[str, dict] = {}
    skipped_past   = 0
    skipped_future = 0
    skipped_no_data = 0

    for raw in raw_events:
        ev = normalize_event(raw)
        if not ev:
            skipped_no_data += 1
            continue
        sd = ev["start_date"]
        if not sd:
            skipped_no_data += 1
            continue
        if sd > window_end:
            skipped_future += 1
            continue
        if sd < today:
            skipped_past += 1
            continue
        name_key = _normalize_title(ev["event_name"])
        if not name_key:
            continue
        entry = by_name.get(name_key)
        if entry is None:
            ev["all_dates"] = {sd}
            by_name[name_key] = ev
        else:
            entry["all_dates"].add(sd)
            if sd < entry["start_date"]:
                entry["start_date"] = sd

    candidates = sorted(by_name.values(),
                        key=lambda e: e["start_date"] or date.max)

    # Backfill: Cobb County's listing API exposes no image, so EVERY
    # event reaches this point with image_url="". Scrape each detail
    # page for an og:image / JSON-LD / body <img>. Concurrent so the
    # latency cost is manageable.
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
            print(f"  ✓ {dates_disp or ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, "
          f"skipped {skipped_existing} existing, "
          f"{skipped_past} past, "
          f"{skipped_future} beyond {window_end}, "
          f"{skipped_no_data} unparseable  "
          f"({multi_date} multi-date event(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
