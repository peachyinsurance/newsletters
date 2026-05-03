#!/usr/bin/env python3
"""
Cleanup real estate weekly:
  - Flip 'approved' rows → 'approved - old' so the slot is freed for the next week
    while the row stays in the exclusion list (anti-repeat history).
  - Keep 'approved - old' rows newer than 8 weeks (still useful for exclusion).
  - Archive 'approved - old' rows older than 8 weeks.
  - Archive 'pending' / 'rejected' / blank-status rows (stale candidates).

This mirrors cleanup_pets_notion() so the rolling exclusion behaves consistently
across sections.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import query_database, archive_page, update_page

NOTION_RE_DB_ID = os.environ.get("NOTION_RE_DB_ID", "")
APPROVED_OLD_WEEKS = 8


def cleanup_real_estate_notion() -> None:
    if not NOTION_RE_DB_ID:
        print("  NOTION_RE_DB_ID not set — skipping cleanup")
        return

    cutoff = (datetime.today() - timedelta(weeks=APPROVED_OLD_WEEKS)).strftime("%Y-%m-%d")
    print(f"  Cutoff for 'approved - old' archival: {cutoff} ({APPROVED_OLD_WEEKS} weeks ago)")

    try:
        pages = query_database(NOTION_RE_DB_ID)
    except Exception as e:
        print(f"  Query failed: {e}")
        return

    print(f"  Scanning {len(pages)} real estate rows…")
    flipped = archived = kept_old = 0

    for page in pages:
        page_id = page["id"]
        props = page["properties"]
        status_prop = props.get("Status", {})
        # Real Estate uses 'select' for Status (matches pet cleanup)
        status_obj = status_prop.get("select") or status_prop.get("status") or {}
        status_name = (status_obj or {}).get("name", "") if isinstance(status_obj, dict) else ""
        date_str = (props.get("Date Generated", {}).get("date") or {}).get("start", "") or ""
        # Title is "Headline" in the RE schema; fall back to "Name" if present
        title_prop = props.get("Headline", {}).get("rich_text") or props.get("Name", {}).get("title") or [{}]
        title = title_prop[0].get("text", {}).get("content", "") if title_prop else ""

        if status_name == "approved":
            update_page(page_id, {"Status": {"select": {"name": "approved - old"}}})
            print(f"  🔄 Flipped 'approved' → 'approved - old': {title}")
            flipped += 1
            continue

        if status_name == "approved - old":
            if not date_str or date_str >= cutoff:
                kept_old += 1
                print(f"  🔒 Keeping 'approved - old': {title} (date: {date_str})")
                continue  # within window — keep for exclusion

        # Anything else (pending/rejected/blank/expired approved-old) → archive
        archive_page(page_id)
        print(f"  Archived: {title} (status: '{status_name}', date: {date_str})")
        archived += 1

    print(f"\nFlipped  {flipped} 'approved' → 'approved - old'")
    print(f"Kept     {kept_old} 'approved - old' rows within {APPROVED_OLD_WEEKS}-week window")
    print(f"Archived {archived} stale RE entries (pending / rejected / >{APPROVED_OLD_WEEKS}w old)")


if __name__ == "__main__":
    cleanup_real_estate_notion()
    print("✓ Real estate cleanup complete")
