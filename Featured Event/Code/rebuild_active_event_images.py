#!/usr/bin/env python3
"""
Rebuild the body GIF + header PNG for every still-active Featured
Event row against the CURRENT templates. Useful after a template
tweak (color, layout, font) when you don't want to re-pick events
just to refresh the rendered images.

Walks the Featured Event Notion DB, skips rejected / approved-old
rows, and for each remaining row:
  1. Reads Event Name, Venue, Address, Date, Image URL, and the
     JSON-encoded Image Candidates field (alternate frames).
  2. Calls build_event_body_gif() against feature_event_body_template2.png.
  3. Calls build_header_image() against feature_event_image.png.
  4. Writes the GIF + PNG to Beehiiv/Code/output/.
  5. PATCHes the Notion row with fresh cache-busted gh-pages URLs
     so Beehiiv/Notion don't serve the cached old image.

The workflow's "Publish to gh-pages" step picks up everything in
Beehiiv/Code/output/ and pushes it to gh-pages/gifs/.

Env vars consumed:
  NOTION_API_KEY
  NOTION_EVENTS_DB_ID
  NEWSLETTER       (optional — defaults to 'all'; East_Cobb_Connect / Perimeter_Post / Lewisville_Lake_Lookout / all)
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent
                    / "NewsletterCreation" / "Code"))
from header_image_maker import build_event_body_gif, build_header_image  # noqa: E402
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_EVENTS_DB_ID,
)

NEWSLETTER = os.environ.get("NEWSLETTER", "all").strip() or "all"
GH_PAGES_BASE = "https://peachyinsurance.github.io/newsletters/gifs"
OUT_DIR = Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"

EXCLUDED_STATUSES = {"rejected", "approved - old"}


def _rt(prop: dict) -> str:
    rt = (prop or {}).get("rich_text") or []
    return "".join(c.get("text", {}).get("content", "") for c in rt)


def _title(prop: dict) -> str:
    t = (prop or {}).get("title") or []
    return "".join(c.get("text", {}).get("content", "") for c in t)


def _safe(s: str, n: int = 40) -> str:
    return "".join(c if c.isalnum() else "_" for c in (s or "")).strip("_")[:n] or "event"


def rebuild_one_row(page: dict) -> tuple[int, int]:
    """Rebuild body GIF + header PNG for one Featured Event row. Returns
    (gif_built, header_built) as 0/1 each so the caller can tally."""
    props = page.get("properties", {}) or {}
    event_name = _rt(props.get("Event Name")) or _title(props.get("Name"))
    if not event_name:
        return 0, 0

    nl_name = ((props.get("Newsletter", {}).get("select") or {}).get("name") or "").strip()
    if not nl_name:
        return 0, 0

    image_url = (props.get("Image URL", {}).get("url") or "").strip()
    venue     = _rt(props.get("Venue"))
    address   = _rt(props.get("Address"))
    date      = _rt(props.get("Date"))

    # Image Candidates is a JSON-encoded rich_text field (the picker
    # writes a list of URL strings). Parse it for the alternate frames.
    ic_text = _rt(props.get("Image Candidates"))
    try:
        candidates = json.loads(ic_text) if ic_text else []
        if not isinstance(candidates, list):
            candidates = []
    except Exception:
        candidates = []

    frame_urls: list[str] = []
    if image_url:
        frame_urls.append(image_url)
    for u in candidates:
        if isinstance(u, str) and u and u not in frame_urls:
            frame_urls.append(u)
        if len(frame_urls) >= 4:
            break

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = _safe(event_name)
    page_id = page.get("id")
    bust = int(time.time())

    gif_built = 0
    if frame_urls:
        try:
            gif_bytes = build_event_body_gif(
                title         = event_name,
                location_name = venue,
                address       = address,
                date          = date,
                photo_urls    = frame_urls,
            )
        except Exception as e:
            print(f"    ✗ body GIF build failed: {e}")
            gif_bytes = b""
        if gif_bytes:
            gif_fname = f"event_gif_{nl_name}_{safe_title}.gif"
            (OUT_DIR / gif_fname).write_bytes(gif_bytes)
            gif_url = f"{GH_PAGES_BASE}/{gif_fname}?v={bust}"
            update_page(page_id, {"GIF URL": {"url": gif_url}})
            print(f"    ✓ body GIF → {gif_fname} ({len(frame_urls)} frames, "
                  f"{len(gif_bytes):,} bytes)")
            gif_built = 1
    else:
        print(f"    · no photo URLs on this row — skipping body GIF")

    header_built = 0
    if image_url:
        try:
            png_bytes = build_header_image(title=event_name, photo_url=image_url)
        except Exception as e:
            print(f"    ✗ header PNG build failed: {e}")
            png_bytes = b""
        if png_bytes:
            png_fname = f"Newsletter_Header_image_{nl_name}_{safe_title}.png"
            (OUT_DIR / png_fname).write_bytes(png_bytes)
            header_url = f"{GH_PAGES_BASE}/{png_fname}?v={bust}"
            update_page(page_id, {"Header Image URL": {"url": header_url}})
            print(f"    ✓ header PNG → {png_fname} ({len(png_bytes):,} bytes)")
            header_built = 1
    return gif_built, header_built


def run(newsletter_name: str | None) -> int:
    if not NOTION_EVENTS_DB_ID:
        print("✗ NOTION_EVENTS_DB_ID is empty — nothing to do")
        return 1
    filters = None
    if newsletter_name and newsletter_name != "all":
        filters = {"property": "Newsletter", "select": {"equals": newsletter_name}}
    rows = query_database(NOTION_EVENTS_DB_ID, filters=filters) or []
    # Drop rejected / archived
    rows = [r for r in rows
            if ((r["properties"].get("Status", {}).get("select") or {}).get("name") or "").strip().lower()
            not in EXCLUDED_STATUSES]
    print(f"  {len(rows)} active Featured Event row(s) to rebuild")

    gifs = headers = 0
    for r in rows:
        name = _rt(r["properties"].get("Event Name")) or _title(r["properties"].get("Name"))
        nl   = ((r["properties"].get("Newsletter", {}).get("select") or {}).get("name") or "?")
        print(f"\n  → {nl}: {name[:60]}")
        g, h = rebuild_one_row(r)
        gifs += g
        headers += h
    print(f"\n  ✓ Rebuilt {gifs} body GIF(s) and {headers} header PNG(s)")
    return 0


def main() -> int:
    if NEWSLETTER.lower() == "all":
        targets: list[str | None] = [None]   # one query, all newsletters
    else:
        targets = [NEWSLETTER]
    rc = 0
    for nl in targets:
        rc = run(nl) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
