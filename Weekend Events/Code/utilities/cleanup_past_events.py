#!/usr/bin/env python3
"""Archive (delete via Notion's archive flag) rows in the Weekend Events
DB whose Date has already passed. Runs weekly so the DB stays focused on
upcoming events only.

Filters by the NEWSLETTER env var (defaults to East_Cobb_Connect) — so
each newsletter's cleanup is independent. Multi-day events with end
dates in the future are KEPT (they're still upcoming).
"""
import os
import sys
from datetime import date, datetime

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1
    today = date.today()
    print(f"Cleaning Weekend Events DB for {NEWSLETTER}")
    print(f"  Today: {today}")

    pages = query_database(WEEKEND_EVENTS_DB_ID, filters={
        "property": "Newsletter",
        "select":   {"equals": NEWSLETTER},
    })
    if not pages:
        print("  No rows for this newsletter.")
        return 0
    print(f"  Found {len(pages)} rows for this newsletter")

    archived = 0
    kept = 0
    for p in pages:
        props = p.get("properties", {})
        d = props.get("Date", {}).get("date") or {}
        start = d.get("start") or ""
        end   = d.get("end") or start  # multi-day fallback to start if no end
        # Parse dates
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
        except Exception:
            end_dt = None
        # Keep rows we can't date (rather than nuking them blindly)
        if end_dt is None:
            kept += 1
            continue
        if end_dt < today:
            r = requests.patch(f"https://api.notion.com/v1/pages/{p['id']}",
                               headers=NOTION_HEADERS,
                               json={"archived": True}, timeout=20)
            if r.ok:
                title = (props.get("Event Name", {}).get("rich_text") or [])
                name = title[0].get("text", {}).get("content", "") if title else "?"
                print(f"  ✓ archived {end_dt}  {name[:60]}")
                archived += 1
            else:
                print(f"  ✗ archive failed for {p['id']}: {r.status_code} {r.text[:120]}")
        else:
            kept += 1
    print(f"\n✓ Done. Archived {archived}, kept {kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
