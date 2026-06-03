"""Vision Internet CMS scraper — `vi-events-tiles-item` list parser.

Vision Internet (now part of Granicus) is the CMS behind a lot of mid-
size city / county gov sites. We've found it on dunwoodyga.gov,
visitlewisville.com — and likely many others as we expand.

All Vision Internet "All Upcoming" tile pages share the same HTML
structure: `<li class="vi-events-tiles-item">` blocks with microdata
(`itemprop=url|startDate|endDate|summary`), category labels, and a
background-image-styled thumbnail.

Per-newsletter wrappers in each newsletter folder call
`run_vision_internet_tiles(source_url, newsletter, default_city,
location_prefix)` with their config. The shared logic — Chrome TLS
impersonation fetch, tile parsing, grouping by event name, upsert —
lives here.

Vision Internet 403s plain Python `requests` on TLS fingerprint, so
curl_cffi with chrome120 impersonation is the default fetch path.
Falls back to plain requests if curl_cffi isn't installed.
"""
from __future__ import annotations

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
from html_utils  import _clean_html, _normalize_title, format_dates_human  # noqa: E402
from notion_save import existing_source_urls, save_event  # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,  # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

END_WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Tile regexes (shared structure across all Vision Internet sites)
# ---------------------------------------------------------------------------
_TILE_RE       = re.compile(r'<li class="vi-events-tiles-item"[^>]*>.*?</li>', re.DOTALL)
_URL_RE        = re.compile(r'<a[^>]+itemprop="url"[^>]+href="([^"]+)"', re.IGNORECASE)
_IMG_RE        = re.compile(r'background-image:\s*url\(([^)]+)\)', re.IGNORECASE)
_START_RE      = re.compile(r"itemprop=['\"]startDate['\"][^>]*datetime=['\"]([^'\"]+)['\"]")
_END_RE        = re.compile(r"itemprop=['\"]endDate['\"][^>]*datetime=['\"]([^'\"]+)['\"]")
_TITLE_RE      = re.compile(r'itemprop="summary"[^>]*>([^<]+)</span>')
_TIME_TEXT_RE  = re.compile(r'<span class="vi-events-tiles-time">([^<]+)<')
_DESC_RE       = re.compile(r'<p class="vi-events-tiles-desc">([^<]*)</p>')
_CATEGORY_RE   = re.compile(r'<span class="vi-events-tiles-category">([^<]+)</span>')


# Vision Internet sites sit behind Akamai bot-manager, which fingerprints
# the TLS/HTTP2 handshake. Akamai retires old Chrome fingerprints over
# time — chrome120 (our original pin) is now flagged on a 403, while a
# current target sails through. Rotate newest-first so a single stale
# target can't take the scraper down; the newest one that curl_cffi
# supports goes first. 403 is treated as retryable here (unlike a normal
# request) because Akamai returns it for both fingerprint rejection and
# IP-reputation throttling — a different target / a short backoff can get
# past the former.
_IMPERSONATE_TARGETS = ("chrome131", "chrome124", "chrome120")


def _fetch_html(url: str) -> str:
    """curl_cffi Chrome TLS impersonation, rotating through modern Chrome
    fingerprints newest-first. Plain requests fallback when curl_cffi
    isn't available — but expect 403 from Vision Internet sites without
    it (Akamai fingerprints plain requests instantly)."""
    cffi_mod = None
    try:
        from curl_cffi import requests as _cffi
        cffi_mod = _cffi
    except ImportError:
        pass

    headers = {
        "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":  "en-US,en;q=0.5",
    }

    def _do_get(target: str | None):
        if cffi_mod is not None and target is not None:
            return cffi_mod.get(url, impersonate=target, timeout=15,
                                allow_redirects=True)
        return requests.get(url, headers=headers, timeout=15,
                            allow_redirects=True)

    # Attempt plan: one try per impersonation target (newest first); if
    # curl_cffi is missing, a single plain-requests attempt.
    targets = list(_IMPERSONATE_TARGETS) if cffi_mod is not None else [None]
    last_status = None
    for attempt, target in enumerate(targets):
        tag = target or "plain-requests"
        try:
            r = _do_get(target)
        except Exception as e:
            print(f"    fetch error ({tag}, {attempt + 1}/{len(targets)}): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200 and r.text:
            if attempt:
                print(f"    ✓ fetched via {tag} after {attempt} earlier reject(s)")
            return r.text
        last_status = r.status_code
        # 403 (fingerprint/IP), 202/429/503 (throttle) → try the next
        # target with a short backoff rather than giving up immediately.
        if r.status_code in (202, 403, 429, 503) and attempt < len(targets) - 1:
            wait = 2 * (attempt + 1)
            print(f"    HTTP {r.status_code} via {tag} — trying next "
                  f"fingerprint in {wait}s")
            time.sleep(wait)
            continue
        print(f"    HTTP {r.status_code} via {tag} from {url}")
        return ""
    if last_status is not None:
        print(f"    ✗ all {len(targets)} fingerprint(s) rejected "
              f"(last HTTP {last_status}) — {url}")
    return ""


def _parse_dt(s: str) -> tuple[date | None, str]:
    """`datetime='2026-05-23T13:00+00:00'` → (date, 'H:MM AM/PM') tuple."""
    if not s:
        return None, ""
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date(), dt.strftime("%-I:%M %p") if (dt.hour or dt.minute) else ""
    except Exception:
        pass
    try:
        return date.fromisoformat(s[:10]), ""
    except Exception:
        return None, ""


def _parse_tile(tile: str, *, base_host: str, default_city: str,
                location_prefix: str, default_address: str) -> dict | None:
    """Extract one event from a tile HTML block."""
    m_title = _TITLE_RE.search(tile)
    m_url   = _URL_RE.search(tile)
    m_start = _START_RE.search(tile)
    if not (m_title and m_url and m_start):
        return None

    title = _clean_html(m_title.group(1))
    href  = m_url.group(1).strip()
    url   = href if href.startswith("http") else urljoin(base_host, href)

    start, start_t = _parse_dt(m_start.group(1))
    m_end = _END_RE.search(tile)
    end, end_t = _parse_dt(m_end.group(1)) if m_end else (start, "")

    if not start:
        return None

    m_time = _TIME_TEXT_RE.search(tile)
    time_str = ""
    if m_time:
        raw = _clean_html(m_time.group(1)).strip()
        time_str = re.sub(r"^\d{1,2}/\d{1,2}/\d{4}\s*", "", raw).strip()
    if not time_str and start_t:
        time_str = f"{start_t} – {end_t}" if end_t and end_t != start_t else start_t

    m_img = _IMG_RE.search(tile)
    image = ""
    if m_img:
        raw_img = m_img.group(1).strip().strip("'\"")
        image = raw_img if raw_img.startswith("http") else urljoin(base_host, raw_img)

    m_desc = _DESC_RE.search(tile)
    description = _clean_html(m_desc.group(1)) if m_desc else ""

    cats = _CATEGORY_RE.findall(tile)
    loc_name = f"{location_prefix}{cats[0] if cats else 'City Calendar'}"

    if is_cancelled_event(title, description):
        return None
    if is_inappropriate_event(title, description, loc_name):
        return None

    return {
        "event_name":  title,
        "description": description,
        "source_url":  url,
        "image_url":   image,
        "start_date":  start,
        "end_date":    end if end and end >= start else start,
        "time":        time_str,
        "location":    loc_name,
        "address":     default_address,
        "city":        default_city,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_vision_internet_tiles(
    source_url: str,
    newsletter: str,
    *,
    default_city: str,
    location_prefix: str,
    default_address: str,
    db_id: str | None = None,
    end_window_days: int = END_WINDOW_DAYS,
) -> int:
    """End-to-end: fetch a Vision Internet `vi-events-tiles-item` page,
    parse each tile, group recurring events by name, upsert to Notion."""
    if db_id is None:
        db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if not db_id:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    base_host = "/".join(source_url.split("/")[:3])
    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=end_window_days)

    print(f"Vision Internet scraper")
    print(f"  → Source:       {source_url}")
    print(f"  → Notion DB:    {db_id[:8]}…")
    print(f"  → Newsletter:   {newsletter}")
    print(f"  → City tag:     {default_city}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    existing = existing_source_urls(db_id, newsletter=newsletter)
    print(f"Dedup: {len(existing)} URLs already in DB for {newsletter}\n")

    html = _fetch_html(source_url)
    if not html:
        return 1
    tiles = _TILE_RE.findall(html)
    print(f"Parsed {len(tiles)} event tile(s) from page\n")

    # Per-occurrence model: each tile is one occurrence carrying its own URL
    # + date. Emit one row per occurrence (no title collapse); de-dupe within
    # this run on (url, date).
    candidates: list[dict] = []
    seen_occ: set[tuple[str, str]] = set()
    skipped_past    = 0
    skipped_future  = 0
    skipped_no_data = 0
    for tile in tiles:
        ev = _parse_tile(tile,
                         base_host=base_host,
                         default_city=default_city,
                         location_prefix=location_prefix,
                         default_address=default_address)
        if not ev:
            skipped_no_data += 1
            continue
        sd = ev["start_date"]
        if sd > window_end:
            skipped_future += 1
            continue
        if sd < today:
            skipped_past += 1
            continue
        if not _normalize_title(ev["event_name"]):
            continue
        occ_key = (ev["source_url"], sd.isoformat())
        if occ_key in seen_occ:
            continue
        seen_occ.add(occ_key)
        ev["all_dates"] = {sd}
        candidates.append(ev)

    candidates.sort(key=lambda e: e["start_date"] or date.max)

    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from detail pages")

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
          f"{skipped_past} past, {skipped_future} beyond {window_end}, "
          f"{skipped_no_data} unparseable")
    return 0
