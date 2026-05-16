#!/usr/bin/env python3
"""Scrape ECC event sources and save upcoming events to the Weekend
Events Notion DB.

Currently scrapes:
  - travelcobb.org/cobb-county-events/
  - visitmariettaga.com/events/

Both sites use The Events Calendar WordPress plugin, which embeds clean
JSON-LD `Event` objects on every list page. No HTML scraping needed —
we just parse the JSON.

Pagination: `?tribe_paged=N` (1, 2, 3, ...). Walk pages until one of:
  - the page returns no events (end of calendar),
  - the page has zero new (URL, date) occurrences (calendar wrapped —
    over-range page numbers redirect to a previously-walked page on
    some Events Calendar themes), or
  - every event on the page falls past window_end.

Recurring events: travelcobb (and similar sites) list one calendar
entry per occurrence, all sharing the same Source URL but with
distinct JSON-LD startDate values. We dedup by (URL, date) tuple
during the scan so each occurrence is evaluated independently. When
saving, we keep one row per URL using the EARLIEST in-window date —
so a weekly market lands in Notion with the soonest upcoming Friday
as its date.

Date window: from today through upcoming_friday + END_WINDOW_DAYS.
Events before today are past; events after the window are too far out
to be useful for the weekly newsletter.

Newsletter tag: every row saved here is tagged with the NEWSLETTER env
var (defaults to East_Cobb_Connect — that's what ECC stands for in the
folder name). Future per-newsletter scrapers can live alongside this
one (e.g., Weekend Events/Code/PP/pp_event_webscraper.py).
"""
import os
import re
import sys
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

SOURCES = [
    "https://travelcobb.org/cobb-county-events/",
    "https://visitmariettaga.com/events/",
]
# Back-compat alias for any older script that imports SOURCE_URL.
SOURCE_URL = SOURCES[0]
# How many days past this week's upcoming Friday count as "in window".
# Matches the Featured Event picker's date window so every event Featured
# Event might choose is guaranteed to be in the DB.
END_WINDOW_DAYS = 14
USER_AGENT  = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120"


# ---------------------------------------------------------------------------
# Scrape one page
# ---------------------------------------------------------------------------
def fetch_page_events(source_url: str, page: int = 1) -> list[dict]:
    """Return a list of JSON-LD Event objects from one paginated page of
    `source_url`. Page 1 hits the bare URL; pages ≥2 use ?tribe_paged=N
    (sites running The Events Calendar plugin may 301 to a pretty URL
    like /events/page/N/, but we follow redirects so either works)."""
    url = source_url if page == 1 else f"{source_url}?tribe_paged={page}"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT},
                         allow_redirects=True)
        if r.status_code != 200 or not r.text:
            return []
    except Exception as e:
        print(f"    [page {page}] fetch error: {e}")
        return []
    # Find all JSON-LD blocks; the events array is the one with @type=Event entries
    events: list[dict] = []
    for blob in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
        r.text, re.DOTALL,
    ):
        try:
            data = json.loads(blob.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            events.extend(d for d in data if isinstance(d, dict) and d.get("@type") == "Event")
        elif isinstance(data, dict) and data.get("@type") == "Event":
            events.append(data)
    return events


# ---------------------------------------------------------------------------
# Normalize one JSON-LD event into our Notion row shape
# ---------------------------------------------------------------------------
def _clean_html(s: str) -> str:
    """Strip HTML tags and decode common entities. Description fields on
    travelcobb arrive HTML-escaped inside JSON-LD."""
    if not s:
        return ""
    # JSON-LD double-escapes entities; decode once
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    s = s.replace("&rsquo;", "'").replace("&lsquo;", "'")
    s = s.replace("&ldquo;", '"').replace("&rdquo;", '"')
    s = s.replace("&nbsp;", " ").replace("&#038;", "&").replace("&#8211;", "–")
    # Strip tags
    s = re.sub(r"<[^>]+>", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_iso_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _location_fields(loc) -> tuple[str, str]:
    """Extract (location_name, address) from a JSON-LD Place."""
    if not isinstance(loc, dict):
        return "", ""
    name = loc.get("name", "") or ""
    addr = loc.get("address", "") or ""
    if isinstance(addr, dict):
        street = addr.get("streetAddress", "") or ""
        city   = addr.get("addressLocality", "") or ""
        region = addr.get("addressRegion", "") or ""
        parts = [p for p in (street, city, region) if p]
        addr_str = ", ".join(parts)
    else:
        addr_str = str(addr)
    return _clean_html(name), _clean_html(addr_str)


def normalize_event(ev: dict) -> dict:
    """Map a JSON-LD Event into our Notion row dict."""
    loc_name, address = _location_fields(ev.get("location", {}))
    start = _parse_iso_date(ev.get("startDate", ""))
    end   = _parse_iso_date(ev.get("endDate", ""))
    # Extract time-of-day if present
    start_str = ev.get("startDate", "") or ""
    end_str   = ev.get("endDate", "") or ""
    time_str = ""
    if "T" in start_str:
        try:
            sdt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            edt = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if "T" in end_str else None
            # Only show times if they're not midnight placeholders
            if sdt.hour or sdt.minute:
                time_str = sdt.strftime("%-I:%M %p")
                if edt and (edt.hour or edt.minute):
                    time_str += " – " + edt.strftime("%-I:%M %p")
        except Exception:
            pass
    return {
        "event_name":  _clean_html(ev.get("name", "")),
        "description": _clean_html(ev.get("description", ""))[:2000],
        "source_url":  ev.get("url", "") or "",
        "image_url":   ev.get("image", "") or "",
        "start_date":  start,
        "end_date":    end,
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
    }


# ---------------------------------------------------------------------------
# Notion save
# ---------------------------------------------------------------------------
def existing_source_urls(db_id: str) -> set[str]:
    """Return the set of Source URLs already in the DB (for dedup)."""
    pages = query_database(db_id) or []
    urls: set[str] = set()
    for p in pages:
        url = (p.get("properties", {}).get("Source URL", {}).get("url") or "").strip()
        if url:
            urls.add(url)
    return urls


def save_event(db_id: str, ev: dict, newsletter: str) -> bool:
    if not ev.get("source_url"):
        return False
    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "Name":         {"title": [{"text": {"content": ev["event_name"][:200] or "(unnamed event)"}}]},
            "Event Name":   {"rich_text": [{"text": {"content": ev["event_name"][:200]}}]},
            "Description":  {"rich_text": [{"text": {"content": ev["description"][:2000]}}]},
            "Source URL":   {"url": ev["source_url"]},
            "Image URL":    {"url": ev["image_url"] or None},
            "Location":     {"rich_text": [{"text": {"content": ev["location"][:200]}}]},
            "Address":      {"rich_text": [{"text": {"content": ev["address"][:200]}}]},
            "Time":         {"rich_text": [{"text": {"content": ev["time"][:80]}}]},
            "Newsletter":   {"select": {"name": newsletter}},
            "Status":       {"select": {"name": "pending"}},
            "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
        },
    }
    if ev["start_date"]:
        date_prop = {"start": ev["start_date"].isoformat()}
        if ev["end_date"] and ev["end_date"] != ev["start_date"]:
            date_prop["end"] = ev["end_date"].isoformat()
        body["properties"]["Date"] = {"date": date_prop}

    r = requests.post("https://api.notion.com/v1/pages",
                      headers=NOTION_HEADERS, json=body, timeout=30)
    if not r.ok:
        # On schema mismatch, try dropping the offending property
        msg = r.text[:300]
        if r.status_code == 400 and "is not a property that exists" in msg:
            bad = re.search(r"`([^`]+)` is not a property", msg)
            if bad:
                body["properties"].pop(bad.group(1), None)
                r = requests.post("https://api.notion.com/v1/pages",
                                  headers=NOTION_HEADERS, json=body, timeout=30)
        if not r.ok:
            print(f"    ✗ save failed: {r.status_code} {r.text[:200]}")
            return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print(f"Sources ({len(SOURCES)}):")
    for s in SOURCES:
        print(f"  - {s}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    # Track every (url, date) occurrence we've evaluated this run, across
    # sources and pages. Used both for wrap detection (server redirecting
    # over-range page numbers to a valid page) and to avoid double-
    # processing the same occurrence listed on both sites.
    seen_occurrences: set[tuple[str, str]] = set()

    # One row per URL — the earliest in-window occurrence wins. Built up
    # across all sources, then saved at the end.
    by_url: dict[str, dict] = {}

    skipped_past   = 0
    skipped_future = 0

    for source_url in SOURCES:
        print(f"━━ {source_url} ━━")
        page = 1
        while True:
            events = fetch_page_events(source_url, page)
            if not events:
                print(f"  [page {page}] no events — stopping")
                break
            new_occurrences = 0
            all_past_end    = True  # flips False as soon as one event ≤ window_end
            for raw in events:
                ev = normalize_event(raw)
                url = ev.get("source_url", "")
                sd  = ev.get("start_date")
                if not url or not sd:
                    continue
                key = (url, sd.isoformat())
                if key in seen_occurrences:
                    continue
                seen_occurrences.add(key)
                new_occurrences += 1
                if sd > window_end:
                    skipped_future += 1
                    continue
                all_past_end = False
                if sd < today:
                    skipped_past += 1
                    continue
                # In window — keep the earliest occurrence for this URL.
                existing_ev = by_url.get(url)
                if existing_ev is None or sd < existing_ev["start_date"]:
                    by_url[url] = ev
            print(f"  [page {page}] {len(events)} listings  "
                  f"({new_occurrences} new occurrences)")
            # Stop conditions:
            #   (a) page brought zero new (URL, date) tuples — server has
            #       redirected over-range page numbers to a valid page,
            #       calendar has wrapped.
            #   (b) every new event on the page is past window_end — calendar
            #       is sorted ascending so later pages would only be further
            #       out. (Only fires when new_occurrences > 0; otherwise
            #       all_past_end is its initial True from an empty filter set.)
            if new_occurrences == 0:
                print(f"  [page {page}] all occurrences already seen (calendar wrapped) — stopping")
                break
            if all_past_end:
                print(f"  [page {page}] every event past {window_end} — stopping")
                break
            page += 1
        print()

    # Sort candidates by start_date for a readable insert log.
    candidates = sorted(by_url.values(),
                        key=lambda e: e["start_date"] or date.max)
    inserted = 0
    skipped_existing = 0
    print(f"━━ Saving {len(candidates)} in-window candidates ━━")
    for ev in candidates:
        url = ev["source_url"]
        if url in existing:
            skipped_existing += 1
            continue
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER):
            inserted += 1
            print(f"  ✓ {ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, "
          f"skipped {skipped_existing} existing, "
          f"{skipped_past} past, "
          f"{skipped_future} beyond {window_end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
