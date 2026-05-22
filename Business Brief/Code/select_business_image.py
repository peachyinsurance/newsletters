#!/usr/bin/env python3
"""Update a Business Brief row's selected photo in Notion.

Called by `select_business_image.yml` when a reviewer picks a different
photo from the Google Places gallery in the review app. Inputs from env:

  SOURCE_URL  — identifies the Business Brief Notion row
  IMAGE_URL   — the photo URL the reviewer picked
  NEWSLETTER  — newsletter scope (prevents cross-newsletter rewrites)

Business briefs don't have a Canva composite — the picked photo lands
directly in the email. So all this script does is PATCH the row's
Photo URL field. The review app rebuild trigger + Beehiiv pipeline
pick it up on their next runs.
"""
import os
import sys

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import (  # noqa: E402
    NOTION_BUSINESS_BRIEF_DB_ID,
    query_database,
    HEADERS,
)


def patch_notion_url(page_id: str, field: str, value: str) -> bool:
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"properties": {field: {"url": value}}},
        timeout=20,
    )
    if not r.ok:
        print(f"  ✗ Notion PATCH failed ({field}): HTTP {r.status_code} {r.text[:200]}")
        return False
    return True


def main() -> int:
    source_url = os.environ.get("SOURCE_URL", "").strip()
    image_url  = os.environ.get("IMAGE_URL",  "").strip()
    newsletter = os.environ.get("NEWSLETTER", "").strip()
    if not source_url or not image_url:
        print(f"✗ Missing SOURCE_URL or IMAGE_URL "
              f"(got source={bool(source_url)}, image={bool(image_url)})")
        return 1
    if not NOTION_BUSINESS_BRIEF_DB_ID:
        print("✗ NOTION_BUSINESS_BRIEF_DB_ID empty")
        return 1

    pages = query_database(NOTION_BUSINESS_BRIEF_DB_ID, filters={
        "property": "Source URL",
        "url":      {"equals": source_url},
    })
    if not pages:
        print(f"✗ No Business Brief row found with Source URL = {source_url}")
        return 1

    # Optionally narrow by newsletter (same source URL could exist for two
    # newsletters if a business was picked for both).
    if newsletter:
        pages = [p for p in pages
                 if ((p["properties"].get("Newsletter", {}).get("select") or {})
                     .get("name", "") == newsletter)] or pages

    for page in pages:
        page_id = page["id"]
        name = (page.get("properties", {}).get("Name", {}).get("title") or [{}])[0] \
                  .get("text", {}).get("content", "")
        if patch_notion_url(page_id, "Photo URL", image_url):
            print(f"  ✓ Photo URL → {image_url[:90]}  ({name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
