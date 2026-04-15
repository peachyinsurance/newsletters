#!/usr/bin/env python3
"""
Archive Approved Items — Runs every Friday at 5pm EST.
Flips all "approved" (and "Tier 1 Winner" / "Tier 2 Winner") statuses
to "approved - old" across pets, restaurants, and featured events databases.
This preserves items in Notion for exclusion checks while clearing the
"approved" slot for next week's picks.
"""
import os
import sys

sys.path.append(os.path.dirname(__file__))
from notion_helper import (
    HEADERS,
    NOTION_PETS_DB_ID,
    NOTION_RESTAURANTS_DB_ID,
    NOTION_EVENTS_DB_ID,
    query_database,
)

import requests


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

    print(f"Done. Total items archived: {total}")
