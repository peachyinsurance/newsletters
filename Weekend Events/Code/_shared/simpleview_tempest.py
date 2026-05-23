"""Simpleview Tempest CMS scraper — sitemap → detail-page JSON-LD walker.

A surprising number of local tourism / CVB sites run on Simpleview's
Tempest CMS: visitsandysprings.org, discoverdunwoody.com, visitroswellga.com.
They all share the same architecture:

  • Listing page is dynamically rendered (no event HTML to scrape).
  • A sitemap at /sitemaps-1-event-default-1-sitemap.xml lists every
    upcoming event's detail page URL.
  • Each detail page is server-rendered with a JSON-LD `@graph` array
    containing a `@type: "Event"` object (name, dates, location, image).
  • Many sites also list every upcoming recurrence in the body text
    ('May 20, May 27, June 3, …'), so for recurring events we collect
    all in-window occurrences, not just the JSON-LD startDate.

`run_simpleview_tempest(sitemap_url, newsletter)` is the entry point —
per-newsletter wrappers call it with their CVB's sitemap URL and the
appropriate Notion tag.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, timedelta
from urllib.parse import urljoin

import requests

sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from html_utils  import (_clean_html, _parse_iso_date,           # noqa: E402
                         _normalize_title, format_dates_human)
from notion_save import existing_source_urls, save_event          # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,              # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

USER_AGENT      = "Mozilla/5.0"
END_WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Body-text date extraction (for recurring-event next-occurrence rescue).
# Tempest series pages render the full recurrence list as plain text.
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
_DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
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
            r = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
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


def _fetch_event_urls(sitemap_url: str) -> list[str]:
    body = _fetch(sitemap_url)
    if not body:
        return []
    return re.findall(r"<loc>([^<]+)</loc>", body)


def _extract_body_dates(html: str, today: date) -> list[date]:
    """Find every 'Month D[, YYYY]' token in the page body. Year defaults
    to today's year, bumping to next year if the candidate is more than
    60 days in the past."""
    clean = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
    clean = re.sub(r"<style.*?</style>", "", clean, flags=re.DOTALL)
    out: list[date] = []
    for m in _DATE_RE.finditer(clean):
        month_name, day_s, year_s = m.group(1).lower(), m.group(2), m.group(3)
        try:
            month = _MONTHS[month_name]
            day   = int(day_s)
            year  = int(year_s) if year_s else today.year
            d = date(year, month, day)
            if not year_s and (today - d).days > 60:
                d = d.replace(year=today.year + 1)
            out.append(d)
        except (ValueError, KeyError):
            continue
    return out


def _normalize_image(image) -> str:
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        return image.get("url", "") or ""
    if isinstance(image, list) and image:
        first = image[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url", "") or ""
    return ""


def _normalize_location(location) -> tuple[str, str]:
    if not location:
        return "", ""
    if isinstance(location, str):
        return _clean_html(location), ""
    if isinstance(location, dict):
        name = _clean_html(location.get("name", "") or "")
        addr_obj = location.get("address", "")
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


def _fetch_event(url: str, today: date, window_end: date) -> dict | None:
    """Fetch one event detail page, find its JSON-LD Event, normalize."""
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
        if isinstance(data, dict):
            for item in data.get("@graph", []):
                if isinstance(item, dict) and item.get("@type") == "Event":
                    return _build_event(item, html, url, today, window_end)
            # Some Tempest sites put the Event directly in the dict
            if data.get("@type") == "Event":
                return _build_event(data, html, url, today, window_end)
    return None


def _build_event(item: dict, html: str, url: str,
                 today: date, window_end: date) -> dict | None:
    """Build the standard event dict with `all_dates` covering every
    in-window occurrence (JSON-LD start + body-text date mentions)."""
    json_start = _parse_iso_date(item.get("startDate", ""))
    json_end   = _parse_iso_date(item.get("endDate", ""))

    candidate_dates: set[date] = set()
    if json_start and today <= json_start <= window_end:
        candidate_dates.add(json_start)
    for d in _extract_body_dates(html, today):
        if today <= d <= window_end:
            candidate_dates.add(d)

    if not candidate_dates:
        return None

    name = item.get("name", "")
    desc = item.get("description", "")
    if is_cancelled_event(name, desc):
        return None

    start = min(candidate_dates)
    end = json_end if (json_end and json_end >= start
                       and (json_end - start).days <= 1) else None

    loc_name, address = _normalize_location(item.get("location"))
    if is_inappropriate_event(name, desc, loc_name):
        return None

    image_url = _normalize_image(item.get("image"))
    if image_url:
        image_url = urljoin(url, image_url)

    return {
        "event_name":  _clean_html(name),
        "description": _clean_html(desc)[:2000],
        "source_url":  url,
        "image_url":   image_url,
        "start_date":  start,
        "end_date":    end,
        "time":        "",
        "location":    loc_name,
        "address":     address,
        "all_dates":   candidate_dates,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_simpleview_tempest(sitemap_url: str, newsletter: str,
                           *, db_id: str | None = None,
                           end_window_days: int = END_WINDOW_DAYS) -> int:
    """Walk one Simpleview Tempest CVB's event sitemap, fetch each detail
    page for JSON-LD, upsert in-window events into the Weekend Events
    Notion DB tagged with `newsletter`."""
    if db_id is None:
        db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if not db_id:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=end_window_days)

    print(f"Simpleview Tempest scraper")
    print(f"  → Sitemap:      {sitemap_url}")
    print(f"  → Notion DB:    {db_id[:8]}…")
    print(f"  → Newsletter:   {newsletter}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    urls = _fetch_event_urls(sitemap_url)
    print(f"Sitemap yielded {len(urls)} event URL(s)\n")
    if not urls:
        return 0

    existing = existing_source_urls(db_id, newsletter=newsletter)
    print(f"Dedup: {len(existing)} URLs already in DB for {newsletter}\n")

    # Group by normalized title so recurring series and duplicate URLs
    # collapse into one Notion row with the union of in-window dates.
    by_name: dict[str, dict] = {}
    skipped_no_data = 0
    for url in urls:
        ev = _fetch_event(url, today, window_end)
        if not ev:
            skipped_no_data += 1
            continue
        name_key = _normalize_title(ev["event_name"])
        if not name_key:
            skipped_no_data += 1
            continue
        entry = by_name.get(name_key)
        if entry is None:
            by_name[name_key] = ev
        else:
            entry["all_dates"] = (entry.get("all_dates") or set()) | (ev.get("all_dates") or set())
            new_earliest = min(entry["all_dates"])
            if new_earliest < entry["start_date"]:
                entry["start_date"] = new_earliest
        time.sleep(0.3)   # be kind to the host

    candidates = sorted(by_name.values(),
                        key=lambda e: e["start_date"] or date.max)

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from event detail pages")

    inserted = 0
    updated  = 0
    multi_date = 0
    print(f"\n━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        if len(ev.get("all_dates") or {}) > 1:
            multi_date += 1
        page_id = existing.get(ev["source_url"])
        if save_event(db_id, ev, newsletter, page_id=page_id):
            dates_disp = format_dates_human(ev.get("all_dates") or [])
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            print(f"  {label} {dates_disp or ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}, "
          f"{skipped_no_data} unparseable  "
          f"({multi_date} multi-date event(s))")
    return 0
