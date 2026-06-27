#!/usr/bin/env python3
"""
Cleanup real estate weekly:
  - Flip 'approved' rows → 'approved - old' so the slot is freed for the next week
    while the row stays in the exclusion list (anti-repeat history).
  - Keep 'approved - old' rows FOREVER — a featured high-value home is never
    re-featured, so its row must persist as permanent exclusion history.
  - Archive 'pending' / 'rejected' / blank-status rows (stale candidates).

Anti-repeat is permanent for Real Estate (unlike pets), because the high-end
inventory in a 5-mile radius is small and repeats are very noticeable.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import query_database, archive_page, update_page

NOTION_RE_DB_ID = os.environ.get("NOTION_RE_DB_ID", "")
NEWSLETTER_SCOPE = (os.environ.get("NEWSLETTER") or "all").strip()


def cleanup_real_estate_notion() -> None:
    if not NOTION_RE_DB_ID:
        print("  NOTION_RE_DB_ID not set — skipping cleanup")
        return

    print("  Featured homes ('approved' / 'approved - old') are kept FOREVER (permanent anti-repeat)")
    if NEWSLETTER_SCOPE.lower() != "all":
        print(f"  Scope: {NEWSLETTER_SCOPE} (rows for other newsletters are skipped)")

    try:
        pages = query_database(NOTION_RE_DB_ID)
    except Exception as e:
        print(f"  Query failed: {e}")
        return

    print(f"  Scanning {len(pages)} real estate rows…")
    flipped = archived = kept_old = skipped_scope = 0

    for page in pages:
        page_id = page["id"]
        props = page["properties"]
        if NEWSLETTER_SCOPE.lower() != "all":
            row_newsletter = (props.get("Newsletter", {}).get("select") or {}).get("name") or ""
            if row_newsletter != NEWSLETTER_SCOPE:
                skipped_scope += 1
                continue
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
            # Permanent retention — never archived, regardless of age.
            kept_old += 1
            print(f"  🔒 Keeping 'approved - old' forever: {title} (date: {date_str})")
            continue

        # Stale CANDIDATE rows (pending / rejected / blank) → archive.
        archive_page(page_id)
        print(f"  Archived stale candidate: {title} (status: '{status_name}', date: {date_str})")
        archived += 1

    print(f"\nFlipped  {flipped} 'approved' → 'approved - old'")
    print(f"Kept     {kept_old} 'approved - old' rows (permanent exclusion)")
    print(f"Archived {archived} stale candidate rows (pending / rejected / blank)")
    if NEWSLETTER_SCOPE.lower() != "all":
        print(f"Skipped  {skipped_scope} rows belonging to other newsletters (scope: {NEWSLETTER_SCOPE})")


if __name__ == "__main__":
    cleanup_real_estate_notion()
    print("✓ Real estate cleanup complete")
