#!/usr/bin/env python3
"""roswell365.com community calendar scraper.

(Linked from roswellgov.com/roswell-365-events/ — roswell365 is where
the actual events live.)

Two-stage walker:
  1. Listing page lists events as `<a href="https://roswell365.com/event/<slug>/">`
     anchors. We extract every event-detail URL across paginated pages
     (?event_page=N).
  2. Each detail page renders a clean JSON-LD `@type: "Event"` object with
     name / startDate / endDate / location. Parse, normalize, save.

Tag: ECC_PP (shared) — Roswell sits between East Cobb and Perimeter,
both newsletters' readers might drive there.
"""
import json
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from html_utils  import (_clean_html, _normalize_title, _parse_dt,  # noqa: E402
                         _normalize_image, _normalize_location,
                         fetch_with_retry)
from notion_save import existing_source_urls, save_event  # noqa: E402
from event_date_filter import effective_today, upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,  # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "ECC_PP")

LISTING_URL    = "https://roswell365.com/event/"
USER_AGENT     = "Mozilla/5.0 (newsletter-automation)"
END_WINDOW_DAYS = 14
# Cap pagination — most weeks page 1 + 2 cover the next 2 weeks fine.
MAX_LISTING_PAGES = 3
DETAIL_THROTTLE_SEC = 0.3


_EVENT_URL_RE = re.compile(
    r'href="(https://roswell365\.com/event/[a-z0-9][a-z0-9\-/]+/)"',
    re.IGNORECASE,
)


def _fetch(url: str) -> str:
    headers = {
        "User-Agent":      USER_AGENT,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    return fetch_with_retry(url, headers, timeout=15)


def fetch_event_urls() -> list[str]:
    """Walk listing pages, collect unique /event/<slug>/ URLs. Stops
    when a page contains no new URLs (calendar wrapped) or we hit
    MAX_LISTING_PAGES."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for page in range(1, MAX_LISTING_PAGES + 1):
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?event_page={page}"
        html = _fetch(url)
        if not html:
            break
        urls = _EVENT_URL_RE.findall(html)
        new_count = 0
        for u in urls:
            # Filter out sub-paths like /event/category/ /event/list/ /event/?...
            if u.rstrip("/").count("/") < 4:  # roswell365.com / event / slug
                continue
            if u in seen_set:
                continue
            seen.append(u)
            seen_set.add(u)
            new_count += 1
        print(f"  [page {page}] {len(urls)} link(s), {new_count} new")
        if new_count == 0:
            break
    return seen


def fetch_event(url: str, today: date, window_end: date) -> dict | None:
    """Fetch one event detail page, return normalized event dict."""
    html = _fetch(url)
    if not html:
        return None
    for blob in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
        html, re.DOTALL,
    ):
        try:
            data = json.loads(blob.strip())
        except json.JSONDecodeError:
            continue
        items = [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Event":
                return _build_event(item, url, today, window_end)
    return None


def _build_event(item: dict, url: str,
                 today: date, window_end: date) -> dict | None:
    start, start_t = _parse_dt(item.get("startDate"))
    end, end_t = _parse_dt(item.get("endDate"))
    if not start:
        return None
    if start < today or start > window_end:
        return None

    name = item.get("name", "")
    desc = item.get("description", "")
    if is_cancelled_event(name, desc):
        return None

    loc_name, address, city = _normalize_location(item.get("location"))
    if is_inappropriate_event(name, desc, loc_name):
        return None

    time_str = ""
    if start_t:
        time_str = f"{start_t} – {end_t}" if end_t and end_t != start_t else start_t

    image = _normalize_image(item.get("image"))

    return {
        "event_name":  _clean_html(name),
        "description": _clean_html(desc)[:2000],
        "source_url":  url,
        "image_url":   image or "",
        "start_date":  start,
        "end_date":    end if end and end >= start else start,
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
        "city":        city or "roswell",   # fallback — every event on roswell365 is in Roswell
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = effective_today()  # honors ISSUE_DATE
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print("Roswell 365 scraper")
    print(f"  → Listing:      {LISTING_URL}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print(f"  → Max pages:    {MAX_LISTING_PAGES}")
    print()

    urls = fetch_event_urls()
    print(f"\nListing yielded {len(urls)} unique event URL(s)\n")
    if not urls:
        return 0

    # Unscoped dedup: geo_tagger re-tags rows by distance after every scrape
    # (see notion_save.existing_source_urls).
    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

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
          f"{skipped_no_data} unparseable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
