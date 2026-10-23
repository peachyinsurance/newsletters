#!/usr/bin/env python3
"""cityoflewisville.com event-calendar scraper for Lewisville Lake Lookout.

Vision Internet CMS but the public calendar page uses the MINI-grid
widget (`calendar_item` classes), not the `vi-events-tiles-item`
list view that visitlewisville.com / dunwoodyga.gov expose. Mini
grid only has title + time + detail-page URL, so we walk the grid
to collect URLs, then fetch each event detail page for the full
JSON-LD `@type=Event` payload.

URL pattern for navigating months:
  /about-lewisville/things-to-do/event-calendar/-curm-5/-cury-2026

We walk current month + next month to cover any 14-day window that
straddles a month boundary.

curl_cffi (Chrome impersonation, via vision_internet._fetch_html)
required: Vision Internet TLS-fingerprints plain Python requests."""
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from urllib.parse import urljoin

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from html_utils  import _clean_html, _normalize_title, _parse_dt  # noqa: E402
from notion_save import existing_source_urls, save_event  # noqa: E402
# Same Vision Internet / Akamai front as visitlewisville.com: reuse the
# shared fetcher that rotates Chrome TLS fingerprints (a lone chrome120
# pin gets 403'd).
from vision_internet import _fetch_html as _fetch  # noqa: E402
from event_date_filter import effective_today, upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,  # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "Lewisville_Lake_Lookout")

BASE_HOST       = "https://www.cityoflewisville.com"
CALENDAR_PATH   = "/about-lewisville/things-to-do/event-calendar"
END_WINDOW_DAYS = 14
DETAIL_THROTTLE_SEC = 0.3


# Mini-calendar grid: each event link → `/Home/Components/Calendar/Event/<ID>/<MOD>`.
# Allow (and strip) trailing query strings — Vision Internet appends
# `?curm=N&cury=YYYY` to the href when the user navigated months.
_EVENT_URL_RE = re.compile(
    r'<a class="calendar_eventlink"\s+href="(/Home/Components/Calendar/Event/\d+/\d+)[^"]*"',
    re.IGNORECASE,
)


def fetch_event_urls(today: date) -> list[str]:
    """Walk current and next month's mini-calendar grids, collect unique
    event detail URLs. URL pattern uses -curm-{N}/-cury-{YYYY}."""
    seen: list[str] = []
    seen_set: set[str] = set()
    months = [today, (today.replace(day=1) + timedelta(days=32)).replace(day=1)]
    for d in months:
        url = f"{BASE_HOST}{CALENDAR_PATH}/-curm-{d.month}/-cury-{d.year}"
        print(f"  Walking {d.strftime('%B %Y')}: {url}")
        html = _fetch(url)
        if not html:
            continue
        urls = _EVENT_URL_RE.findall(html)
        new = 0
        for u in urls:
            full = urljoin(BASE_HOST, u)
            if full not in seen_set:
                seen.append(full)
                seen_set.add(full)
                new += 1
        print(f"    → {len(urls)} link(s), {new} new")
    return seen


def fetch_event(url: str, today: date, window_end: date) -> dict | None:
    """Fetch one event detail page and parse its JSON-LD Event."""
    html = _fetch(url)
    if not html:
        return None
    m = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
                  html, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(1).strip())
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("@type") != "Event":
        return None

    start, start_t = _parse_dt(d.get("startDate"))
    end, end_t = _parse_dt(d.get("endDate"))
    if not start or start < today or start > window_end:
        return None

    name = d.get("name") or ""
    desc = d.get("description") or ""
    if is_cancelled_event(name, desc):
        return None

    loc = d.get("location") or {}
    if isinstance(loc, dict):
        loc_name = loc.get("name") or ""
        address  = loc.get("address") or ""
        if isinstance(address, dict):
            address = ", ".join(filter(None, [
                address.get("streetAddress"),
                address.get("addressLocality"),
                address.get("addressRegion"),
            ]))
    else:
        loc_name = ""
        address  = str(loc)

    if is_inappropriate_event(name, desc, loc_name):
        return None

    time_str = ""
    if start_t:
        time_str = f"{start_t} – {end_t}" if end_t and end_t != start_t else start_t

    return {
        "event_name":  _clean_html(name),
        "description": _clean_html(desc)[:2000],
        "source_url":  url,
        "image_url":   "",
        "start_date":  start,
        "end_date":    end if end and end >= start else start,
        "time":        time_str,
        "location":    _clean_html(loc_name),
        "address":     _clean_html(address),
        "city":        "lewisville",
    }


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = effective_today()  # honors ISSUE_DATE
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print("City of Lewisville calendar scraper (Vision Internet — mini-grid)")
    print(f"  → Base:         {BASE_HOST}{CALENDAR_PATH}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    # Unscoped dedup: geo_tagger re-tags rows by distance after every scrape
    # (see notion_save.existing_source_urls).
    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    urls = fetch_event_urls(today)
    print(f"\nCollected {len(urls)} unique event detail URL(s)\n")
    if not urls:
        return 0

    # Per-occurrence model: one detail page per occurrence (own URL + date).
    # Emit one row per occurrence; de-dupe within this run on (url, date).
    candidates: list[dict] = []
    seen_occ: set[tuple[str, str]] = set()
    skipped_no_data = 0
    for url in urls:
        ev = fetch_event(url, today, window_end)
        if not ev:
            skipped_no_data += 1
            continue
        if not _normalize_title(ev["event_name"]):
            skipped_no_data += 1
            continue
        occ_key = (ev["source_url"], ev["start_date"].isoformat())
        if occ_key not in seen_occ:
            seen_occ.add(occ_key)
            ev["all_dates"] = {ev["start_date"]}
            candidates.append(ev)
        time.sleep(DETAIL_THROTTLE_SEC)

    candidates.sort(key=lambda e: e["start_date"] or date.max)

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from detail pages")

    inserted = 0
    updated  = 0
    print(f"\n━━ Saving {len(candidates)} occurrence(s) ━━")
    for ev in candidates:
        page_id = existing.get((ev["source_url"], ev["start_date"].isoformat()))
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER, page_id=page_id):
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            print(f"  {label} {ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}, "
          f"{skipped_no_data} unparseable / out-of-window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
