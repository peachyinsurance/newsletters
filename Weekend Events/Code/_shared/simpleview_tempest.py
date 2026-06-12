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
from datetime import date, datetime, timedelta
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


def _normalize_location(location) -> tuple[str, str, str]:
    """Returns (location_name, address, city). City is returned separately
    for the Notion City column / shared-tag sweep."""
    if not location:
        return "", "", ""
    if isinstance(location, str):
        return _clean_html(location), "", ""
    if isinstance(location, dict):
        name = _clean_html(location.get("name", "") or "")
        city = ""
        addr_obj = location.get("address", "")
        if isinstance(addr_obj, dict):
            city = (addr_obj.get("addressLocality", "") or "").strip()
            parts = [
                addr_obj.get("streetAddress", ""),
                city,
                addr_obj.get("addressRegion", ""),
            ]
            addr = _clean_html(", ".join(p for p in parts if p))
        else:
            addr = _clean_html(str(addr_obj or ""))
        return name, addr, _clean_html(city).lower()
    return "", "", ""


# ---------------------------------------------------------------------------
# Body fallbacks for events whose JSON-LD `location` is degenerate.
#
# Some Tempest events ship a JSON-LD location where the venue/address is
# just the event title repeated (e.g. visitroswellga dance-camp →
# {"address": "Dance Camp", "name": "Dance Camp"}). The page itself still
# carries the real data: schema.org PostalAddress microdata for the
# address and a bold "Location:" list item for the venue name.
# ---------------------------------------------------------------------------
def _extract_microdata_address(html: str) -> tuple[str, str]:
    """Pull (address, city) from the page's <meta itemprop> PostalAddress
    microdata. addressRegion is often blank in the meta — recover it from
    the visible 'City, ST' text when needed."""
    def _meta(prop: str) -> str:
        m = re.search(rf'<meta\s+itemprop=["\']{prop}["\']\s+content=["\']([^"\']*)["\']',
                      html, re.IGNORECASE)
        return (m.group(1).strip() if m else "")

    street   = _meta("streetAddress")
    locality = _meta("addressLocality")
    region   = _meta("addressRegion")
    postal   = _meta("postalCode")
    if not (street or locality):
        return "", ""
    if not region and locality:
        mm = re.search(rf'{re.escape(locality)},\s*([A-Z]{{2}})\b', html)
        region = mm.group(1) if mm else ""
    parts = [p for p in (street, locality, region, postal) if p]
    return _clean_html(", ".join(parts)), _clean_html(locality).lower()


def _extract_body_venue(html: str) -> str:
    """The real venue from the bold 'Location:' list item."""
    m = re.search(r'Location:\s*</span>\s*([^<]+)</li>', html, re.IGNORECASE)
    return _clean_html(m.group(1).strip()) if m else ""


def _format_time_range_iso(start_str: str, end_str: str) -> str:
    """JSON-LD start/end (e.g. '2026-06-01T10:00:00-04:00') → '10:00 AM –
    3:30 PM'. The offset is the venue's own local time, so no conversion is
    needed. Empty for all-day / midnight (time-less) listings."""
    def _p(s: str):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None
    sdt = _p(start_str)
    if sdt is None or not (sdt.hour or sdt.minute):
        return ""
    out = sdt.strftime("%-I:%M %p")
    edt = _p(end_str)
    if edt and (edt.hour or edt.minute) and \
            (edt.hour, edt.minute) != (sdt.hour, sdt.minute):
        out += " – " + edt.strftime("%-I:%M %p")
    return out


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

    loc_name, address, city = _normalize_location(item.get("location"))

    # A real street address contains a number. When the JSON-LD location is
    # degenerate (no digits — usually the event title repeated), recover the
    # real address from the page's PostalAddress microdata and the venue from
    # the body "Location:" label.
    if not re.search(r"\d", address or ""):
        md_addr, md_city = _extract_microdata_address(html)
        if md_addr:
            address = md_addr
            city = md_city or city
        body_venue = _extract_body_venue(html)
        if body_venue:
            loc_name = body_venue

    if is_inappropriate_event(name, desc, loc_name):
        return None

    # The listing hardcoded no time; derive it from the JSON-LD start/end,
    # whose offsets are already the venue's local time.
    time_str = _format_time_range_iso(item.get("startDate", ""),
                                      item.get("endDate", ""))

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
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
        "city":        city,
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

    # Per-occurrence model: Tempest has ONE detail page per event that may
    # span several in-window dates (recurring series). Emit one occurrence
    # row per date — all sharing the single URL, each with its own Date — so
    # the Weekend Planner links every per-day card to the correct day with no
    # JSON date→url map. De-dupe within this run on (url, date).
    candidates: list[dict] = []
    seen_occ: set[tuple[str, str]] = set()
    skipped_no_data = 0
    for url in urls:
        ev = _fetch_event(url, today, window_end)
        if not ev:
            skipped_no_data += 1
            continue
        if not _normalize_title(ev["event_name"]):
            skipped_no_data += 1
            continue
        for d in sorted(ev.get("all_dates") or {ev["start_date"]}):
            occ_key = (ev["source_url"], d.isoformat())
            if occ_key in seen_occ:
                continue
            seen_occ.add(occ_key)
            occ = dict(ev)
            occ["start_date"] = d
            occ["all_dates"]  = {d}
            # end_date (contiguous 2-day span) only applies to the row dated
            # to the original start; other split occurrences are single-day.
            occ["end_date"] = ev.get("end_date") if d == ev.get("start_date") else None
            candidates.append(occ)
        time.sleep(0.3)   # be kind to the host

    candidates.sort(key=lambda e: e["start_date"] or date.max)

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from event detail pages")

    inserted = 0
    updated  = 0
    print(f"\n━━ Saving {len(candidates)} occurrence(s) ━━")
    for ev in candidates:
        page_id = existing.get((ev["source_url"], ev["start_date"].isoformat()))
        if save_event(db_id, ev, newsletter, page_id=page_id):
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
