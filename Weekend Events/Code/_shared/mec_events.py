"""Modern Events Calendar (MEC) WordPress plugin scraper — LIBRARY.

A surprising number of nature centers, museums, and arts venues run
WordPress + Modern Events Calendar (MEC) plugin. They all share the
same architecture:

  • WP REST API at /wp-json/wp/v2/mec-events lists all events with
    titles, URLs, excerpts, featured_media IDs.
  • The actual event start datetime is NOT exposed in the REST
    response (MEC stores it in postmeta, which isn't part of the
    default WP REST schema). We have to fetch each detail page.
  • Detail pages render `data-datetime="Thu Jun 11 2026 11:30:00"`
    on the MEC countdown widget — that's our authoritative start.
  • Detail pages also have a `mec-single-event-time` block with
    'HH:MM AM - HH:MM PM' display and a `mec-address` span with
    the venue street address.

`run_mec_source(site_url, newsletter, venue_*)` is the entry point.
Per-newsletter wrappers call it with their MEC site's base URL,
the appropriate Notion tag, and (for single-venue sites) the city
and state — most MEC sites are a single venue hosting all events
at their own location, so hardcoding city/state avoids the
'address has no city' parsing trap.

Cost: 100% free (direct HTTP). One REST call + N detail-page
fetches where N is bounded by the per_page parameter (default 100).
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
from html_utils  import _clean_html, _normalize_title              # noqa: E402
from notion_save import existing_source_urls, save_event           # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,               # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

USER_AGENT      = "Mozilla/5.0"
END_WINDOW_DAYS = 14
PER_PAGE        = 100   # WP REST hard caps at 100; covers most MEC sites


# ---------------------------------------------------------------------------
# Low-level fetch with retry
# ---------------------------------------------------------------------------
def _fetch(url: str, accept: str = "text/html") -> str:
    headers = {
        "User-Agent":      USER_AGENT,
        "Accept":          accept,
        "Accept-Language": "en-US,en;q=0.5",
    }
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20, headers=headers,
                             allow_redirects=True)
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
# MEC date extraction from detail page
# ---------------------------------------------------------------------------
# MEC's countdown widget: data-datetime="Thu Jun 11 2026 11:30:00"
_DATETIME_RE = re.compile(r'data-datetime="([^"]+)"')
# Fallback: human label inside mec-start-date-label span: "Jun 11 2026"
_DATE_LABEL_RE = re.compile(
    r'<span class="mec-start-date-label">([^<]+)</span>')
# Time display: "9:00 AM - 11:30 AM" inside mec-single-event-time block
_TIME_RE = re.compile(
    r'mec-single-event-time.{0,400}?(\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M)',
    re.DOTALL | re.IGNORECASE)
# Venue street address: mec-address">9135 Willeo Rd
_ADDRESS_RE = re.compile(r'mec-address"[^>]*>([^<]+)')
# Venue name: usually the <h3 class="mec-event-venue"> or similar
_VENUE_RE = re.compile(
    r'class="mec-single-event-location".{0,800}?<dd[^>]*>([^<]+)</dd>',
    re.DOTALL)


def _parse_data_datetime(s: str) -> datetime | None:
    """Parse MEC's `data-datetime="Thu Jun 11 2026 11:30:00"`."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%a %b %d %Y %H:%M:%S",
                "%a %b %d %Y %H:%M",
                "%b %d %Y %H:%M:%S",
                "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _extract_event_from_detail(html: str) -> tuple[date | None, str, str, str]:
    """Returns (start_date, time_display, venue_name, address).

    Falls back gracefully if any field is missing. Start date is the
    only required field — caller drops the event if it's None."""
    start: date | None = None
    m = _DATETIME_RE.search(html)
    if m:
        dt = _parse_data_datetime(m.group(1))
        if dt:
            start = dt.date()
    if start is None:
        m = _DATE_LABEL_RE.search(html)
        if m:
            dt = _parse_data_datetime(m.group(1).strip())
            if dt:
                start = dt.date()

    time_disp = ""
    m = _TIME_RE.search(html)
    if m:
        time_disp = re.sub(r"\s+", " ", m.group(1)).strip()

    venue_name = ""
    m = _VENUE_RE.search(html)
    if m:
        venue_name = _clean_html(m.group(1)).strip()

    address = ""
    m = _ADDRESS_RE.search(html)
    if m:
        address = _clean_html(m.group(1)).strip()

    return start, time_disp, venue_name, address


# ---------------------------------------------------------------------------
# WP REST list pull
# ---------------------------------------------------------------------------
def _fetch_mec_events(site_url: str) -> list[dict]:
    """Hit /wp-json/wp/v2/mec-events?per_page=100 and return the raw
    array. Empty list on any HTTP error."""
    api_url = f"{site_url.rstrip('/')}/wp-json/wp/v2/mec-events?per_page={PER_PAGE}"
    body = _fetch(api_url, accept="application/json")
    if not body:
        return []
    try:
        items = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"    ✗ JSON decode error: {e}")
        return []
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_mec_source(site_url: str,
                   newsletter: str,
                   *,
                   venue_name: str | None = None,
                   venue_city: str | None = None,
                   venue_state: str | None = None,
                   db_id: str | None = None,
                   end_window_days: int = END_WINDOW_DAYS) -> int:
    """Walk one MEC WordPress site, upsert in-window events into the
    Weekend Events Notion DB tagged with `newsletter`.

    `venue_name` / `venue_city` / `venue_state` override per-event
    parsing. Most MEC sites are single-venue (a nature center, a
    museum, an arts center) hosting all events at their own location,
    so hardcoding venue + city in the wrapper is cleaner than fighting
    site-specific regex selectors on the detail pages. Use lowercase
    for city (e.g. "roswell") and 2-letter postal for state ("GA")."""
    if db_id is None:
        db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if not db_id:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=end_window_days)

    print(f"MEC scraper")
    print(f"  → Site:         {site_url}")
    print(f"  → Notion DB:    {db_id[:8]}…")
    print(f"  → Newsletter:   {newsletter}")
    print(f"  → Venue:        city={venue_city or '<parse>'}  state={venue_state or '<parse>'}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    raw = _fetch_mec_events(site_url)
    print(f"WP REST returned {len(raw)} event(s)\n")
    if not raw:
        return 0

    existing = existing_source_urls(db_id, newsletter=newsletter)
    print(f"Dedup: {len(existing)} URLs already in DB for {newsletter}\n")

    candidates: list[dict] = []
    skipped_no_date    = 0
    skipped_out_window = 0
    skipped_no_data    = 0
    skipped_nsfw       = 0

    for raw_ev in raw:
        url = (raw_ev.get("link") or "").strip()
        name = _clean_html((raw_ev.get("title") or {}).get("rendered", ""))
        if not url or not name:
            skipped_no_data += 1
            continue

        excerpt = _clean_html(
            (raw_ev.get("excerpt") or {}).get("rendered", ""))[:2000]

        if is_cancelled_event(name, excerpt):
            skipped_nsfw += 1
            continue

        html = _fetch(url)
        if not html:
            skipped_no_data += 1
            continue
        time.sleep(0.3)   # be kind to the host

        start, time_disp, parsed_venue, address = _extract_event_from_detail(html)
        if start is None:
            skipped_no_date += 1
            continue
        if start < today or start > window_end:
            skipped_out_window += 1
            continue

        # Wrapper override beats per-page extraction. Most MEC sites
        # are single-venue so the wrapper's hardcoded value is more
        # reliable than fighting site-specific selectors.
        loc = venue_name or parsed_venue

        if is_inappropriate_event(name, excerpt, loc):
            skipped_nsfw += 1
            continue

        candidates.append({
            "event_name":  name,
            "description": excerpt,
            "source_url":  url,
            "image_url":   "",   # backfilled below via og:image
            "start_date":  start,
            "end_date":    start,
            "time":        time_disp,
            "location":    loc,
            "address":     address,
            "city":        (venue_city or "").lower(),
            "state":       (venue_state or "").upper(),
        })

    print(f"\nFiltered to {len(candidates)} keep:")
    print(f"  {skipped_no_date:>3} dropped — no parseable start date")
    print(f"  {skipped_out_window:>3} dropped — outside {today}..{window_end} window")
    print(f"  {skipped_no_data:>3} dropped — fetch failure / no title")
    print(f"  {skipped_nsfw:>3} dropped — cancelled / NSFW")

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from event detail pages")

    inserted = 0
    updated  = 0
    print(f"\n━━ Saving {len(candidates)} occurrence(s) ━━")
    for ev in sorted(candidates, key=lambda e: e["start_date"] or date.max):
        page_id = existing.get((ev["source_url"], ev["start_date"].isoformat()))
        if save_event(db_id, ev, newsletter, page_id=page_id):
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            print(f"  {label} {ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}")
    return 0


if __name__ == "__main__":
    print("mec_events is a library — invoke run_mec_source() from a "
          "per-newsletter wrapper.")
    sys.exit(1)
