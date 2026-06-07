#!/usr/bin/env python3
"""Springs Cinema & Taphouse weekend movie-showings scraper.

Tag: ECC_PP (shared) — Springs Cinema & Taphouse is on Roswell Rd in
Sandy Springs, a shared coverage area, so its showings surface in both
East Cobb Connect and Perimeter Post via _SHARED_NEWSLETTER_TAGS.

Platform notes (Filmbot): the public listing page (/unique-programming)
renders only the CURRENT day's showtimes with no dates, and the GraphQL
API gates showing data behind login. The reliable public source of dated
showtimes is each film's /movie/<slug> detail page, which server-renders
its full schedule as "Month Day, h:mm am/pm" anchor links. So we:

  1. enumerate the current film slugs from /unique-programming,
  2. read each /movie/<slug> page's dated showtimes,
  3. keep only Friday / Saturday / Sunday dates in the forward window,
  4. emit ONE Notion row per (film, weekend date), with that day's
     showtimes aggregated into the Time field.
"""
from __future__ import annotations

import html as _htmllib
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "_shared"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_save import existing_source_urls, save_event           # noqa: E402
from event_date_filter import upcoming_friday as _upcoming_friday   # noqa: E402

BASE         = "https://www.springscinema.com"
LISTING_URL  = f"{BASE}/unique-programming"
VENUE        = "Springs Cinema & Taphouse"
ADDRESS      = "5920 Roswell Rd, Sandy Springs, GA 30328"
CITY         = "sandy springs"          # shared city → ECC_PP via normalize sweep
NEWSLETTER_DEFAULT = "ECC_PP"
# How far forward to look. Fri/Sat/Sun within [today, upcoming_friday + N].
END_WINDOW_DAYS = 9
WEEKEND_DAYS    = {4, 5, 6}              # Python weekday(): Fri=4, Sat=5, Sun=6

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Slug appears in checkout links on both the listing and movie pages.
_SLUG_RE     = re.compile(r"/checkout/showing/([a-z0-9-]+)/\d+", re.I)
# Showtime anchor text on a /movie/<slug> page, e.g. "June 7, 1:00 pm".
_SHOWTIME_RE = re.compile(r">\s*([A-Z][a-z]+ \d{1,2}),\s*(\d{1,2}:\d{2}\s*[apAP][mM])\s*<")


def _fetch(url: str, retries: int = 3) -> str:
    """GET with a browser UA, following redirects. Plain requests works on
    this host (no TLS-fingerprint block). Returns '' on failure."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r.text
            print(f"    HTTP {r.status_code} for {url}")
        except Exception as e:
            print(f"    fetch error ({attempt + 1}/{retries}) {url}: {e}")
        time.sleep(2 * (attempt + 1))
    return ""


def _og(prop: str, html: str) -> str:
    m = re.search(rf'<meta\s+property="og:{prop}"\s+content="([^"]*)"', html, re.I)
    return _htmllib.unescape(m.group(1).strip()) if m else ""


def movie_slugs(listing_html: str) -> list[str]:
    """Distinct film slugs currently listed, in first-seen order."""
    seen: list[str] = []
    for slug in _SLUG_RE.findall(listing_html):
        s = slug.lower()
        if s not in seen:
            seen.append(s)
    return seen


def _resolve_year(month_day: str, today: date) -> date | None:
    """'June 7' (no year) → a date. Assume the current year; if that lands
    well in the past (a Dec→Jan rollover near year end), bump a year."""
    for yr in (today.year, today.year + 1):
        try:
            d = datetime.strptime(f"{month_day} {yr}", "%B %d %Y").date()
        except ValueError:
            return None
        if d >= today - timedelta(days=60):
            return d
    return None


def parse_movie(movie_html: str, slug: str, today: date) -> dict | None:
    """Parse a /movie/<slug> page into title/image/description + showings
    grouped by date: {date: ['10:15 AM', '1:00 PM', ...]}."""
    by_date: dict[date, list[tuple[datetime, str]]] = defaultdict(list)
    for month_day, raw_time in _SHOWTIME_RE.findall(movie_html):
        d = _resolve_year(month_day, today)
        if not d:
            continue
        try:
            t = datetime.strptime(raw_time.replace(" ", "").upper(), "%I:%M%p")
        except ValueError:
            continue
        by_date[d].append((t, t.strftime("%-I:%M %p")))
    if not by_date:
        return None
    showings = {}
    for d, times in by_date.items():
        ordered = [label for _, label in sorted(set(times), key=lambda x: x[0])]
        showings[d] = ordered
    return {
        "slug":        slug,
        "title":       _og("title", movie_html) or slug.replace("-", " ").title(),
        "image":       _og("image", movie_html),
        "description": _og("description", movie_html),
        "showings":    showings,
    }


def build_events(movie: dict, today: date, window_end: date) -> list[dict]:
    """One event per (film, weekend date) within the window."""
    events: list[dict] = []
    synopsis = (movie["description"] or "").strip()
    desc = (f"{synopsis} " if synopsis else "") + f"Now playing at {VENUE}."
    for d, times in sorted(movie["showings"].items()):
        if d.weekday() not in WEEKEND_DAYS:
            continue
        if not (today <= d <= window_end):
            continue
        events.append({
            "event_name":  movie["title"],
            "description": desc.strip()[:2000],
            "source_url":  f"{BASE}/movie/{movie['slug']}",
            "image_url":   movie["image"],
            "start_date":  d,
            "end_date":    d,
            "time":        "Showtimes: " + ", ".join(times),
            "location":    VENUE,
            "address":     ADDRESS,
            "city":        CITY,
        })
    return events


def run(newsletter: str | None = None, db_id: str | None = None) -> int:
    newsletter = newsletter or os.environ.get("NEWSLETTER", NEWSLETTER_DEFAULT)
    if db_id is None:
        db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if not db_id:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)
    print("Springs Cinema & Taphouse scraper")
    print(f"  → Listing:     {LISTING_URL}")
    print(f"  → Notion DB:   {db_id[:8]}…")
    print(f"  → Newsletter:  {newsletter}")
    print(f"  → Weekend window: {today} → {window_end} (Fri/Sat/Sun only)\n")

    listing = _fetch(LISTING_URL)
    if not listing:
        return 1
    slugs = movie_slugs(listing)
    print(f"Found {len(slugs)} current film(s): {', '.join(slugs) or '(none)'}\n")

    existing = existing_source_urls(db_id, newsletter=newsletter)

    candidates: list[dict] = []
    for slug in slugs:
        page = _fetch(f"{BASE}/movie/{slug}")
        if not page:
            continue
        movie = parse_movie(page, slug, today)
        if not movie:
            continue
        candidates.extend(build_events(movie, today, window_end))
        time.sleep(0.5)

    candidates.sort(key=lambda e: (e["start_date"], e["event_name"]))

    inserted = updated = 0
    print(f"━━ Saving {len(candidates)} weekend showing-day(s) ━━")
    for ev in candidates:
        page_id = existing.get((ev["source_url"], ev["start_date"].isoformat()))
        if save_event(db_id, ev, newsletter, page_id=page_id):
            updated += bool(page_id)
            inserted += (not page_id)
            print(f"  {'↻' if page_id else '✓'} {ev['start_date']} "
                  f"{ev['event_name'][:50]}  [{ev['time'][:40]}]")
    print(f"\n✓ Done. Inserted {inserted}, refreshed {updated}.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
