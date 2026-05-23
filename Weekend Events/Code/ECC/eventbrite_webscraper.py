#!/usr/bin/env python3
"""Eventbrite scraper — via Apify's aitorsm/eventbrite actor.

Replaces the prior direct HTML scrape which started returning HTTP 405
from Eventbrite's bot detection on GitHub Actions data-center IPs (the
same URLs return 200 fine from a residential IP). curl_cffi with
Chrome TLS impersonation didn't reliably break the block.

Apify runs the scraper on residential proxies, so the IP block goes
away. Output is upserted into the Weekend Events Notion DB via the
shared save_event helper (same as the 4 other weekend-events scrapers).

Cost: aitorsm/eventbrite charges $0.02 per event scraped (pay-per-event
pricing). With city=marietta, no category filter, and maxPages=3 the
typical run pulls ~60-150 events ≈ $1.20-$3.00 per scrape. Weekly use
runs roughly $5-12/month. Tune MAX_PAGES down to cap cost.

Set EVENTBRITE_DEBUG=1 to dump the first raw item — useful first time
the actor schema changes and we need to re-map field names.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

# Sibling-folder import of shared helpers + Notion save.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from ecc_event_webscraper import (  # noqa: E402
    _clean_html,
    existing_source_urls,
    save_event,
)
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import is_cancelled_event, backfill_images  # noqa: E402

APIFY_API_KEY        = os.environ.get("APIFY_API_KEY", "")
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER           = os.environ.get("NEWSLETTER", "East_Cobb_Connect")
DEBUG                = os.environ.get("EVENTBRITE_DEBUG", "") == "1"

ACTOR_ID    = "aitorsm~eventbrite"   # tilde-form for the API path
COUNTRY     = "united-states"
CITY        = "marietta"             # passed to Apify; Eventbrite resolves loosely
MAX_PAGES   = 3                      # caps cost (Apify charges $0.02/event)
END_WINDOW_DAYS = 14

# Post-fetch city allow-list. Eventbrite's "marietta" search returns
# events from Atlanta, Kennesaw, Powder Springs, etc. — we keep only
# the ones whose venue city is in our newsletter's coverage area.
ALLOWED_CITIES = {
    "marietta", "east cobb", "kennesaw", "smyrna", "vinings",
    "sandy springs", "dunwoody", "atlanta",
}


# ---------------------------------------------------------------------------
# Apify call
# ---------------------------------------------------------------------------
def fetch_events(start: date, end: date) -> list[dict]:
    """Trigger a sync Apify run and return the dataset items."""
    payload = {
        "country":   COUNTRY,
        "city":      CITY,
        "category":  "",                       # all categories
        "startDate": start.isoformat(),
        "endDate":   end.isoformat(),
        "maxPages":  MAX_PAGES,
    }
    print(f"  Calling Apify actor {ACTOR_ID}")
    print(f"    city={CITY}, window={start}..{end}, maxPages={MAX_PAGES}")
    try:
        r = requests.post(
            f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items",
            headers={"Authorization": f"Bearer {APIFY_API_KEY}",
                     "Content-Type":  "application/json"},
            json=payload, timeout=600,
        )
    except Exception as e:
        print(f"  ✗ Apify request error: {e}")
        return []
    if r.status_code not in (200, 201):
        print(f"  ✗ Apify HTTP {r.status_code}: {r.text[:400]}")
        return []
    items = r.json() or []
    print(f"  Apify returned {len(items)} item(s)")
    if DEBUG and items:
        print(f"  [DEBUG] first item keys: {sorted(items[0].keys())}")
        print(f"  [DEBUG] first item:\n{json.dumps(items[0], indent=2, default=str)[:2000]}")
    return items


# ---------------------------------------------------------------------------
# Normalize one Apify event → our standard event dict
# ---------------------------------------------------------------------------
def _parse_date(s) -> date | None:
    if not s:
        return None
    s = str(s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _first_str(d: dict, *keys) -> str:
    """Try each key on `d`; return the first non-empty string value."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for inner in ("url", "src", "name", "value"):
                iv = v.get(inner)
                if isinstance(iv, str) and iv.strip():
                    return iv.strip()
    return ""


def normalize_event(item: dict) -> dict | None:
    """Map an Apify aitorsm/eventbrite item to our standard event dict.
    Defensive about field names — the actor may rename fields between
    versions. Returns None if essential fields (name, url) are missing
    or if the event is marked cancelled."""
    name = _first_str(item, "name", "title", "eventName")
    url  = _first_str(item, "url", "eventUrl", "link", "permalink")
    if not name or not url:
        return None

    description = _clean_html(_first_str(item, "description", "summary",
                                         "fullDescription", "shortDescription"))[:2000]
    if is_cancelled_event(name, description):
        return None

    # Dates — Apify items typically expose startDate / endDate ISO strings.
    # Some shapes nest under "dates" or "schedule".
    start_raw = (_first_str(item, "startDate", "start", "starts", "dateStart")
                 or (item.get("dates") or {}).get("start")
                 or (item.get("schedule") or {}).get("start"))
    end_raw   = (_first_str(item, "endDate", "end", "ends", "dateEnd")
                 or (item.get("dates") or {}).get("end")
                 or (item.get("schedule") or {}).get("end"))
    start = _parse_date(start_raw)
    end   = _parse_date(end_raw) or start

    # Time — try to extract HH:MM from the start datetime
    time_str = ""
    if start_raw and "T" in str(start_raw):
        try:
            sdt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            if sdt.hour or sdt.minute:
                time_str = sdt.strftime("%-I:%M %p")
                if end_raw and "T" in str(end_raw):
                    try:
                        edt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                        if edt.hour or edt.minute:
                            time_str += " – " + edt.strftime("%-I:%M %p")
                    except Exception:
                        pass
        except Exception:
            pass

    # Venue — could be dict with nested address, or flat strings
    venue = item.get("venue") or item.get("location") or {}
    if isinstance(venue, dict):
        loc_name = _first_str(venue, "name", "title")
        addr_parts = []
        for k in ("address", "streetAddress", "address1", "addressLine1"):
            v = venue.get(k)
            if isinstance(v, dict):
                v = _first_str(v, "localizedAddressDisplay", "address1",
                                  "streetAddress", "line1")
            if isinstance(v, str) and v.strip() and v.strip() not in addr_parts:
                addr_parts.append(v.strip())
        city_str = _first_str(venue, "city", "localityName", "town").lower()
        region   = _first_str(venue, "region", "state", "stateName")
        if city_str:
            addr_parts.append(city_str.title())
        if region:
            addr_parts.append(region)
        address = ", ".join(addr_parts)
    else:
        loc_name = str(venue or "")
        address  = ""
        city_str = ""

    image = _first_str(item, "image", "imageUrl", "logo", "thumbnail", "imageURL")

    return {
        "event_name":  name,
        "description": description,
        "source_url":  url,
        "image_url":   image,
        "start_date":  start,
        "end_date":    end,
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
        "city":        city_str,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not APIFY_API_KEY:
        print("✗ APIFY_API_KEY is not set in env.")
        return 1
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today = date.today()
    start = _upcoming_friday(today)
    end   = start + timedelta(days=END_WINDOW_DAYS)

    print("Eventbrite scraper (via Apify)")
    print(f"  → Actor:        {ACTOR_ID}")
    print(f"  → City search:  {CITY}  (then filtered to {sorted(ALLOWED_CITIES)})")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {start} → {end}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    raw_events = fetch_events(start, end)
    if not raw_events:
        return 0

    candidates: list[dict] = []
    skipped_city    = 0
    skipped_no_data = 0
    skipped_dup     = 0
    seen_urls: set[str] = set()
    for raw in raw_events:
        ev = normalize_event(raw)
        if not ev:
            skipped_no_data += 1
            continue
        if ev["source_url"] in seen_urls:
            skipped_dup += 1
            continue
        seen_urls.add(ev["source_url"])
        # City filter — only Marietta-area events. Online-only / empty city
        # is dropped (no venue context to evaluate).
        city = ev.get("city", "")
        if not city or city not in ALLOWED_CITIES:
            skipped_city += 1
            continue
        candidates.append(ev)

    # Some Apify items may arrive without an image; backfill from the
    # event detail page (og:image / JSON-LD). Concurrent so latency cost
    # is bounded.
    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from source pages")

    inserted = 0
    updated  = 0
    print(f"━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        page_id = existing.get(ev["source_url"])
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER, page_id=page_id):
            label = "↻" if page_id else "✓"
            disp_date = ev["start_date"] or "no-date"
            if page_id:
                updated += 1
            else:
                inserted += 1
            print(f"  {label} {disp_date}  {ev['event_name'][:60]}  ({ev.get('city','?')})")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}, "
          f"{skipped_city} wrong city, "
          f"{skipped_dup} duplicates, "
          f"{skipped_no_data} unparseable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
