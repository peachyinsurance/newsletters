#!/usr/bin/env python3
"""Approve / reject In Search Of rows — Notion sync only.

In Search Of is a multi-select section (like Meme Corner): the reviewer
approves the listings to feature, then clears the rest. Two modes, driven
by env vars:

  Mode 1 — APPROVE one listing (per-tile click in the review UI):
    JOB_LISTINGS_URL = the posting/careers URL of the row to flip to
                       Status=approved.
    NEWSLETTER       = newsletter the row belongs to (scoping).

  Mode 2 — REJECT REMAINING (the "Reject the rest" button):
    REJECT_REMAINING = "true"
    NEWSLETTER       = which newsletter's pending rows to clear.
    APPROVED_URLS    = comma-separated Job Listings URLs to KEEP (don't
                       reject), belt-and-suspenders alongside the status
                       check.

  Mode 3 — SET SELECTION (the "Submit selection" button):
    SET_SELECTION    = "true"
    NEWSLETTER       = which newsletter's rows to set.
    APPROVED_URLS    = comma-separated Job Listings URLs that should be
                       Status=approved. Every listed URL → approved; any row
                       that is currently approved but NOT in the list → back
                       to pending (so deselecting a listing returns it to the
                       candidate pool). Pending/rejected rows are left as-is.
                       This makes Submit the single source of truth, so the
                       reviewer can freely toggle picks and re-submit.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_IN_SEARCH_OF_DB_ID,
)


def _url_of(page: dict) -> str:
    return (page.get("properties", {}).get("Job Listings URL", {}).get("url") or "").strip()


def _status_of(page: dict) -> str:
    sel = page.get("properties", {}).get("Status", {}).get("select") or {}
    return (sel.get("name") or "").strip()


def _newsletter_of(page: dict) -> str:
    sel = page.get("properties", {}).get("Newsletter", {}).get("select") or {}
    return (sel.get("name") or "").strip()


def approve_one(url: str, newsletter: str) -> int:
    if not url:
        print("✗ JOB_LISTINGS_URL is empty")
        return 1
    filters = {"and": [
        {"property": "Job Listings URL", "url":    {"equals": url}},
        {"property": "Newsletter",       "select": {"equals": newsletter}},
    ]} if newsletter else {"property": "Job Listings URL", "url": {"equals": url}}
    rows = query_database(NOTION_IN_SEARCH_OF_DB_ID, filters=filters)
    if not rows:
        print(f"✗ No In Search Of row found for {url} (newsletter={newsletter!r})")
        return 1
    update_page(rows[0]["id"], {"Status": {"select": {"name": "approved"}}})
    print(f"✓ Approved In Search Of listing {url}")
    return 0


def reject_remaining(newsletter: str, keep_urls: list[str]) -> int:
    rows = query_database(NOTION_IN_SEARCH_OF_DB_ID, filters={
        "property": "Newsletter", "select": {"equals": newsletter},
    }) if newsletter else query_database(NOTION_IN_SEARCH_OF_DB_ID)
    keep = {u.strip() for u in keep_urls if u.strip()}
    rejected = 0
    for p in rows:
        if _status_of(p) != "pending":
            continue
        if newsletter and _newsletter_of(p) != newsletter:
            continue
        if _url_of(p) in keep:
            continue
        update_page(p["id"], {"Status": {"select": {"name": "rejected"}}})
        rejected += 1
    print(f"✓ Rejected {rejected} remaining pending row(s)")
    return 0


def set_selection(newsletter: str, approved_urls: list[str]) -> int:
    """Make `approved_urls` the EXACT set of approved rows for this newsletter:
    listed URLs → approved; previously-approved rows not in the list → pending.
    Pending/rejected rows are untouched."""
    rows = query_database(NOTION_IN_SEARCH_OF_DB_ID, filters={
        "property": "Newsletter", "select": {"equals": newsletter},
    }) if newsletter else query_database(NOTION_IN_SEARCH_OF_DB_ID)
    keep = {u.strip() for u in approved_urls if u.strip()}
    approved = reset = 0
    for p in rows:
        if newsletter and _newsletter_of(p) != newsletter:
            continue
        url, cur = _url_of(p), _status_of(p)
        if url in keep:
            if cur != "approved":
                update_page(p["id"], {"Status": {"select": {"name": "approved"}}})
                approved += 1
        elif cur == "approved":
            # Deselected since last submit — return it to the candidate pool.
            update_page(p["id"], {"Status": {"select": {"name": "pending"}}})
            reset += 1
    print(f"✓ Selection set: {len(keep)} approved "
          f"({approved} newly approved, {reset} returned to pending)")
    return 0


if __name__ == "__main__":
    if not NOTION_IN_SEARCH_OF_DB_ID:
        print("✗ NOTION_IN_SEARCH_OF_DB_ID not set in env")
        sys.exit(1)
    nl = os.environ.get("NEWSLETTER", "").strip()
    if os.environ.get("SET_SELECTION", "").strip().lower() == "true":
        urls = os.environ.get("APPROVED_URLS", "").split(",")
        sys.exit(set_selection(nl, urls))
    if os.environ.get("REJECT_REMAINING", "").strip().lower() == "true":
        keep = os.environ.get("APPROVED_URLS", "").split(",")
        sys.exit(reject_remaining(nl, keep))
    sys.exit(approve_one(os.environ.get("JOB_LISTINGS_URL", "").strip(), nl))
