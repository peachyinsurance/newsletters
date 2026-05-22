#!/usr/bin/env python3
"""
Archive Approved Items — Runs every Friday at 5pm EST.

Two passes:

  1. Section archive: flips ALL "approved" (and "Tier 1 Winner" /
     "Tier 2 Winner") rows to "approved - old" across pets,
     restaurants, and featured events. Preserves the row for
     exclusion / dedup, frees the slot for next week's picks.

  2. Stale manually-edited sweep: walks every DB that has a
     Manually Edited + Date Generated + Status trio and flips
     manually-edited rows older than MAX_MANUAL_EDIT_DAYS to
     "approved - old". Without this, a manually-edited row stays
     "current" forever (the save_X_to_notion functions skip
     overwriting any current row with Manually Edited = True),
     so the section content goes stale until a human un-flags it.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(__file__))
from notion_helper import (
    HEADERS,
    NOTION_PETS_DB_ID,
    NOTION_RESTAURANTS_DB_ID,
    NOTION_EVENTS_DB_ID,
    NOTION_LOWDOWN_DB_ID,
    NOTION_RE_DB_ID,
    NOTION_INTRO_DB_ID,
    NOTION_FREE_EVENTS_DB_ID,
    NOTION_BUSINESS_BRIEF_DB_ID,
    NOTION_TIPS_DB_ID,
    query_database,
)

import requests

# Stale-manual-edit window. Rows older than this with
# Manually Edited == True flip to approved - old on Friday.
MAX_MANUAL_EDIT_DAYS = 7


def flip_status(database_id: str, db_name: str, active_statuses: list[str]) -> int:
    """Flip all pages with active statuses to 'approved - old'.
    Returns count of pages flipped."""
    flipped = 0
    for status in active_statuses:
        try:
            pages = query_database(database_id, filters={
                "property": "Status",
                "status":   {"equals": status}
            })
        except Exception:
            # Try select filter instead of status
            try:
                pages = query_database(database_id, filters={
                    "property": "Status",
                    "select":   {"equals": status}
                })
            except Exception:
                pages = []

        for page in pages:
            page_id = page["id"]
            name_prop = page["properties"].get("Name", {}).get("title", [{}])
            name = name_prop[0].get("text", {}).get("content", "") if name_prop else "unknown"

            r = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=HEADERS,
                json={"properties": {"Status": {"select": {"name": "approved - old"}}}},
                timeout=30,
            )
            if r.ok:
                flipped += 1
                print(f"  ✓ {name}: {status} → approved - old")
            else:
                print(f"  ✗ Failed to update {name}: {r.text[:200]}")

    return flipped


def flip_stale_manual_edits(database_id: str, db_name: str,
                            max_age_days: int = MAX_MANUAL_EDIT_DAYS) -> int:
    """Walk the DB, find rows where:
      - Manually Edited == True
      - Status is current (anything other than "approved - old" /
        "rejected" — covers approved, pending, Tier 1, etc.)
      - Date Generated is older than `max_age_days`
    Flip those Status → "approved - old" so next week's automation
    can populate fresh content without being blocked by the manual-
    edit preservation logic in save_X_to_notion."""
    if not database_id:
        return 0
    try:
        pages = query_database(database_id)
    except Exception as e:
        print(f"  ⚠ Could not query {db_name}: {e}")
        return 0

    cutoff = datetime.today().date() - timedelta(days=max_age_days)
    flipped = 0
    for page in pages:
        props = page.get("properties", {}) or {}
        # Filter 1: must be flagged manually edited
        if not props.get("Manually Edited", {}).get("checkbox", False):
            continue
        # Filter 2: must be in a current status (skip already-archived / rejected)
        status_sel = props.get("Status", {}).get("select") or {}
        status_name = (status_sel.get("name") or "").strip()
        if status_name in ("approved - old", "rejected", ""):
            continue
        # Filter 3: must be older than the cutoff
        date_str = (props.get("Date Generated", {}).get("date") or {}).get("start", "") or ""
        if not date_str:
            continue
        try:
            row_date = datetime.fromisoformat(date_str[:10]).date()
        except Exception:
            continue
        if row_date >= cutoff:
            continue

        # Flip Status → approved - old
        page_id = page["id"]
        name_prop = props.get("Name", {}).get("title", [{}])
        name = name_prop[0].get("text", {}).get("content", "") if name_prop else "unknown"
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS,
            json={"properties": {"Status": {"select": {"name": "approved - old"}}}},
            timeout=30,
        )
        if r.ok:
            flipped += 1
            age_days = (datetime.today().date() - row_date).days
            print(f"  ✓ {name} ({age_days}d old, {status_name}) → approved - old")
        else:
            print(f"  ✗ Failed to update {name}: {r.text[:200]}")
    return flipped


if __name__ == "__main__":
    print("Archiving approved items...\n")
    total = 0

    # Pets: approved → approved - old
    if NOTION_PETS_DB_ID:
        print("Pets:")
        count = flip_status(NOTION_PETS_DB_ID, "Pets", ["approved"])
        total += count
        print(f"  Flipped {count} pets\n")

    # Restaurants: Tier 1 Winner, Tier 2 Winner → approved - old
    if NOTION_RESTAURANTS_DB_ID:
        print("Restaurants:")
        count = flip_status(NOTION_RESTAURANTS_DB_ID, "Restaurants", ["Tier 1 Winner", "Tier 2 Winner"])
        total += count
        print(f"  Flipped {count} restaurants\n")

    # Featured Events: approved → approved - old
    if NOTION_EVENTS_DB_ID:
        print("Featured Events:")
        count = flip_status(NOTION_EVENTS_DB_ID, "Featured Events", ["approved"])
        total += count
        print(f"  Flipped {count} events\n")

    print(f"Section archive done. Total items archived: {total}\n")

    # ── Pass 2: stale manually-edited rows across every DB ───────────
    print(f"Sweeping for manually-edited rows older than "
          f"{MAX_MANUAL_EDIT_DAYS} days...\n")
    stale_total = 0
    sections = [
        ("Welcome Intro",   NOTION_INTRO_DB_ID),
        ("Featured Events", NOTION_EVENTS_DB_ID),
        ("Restaurants",     NOTION_RESTAURANTS_DB_ID),
        ("Pets",            NOTION_PETS_DB_ID),
        ("Local Lowdown",   NOTION_LOWDOWN_DB_ID),
        ("Real Estate",     NOTION_RE_DB_ID),
        ("Free Events",     NOTION_FREE_EVENTS_DB_ID),
        ("Business Brief",  NOTION_BUSINESS_BRIEF_DB_ID),
        ("Insurance Tip",   NOTION_TIPS_DB_ID),
    ]
    for label, db_id in sections:
        if not db_id:
            continue
        print(f"{label}:")
        n = flip_stale_manual_edits(db_id, label)
        stale_total += n
        print(f"  Flipped {n} stale manually-edited row(s)\n")

    print(f"Done. Section archive: {total}, stale manual edits: {stale_total}.")
