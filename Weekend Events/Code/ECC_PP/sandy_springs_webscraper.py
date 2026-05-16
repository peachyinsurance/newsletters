#!/usr/bin/env python3
"""Scrape visitsandysprings.org for events shared between East Cobb
Connect and Perimeter Post (NEWSLETTER tag = ECC_PP).

Sandy Springs uses Simpleview Tempest CMS — the /events/ listing page
renders client-side, so we can't parse it like the other ECC scrapers
do. Instead, the site exposes a sitemap with every event detail page:

    https://www.visitsandysprings.org/sitemaps-1-event-default-1-sitemap.xml

Each detail page is server-rendered with a JSON-LD `Event` object
nested inside the page's `@graph` array.

Recurring-event handling: the JSON-LD `startDate` on a series page is
the SERIES start (e.g., a weekly pub quiz shows Feb 4 even though the
next occurrence is next Tuesday). Detail pages also list every upcoming
occurrence in the body text ('May 20, May 27, June 3, …'). When the
JSON-LD date is past, we parse those body-text dates and use the
EARLIEST FUTURE one as the event's start_date.

Shared helpers (_clean_html, _parse_iso_date, _normalize_title,
existing_source_urls, save_event) are imported from the ECC scraper to
avoid drift.
"""
import os
import re
import sys
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests

# Pull shared helpers from the sibling ECC scraper module.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ECC"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from ecc_event_webscraper import (  # noqa: E402
    _clean_html,
    _parse_iso_date,
    _normalize_title,
    existing_source_urls,
    save_event,
    format_dates_human,
)
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "ECC_PP")

SITEMAP_URL = "https://www.visitsandysprings.org/sitemaps-1-event-default-1-sitemap.xml"
USER_AGENT  = "Mozilla/5.0"
END_WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# HTTP fetch with retry (mirrors the ECC scraper's retry policy)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Sitemap parse → list of event detail URLs
# ---------------------------------------------------------------------------
def fetch_event_urls() -> list[str]:
    body = _fetch(SITEMAP_URL)
    if not body:
        return []
    return re.findall(r"<loc>([^<]+)</loc>", body)


# ---------------------------------------------------------------------------
# Body-text date extraction (for recurring-event next-occurrence rescue)
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
_DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
    re.IGNORECASE,
)


def _extract_body_dates(html: str, today: date) -> list[date]:
    """Find every 'Month D[, YYYY]' token in the page body. Year defaults
    to today's year, bumping to next year if the candidate is more than
    60 days in the past (so 'May 20' read in late-May resolves to next
    week if appropriate, but a true 6-months-ago date doesn't get
    treated as 6-months-from-now)."""
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


# ---------------------------------------------------------------------------
# Detail-page fetch → normalized event dict
# ---------------------------------------------------------------------------
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


def fetch_event(url: str, today: date, window_end: date) -> dict | None:
    """Fetch one event detail page and return a normalized event dict
    (or None if no JSON-LD Event is found or no in-window date exists)."""
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
        if not isinstance(data, dict):
            continue
        for item in data.get("@graph", []):
            if isinstance(item, dict) and item.get("@type") == "Event":
                return _build_event(item, html, url, today, window_end)
    return None


def _build_event(item: dict, html: str, url: str,
                 today: date, window_end: date) -> dict | None:
    """Build an event dict with `all_dates` = the set of every in-window
    occurrence. Sandy Springs series pages list each upcoming date in the
    body text ('May 20, May 27, June 3, …'), so for recurring events we
    collect every one of those that falls in our window, not just the
    earliest."""
    json_start = _parse_iso_date(item.get("startDate", ""))
    json_end   = _parse_iso_date(item.get("endDate", ""))

    # Candidate dates: JSON-LD start (if in window) + body-text dates.
    candidate_dates: set[date] = set()
    if json_start and today <= json_start <= window_end:
        candidate_dates.add(json_start)
    for d in _extract_body_dates(html, today):
        if today <= d <= window_end:
            candidate_dates.add(d)

    if not candidate_dates:
        return None

    start = min(candidate_dates)
    end = json_end if (json_end and json_end >= start
                       and (json_end - start).days <= 1) else None

    loc_name, address = _normalize_location(item.get("location"))
    # Sandy Springs' JSON-LD frequently uses host-relative image paths
    # like "/imager/cmsimages/…". Absolutize against the source URL so
    # downstream consumers (header builder, GIF maker) can fetch them.
    image_url = _normalize_image(item.get("image"))
    if image_url:
        image_url = urljoin(url, image_url)
    return {
        "event_name":  _clean_html(item.get("name", "")),
        "description": _clean_html(item.get("description", ""))[:2000],
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
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1
    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print(f"Sandy Springs scraper")
    print(f"  Sitemap:        {SITEMAP_URL}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    urls = fetch_event_urls()
    print(f"Sitemap yielded {len(urls)} event URL(s)\n")
    if not urls:
        return 0

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    # Group by normalized title so recurring series (one URL, many body
    # dates) and any duplicate URLs collapse into one Notion row with the
    # union of all in-window dates.
    by_name: dict[str, dict] = {}
    skipped_no_data = 0
    for url in urls:
        ev = fetch_event(url, today, window_end)
        if not ev:
            skipped_no_data += 1
            print(f"  · no in-window event data: {url}")
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
        time.sleep(0.3)  # be kind to the host between detail-page fetches

    candidates = sorted(by_name.values(), key=lambda e: e["start_date"] or date.max)

    inserted = 0
    skipped_existing = 0
    multi_date = 0
    print(f"\n━━ Saving {len(candidates)} unique event(s) ━━")
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
          f"{skipped_no_data} unparseable  "
          f"({multi_date} multi-date event(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
