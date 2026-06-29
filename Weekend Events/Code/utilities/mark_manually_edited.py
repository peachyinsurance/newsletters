#!/usr/bin/env python3
"""Set `Manually Edited = True` on rows in the Weekend Events DB so a
re-scrape won't overwrite their content (save_event leaves Manually Edited
rows' curated fields intact — see _shared/notion_save.py).

`NEWSLETTER` env var modes (same convention as cleanup_past_events.py):
  • `all` (or empty) — mark every newsletter tag, including ECC_PP.
  • specific name (e.g. `East_Cobb_Connect`) — mark just that one tag
    (plus the shared ECC_PP tag when marking an ECC/PP newsletter).

Rows already checked are skipped (no redundant PATCH). Set DRY_RUN=1 to
preview without writing.
"""
import os
import sys

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "all")
DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")

ALL_NEWSLETTER_TAGS = [
    "East_Cobb_Connect",
    "Perimeter_Post",
    "Lewisville_Lake_Lookout",
    "ECC_PP",
]


def _mark_newsletter(tag: str) -> tuple[int, int, int]:
    """Mark one newsletter tag. Returns (marked, already, failed)."""
    pages = query_database(WEEKEND_EVENTS_DB_ID, filters={
        "property": "Newsletter",
        "select":   {"equals": tag},
    })
    if not pages:
        print(f"  {tag}: no rows")
        return 0, 0, 0
    print(f"  {tag}: {len(pages)} row(s) to scan")

    marked = already = failed = 0
    for p in pages:
        props = p.get("properties", {})
        title = (props.get("Event Name", {}).get("rich_text") or [])
        name = title[0].get("text", {}).get("content", "") if title else "?"
        if (props.get("Manually Edited", {}) or {}).get("checkbox") is True:
            already += 1
            continue
        if DRY_RUN:
            print(f"    · would mark  [{tag}]  {name[:55]}")
            marked += 1
            continue
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{p['id']}",
            headers=NOTION_HEADERS,
            json={"properties": {"Manually Edited": {"checkbox": True}}},
            timeout=20,
        )
        if r.ok:
            print(f"    ✓ marked  [{tag}]  {name[:55]}")
            marked += 1
        else:
            print(f"    ✗ failed {p['id']}: {r.status_code} {r.text[:120]}")
            failed += 1
    return marked, already, failed


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    requested = (NEWSLETTER or "").strip()
    if requested.lower() in ("", "all"):
        tags = ALL_NEWSLETTER_TAGS
    else:
        tags = [requested]
        if requested in ("East_Cobb_Connect", "Perimeter_Post"):
            tags.append("ECC_PP")

    mode = "DRY RUN (no writes)" if DRY_RUN else "WRITING"
    print(f"Marking Weekend Events as Manually Edited — {mode}")
    print(f"  newsletter(s): {tags}\n")

    tot_marked = tot_already = tot_failed = 0
    for tag in tags:
        m, a, f = _mark_newsletter(tag)
        tot_marked += m
        tot_already += a
        tot_failed += f

    print(f"\n✓ Done. Marked {tot_marked}, already-checked {tot_already}, "
          f"failed {tot_failed} across {len(tags)} tag(s)")
    return 1 if tot_failed else 0


if __name__ == "__main__":
    sys.exit(main())
