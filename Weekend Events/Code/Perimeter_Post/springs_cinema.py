#!/usr/bin/env python3
"""Springs Cinema & Taphouse UNIQUE-PROGRAMMING weekend scraper.

Tag: ECC_PP (shared) — Springs Cinema & Taphouse is on Roswell Rd in
Sandy Springs, a shared coverage area, so its screenings surface in both
East Cobb Connect and Perimeter Post via _SHARED_NEWSLETTER_TAGS.

We want ONLY the cinema's "unique programming" — the special / curated
screenings (classics, anniversary nights, sing-alongs, film series, fan
events like Risky Business, Goodfellas, Ace Ventura) — NOT the regular
first-run showtimes.

Platform notes (Filmbot): the /unique-programming page renders its
special-programming list in a JavaScript carousel that is NOT in the page
HTML, and the GraphQL API gates showing data behind login — so neither is
scrapeable server-side. An earlier approach tried to derive the special
films by subtracting the /showtimes/ + /unique-programming slugs from the
sitemap, but /showtimes/ only server-renders a partial slate (~8 of ~35
films), so first-run wide releases (Toy Story 5, The Odyssey, Supergirl,
Minions) leaked straight through. That heuristic is abandoned.

Instead we classify by SHOWTIME DENSITY, read from each film's own
/movie/<slug> page (which server-renders its full dated schedule as
"Month Day, h:mm am/pm" anchors). A regular first-run wide release plays a
full daily slate (4–12 showtimes on a single day); curated "unique
programming" — classics, the Retro Rewind / Summer Kid series, fan events,
one-off screenings — runs only 1–3 shows on any given day. So:

  unique programming = every /movie/<slug> in sitemap.xml
                       − private rentals (slugs starting with "-")
                       − any film with a full daily slate
                         (>= FIRST_RUN_DAILY_SHOWTIMES on any one day)

We keep Fri/Sat/Sun dates in the forward window and emit ONE Notion row
per (film, weekend date), with that day's showtimes aggregated into Time.
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

BASE          = "https://www.springscinema.com"
SITEMAP_URL   = f"{BASE}/sitemap.xml"
VENUE         = "Springs Cinema & Taphouse"
ADDRESS       = "5920 Roswell Rd, Sandy Springs, GA 30328"
CITY          = "sandy springs"          # shared city → ECC_PP via normalize sweep
NEWSLETTER_DEFAULT = "ECC_PP"
END_WINDOW_DAYS = 9                       # Fri/Sat/Sun within [today, upcoming_friday + N]
WEEKEND_DAYS    = {4, 5, 6}               # Python weekday(): Fri=4, Sat=5, Sun=6
# A film playing this many showtimes on ANY single day is a regular
# first-run wide release, not curated programming. Observed separation is
# clean: special screenings peak at 3 shows/day, wide releases run 4–12.
FIRST_RUN_DAILY_SHOWTIMES = 4

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_MOVIE_SLUG_RE = re.compile(r"/movie/([a-z0-9-]+)", re.I)
# Showtime anchor text on a /movie/<slug> page, e.g. "June 7, 1:00 pm".
_SHOWTIME_RE   = re.compile(r">\s*([A-Z][a-z]+ \d{1,2}),\s*(\d{1,2}:\d{2}\s*[apAP][mM])\s*<")


def _fetch(url: str, retries: int = 3) -> str:
    """GET with a browser UA, following redirects. Returns '' on failure."""
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


def _slugs(text: str) -> set[str]:
    return {s.lower() for s in _MOVIE_SLUG_RE.findall(text)}


def candidate_film_slugs() -> list[str]:
    """Every film slug in the sitemap, minus private rentals (slugs that
    start with '-'). First-run wide releases are weeded out later by
    showtime density (see peak_daily_showtimes / FIRST_RUN_DAILY_SHOWTIMES)."""
    return sorted(s for s in _slugs(_fetch(SITEMAP_URL)) if not s.startswith("-"))


def peak_daily_showtimes(movie: dict) -> int:
    """The most showtimes the film plays on any single day across its full
    schedule. Wide releases run a dense daily slate; curated screenings
    don't — so this cleanly separates first-run from unique programming."""
    return max((len(times) for times in movie["showings"].values()), default=0)


def _og(prop: str, html: str) -> str:
    m = re.search(rf'<meta\s+property="og:{prop}"\s+content="([^"]*)"', html, re.I)
    return _htmllib.unescape(m.group(1).strip()) if m else ""


def _resolve_year(month_day: str, today: date) -> date | None:
    """'June 7' (no year) → a date. Assume the current year; bump a year if
    that lands well in the past (Dec→Jan rollover)."""
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
    grouped by date."""
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
    showings = {d: [lbl for _, lbl in sorted(set(v), key=lambda x: x[0])]
                for d, v in by_date.items()}
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
    desc = (f"{synopsis} " if synopsis else "") + f"Special screening at {VENUE}."
    for d, times in sorted(movie["showings"].items()):
        if d.weekday() not in WEEKEND_DAYS or not (today <= d <= window_end):
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
    print("Springs Cinema & Taphouse — unique programming scraper")
    print(f"  → Notion DB:   {db_id[:8]}…  Newsletter: {newsletter}")
    print(f"  → Weekend window: {today} → {window_end} (Fri/Sat/Sun only)\n")

    slugs = candidate_film_slugs()
    if not slugs:
        print("No films found (sitemap empty?).")
        return 1
    print(f"{len(slugs)} film(s) in sitemap; filtering out first-run wide "
          f"releases (>= {FIRST_RUN_DAILY_SHOWTIMES} shows/day)\n")

    existing = existing_source_urls(db_id, newsletter=newsletter)

    candidates: list[dict] = []
    skipped_first_run = 0
    for slug in slugs:
        page = _fetch(f"{BASE}/movie/{slug}")
        if not page:
            continue
        movie = parse_movie(page, slug, today)
        if not movie:
            time.sleep(0.4)
            continue
        peak = peak_daily_showtimes(movie)
        if peak >= FIRST_RUN_DAILY_SHOWTIMES:
            print(f"  – first-run, skipped: {movie['title'][:45]} "
                  f"({peak} shows/day)")
            skipped_first_run += 1
            time.sleep(0.4)
            continue
        print(f"  ✓ unique programming: {movie['title'][:45]} "
              f"({peak} shows/day)")
        candidates.extend(build_events(movie, today, window_end))
        time.sleep(0.4)
    print(f"\n{skipped_first_run} first-run film(s) skipped; "
          f"{len(candidates)} weekend showing-day(s) from unique programming\n")

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
