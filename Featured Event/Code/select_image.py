#!/usr/bin/env python3
"""Update a Featured Event row's selected image in Notion + generate the
Canva-style header composite immediately.

Called by the `select_image.yml` GitHub workflow when a reviewer picks an
image in the review app. Inputs come from env vars:

  SOURCE_URL    — the event's source_url (identifies the Notion row)
  IMAGE_URL     — the URL the reviewer picked
  NEWSLETTER    — the newsletter name (e.g. East_Cobb_Connect) — needed
                  for the composite filename written to gh-pages

What it does:
  1. PATCH the Notion row: Image URL = <picked URL>
  2. Generate the Canva composite at
     `Beehiiv/Code/output/Newsletter_Header_image_<NEWSLETTER>.png`
     using the event's title + the newly-picked image
  3. Workflow's next step publishes that PNG to gh-pages so it's reachable
     from Beehiiv body swaps and from the review app's header preview
  4. PATCH the Notion row: Header Image URL = <gh-pages URL>
"""
import os
import sys
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import (   # noqa: E402
    NOTION_EVENTS_DB_ID,
    query_database,
    HEADERS,
)
from header_image_maker import build_header_image  # noqa: E402

GH_PAGES_BASE = "https://peachyinsurance.github.io/newsletters/gifs"


def _extract_text(prop):
    if not prop:
        return ""
    t = prop.get("type")
    if t == "rich_text":
        return "".join(i.get("text", {}).get("content", "") for i in prop.get("rich_text", []))
    if t == "title":
        return "".join(i.get("text", {}).get("content", "") for i in prop.get("title", []))
    if t == "url":
        return prop.get("url") or ""
    return ""


def patch_notion_field(page_id: str, field: str, value: str) -> bool:
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
        props   = page.get("properties", {}) or {}
        title   = _extract_text(props.get("Event Name", {})) or "Featured Event"
        nl_name = newsletter or (
            (props.get("Newsletter", {}).get("select") or {}).get("name", "")
        ) or "East_Cobb_Connect"

        # 1. Update selected image URL
        if not patch_notion_field(page_id, "Image URL", image_url):
            return 1
        print(f"  ✓ Image URL → {image_url[:90]}")

        # 2. Build the Canva composite using the picked image
        try:
            png_bytes = build_header_image(title=title, photo_url=image_url)
        except Exception as e:
            print(f"  ✗ Header composite generation failed: {e}")
            return 1
        if not png_bytes:
            print(f"  ✗ Header composite returned empty bytes")
            return 1

        out_dir = Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"Newsletter_Header_image_{nl_name}.png"
        out_file.write_bytes(png_bytes)
        print(f"  ✓ Wrote composite → {out_file} ({len(png_bytes):,} bytes)")

        # 3. Compute the URL it'll live at on gh-pages (the workflow
        # publishes Beehiiv/Code/output/* to gh-pages/gifs/* right after).
        header_url = f"{GH_PAGES_BASE}/Newsletter_Header_image_{nl_name}.png"

        # 4. Save header URL to Notion
        if not patch_notion_field(page_id, "Header Image URL", header_url):
            print(f"  ⚠ Could not save Header Image URL — continuing")
        else:
            print(f"  ✓ Header Image URL → {header_url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
