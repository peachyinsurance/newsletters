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
import time
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import (   # noqa: E402
    NOTION_EVENTS_DB_ID,
    query_database,
    HEADERS,
)
from header_image_maker import build_header_image, build_event_body_gif  # noqa: E402

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

        # Per-event filename matches what Featured_Event.py picker writes.
        # The generic `Newsletter_Header_image_{nl}.png` is shared across
        # every event for that newsletter, so writing here would overwrite
        # any other event's composite — and since the Notion URL is set to
        # this same path, approving any other event later would surface
        # whichever event's composite was last written (the "default /
        # wrong image" symptom).
        safe_title = "".join(c if c.isalnum() else "_" for c in title)[:40] or "event"
        fname = f"Newsletter_Header_image_{nl_name}_{safe_title}.png"
        out_dir = Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / fname
        out_file.write_bytes(png_bytes)
        print(f"  ✓ Wrote composite → {out_file} ({len(png_bytes):,} bytes)")

        # 3. Compute the URL it'll live at on gh-pages (the workflow
        # publishes Beehiiv/Code/output/* to gh-pages/gifs/* right after).
        # Append a cache-bust query string so browsers + Notion don't
        # show the previous regeneration's image.
        cache_bust = int(time.time())
        header_url = f"{GH_PAGES_BASE}/{fname}?v={cache_bust}"

        # 4. Save header URL to Notion
        if not patch_notion_field(page_id, "Header Image URL", header_url):
            print(f"  ⚠ Could not save Header Image URL — continuing")
        else:
            print(f"  ✓ Header Image URL → {header_url}")

        # 5. Rebuild the Canva-style body GIF with the newly chosen image
        # as frame 1 (followed by the remaining Image Candidates).
        import json as _json
        ic_text = _extract_text(props.get("Image Candidates", {}))
        try:
            candidates = _json.loads(ic_text) if ic_text else []
        except Exception:
            candidates = []
        frame_urls = [image_url] + [u for u in candidates if u != image_url][:3]
        venue   = _extract_text(props.get("Venue", {}))
        address = _extract_text(props.get("Address", {}))
        date    = _extract_text(props.get("Date", {}))
        try:
            gif_bytes = build_event_body_gif(
                title=title, location_name=venue, address=address, date=date,
                photo_urls=frame_urls,
            )
        except Exception as e:
            print(f"  · body GIF rebuild failed: {e}")
            gif_bytes = b""
        if gif_bytes:
            safe = "".join(c if c.isalnum() else "_" for c in title)[:40] or "event"
            gif_fname = f"event_gif_{nl_name}_{safe}.gif"
            (out_dir / gif_fname).write_bytes(gif_bytes)
            gif_cache_bust = int(time.time())
            gif_url = f"{GH_PAGES_BASE}/{gif_fname}?v={gif_cache_bust}"
            patch_notion_field(page_id, "GIF URL", gif_url)
            print(f"  ✓ Body GIF → {gif_url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
