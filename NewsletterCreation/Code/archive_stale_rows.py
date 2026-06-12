#!/usr/bin/env python3
"""
One-shot stale-data cleanup: for a given newsletter, find rows in every
section DB whose `Date Generated` is older than the cutoff and flip their
`Status` to `approved - old` (the assembler's "exclude from rendering"
sentinel).

Use when the landing page is showing data from previous weeks because old
approved rows linger in the DBs. After this runs, dispatch
`assemble_newsletter.yml` (newsletter=<X>, section=all) to repaint the page.

Inputs (env vars):
  NEWSLETTER     — newsletter name (e.g. East_Cobb_Connect, all)
  CUTOFF_DAYS    — anything older than today minus N days is archived (default 5)

Each section DB is queried with a filter for the newsletter, then iterated.
Skips rows already in `approved - old` or `rejected` (we'd just be redoing
work). Prints per-DB counts.
"""
import os
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(__file__))
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_PETS_DB_ID,
    NOTION_RESTAURANTS_DB_ID,
    NOTION_LOWDOWN_DB_ID,
    NOTION_RE_DB_ID,
    NOTION_EVENTS_DB_ID,
    NOTION_INTRO_DB_ID,
    NOTION_TIPS_DB_ID,
    NOTION_FREE_EVENTS_DB_ID,
    NOTION_POLLS_DB_ID,
    NOTION_WEEKEND_PLANNER_DB_ID,
    NOTION_BUSINESS_BRIEF_DB_ID,
    NOTION_MEMES_DB_ID,
    NOTION_IN_SEARCH_OF_DB_ID,
)

NEWSLETTER  = os.environ.get("NEWSLETTER", "all").strip()
CUTOFF_DAYS = int(os.environ.get("CUTOFF_DAYS", "5"))

# CUTOFF_DAYS=0 → archive EVERY row (regardless of date) — full nuke for
# the chosen newsletter. Otherwise archive rows older than (today - N days).
ARCHIVE_ALL  = CUTOFF_DAYS == 0
CUTOFF_DATE  = date.today() - timedelta(days=CUTOFF_DAYS)
SKIP_STATUSES = {"approved - old", "rejected"}

# (db_id, friendly_label) — order matches landing-page render order roughly.
SECTION_DBS = [
    (NOTION_INTRO_DB_ID,           "Welcome Intro"),
    (NOTION_EVENTS_DB_ID,          "Featured Event"),
    (NOTION_RESTAURANTS_DB_ID,     "Restaurants"),
    (NOTION_RE_DB_ID,              "Real Estate"),
    (NOTION_LOWDOWN_DB_ID,         "Local Lowdown"),
    (NOTION_PETS_DB_ID,            "Pets"),
    (NOTION_WEEKEND_PLANNER_DB_ID, "Weekend Planner"),
    (NOTION_FREE_EVENTS_DB_ID,     "Free Events"),
    (NOTION_TIPS_DB_ID,            "Insurance Tip"),
    (NOTION_BUSINESS_BRIEF_DB_ID,  "Business Brief"),
    (NOTION_IN_SEARCH_OF_DB_ID,    "In Search Of"),
    (NOTION_MEMES_DB_ID,           "Memes"),
    (NOTION_POLLS_DB_ID,           "Reader Poll"),
]


def _row_status(props: dict) -> str:
    return ((props.get("Status") or {}).get("select") or {}).get("name", "") or ""


def _row_date(props: dict) -> str:
    """Return the row's `Date Generated` (ISO YYYY-MM-DD) or '' if missing."""
    return ((props.get("Date Generated") or {}).get("date") or {}).get("start", "") or ""


def _row_newsletter(props: dict) -> str:
    return ((props.get("Newsletter") or {}).get("select") or {}).get("name", "") or ""


def archive_stale_in_db(db_id: str, label: str) -> tuple[int, int]:
    """Return (archived_count, scanned_count). Flips Status of stale rows."""
    if not db_id:
        print(f"  [{label}] DB id not configured — skipping")
        return (0, 0)

    # Filter at the API level when we can — saves a lot of pagination
    filters: dict | None = None
    if NEWSLETTER and NEWSLETTER.lower() != "all":
        filters = {
            "property": "Newsletter",
            "select":   {"equals": NEWSLETTER},
        }

    try:
        pages = query_database(db_id, filters=filters)
    except Exception as e:
        print(f"  [{label}] query failed: {e}")
        return (0, 0)

    scanned = len(pages)
    archived = 0
    for page in pages:
        props = page.get("properties", {})
        status = _row_status(props)
        if status in SKIP_STATUSES:
            continue
        date_str = _row_date(props)
        if not ARCHIVE_ALL:
            # Rows with no date — treat as stale (legacy / orphan rows)
            if date_str:
                try:
                    row_date = date.fromisoformat(date_str)
                except ValueError:
                    row_date = None
                if row_date and row_date >= CUTOFF_DATE:
                    continue  # row is current — keep it

        nl = _row_newsletter(props)
        try:
            update_page(page["id"], {"Status": {"select": {"name": "approved - old"}}})
            archived += 1
            title_prop = props.get("Name", {}).get("title", [])
            title = title_prop[0]["text"]["content"] if title_prop else "(no title)"
            print(f"    archived [{nl}] {date_str or 'no date'} → {title[:60]}")
        except Exception as e:
            print(f"    ✗ failed to archive {page['id']}: {e}")

    print(f"  [{label}] archived {archived} of {scanned} scanned")
    return (archived, scanned)


def main() -> None:
    print("=" * 60)
    print(f"Archiving stale rows for: {NEWSLETTER!r}")
    if ARCHIVE_ALL:
        print(f"Cutoff: ARCHIVE EVERYTHING (CUTOFF_DAYS=0 — full nuke)")
    else:
        print(f"Cutoff: anything dated before {CUTOFF_DATE} (older than {CUTOFF_DAYS} days)")
    print("=" * 60)

    total_archived = 0
    total_scanned  = 0
    for db_id, label in SECTION_DBS:
        a, s = archive_stale_in_db(db_id, label)
        total_archived += a
        total_scanned  += s

    print("=" * 60)
    print(f"DONE — archived {total_archived} of {total_scanned} rows total")
    print("Now run: Assemble Newsletter Pages with section=all to repaint")
    print("=" * 60)


if __name__ == "__main__":
    main()
