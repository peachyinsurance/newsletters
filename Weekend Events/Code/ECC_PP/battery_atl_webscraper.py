#!/usr/bin/env python3
"""Scrape The Battery Atlanta's events calendar and save in-window
events to the Weekend Events Notion DB.

The Battery's WP site uses The Events Calendar plugin (same as
travelcobb / visitmariettaga / kennesaw-ga.gov), so the parsing is
identical: JSON-LD Event objects on every `?tribe_paged=N` page.
This file just provides the URL + main entry; everything else
(fetch_page_events, normalize_event, save_event, dedup) is imported
from ecc_event_webscraper.

Newsletter tag: defaults to East_Cobb_Connect. The Battery is a
20-minute drive from East Cobb and a destination for Cumberland-area
readers. Lives in ECC_PP/ alongside sandy_springs because both are
'destination' scrapers (places people drive to) — but tags ECC only
unless NEWSLETTER env var is overridden.

Window: today → upcoming_friday + 14 days (matches the other
weekend-events scrapers and the Featured Event picker).
"""
import os
import sys
from datetime import date, timedelta

# Sibling-folder import of the shared Tribe-Events helpers + Notion save.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ECC"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from ecc_event_webscraper import (  # noqa: E402
    fetch_page_events,
    normalize_event,
    _normalize_title,
    existing_source_urls,
    save_event,
    format_dates_human,
)
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import is_cancelled_event  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

SOURCE = "https://batteryatl.com/events-calendar/"
END_WINDOW_DAYS = 14


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today      = date.today()
    window_end = _upcoming_friday(today) + timedelta(days=END_WINDOW_DAYS)

    print("Battery Atlanta scraper")
    print(f"  → Source:       {SOURCE}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {today} → {window_end}")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup: {len(existing)} URLs already in DB\n")

    # Track (url, date) tuples to detect calendar wrap (server redirects
    # over-range page numbers to a valid page).
    seen_occurrences: set[tuple[str, str]] = set()
    # Group by normalized title so recurring events (Jazz Brunch every
    # Sat/Sun, Braves home stands, etc.) collapse into one Notion row
    # with `all_dates` covering every in-window occurrence.
    by_name: dict[str, dict] = {}

    skipped_past   = 0
    skipped_future = 0

    page = 1
    while True:
        events = fetch_page_events(SOURCE, page)
        if not events:
            print(f"  [page {page}] no events — stopping")
            break
        new_occurrences = 0
        all_past_end    = True
        for raw in events:
            ev = normalize_event(raw)
            url = ev.get("source_url", "")
            sd  = ev.get("start_date")
            name = ev.get("event_name", "")
            if not url or not sd or not name:
                continue
            if is_cancelled_event(name, ev.get("description", "")):
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
            name_key = _normalize_title(name)
            if not name_key:
                continue
            entry = by_name.get(name_key)
            if entry is None:
                ev["all_dates"] = {sd}
                by_name[name_key] = ev
            else:
                entry["all_dates"].add(sd)
                if sd < entry["start_date"]:
                    entry["start_date"] = sd
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
    candidates = sorted(by_name.values(),
                        key=lambda e: e["start_date"] or date.max)

    # Battery's JSON-LD includes inline `image` URLs already, so og:image
    # backfill is usually a no-op — but run it anyway for the few events
    # that ship without one.
    from event_image_scraper import backfill_images  # noqa: E402
    filled = backfill_images(candidates)
    if filled:
        print(f"  ↳ Backfilled {filled} image(s) from source pages")

    inserted = 0
    skipped_existing = 0
    multi_date = 0
    print(f"━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        if len(ev.get("all_dates") or {}) > 1:
            multi_date += 1
        if ev["source_url"] in existing:
            skipped_existing += 1
            continue
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER):
            inserted += 1
            dates_disp = format_dates_human(ev.get("all_dates") or [])
            print(f"  ✓ {dates_disp or ev['start_date']}  {ev['event_name'][:60]}")
    print()
    print(f"✓ Done. Inserted {inserted}, "
          f"skipped {skipped_existing} existing, "
          f"{skipped_past} past, "
          f"{skipped_future} beyond {window_end}  "
          f"({multi_date} multi-date event(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
