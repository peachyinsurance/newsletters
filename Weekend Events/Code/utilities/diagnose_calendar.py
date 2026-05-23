#!/usr/bin/env python3
"""Diagnostic for the Tribe Events scrapers. Walks the calendar pages
the same way _shared/tribe_events.run_tribe_source does, but instead
of saving to Notion it prints every unique Source URL with its title
and every date it appears on. Tells you whether a calendar truly has
only ~22 events or whether something else is going on.

Usage (from repo root):
    NOTION_API_KEY=... NOTION_WEEKEND_EVENTS_DB_ID=... \\
    python3 "Weekend Events/Code/utilities/diagnose_calendar.py"
"""
import os
import sys
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from tribe_events import fetch_page_events, normalize_event  # noqa: E402

# Hardcoded list of Tribe Events sources to diagnose. Mirror whatever
# per-source wrappers live under East_Cobb_Connect/ (travel_cobb.py,
# visit_marietta.py, kennesaw.py, battery_atlanta.py).
SOURCES = [
    "https://travelcobb.org/cobb-county-events/",
    "https://visitmariettaga.com/events/",
    "https://www.kennesaw-ga.gov/events/category/events/",
    "https://batteryatl.com/events-calendar/",
]


def main() -> int:
    print(f"Diagnostic walk of {len(SOURCES)} source(s):")
    for s in SOURCES:
        print(f"  - {s}")
    print()

    # url -> { "title": str, "dates": list[str] }
    by_url: dict[str, dict] = defaultdict(lambda: {"title": "", "dates": []})
    pages_seen = 0
    raw_total = 0
    for source_url in SOURCES:
        print(f"━━ {source_url} ━━")
        page = 1
        while True:
            events = fetch_page_events(source_url, page)
            if not events:
                print(f"  [page {page}] no events — stopping")
                break
            pages_seen += 1
            raw_total += len(events)
            new_on_page = 0
            for raw in events:
                ev = normalize_event(raw)
                url = ev.get("source_url", "")
                if not url:
                    continue
                if url not in by_url:
                    new_on_page += 1
                by_url[url]["title"] = ev.get("event_name", "")[:80] or by_url[url]["title"]
                date_str = ev["start_date"].isoformat() if ev.get("start_date") else "(no-date)"
                by_url[url]["dates"].append(date_str)
            print(f"  [page {page}] {len(events)} events  ({new_on_page} URLs new this page)")
            if new_on_page == 0:
                print(f"  [page {page}] all events already seen — stopping")
                break
            page += 1
        print()

    print()
    print(f"=== Summary ===")
    print(f"  Pages walked:        {pages_seen}")
    print(f"  Raw event listings:  {raw_total}")
    print(f"  Unique Source URLs:  {len(by_url)}")
    print()

    # Sort by earliest occurrence date so the listing is readable.
    def _sort_key(item):
        dates = [d for d in item[1]["dates"] if d != "(no-date)"]
        return min(dates) if dates else "9999"
    items = sorted(by_url.items(), key=_sort_key)

    print(f"=== Unique URLs (title, occurrences, dates) ===")
    for url, info in items:
        n = len(info["dates"])
        # Show first 5 dates + count
        unique_dates = sorted(set(info["dates"]))
        date_preview = ", ".join(unique_dates[:5])
        if len(unique_dates) > 5:
            date_preview += f", … (+{len(unique_dates) - 5} more)"
        flag = " [RECURRING]" if n > 1 else ""
        print(f"  · {info['title'][:65]:<65}  ×{n:<2}{flag}")
        print(f"      {url}")
        print(f"      dates: {date_preview}")

    # Optional: compare against what's already in the Notion DB
    db_id = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
    if db_id:
        try:
            from notion_save import existing_source_urls
            existing = existing_source_urls(db_id)
            in_db = {u for u in by_url if u in existing}
            not_in_db = {u for u in by_url if u not in existing}
            in_db_but_not_calendar = existing - set(by_url)
            print()
            print(f"=== Notion DB cross-check ===")
            print(f"  In DB and visible in calendar:   {len(in_db)}")
            print(f"  In calendar but NOT in DB:        {len(not_in_db)}")
            print(f"  In DB but NOT in calendar:        {len(in_db_but_not_calendar)}")
            if not_in_db:
                print()
                print(f"  Calendar URLs missing from DB (would be inserted on next run):")
                for u in sorted(not_in_db):
                    print(f"    + {by_url[u]['title'][:60]}  →  {u}")
        except Exception as e:
            print(f"\n  (Notion cross-check skipped: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
