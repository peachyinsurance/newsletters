#!/usr/bin/env python3
"""
Cleanup free events weekly:
  - Flip 'approved' rows → 'approved - old' (frees the slot, keeps in exclusion list).
  - Keep 'approved - old' newer than 8 weeks (still useful for exclusion).
  - Archive 'approved - old' older than 8 weeks.
  - Archive 'pending' / 'rejected' / blank-status rows (stale candidates).

Mirrors cleanup_pets_notion + cleanup_real_estate_notion so all sections share
the same rolling-exclusion behavior.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import query_database, archive_page, update_page

NOTION_FREE_EVENTS_DB_ID = os.environ.get("NOTION_FREE_EVENTS_DB_ID", "")
APPROVED_OLD_WEEKS = 8


def cleanup_free_events_notion() -> None:
    if not NOTION_FREE_EVENTS_DB_ID:
        print("  NOTION_FREE_EVENTS_DB_ID not set — skipping cleanup")
        return

    cutoff = (datetime.today() - timedelta(weeks=APPROVED_OLD_WEEKS)).strftime("%Y-%m-%d")
    print(f"  Cutoff for 'approved - old' archival: {cutoff} ({APPROVED_OLD_WEEKS} weeks ago)")

    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID)
    except Exception as e:
        print(f"  Query failed: {e}")
        return

    print(f"  Scanning {len(pages)} free-event rows…")
    flipped = archived = kept_old = 0

    for page in pages:
        page_id = page["id"]
        props = page["properties"]
        status_obj = props.get("Status", {}).get("select") or props.get("Status", {}).get("status") or {}
        status_name = (status_obj or {}).get("name", "") if isinstance(status_obj, dict) else ""
        date_str = (props.get("Date Generated", {}).get("date") or {}).get("start", "") or ""
        name_title = props.get("Name", {}).get("title") or [{}]
        name = name_title[0].get("text", {}).get("content", "") if name_title else ""

        if status_name == "approved":
            update_page(page_id, {"Status": {"select": {"name": "approved - old"}}})
            print(f"  🔄 Flipped 'approved' → 'approved - old': {name}")
            flipped += 1
            continue

        if status_name == "approved - old":
            if not date_str or date_str >= cutoff:
                kept_old += 1
                print(f"  🔒 Keeping 'approved - old': {name} (date: {date_str})")
                continue  # within window — keep for exclusion

        archive_page(page_id)
        print(f"  Archived: {name} (status: '{status_name}', date: {date_str})")
        archived += 1

    print(f"\nFlipped  {flipped} 'approved' → 'approved - old'")
    print(f"Kept     {kept_old} 'approved - old' rows within {APPROVED_OLD_WEEKS}-week window")
    print(f"Archived {archived} stale Free Event entries (pending / rejected / >{APPROVED_OLD_WEEKS}w old)")


if __name__ == "__main__":
    cleanup_free_events_notion()
    print("✓ Free events cleanup complete")
