#!/usr/bin/env python3
"""Denton County calendar scraper for Lewisville Lake Lookout.

Denton County's CivicEngage CMS exposes a per-module RSS feed at
`/RSSFeed.aspx?ModID=58` (ModID=58 is the public events calendar).
Returns ~24 upcoming events as standard RSS 2.0 items with HTML-
escaped descriptions that pack `Event date(s)`, `Event Time`, and
`Location` into the description field.

Note: <pubDate> is when the event was PUBLISHED, not when it
happens. We have to parse the event date out of the description body
('Event dates: May 4, 2026 - June 15, 2026' or 'Event date: May 25,
2026'). Defensive about both single-date and range formats.
"""
import html as html_lib
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
NEWSLETTER = os.environ.get("NEWSLETTER", "Lewisville_Lake_Lookout")

RSS_URL         = "https://www.dentoncounty.gov/RSSFeed.aspx?ModID=58"
END_WINDOW_DAYS = 14
USER_AGENT      = "Mozilla/5.0 (newsletter-automation)"


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
_DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(\d{1,2}),?\s*(\d{4})\b",
    re.IGNORECASE,
)


def _fetch(url: str) -> str:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15,
                             headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"},
                             allow_redirects=True)
        except Exception as e:
            print(f"  fetch error (attempt {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200 and r.text:
            return r.text
        if r.status_code in (429, 503) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        print(f"  HTTP {r.status_code} from {url}")
        return ""
    return ""


def _parse_event_dates(desc: str) -> list[date]:
    """Extract start (and optional end) dates from the RSS description.

    Description shapes:
      Event date: May 25, 2026
      Event dates: May 4, 2026 - June 15, 2026

    Returns up to two dates [start] or [start, end]. Empty list if no
    valid dates parseable."""
    out: list[date] = []
    for m in _DATE_RE.finditer(desc):
        month_name, day_s, year_s = m.group(1).lower(), m.group(2), m.group(3)
        try:
            out.append(date(int(year_s), _MONTHS[month_name], int(day_s)))
        except (ValueError, KeyError):
            continue
    return out


def _parse_event_time(desc: str) -> str:
    """Extract 'Event Time: HH:MM AM/PM - HH:MM AM/PM' if present."""
    m = re.search(r"Event Time:\s*</strong>\s*([^<]+)", desc, re.IGNORECASE)
    if not m:
        return ""
    raw = m.group(1).strip()
    # Filter the all-day placeholder ("12:00 AM - 11:59 PM") which is
    # CivicEngage's "no specific time" marker — display empty instead.
    if re.match(r"^\s*12:00\s*AM\s*-\s*11:59\s*PM\s*$", raw, re.IGNORECASE):
        return ""
    return raw


def _parse_event_location(desc: str) -> tuple[str, str]:
    """Extract 'Location: ...' value. Returns (location_name, address)."""
    m = re.search(r"Location:\s*</strong>\s*([\s\S]*?)(?=<strong>|$)", desc, re.IGNORECASE)
    if not m:
        return "", ""
    raw = re.sub(r"<br\s*/?>", "\n", m.group(1), flags=re.IGNORECASE)
    raw = _clean_html(raw).strip()
    if not raw:
        return "", ""
    # Two-line "Name\nAddress" → split. Single line → all address.
    parts = [p.strip() for p in raw.splitlines() if p.strip()]
    if len(parts) >= 2:
        return parts[0], ", ".join(parts[1:])
    return "", parts[0]


def _parse_event_description(desc: str) -> str:
    m = re.search(r"Description:\s*</strong>\s*<br\s*/?>([\s\S]*)", desc, re.IGNORECASE)
    if not m:
        return ""
    return _clean_html(m.group(1)).strip()


def _normalize_item(item_xml: str, today: date, window_end: date) -> dict | None:
    """One RSS <item> → standard event dict, or None if out-of-window /
    unparseable / cancelled / inappropriate."""
    m_title = re.search(r"<title>([\s\S]*?)</title>", item_xml)
    m_link  = re.search(r"<link>([\s\S]*?)</link>",  item_xml)
    m_desc  = re.search(r"<description>([\s\S]*?)</description>", item_xml)
    if not (m_title and m_link and m_desc):
        return None
    title = html_lib.unescape(m_title.group(1)).strip()
    link  = html_lib.unescape(m_link.group(1)).strip()
    # Description body is HTML-escaped inside the RSS; un-escape first.
    desc_inner = html_lib.unescape(m_desc.group(1))

    dates = _parse_event_dates(desc_inner)
    if not dates:
        return None
    start = dates[0]
    end   = dates[1] if len(dates) > 1 else start
    if end < today or start > window_end:
        return None
    # Clamp to window for downstream consumers
    effective_start = max(start, today)

    full_desc = _parse_event_description(desc_inner)
    if is_cancelled_event(title, full_desc):
        return None
    loc_name, address = _parse_event_location(desc_inner)
    if is_inappropriate_event(title, full_desc, loc_name):
        return None

    return {
        "event_name":  title,
        "description": full_desc[:2000],
        "source_url":  link,
        "image_url":   "",   # RSS feed doesn't carry images
        "start_date":  effective_start,
        "end_date":    min(end, window_end),
        "time":        _parse_event_time(desc_inner),
        "location":    loc_name or "Denton County",
        "address":     address or "Denton County, TX",
        "city":        "denton",
    }


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print("Denton County calendar scraper (CivicEngage RSS)")
    print(f"  → RSS feed:     {RSS_URL}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID, newsletter=NEWSLETTER)
    print(f"Dedup: {len(existing)} URLs already in DB for {NEWSLETTER}\n")

    xml = _fetch(RSS_URL)
    if not xml:
        return 1

    items = re.findall(r"<item>([\s\S]*?)</item>", xml)
    print(f"RSS yielded {len(items)} item(s)\n")

    by_name: dict[str, dict] = {}
    skipped_no_data = 0
    for raw in items:
        ev = _normalize_item(raw, today, window_end)
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
          f"{skipped_no_data} unparseable / out-of-window  ({multi_date} multi-date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
