#!/usr/bin/env python3
"""
One-shot cleanup: walk the Pets DB and archive duplicate rows that share
the same Source URL for a newsletter. Keeps the row with the highest
"priority" status (approved > approved-old > pending) and most recent
Date Generated; archives the rest as `approved - old` (non-destructive,
recoverable by filtering on that status).

Run once after fixing the pet-save-time dedup bug:
    NEWSLETTER=East_Cobb_Connect python NewsletterCreation/Code/dedupe_pets.py

Or NEWSLETTER=all to sweep every newsletter.
"""
import os
import sys
from collections import defaultdict

sys.path.append(os.path.dirname(__file__))
from notion_helper import query_database, update_page, NOTION_PETS_DB_ID

NEWSLETTER = os.environ.get("NEWSLETTER", "all")

# Higher rank = higher priority to keep
STATUS_RANK = {"approved": 3, "approved - old": 2, "pending": 1, "rejected": 0, "": 0}


def _row_status(p): return ((p["properties"].get("Status") or {}).get("select") or {}).get("name", "")
def _row_url(p):    return p["properties"].get("Source URL", {}).get("url", "") or ""
def _row_date(p):   return ((p["properties"].get("Date Generated") or {}).get("date") or {}).get("start", "") or ""
def _row_nl(p):     return ((p["properties"].get("Newsletter") or {}).get("select") or {}).get("name", "")


def main() -> None:
    pages = query_database(NOTION_PETS_DB_ID)
    if NEWSLETTER != "all":
        pages = [p for p in pages if _row_nl(p) == NEWSLETTER]
    print(f"Scanning {len(pages)} pet rows for duplicates (newsletter={NEWSLETTER})…")

    by_key = defaultdict(list)
    for p in pages:
        url = _row_url(p)
        if not url:
            continue
        key = (_row_nl(p), url.rstrip("/").removesuffix("/details"))
        by_key[key].append(p)

    archived = 0
    dup_groups = 0
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        dup_groups += 1
        # Sort: higher status rank first, then more recent date_generated
        group.sort(
            key=lambda p: (STATUS_RANK.get(_row_status(p), 0), _row_date(p)),
            reverse=True,
        )
        keeper, dupes = group[0], group[1:]
        print(f"\n  [{key[0]}] {key[1][:60]}")
        print(f"    KEEP: {_row_status(keeper) or 'no status'} / {_row_date(keeper)} ({keeper['id'][:8]}…)")
        for d in dupes:
            print(f"    ARCH: {_row_status(d) or 'no status'} / {_row_date(d)} ({d['id'][:8]}…)")
            try:
                update_page(d["id"], {"Status": {"select": {"name": "approved - old"}}})
                archived += 1
            except Exception as e:
                print(f"      ✗ failed: {e}")

    print(f"\nDONE — {dup_groups} duplicate groups, archived {archived} extra rows")


if __name__ == "__main__":
    main()
