#!/usr/bin/env python3
"""Scrape travelcobb.org/cobb-county-events/ and save upcoming events to
the Weekend Events Notion DB.

travelcobb.org uses The Events Calendar WordPress plugin, which embeds
clean JSON-LD `Event` objects on every list page. No HTML scraping
needed — we just parse the JSON.

Pagination: `?tribe_paged=N` (1, 2, 3, ...). Each page returns up to 20
events. Walk every page until one returns no events (end of calendar).

Dedup: by Source URL (each event has a unique permalink on travelcobb.org).
Skip rows where the URL already exists in the DB, and only upload new
events.

Date filter: skip any event whose startDate is before today (multi-day
events that started in the past are still considered past).

Newsletter tag: every row saved here is tagged with the NEWSLETTER env
var (defaults to East_Cobb_Connect — that's what ECC stands for in the
folder name). Future per-newsletter scrapers can live alongside this
one (e.g., Weekend Events/Code/PP/pp_event_webscraper.py).
"""
import os
import re
import sys
import json
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

SOURCE_URL  = "https://travelcobb.org/cobb-county-events/"
USER_AGENT  = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120"


# ---------------------------------------------------------------------------
# Scrape one page
# ---------------------------------------------------------------------------
def fetch_page_events(page: int) -> list[dict]:
    """Return a list of JSON-LD Event objects from one paginated page."""
    url = SOURCE_URL if page == 1 else f"{SOURCE_URL}?tribe_paged={page}"
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
    print(f"Scraping {SOURCE_URL}")
    print(f"  → Notion DB:  {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter: {NEWSLETTER}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")
    today = date.today()

    seen_urls: set[str] = set()
    inserted = 0
    skipped_existing = 0
    skipped_past = 0
    page = 1
    while True:
        events = fetch_page_events(page)
        if not events:
            print(f"  [page {page}] no events — stopping")
            break
        print(f"  [page {page}] {len(events)} events")
        new_urls_this_page = 0
        for raw in events:
            ev = normalize_event(raw)
            url = ev.get("source_url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            new_urls_this_page += 1
            if url in existing:
                skipped_existing += 1
                continue
            if ev["start_date"] and ev["start_date"] < today:
                skipped_past += 1
                continue
            if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER):
                inserted += 1
                print(f"      ✓ {ev['start_date']}  {ev['event_name'][:60]}")
        # WordPress's Events Calendar sometimes redirects over-range page
        # numbers (?tribe_paged=999) back to a valid page instead of
        # returning 404. When that happens, every event on the "new" page
        # is already in seen_urls and we'd loop forever. If a page brings
        # zero new URLs, the calendar has wrapped — stop.
        if new_urls_this_page == 0:
            print(f"  [page {page}] all events already seen (calendar wrapped) — stopping")
            break
        page += 1
    print()
    print(f"✓ Done. Inserted {inserted}, skipped {skipped_existing} existing, "
          f"{skipped_past} past")
    return 0


if __name__ == "__main__":
    sys.exit(main())
