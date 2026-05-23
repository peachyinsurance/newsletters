#!/usr/bin/env python3
"""Scrape ECC event sources and save upcoming events to the Weekend
Events Notion DB.

Currently scrapes:
  - travelcobb.org/cobb-county-events/
  - visitmariettaga.com/events/
  - kennesaw-ga.gov/events/category/events/

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
import html
import os
import re
import sys
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import is_cancelled_event, is_inappropriate_event  # noqa: E402

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

SOURCES = [
    "https://travelcobb.org/cobb-county-events/",
    "https://visitmariettaga.com/events/",
    "https://www.kennesaw-ga.gov/events/category/events/",
]
# Back-compat alias for any older script that imports SOURCE_URL.
SOURCE_URL = SOURCES[0]
# How many days past this week's upcoming Friday count as "in window".
# Matches the Featured Event picker's date window so every event Featured
# Event might choose is guaranteed to be in the DB.
END_WINDOW_DAYS = 14
# Keep this UA short. visitmariettaga's WAF 403s anything that looks like
# a "spoofed" desktop browser (full Chrome UA strings are blocked, but
# bare Mozilla/5.0 passes through). travelcobb accepts either, so this
# is the common denominator.
USER_AGENT  = "Mozilla/5.0"


# ---------------------------------------------------------------------------
# Scrape one page
# ---------------------------------------------------------------------------
def fetch_page_events(source_url: str, page: int = 1) -> list[dict]:
    """Return a list of JSON-LD Event objects from one paginated page of
    `source_url`. Page 1 hits the bare URL; pages ≥2 use ?tribe_paged=N
    (sites running The Events Calendar plugin may 301 to a pretty URL
    like /events/page/N/, but we follow redirects so either works).

    Retries on transient codes that some Cloudflare-fronted sites use as
    soft bot-checks (202, 429, 503). A real 4xx/5xx other than those
    fails fast and returns []."""
    url = source_url if page == 1 else f"{source_url}?tribe_paged={page}"
    # Browser-like headers — kennesaw-ga.gov returns HTTP 202 (CF soft
    # challenge) when Accept/Accept-Language are missing.
    headers = {
        "User-Agent":      USER_AGENT,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
        except Exception as e:
            print(f"    [page {page}] fetch error (attempt {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
            r = None
            continue
        if r.status_code == 200 and r.text:
            break
        if r.status_code in (202, 429, 503) and attempt < 2:
            wait = 3 * (attempt + 1)
            print(f"    [page {page}] HTTP {r.status_code} — retry {attempt + 1}/3 in {wait}s")
            time.sleep(wait)
            continue
        print(f"    [page {page}] HTTP {r.status_code} from {url}")
        return []
    if r is None or r.status_code != 200 or not r.text:
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
    """Strip HTML tags and decode HTML entities. Description and name
    fields arrive HTML-escaped inside JSON-LD (literal `&#8217;`, `&amp;`,
    etc.) — html.unescape handles named, decimal, and hex entities in one
    pass. Tags are stripped after decoding.

    Also strips stray `\\'` and `\\"` sequences — batteryatl.com (and a
    few other Tribe Events sites) emit invalid JSON escapes that
    json.loads passes through as literal backslash-quote pairs."""
    if not s:
        return ""
    s = html.unescape(s)
    s = s.replace("\\'", "'").replace('\\"', '"')
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def format_dates_human(dates) -> str:
    """Format an iterable of date objects as 'May 22nd, 29th, June 5th'.
    Groups consecutive same-month entries under one month name and adds
    English ordinal suffixes to the day numbers."""
    seen = sorted(set(d for d in dates if d))
    if not seen:
        return ""

    def _ord(n: int) -> str:
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"

    chunks: list[str] = []
    cur_key: tuple[int, int] | None = None
    cur_month_name = ""
    cur_days: list[str] = []
    for d in seen:
        key = (d.year, d.month)
        if key != cur_key:
            if cur_days:
                chunks.append(f"{cur_month_name} {', '.join(cur_days)}")
            cur_key = key
            cur_month_name = d.strftime("%B")
            cur_days = [_ord(d.day)]
        else:
            cur_days.append(_ord(d.day))
    if cur_days:
        chunks.append(f"{cur_month_name} {', '.join(cur_days)}")
    return ", ".join(chunks)


def _normalize_title(t: str) -> str:
    """Lowercased, punctuation-stripped title key used for cross-source
    dedup. 'Marietta Greek Festival 2026' and 'The Marietta Greek
    Festival' both reduce to 'marietta greek festival' so they collide
    in the (title, date) dedup set."""
    if not t:
        return ""
    s = t.lower()
    s = re.sub(r"\b20\d{2}\b", "", s)         # strip 4-digit years (2025, 2026, …)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)        # strip punctuation
    s = re.sub(r"^(the|a|an)\s+", "", s)      # leading article
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
def existing_source_urls(db_id: str,
                         newsletter: str | None = None) -> dict[str, str]:
    """Return mapping of Source URL → Notion page_id for rows in the
    Weekend Events DB. Callers can use `url in dict` for membership
    tests AND get the page_id for upserting recurring-event content
    (Dates field, refreshed Description / Image URL, etc.) on re-scrape.

    `newsletter` scopes the lookup to rows tagged with that newsletter.
    Crucial for the Apify Eventbrite scraper's multi-newsletter mode
    (Pattern B) — without scoping, an East_Cobb_Connect run that
    encounters a URL already saved under Perimeter_Post would update
    the PP row in place, corrupting cross-newsletter state. Defaults
    to None (all newsletters) for the per-source scrapers that only
    serve one newsletter each."""
    filters = None
    if newsletter:
        filters = {"property": "Newsletter", "select": {"equals": newsletter}}
    pages = query_database(db_id, filters=filters) or []
    out: dict[str, str] = {}
    for p in pages:
        url = (p.get("properties", {}).get("Source URL", {}).get("url") or "").strip()
        if url:
            out[url] = p.get("id", "")
    return out


def save_event(db_id: str, ev: dict, newsletter: str,
               page_id: str | None = None) -> bool:
    """Create a new event row, OR update an existing row when `page_id`
    is provided.

    Update mode refreshes content fields (Event Name, Description, Image
    URL, Location, Address, Time, **Dates**, Date, Date Generated) while
    leaving Newsletter, Status, Source URL, and Manually Edited intact
    so manual curation isn't clobbered on re-scrape.

    The `Dates` field is written in ISO comma-separated format
    (`2026-05-22, 2026-05-23, ...`) — Weekend_Planner.fetch parses ISO
    matches out of it to determine which target-weekend days a recurring
    event covers. Without ISO format the recurring rows whose primary
    Date is before the target weekend get filtered out entirely."""
    if not ev.get("source_url"):
        return False

    dates_display = ev.get("dates_display") or ""
    if not dates_display and ev.get("all_dates"):
        # ISO comma-separated — machine-parsable by Weekend Planner's
        # fetch_weekend_events_from_notion (regex `\d{4}-\d{2}-\d{2}`).
        dates_display = ", ".join(d.isoformat() for d in sorted(ev["all_dates"]))

    # Content properties — refreshed in both create and update modes.
    content = {
        "Event Name":     {"rich_text": [{"text": {"content": ev["event_name"][:200]}}]},
        "Description":    {"rich_text": [{"text": {"content": ev["description"][:2000]}}]},
        "Image URL":      {"url": ev["image_url"] or None},
        "Location":       {"rich_text": [{"text": {"content": ev["location"][:200]}}]},
        "Address":        {"rich_text": [{"text": {"content": ev["address"][:200]}}]},
        "Time":           {"rich_text": [{"text": {"content": ev["time"][:80]}}]},
        "Dates":          {"rich_text": [{"text": {"content": dates_display[:500]}}]},
        "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
    }
    if ev["start_date"]:
        date_prop = {"start": ev["start_date"].isoformat()}
        if ev["end_date"] and ev["end_date"] != ev["start_date"]:
            date_prop["end"] = ev["end_date"].isoformat()
        content["Date"] = {"date": date_prop}

    def _send_with_heal(method: str, url: str, props: dict) -> requests.Response:
        """POST/PATCH with Notion's missing-property self-heal: if Notion
        rejects a property we sent, drop it and retry. Loops because
        Notion only reports one missing prop per response."""
        if method == "POST":
            body = {"parent": {"database_id": db_id}, "properties": props}
        else:
            body = {"properties": props}
        r = requests.request(method, url, headers=NOTION_HEADERS,
                             json=body, timeout=30)
        attempts = 0
        while (not r.ok and r.status_code == 400
               and "is not a property that exists" in r.text
               and attempts < 5):
            attempts += 1
            m = (re.search(r"`([^`]+)` is not a property", r.text)
                 or re.search(r'"message":"([^"]+?) is not a property', r.text))
            if not m:
                break
            bad_prop = m.group(1)
            if bad_prop not in props:
                break
            print(f"    ⚠ dropping unknown Notion property '{bad_prop}' and retrying")
            props.pop(bad_prop, None)
            body["properties"] = props
            r = requests.request(method, url, headers=NOTION_HEADERS,
                                 json=body, timeout=30)
        return r

    if page_id:
        # Update existing row — preserve Source URL, Newsletter, Status,
        # Manually Edited (they're not in `content`).
        r = _send_with_heal(
            "PATCH", f"https://api.notion.com/v1/pages/{page_id}",
            dict(content),
        )
        if not r.ok:
            print(f"    ✗ update failed: {r.status_code} {r.text[:200]}")
            return False
        return True

    # Create new row — add the fixed properties on top of the content set.
    create_props = dict(content)
    create_props["Name"]        = {"title": [{"text": {"content": ev["event_name"][:200] or "(unnamed event)"}}]}
    create_props["Source URL"]  = {"url": ev["source_url"]}
    create_props["Newsletter"]  = {"select": {"name": newsletter}}
    create_props["Status"]      = {"select": {"name": "pending"}}
    r = _send_with_heal("POST", "https://api.notion.com/v1/pages", create_props)
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
    # processing the same occurrence.
    seen_occurrences: set[tuple[str, str]] = set()

    # GROUP BY NORMALIZED TITLE. Each entry: the first-seen event dict
    # (source-order priority for URL/image/venue) plus `all_dates` — the
    # set of every in-window occurrence seen across pages AND sources.
    # That collapses both
    #   (a) recurring events (Acworth Farmers Market every Friday), and
    #   (b) cross-source duplicates (same event on travelcobb + visitmariettaga)
    # into one row tagged with every date it happens.
    by_name: dict[str, dict] = {}

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
                name = ev.get("event_name", "")
                if not url or not sd or not name:
                    continue
                if is_cancelled_event(name, ev.get("description", "")):
                    continue
                if is_inappropriate_event(name, ev.get("description", ""),
                                          ev.get("location", "")):
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
                # In window — group under normalized title.
                name_key = _normalize_title(name)
                if not name_key:
                    continue
                entry = by_name.get(name_key)
                if entry is None:
                    # First sighting of this event title: seed with this
                    # listing's metadata + a fresh date set.
                    ev["all_dates"] = {sd}
                    by_name[name_key] = ev
                else:
                    entry["all_dates"].add(sd)
                    # If this occurrence is earlier than the previous
                    # primary, promote it to start_date (used as the
                    # Notion `Date` field for filter/sort).
                    if sd < entry["start_date"]:
                        entry["start_date"] = sd
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

    # Sort by earliest occurrence for a readable insert log.
    candidates = sorted(by_name.values(),
                        key=lambda e: e["start_date"] or date.max)

    # Backfill: any event whose JSON-LD `image` wasn't populated, scrape
    # the source page for og:image / twitter:image / JSON-LD / body
    # <img>. Runs concurrently so it doesn't blow up scrape latency.
    import sys as _sys, os as _os
    _sys.path.append(_os.path.join(_os.path.dirname(__file__), "..", "..",
                                   "..", "NewsletterCreation", "Code"))
    from event_image_scraper import backfill_images  # noqa: E402
    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from source pages")

    inserted = 0
    updated = 0
    multi_date = 0
    print(f"━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        if len(ev.get("all_dates") or {}) > 1:
            multi_date += 1
        url = ev["source_url"]
        page_id = existing.get(url)
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER, page_id=page_id):
            dates_disp = format_dates_human(ev.get("all_dates") or [])
            if page_id:
                updated += 1
                print(f"  ↻ {dates_disp or ev['start_date']}  {ev['event_name'][:60]}")
            else:
                inserted += 1
                print(f"  ✓ {dates_disp or ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}, "
          f"{skipped_past} past, "
          f"{skipped_future} beyond {window_end}  "
          f"({multi_date} multi-date event(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
