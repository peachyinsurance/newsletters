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
from datetime import date, datetime, timedelta

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from html_utils  import _clean_html, _normalize_title, format_dates_human  # noqa: E402
from notion_save import existing_source_urls, save_event  # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
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
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
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


def _parse_dt(s) -> tuple[date | None, str]:
    """ISO datetime → (date, time-of-day or '')."""
    if not s:
        return None, ""
    s = str(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date(), dt.strftime("%-I:%M %p") if (dt.hour or dt.minute) else ""
    except Exception:
        pass
    try:
        return date.fromisoformat(s[:10]), ""
    except Exception:
        return None, ""


def _normalize_location(loc) -> tuple[str, str]:
    if not loc:
        return "", ""
    if isinstance(loc, str):
        return _clean_html(loc), ""
    if isinstance(loc, dict):
        name = _clean_html(loc.get("name", "") or "")
        addr_obj = loc.get("address", "")
        if isinstance(addr_obj, dict):
            parts = [
                addr_obj.get("streetAddress", ""),
                addr_obj.get("addressLocality", ""),
                addr_obj.get("addressRegion", ""),
            ]
            addr = _clean_html(", ".join(p for p in parts if p))
        else:
            addr = _clean_html(str(addr_obj or ""))
        return name, addr
    return "", ""


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

    loc_name, address = _normalize_location(item.get("location"))
    if is_inappropriate_event(name, desc, loc_name):
        return None

    time_str = ""
    if start_t:
        time_str = f"{start_t} – {end_t}" if end_t and end_t != start_t else start_t

    image = item.get("image") or ""
    if isinstance(image, dict):
        image = image.get("url", "") or ""
    elif isinstance(image, list) and image:
        first = image[0]
        image = first if isinstance(first, str) else (first.get("url", "") if isinstance(first, dict) else "")

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

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID, newsletter=NEWSLETTER)
    print(f"Dedup: {len(existing)} URLs already in DB for {NEWSLETTER}\n")

    by_name: dict[str, dict] = {}
    skipped_no_data = 0
    for url in urls:
        ev = fetch_event(url, today, window_end)
        if not ev:
            skipped_no_data += 1
            continue
        name_key = _normalize_title(ev["event_name"])
        if not name_key:
            skipped_no_data += 1
            continue
        entry = by_name.get(name_key)
        if entry is None:
            ev["all_dates"] = {ev["start_date"]}
            by_name[name_key] = ev
        else:
            entry["all_dates"].add(ev["start_date"])
            if ev["start_date"] < entry["start_date"]:
                entry["start_date"] = ev["start_date"]
        time.sleep(DETAIL_THROTTLE_SEC)

    candidates = sorted(by_name.values(),
                        key=lambda e: e["start_date"] or date.max)

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from detail pages")

    inserted = 0
    updated  = 0
    multi_date = 0
    print(f"\n━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        if len(ev.get("all_dates") or {}) > 1:
            multi_date += 1
        page_id = existing.get(ev["source_url"])
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER, page_id=page_id):
            dates_disp = format_dates_human(ev.get("all_dates") or [])
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            print(f"  {label} {dates_disp or ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}, "
          f"{skipped_no_data} unparseable  ({multi_date} multi-date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
