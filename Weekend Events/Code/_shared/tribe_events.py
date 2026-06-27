"""The Events Calendar (Tribe Events) WordPress-plugin scraper.

Many local government / tourism sites in the Atlanta area run this
plugin: travelcobb.org, visitmariettaga.com, kennesaw-ga.gov,
batteryatl.com. They all emit identical JSON-LD `Event` objects on
every paginated listing page, so one scraper covers them all.

Per-source thin-wrapper pattern: each Tribe Events source lives in
its own per-newsletter file (East_Cobb_Connect/travel_cobb.py, etc.)
that just calls `run_tribe_source(url, newsletter)`. That keeps the
per-newsletter folder organization clean without duplicating walker
logic.

If a target source has its own quirk (cancellation filter, custom
city allow-list, etc.), call `walk_tribe_source()` directly and add
the quirk in the wrapper.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from html_utils import (_clean_html, _normalize_title,             # noqa: E402
                        _parse_iso_date, parse_jsonld_price)
from notion_save import existing_source_urls, save_event  # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,           # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images,
                                 upgrade_detail_images)

USER_AGENT      = "Mozilla/5.0 (newsletter-automation)"
END_WINDOW_DAYS = 14

# Matches a per-day segment header like "Friday, June 19th" inside a
# multi-day series blurb.
_DAY_SEGMENT_RE = re.compile(
    r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?',
    re.IGNORECASE,
)


def _trim_series_description(desc: str, occ_date) -> str:
    """Keep only THIS occurrence's day from a multi-day series blurb.

    Tribe series events (e.g. a Braves weekend homestand) describe every game
    in one description: "…Friday, June 19th: First Pitch 7:15 pm  Saturday,
    June 20th: …  Sunday, June 21st: … (Kids Giveaway: Braves LED Light)…".
    Each per-date occurrence otherwise inherits the WHOLE blurb, so the writer
    misattributes another day's promo (the Sunday giveaway showed up on the
    Friday card). When such a per-day breakdown is present, return the intro
    plus only the segment that matches `occ_date`. Returns `desc` unchanged
    when there's no multi-day breakdown or the occurrence's day isn't found."""
    if not desc or occ_date is None:
        return desc
    markers = list(_DAY_SEGMENT_RE.finditer(desc))
    if len(markers) < 2:
        return desc  # single-day / no per-day breakdown — leave as-is
    intro = desc[:markers[0].start()].strip()
    want_weekday = occ_date.strftime("%A").lower()
    want_day = occ_date.day
    chosen = None
    for i, m in enumerate(markers):
        seg = desc[m.start():(markers[i + 1].start() if i + 1 < len(markers) else len(desc))].strip()
        weekday = m.group(1).lower()
        day_num = int(m.group(3))
        if weekday == want_weekday and day_num == want_day:
            chosen = seg
            break
        if chosen is None and weekday == want_weekday:
            chosen = seg  # weekday-only fallback (date text slightly off)
    if not chosen:
        return desc  # this occurrence's day not listed — keep full text
    # Drop the generic series footer (subject-to-change / road closures / tickets).
    chosen = re.split(r'\*\s*Date\s*&\s*Time|Please be advised|Purchase your single',
                      chosen, maxsplit=1)[0].strip()
    return (intro + " " + chosen).strip() if intro else chosen


def fetch_page_events(source_url: str, page: int = 1) -> list[dict]:
    """Return a list of JSON-LD Event objects from one paginated page of
    `source_url`. Page 1 hits the bare URL; pages ≥2 use ?tribe_paged=N
    (sites may 301 to /events/page/N/ — we follow redirects either way).

    Retries on transient codes that some Cloudflare-fronted sites use as
    soft bot-checks (202, 429, 503). Real 4xx/5xx returns []."""
    url = source_url if page == 1 else f"{source_url}?tribe_paged={page}"
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


def _location_fields(loc) -> tuple[str, str, str]:
    """Extract (location_name, address, city) from a JSON-LD Place.
    `city` is returned separately for the City column / shared-tag sweep."""
    if not isinstance(loc, dict):
        return "", "", ""
    name = loc.get("name", "") or ""
    addr = loc.get("address", "") or ""
    city = ""
    if isinstance(addr, dict):
        street = addr.get("streetAddress", "") or ""
        city   = (addr.get("addressLocality", "") or "").strip()
        region = addr.get("addressRegion", "") or ""
        parts = [p for p in (street, city, region) if p]
        addr_str = ", ".join(parts)
    else:
        addr_str = str(addr)
    return _clean_html(name), _clean_html(addr_str), _clean_html(city).lower()


def _geo_coords(location) -> tuple:
    """Pull (lat, lng) from a JSON-LD Place.geo GeoCoordinates block, if present.
    Tribe sites (e.g. The Battery) ship exact coordinates — using them directly
    beats geocoding a suite-number address that often won't resolve."""
    if not isinstance(location, dict):
        return (None, None)
    geo = location.get("geo")
    if not isinstance(geo, dict):
        return (None, None)
    try:
        lat = float(geo.get("latitude"))
        lng = float(geo.get("longitude"))
    except (TypeError, ValueError):
        return (None, None)
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return (lat, lng)
    return (None, None)


def normalize_event(ev: dict) -> dict:
    """Map a JSON-LD Event into our Notion row dict."""
    loc_name, address, city = _location_fields(ev.get("location", {}))
    geo_lat, geo_lng = _geo_coords(ev.get("location", {}))
    start = _parse_iso_date(ev.get("startDate", ""))
    end   = _parse_iso_date(ev.get("endDate", ""))
    start_str = ev.get("startDate", "") or ""
    end_str   = ev.get("endDate", "") or ""
    time_str = ""
    if "T" in start_str:
        try:
            sdt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            edt = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if "T" in end_str else None
            if sdt.hour or sdt.minute:
                time_str = sdt.strftime("%-I:%M %p")
                if edt and (edt.hour or edt.minute):
                    time_str += " – " + edt.strftime("%-I:%M %p")
        except Exception:
            pass
    # Multi-day series listings repeat every day's promo in one description;
    # trim to just this occurrence's day so per-date cards don't inherit
    # another day's giveaway/event (see _trim_series_description).
    description = _trim_series_description(_clean_html(ev.get("description", "")), start)
    return {
        "event_name":  _clean_html(ev.get("name", "")),
        "description": description[:2000],
        "source_url":  ev.get("url", "") or "",
        "image_url":   ev.get("image", "") or "",
        "start_date":  start,
        "end_date":    end,
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
        "city":        city,
        "geo_lat":     geo_lat,
        "geo_lng":     geo_lng,
        # Published ticket price from JSON-LD offers ('Free' / '$25' / '$10–$30'),
        # blank when the source doesn't expose one. An explicit price beats
        # scanning the blurb for 'free' when deciding what's actually free.
        "price":       parse_jsonld_price(ev),
    }


def _rest_events_endpoint(source_url: str) -> str:
    """Derive the Events Calendar REST endpoint for a site from any of its page
    URLs → https://host/wp-json/tribe/events/v1/events. '' if unparseable."""
    p = urlparse(source_url)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}/wp-json/tribe/events/v1/events"


def normalize_rest_event(ev: dict) -> dict:
    """Map one Events Calendar REST API event object into our Notion row dict
    (same shape as normalize_event, which maps JSON-LD)."""
    sdd = ev.get("start_date_details") or {}
    edd = ev.get("end_date_details") or {}

    def _date_from(details: dict, fallback: str):
        try:
            return date(int(details["year"]), int(details["month"]), int(details["day"]))
        except Exception:
            return _parse_iso_date(fallback)

    start = _date_from(sdd, ev.get("start_date", ""))
    end   = _date_from(edd, ev.get("end_date", ""))

    # Time string (skip all-day / midnight). End time dropped when it's the
    # 23:59 all-day sentinel.
    time_str = ""
    try:
        if not ev.get("all_day"):
            sh, sm = int(sdd.get("hour", 0)), int(sdd.get("minutes", 0))
            if sh or sm:
                time_str = datetime(2000, 1, 1, sh, sm).strftime("%-I:%M %p")
                eh, em = int(edd.get("hour", 0)), int(edd.get("minutes", 0))
                if (eh or em) and not (eh == 23 and em == 59):
                    time_str += " – " + datetime(2000, 1, 1, eh, em).strftime("%-I:%M %p")
    except Exception:
        pass

    img = ev.get("image")
    image_url = (img.get("url", "") if isinstance(img, dict)
                 else (img if isinstance(img, str) else "")) or ""

    ven = ev.get("venue") if isinstance(ev.get("venue"), dict) else {}
    loc_name = _clean_html(html.unescape(ven.get("venue", "") or ""))
    city = (ven.get("city", "") or "").strip().lower()
    addr_parts = [ven.get("address", ""), ven.get("city", ""), ven.get("stateprovince", "")]
    address = _clean_html(", ".join(p for p in addr_parts if p))
    geo_lat = geo_lng = None
    try:
        geo_lat = float(ven.get("geo_lat"))
        geo_lng = float(ven.get("geo_lng"))
        if not (-90 <= geo_lat <= 90 and -180 <= geo_lng <= 180):
            geo_lat = geo_lng = None
    except (TypeError, ValueError):
        geo_lat = geo_lng = None

    name = _clean_html(html.unescape(ev.get("title", "") or ""))
    description = _trim_series_description(
        _clean_html(html.unescape(ev.get("description", "") or "")), start)

    return {
        "event_name":  name,
        "description": description[:2000],
        "source_url":  ev.get("url", "") or "",
        "image_url":   image_url,
        "start_date":  start,
        "end_date":    end,
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
        "city":        city,
        "geo_lat":     geo_lat,
        "geo_lng":     geo_lng,
        # REST exposes the published price as a plain string ('Free', '$25', …).
        "price":       (ev.get("cost") or "").strip(),
    }


def fetch_events_rest(source_url: str, start_d, end_d, max_pages: int = 20):
    """Fetch every event in [start_d, end_d] from the site's Events Calendar
    REST API, normalized to our row dicts. Paginates 50/page until done.

    Returns None when the endpoint isn't usable (missing plugin route, non-JSON,
    transport error) so the caller falls back to the HTML walker. Returns [] when
    the API works but the window holds no events."""
    endpoint = _rest_events_endpoint(source_url)
    if not endpoint:
        return None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    raw: list[dict] = []
    saw_valid = False
    page = 1
    while page <= max_pages:
        params = {
            "start_date": start_d.isoformat(),
            "end_date":   end_d.isoformat(),
            "per_page":   50,
            "page":       page,
        }
        # Retry transient Cloudflare soft-challenges (202/429/503) that front
        # some of these sites — same codes the HTML fetcher rides out — before
        # giving up and falling back.
        r = None
        for attempt in range(3):
            try:
                r = requests.get(endpoint, params=params, headers=headers, timeout=15)
            except Exception as e:
                print(f"    [REST] request error (attempt {attempt + 1}/3): {e}")
                time.sleep(2 * (attempt + 1))
                r = None
                continue
            if r.status_code in (202, 429, 503) and attempt < 2:
                wait = 3 * (attempt + 1)
                print(f"    [REST page {page}] HTTP {r.status_code} — retry {attempt + 1}/3 in {wait}s")
                time.sleep(wait)
                continue
            break
        if r is None:
            return [normalize_rest_event(x) for x in raw] if saw_valid else None
        # TEC returns 404 with code 'event-archive-page-not-found' when the
        # window has no (more) events — a valid empty result, not a missing
        # endpoint ('rest_no_route'). Distinguish via the body's code.
        if r.status_code == 404:
            try:
                code = (r.json() or {}).get("code", "")
            except Exception:
                code = ""
            if "page-not-found" in code:
                break  # no more events in window — done
            return None  # rest_no_route / not a TEC REST site → fall back
        if r.status_code != 200:
            print(f"    [REST] HTTP {r.status_code}")
            return [normalize_rest_event(x) for x in raw] if saw_valid else None
        try:
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, dict) or "events" not in data:
            return None
        saw_valid = True
        raw.extend(data.get("events") or [])
        total_pages = data.get("total_pages") or 1
        if page >= total_pages or not data.get("next_rest_url"):
            break
        page += 1
    return [normalize_rest_event(x) for x in raw]


def run_tribe_source(source_url: str, newsletter: str,
                     db_id: str | None = None,
                     end_window_days: int = END_WINDOW_DAYS,
                     refine_detail_images: bool = False) -> int:
    """End-to-end: walk one Tribe Events source, group recurring events,
    backfill images, upsert into the Weekend Events Notion DB tagged with
    `newsletter`. Wrap in `if __name__ == '__main__': sys.exit(run_tribe_source(...))`
    from a thin per-source script.

    `db_id` defaults to NOTION_WEEKEND_EVENTS_DB_ID from env."""
    if db_id is None:
        db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if not db_id:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=end_window_days)

    print(f"Tribe Events scraper")
    print(f"  → Source:       {source_url}")
    print(f"  → Notion DB:    {db_id[:8]}…")
    print(f"  → Newsletter:   {newsletter}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    existing = existing_source_urls(db_id)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    seen_occurrences: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    skipped_past   = 0
    skipped_future = 0

    def _consider(ev: dict) -> str:
        """Filter one normalized event into `candidates`. Returns a status:
        'add' | 'past' | 'future' | 'notitle' | 'dup' | 'skip'. Mutates
        candidates / seen_occurrences / skip counters (nonlocal)."""
        nonlocal skipped_past, skipped_future
        url = ev.get("source_url", "")
        sd  = ev.get("start_date")
        name = ev.get("event_name", "")
        if not url or not sd or not name:
            return "skip"
        if is_cancelled_event(name, ev.get("description", "")):
            return "skip"
        if is_inappropriate_event(name, ev.get("description", ""), ev.get("location", "")):
            return "skip"
        key = (url, sd.isoformat())
        if key in seen_occurrences:
            return "dup"
        seen_occurrences.add(key)
        if sd > window_end:
            skipped_future += 1
            return "future"
        if sd < today:
            skipped_past += 1
            return "past"
        if not _normalize_title(name):
            return "notitle"
        # Per-occurrence model: each occurrence carries its own URL + date
        # (the (url, date) de-dupe above guarantees uniqueness). One row per
        # occurrence — no title collapse.
        ev["all_dates"] = {sd}
        candidates.append(ev)
        return "add"

    # Prefer the Events Calendar REST API: it honors the date window and
    # paginates reliably. Some sites' HTML listings ignore ?tribe_paged (every
    # "page" returns the same first ~20 events — travelcobb does this), so the
    # JSON-LD walker silently stops short and misses later-window events (e.g.
    # July 4). Fall back to the HTML JSON-LD walker only if REST is unavailable.
    rest_events = fetch_events_rest(source_url, today, window_end)
    if rest_events is not None:
        print(f"━━ {source_url} (REST API) ━━")
        kept = sum(1 for ev in rest_events if _consider(ev) == "add")
        print(f"  REST: {len(rest_events)} event(s) in window → {kept} kept")
    else:
        print(f"━━ {source_url} (HTML listing — REST unavailable) ━━")
        page = 1
        while True:
            events = fetch_page_events(source_url, page)
            if not events:
                print(f"  [page {page}] no events — stopping")
                break
            new_occurrences = 0
            all_past_end    = True
            for raw in events:
                status = _consider(normalize_event(raw))
                if status in ("add", "past", "future", "notitle"):
                    new_occurrences += 1
                if status in ("add", "past", "notitle"):
                    all_past_end = False
            print(f"  [page {page}] {len(events)} listings  "
                  f"({new_occurrences} new occurrences)")
            if new_occurrences == 0:
                print(f"  [page {page}] calendar wrapped — stopping")
                break
            if all_past_end:
                print(f"  [page {page}] every event past {window_end} — stopping")
                break
            page += 1
    print()

    candidates.sort(key=lambda e: e["start_date"] or date.max)

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from source pages")

    # Some Tribe sources (notably The Battery) emit unreliable LISTING
    # thumbnails — a shared placeholder graphic, or the wrong event's
    # image. For those, re-derive each event's image from its DETAIL page:
    # a "Click here for more information!" button's external promo hero
    # (e.g. mlb.com/braves → mlbstatic), else the detail page's own
    # og:image. This overrides the listing image only when a real detail
    # image is found.
    if refine_detail_images:
        upgraded = upgrade_detail_images(candidates)
        if upgraded:
            print(f"  ↳ Upgraded {upgraded} image(s) from detail pages "
                  f"(more-info button / og:image)")

    inserted = 0
    updated  = 0
    print(f"━━ Saving {len(candidates)} occurrence(s) ━━")
    for ev in candidates:
        page_id = existing.get((ev["source_url"], ev["start_date"].isoformat()))
        if save_event(db_id, ev, newsletter, page_id=page_id):
            if page_id:
                updated += 1
                print(f"  ↻ {ev['start_date']}  {ev['event_name'][:60]}")
            else:
                inserted += 1
                print(f"  ✓ {ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}, "
          f"{skipped_past} past, "
          f"{skipped_future} beyond {window_end}")
    return 0
