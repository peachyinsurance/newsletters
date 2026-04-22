#!/usr/bin/env python3
"""
Cleanup free events: archive Notion entries older than 8 weeks.
Keeps the exclusion list (approved + approved - old) from growing unbounded.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import query_database, archive_page

NOTION_FREE_EVENTS_DB_ID = os.environ.get("NOTION_FREE_EVENTS_DB_ID", "")


def cleanup_old_free_events() -> None:
    """Archive Free Events rows older than 8 weeks."""
    if not NOTION_FREE_EVENTS_DB_ID:
        print("  NOTION_FREE_EVENTS_DB_ID not set — skipping cleanup")
        return

    cutoff = (datetime.today() - timedelta(weeks=8)).strftime("%Y-%m-%d")
    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Date Generated",
            "date":     {"before": cutoff}
        })
    except Exception as e:
        print(f"  Query failed: {e}")
        pages = []

    count = 0
    for page in pages:
        name_title = page["properties"].get("Name", {}).get("title") or [{}]
        name = name_title[0].get("text", {}).get("content", "") if name_title else ""
        archive_page(page["id"])
        print(f"  Archived: {name}")
        count += 1

    if count:
        print(f"\n  Archived {count} free events entries older than 8 weeks")
    else:
        print("  No free events entries older than 8 weeks")


if __name__ == "__main__":
    cleanup_old_free_events()
    print("✓ Free events cleanup complete")
