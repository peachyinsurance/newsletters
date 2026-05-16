#!/usr/bin/env python3
"""Update a Featured Event row's `Image URL` field in Notion.

Called by the `select_image.yml` GitHub workflow when a reviewer picks a
different image in the review app. Inputs come from env vars:

  SOURCE_URL    — the event's source_url (used to find the matching row)
  IMAGE_URL     — the URL to write into the row's `Image URL` field
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import (
    NOTION_EVENTS_DB_ID,
    query_database,
    HEADERS,
)
import requests


def main() -> int:
    source_url = os.environ.get("SOURCE_URL", "").strip()
    image_url  = os.environ.get("IMAGE_URL", "").strip()
    if not source_url or not image_url:
        print(f"✗ Missing SOURCE_URL or IMAGE_URL (got source={bool(source_url)}, image={bool(image_url)})")
        return 1

    # Find the matching row by Source URL
    pages = query_database(NOTION_EVENTS_DB_ID, filters={
        "property": "Source URL",
        "url":      {"equals": source_url},
    })
    if not pages:
        print(f"✗ No Notion row found with Source URL = {source_url}")
        return 1
    if len(pages) > 1:
        print(f"⚠ Multiple rows ({len(pages)}) matched Source URL — updating all")

    for page in pages:
        page_id = page["id"]
        r = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS,
            json={"properties": {"Image URL": {"url": image_url}}},
            timeout=20,
        )
        if r.ok:
            print(f"✓ Updated {page_id} → Image URL = {image_url}")
        else:
            print(f"✗ Failed to update {page_id}: HTTP {r.status_code} {r.text[:200]}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
