#!/usr/bin/env python3
"""Approve a Business Brief pick — Notion sync only.

Marks the row matching APPROVED_URL + APPROVED_NEWSLETTER as
"approved" and any other still-pending business brief rows in the
same newsletter as "rejected". The gh-pages JSON is updated
client-side by the review app before this fires.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import approve_business_brief_in_notion  # noqa: E402

APPROVED_URL        = os.environ["APPROVED_URL"]
APPROVED_NEWSLETTER = os.environ.get("APPROVED_NEWSLETTER", "").strip()

approve_business_brief_in_notion(APPROVED_URL, newsletter_hint=APPROVED_NEWSLETTER)
print("✓ Notion updated")
