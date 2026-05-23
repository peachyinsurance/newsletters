#!/usr/bin/env python3
"""Archive (delete via Notion's archive flag) rows in the Weekend Events
DB whose Date has already passed. Runs weekly so the DB stays focused on
upcoming events only.

`NEWSLETTER` env var modes:
  • `all` (or empty) — clean every newsletter tag, including the ECC_PP
    shared tag.
  • specific name (e.g. `East_Cobb_Connect`) — clean just that one tag.

Multi-day events with end dates in the future are KEPT (still upcoming).
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
NEWSLETTER = os.environ.get("NEWSLETTER", "all")

# Newsletter tags `NEWSLETTER=all` will sweep. ECC_PP rows are
# date-archived too — multi-newsletter cleanup is one pass over the
# union, not per-newsletter (a row tagged ECC_PP is shared by ECC and
# PP but only needs to be archived once when its Date passes).
ALL_NEWSLETTER_TAGS = [
    "East_Cobb_Connect",
    "Perimeter_Post",
    "Lewisville_Lake_Lookout",
    "ECC_PP",
]


def _clean_newsletter(tag: str, today: date) -> tuple[int, int]:
    """Sweep one newsletter tag. Returns (archived, kept)."""
    pages = query_database(WEEKEND_EVENTS_DB_ID, filters={
        "property": "Newsletter",
        "select":   {"equals": tag},
    })
    if not pages:
        print(f"  {tag}: no rows")
        return 0, 0
    print(f"  {tag}: {len(pages)} row(s) to scan")

    archived = 0
    kept = 0
    for p in pages:
        props = p.get("properties", {})
        d = props.get("Date", {}).get("date") or {}
        start = d.get("start") or ""
        end   = d.get("end") or start  # multi-day fallback
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
        except Exception:
            end_dt = None
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
                print(f"    ✓ archived {end_dt}  [{tag}]  {name[:55]}")
                archived += 1
            else:
                print(f"    ✗ archive failed for {p['id']}: {r.status_code} {r.text[:120]}")
        else:
            kept += 1
    return archived, kept


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1
    today = date.today()

    requested = (NEWSLETTER or "").strip()
    if requested.lower() in ("", "all"):
        tags = ALL_NEWSLETTER_TAGS
    else:
        tags = [requested]
        # Always include ECC_PP when sweeping a specific ECC/PP tag,
        # since ECC_PP rows are visible to that newsletter.
        if requested in ("East_Cobb_Connect", "Perimeter_Post"):
            tags.append("ECC_PP")

    print(f"Cleaning Weekend Events DB — newsletter(s): {tags}")
    print(f"  Today: {today}\n")

    total_archived = 0
    total_kept     = 0
    for tag in tags:
        a, k = _clean_newsletter(tag, today)
        total_archived += a
        total_kept     += k

    print(f"\n✓ Done. Archived {total_archived}, kept {total_kept} across {len(tags)} tag(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
