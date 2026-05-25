"""Patch.com calendar scraper — LIBRARY.

Patch is built on Next.js with server-side hydration. The calendar
listing page (`/<region>/<patch>/calendar`) embeds the full event
list in a Next.js data endpoint at:

  /_next/data/<buildId>/<region>/<patch>/calendar.json

The buildId rotates on each deploy, so we fetch the calendar HTML
first to scrape the current buildId out of the `__NEXT_DATA__`
script tag, then call the data endpoint to get the events.

The data endpoint returns a dict keyed by Unix timestamp:
  pageProps.mainContent.allEvents = {
      "1780833600": [{event obj}, ...],
      ...
  }
Each event object has a rich, structured schema (title,
displayDate, canonicalUrl, address as a nested PostalAddress-shaped
dict, ogImageUrl, body, eventType, sharedPatches) — no detail-page
fetch needed.

Cost: $0 — two HTTP requests per scrape, regardless of event count.

Per-newsletter wrappers call `run_patch_source(patch_slug, newsletter)`
with their target edition (e.g. 'georgia/dunwoody', 'georgia/marietta').
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
PATCH_BASE      = "https://patch.com"


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
# Build-ID + data endpoint lookup
# ---------------------------------------------------------------------------
def _fetch_calendar_data(patch_slug: str) -> dict | None:
    """Two requests: (1) calendar HTML to extract Next.js buildId,
    (2) the build-pinned JSON data endpoint that returns all events."""
    cal_url = f"{PATCH_BASE}/{patch_slug.strip('/')}/calendar"
    html = _fetch(cal_url)
    if not html:
        return None
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    if not m:
        print(f"    ✗ Could not find buildId in {cal_url}")
        return None
    build_id = m.group(1)

    data_url = (f"{PATCH_BASE}/_next/data/{build_id}/"
                f"{patch_slug.strip('/')}/calendar.json")
    body = _fetch(data_url, accept="application/json")
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        print(f"    ✗ JSON decode error: {e}")
        return None


# ---------------------------------------------------------------------------
# Normalize one Patch event → standard event dict
# ---------------------------------------------------------------------------
# Patch returns displayDate / displayDateTimestamp in UTC. We convert
# to the local timezone for both the calendar date (so an evening event
# stays on the same day in the reader's eyes) and the human time
# display ("7:00 PM"). zoneinfo handles DST correctly without us
# tracking offset rules per-month.
def _local_zone_for(patch_slug: str) -> ZoneInfo:
    """Best-effort timezone lookup from the patch slug. Default to
    America/New_York (covers all GA / NY / NC / FL / MA editions).
    Add cases as new states get scraped."""
    region = patch_slug.lower().split("/", 1)[0] if "/" in patch_slug else ""
    central = {"texas", "illinois", "missouri", "iowa", "arkansas",
               "louisiana", "minnesota", "oklahoma", "wisconsin"}
    mountain = {"colorado", "arizona", "utah", "newmexico", "wyoming",
                "montana", "idaho"}
    pacific  = {"california", "washington", "oregon", "nevada"}
    if region in central:  return ZoneInfo("America/Chicago")
    if region in mountain: return ZoneInfo("America/Denver")
    if region in pacific:  return ZoneInfo("America/Los_Angeles")
    return ZoneInfo("America/New_York")


def _normalize_event(raw: dict, patch_slug: str) -> dict | None:
    """Map a Patch event JSON entry to our standard event dict."""
    name = (raw.get("title") or raw.get("ogTitle") or "").strip()
    rel  = (raw.get("canonicalUrl") or raw.get("itemAlias") or "").strip()
    if not name or not rel:
        return None
    url = (rel if rel.startswith("http")
           else f"{PATCH_BASE}{rel if rel.startswith('/') else '/' + rel}")

    description = _clean_html(raw.get("body") or "")[:2000]
    if is_cancelled_event(name, description):
        return None

    # Date — Patch gives us displayDate (ISO) and displayDateTimestamp
    # (unix seconds). Prefer the timestamp since it's unambiguous.
    ts = raw.get("displayDateTimestamp")
    start_dt = None
    if isinstance(ts, (int, float)):
        start_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    else:
        iso = raw.get("displayDate")
        if iso:
            try:
                start_dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            except ValueError:
                pass
    if start_dt is None:
        return None

    # Convert UTC → local zone (handles DST correctly).
    local_dt = start_dt.astimezone(_local_zone_for(patch_slug))
    time_str = local_dt.strftime("%-I:%M %p")
    start    = local_dt.date()

    # Address — Patch returns a structured dict.
    addr = raw.get("address") or {}
    if isinstance(addr, dict):
        venue_name = (addr.get("name") or "").strip()
        street     = (addr.get("streetAddress") or "").strip()
        city       = (addr.get("city") or "").strip().lower()
        state      = (addr.get("region") or "").strip().upper()
        zip_code   = (addr.get("postalCode") or "").strip()
        full_addr  = ", ".join(p for p in (street, addr.get("city",""), state, zip_code) if p)
    else:
        venue_name = ""
        full_addr  = str(addr or "").strip()
        city = state = ""

    if is_inappropriate_event(name, description, venue_name):
        return None

    # Image
    image = (raw.get("ogImageUrl") or raw.get("imageThumbnail") or "").strip()

    return {
        "event_name":  name,
        "description": description,
        "source_url":  url,
        "image_url":   image,
        "start_date":  start,
        "end_date":    start,
        "time":        time_str,
        "location":    venue_name,
        "address":     full_addr,
        "city":        city,
        "state":       state,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_patch_source(patch_slug: str,
                     newsletter: str,
                     *,
                     db_id: str | None = None,
                     end_window_days: int = END_WINDOW_DAYS) -> int:
    """Walk one Patch edition's calendar and upsert in-window events
    into the Weekend Events Notion DB tagged with `newsletter`.

    `patch_slug` is the URL fragment after patch.com/, e.g.
    'georgia/dunwoody' or 'georgia/sandysprings'."""
    if db_id is None:
        db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if not db_id:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=end_window_days)

    print(f"Patch scraper")
    print(f"  → Patch slug:   {patch_slug}")
    print(f"  → Notion DB:    {db_id[:8]}…")
    print(f"  → Newsletter:   {newsletter}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    data = _fetch_calendar_data(patch_slug)
    if not data:
        return 0
    mc = (data.get("pageProps") or {}).get("mainContent") or {}
    all_events = mc.get("allEvents") or {}
    promoted   = mc.get("promotedEvents") or {}
    total = mc.get("totalCount", "?")
    print(f"Patch reports {total} total event(s); allEvents has "
          f"{sum(len(v) if isinstance(v,list) else 1 for v in all_events.values())} entries, "
          f"promotedEvents {len(promoted)}\n")

    existing = existing_source_urls(db_id, newsletter=newsletter)
    print(f"Dedup: {len(existing)} URLs already in DB for {newsletter}\n")

    candidates: list[dict] = []
    seen_urls: set[str] = set()
    seen_name_keys: set[tuple[str,str]] = set()
    skipped_out_window = 0
    skipped_no_data    = 0
    skipped_nsfw       = 0
    skipped_dup        = 0

    # Promoted events use the same schema; merge both streams.
    for source_dict in (all_events, promoted):
        for ts_key, raw_or_list in source_dict.items():
            for raw in (raw_or_list if isinstance(raw_or_list, list) else [raw_or_list]):
                ev = _normalize_event(raw, patch_slug)
                if not ev:
                    skipped_no_data += 1
                    continue
                if ev["source_url"] in seen_urls:
                    skipped_dup += 1
                    continue
                seen_urls.add(ev["source_url"])
                name_key = _normalize_title(ev["event_name"])
                date_key = ev["start_date"].isoformat() if ev["start_date"] else ""
                if name_key and (name_key, date_key) in seen_name_keys:
                    skipped_dup += 1
                    continue
                if name_key:
                    seen_name_keys.add((name_key, date_key))
                if ev["start_date"] < today or ev["start_date"] > window_end:
                    skipped_out_window += 1
                    continue
                candidates.append(ev)

    print(f"Filtered to {len(candidates)} keep:")
    print(f"  {skipped_dup:>3} dropped — duplicate URL / (name,date)")
    print(f"  {skipped_out_window:>3} dropped — outside {today}..{window_end} window")
    print(f"  {skipped_no_data:>3} dropped — missing essentials / NSFW")
    print(f"  {skipped_nsfw:>3} dropped — cancelled / inappropriate")

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from event detail pages")

    inserted = 0
    updated  = 0
    print(f"\n━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in sorted(candidates, key=lambda e: e["start_date"] or date.max):
        page_id = existing.get(ev["source_url"])
        if save_event(db_id, ev, newsletter, page_id=page_id):
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            print(f"  {label} {ev['start_date']}  "
                  f"{ev['event_name'][:55]:55s}  ({ev.get('city','?')})")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}")
    return 0


if __name__ == "__main__":
    print("patch_events is a library — invoke run_patch_source() from a "
          "per-newsletter wrapper.")
    sys.exit(1)
