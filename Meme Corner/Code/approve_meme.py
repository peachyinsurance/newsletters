#!/usr/bin/env python3
"""
Approve / reject memes — Notion sync only.

Two modes (driven by env vars):

  Mode 1 — APPROVE one meme (called per-tile click in the review UI):
    APPROVED_PERMALINK = the Reddit permalink of the row to flip to
                         Status=approved.
    NEWSLETTER         = newsletter the meme belongs to (sanity check).

  Mode 2 — REJECT REMAINING (called from a "Reject the Rest" button):
    REJECT_REMAINING   = "true"
    NEWSLETTER         = which newsletter's pending rows to clear.
    APPROVED_PERMALINKS = optional comma-separated list of permalinks
                          that should STAY approved/pending and not be
                          rejected (belt-and-suspenders alongside the
                          status check).

Mode 2 flips every row for that newsletter whose Status is "pending"
to "rejected", leaving "approved" rows untouched. Use after the user
has approved their 4 picks.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_MEMES_DB_ID,
)


def _status_of(page: dict) -> str:
    sel = page.get("properties", {}).get("Status", {}).get("select") or {}
    return (sel.get("name") or "").strip()


def _permalink_of(page: dict) -> str:
    return (page.get("properties", {}).get("Reddit Permalink", {}).get("url") or "").strip()


def _newsletter_of(page: dict) -> str:
    sel = page.get("properties", {}).get("Newsletter", {}).get("select") or {}
    return (sel.get("name") or "").strip()


def approve_one(permalink: str, newsletter: str) -> int:
    """Find the row with this permalink + newsletter, flip Status to
    'approved'. Returns 0 on success, 1 on failure."""
    if not permalink:
        print("✗ APPROVED_PERMALINK is empty")
        return 1
    rows = query_database(NOTION_MEMES_DB_ID, filters={
        "and": [
            {"property": "Reddit Permalink", "url":    {"equals": permalink}},
            {"property": "Newsletter",       "select": {"equals": newsletter}},
        ]
    }) if newsletter else query_database(NOTION_MEMES_DB_ID, filters={
        "property": "Reddit Permalink", "url": {"equals": permalink},
    })
    if not rows:
        print(f"✗ No meme row found for {permalink} (newsletter={newsletter!r})")
        return 1
    page_id = rows[0]["id"]
    update_page(page_id, {
        "Status": {"select": {"name": "approved"}},
    })
    print(f"✓ Approved meme {permalink}")
    return 0


def reject_remaining(newsletter: str, keep_permalinks: list[str]) -> int:
    """Bulk-reject every Status=pending row for this newsletter that
    isn't in keep_permalinks. Returns 0 on success."""
    rows = query_database(NOTION_MEMES_DB_ID, filters={
        "property": "Newsletter", "select": {"equals": newsletter},
    }) if newsletter else query_database(NOTION_MEMES_DB_ID)
    keep = {p.strip() for p in keep_permalinks if p.strip()}
    rejected = 0
    for page in rows:
        if _status_of(page) != "pending":
            continue
        if _permalink_of(page) in keep:
            continue
        update_page(page["id"], {
            "Status": {"select": {"name": "rejected"}},
        })
        rejected += 1
    print(f"✓ Rejected {rejected} pending meme(s) for {newsletter or 'all newsletters'}")
    return 0


def main() -> int:
    if not NOTION_MEMES_DB_ID:
        print("✗ NOTION_MEMES_DB_ID is empty")
        return 1
    newsletter = os.environ.get("NEWSLETTER", "").strip()
    if (os.environ.get("REJECT_REMAINING", "").lower() in ("true", "1", "yes")):
        keep = [p.strip() for p in
                (os.environ.get("APPROVED_PERMALINKS", "") or "").split(",")]
        return reject_remaining(newsletter, keep)
    return approve_one(
        os.environ.get("APPROVED_PERMALINK", "").strip(),
        newsletter,
    )


if __name__ == "__main__":
    sys.exit(main())
