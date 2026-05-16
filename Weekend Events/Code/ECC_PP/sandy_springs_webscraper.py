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


def fetch_event(url: str, today: date) -> dict | None:
    """Fetch one event detail page and return a normalized event dict
    (or None if no JSON-LD Event is found)."""
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
                return _build_event(item, html, url, today)
    return None


def _build_event(item: dict, html: str, url: str, today: date) -> dict:
    start = _parse_iso_date(item.get("startDate", ""))
    end   = _parse_iso_date(item.get("endDate", ""))

    # Series pages list the SERIES start in JSON-LD. When that's already
    # past, scan the body text for the next upcoming occurrence and use
    # it as the saved date.
    if start and start < today:
        body_future = sorted(d for d in _extract_body_dates(html, today) if d >= today)
        if body_future:
            start = body_future[0]
            if end and end < start:
                end = None

    loc_name, address = _normalize_location(item.get("location"))
    return {
        "event_name":  _clean_html(item.get("name", "")),
        "description": _clean_html(item.get("description", ""))[:2000],
        "source_url":  url,
        "image_url":   _normalize_image(item.get("image")),
        "start_date":  start,
        "end_date":    end,
        "time":        "",
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

    by_url: dict[str, dict] = {}
    skipped_past   = 0
    skipped_future = 0
    skipped_no_data = 0
    for url in urls:
        ev = fetch_event(url, today)
        if not ev:
            skipped_no_data += 1
            print(f"  · no event data: {url}")
            continue
        sd = ev.get("start_date")
        if not sd:
            skipped_no_data += 1
            print(f"  · no usable date: {ev['event_name'][:60]}")
            continue
        if sd > window_end:
            skipped_future += 1
            continue
        if sd < today:
            skipped_past += 1
            continue
        prior = by_url.get(url)
        if prior is None or sd < prior["start_date"]:
            by_url[url] = ev
        time.sleep(0.3)  # be kind to the host between detail-page fetches

    # Name-level dedup so an event also added by a different scraper run
    # (or listed twice under different slugs) collapses to one row.
    candidates = sorted(by_url.values(), key=lambda e: e["start_date"] or date.max)
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
          f"{skipped_past} past, "
          f"{skipped_future} beyond {window_end}, "
          f"{skipped_no_data} unparseable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
