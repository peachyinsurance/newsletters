#!/usr/bin/env python3
"""Save the reviewer's photo selection for a Business Brief row.

Two modes, decided by how many URLs the reviewer picked:

  1 URL  → save it as the static Photo URL (no GIF). Same behavior
           the section had before.
  2-3 URLs → build an animated GIF cycling through those photos,
             push it to gh-pages, and save the GIF URL as the row's
             Photo URL so downstream renderers automatically use it.

Inputs come from env:

  SOURCE_URL  — identifies the Business Brief Notion row
  IMAGE_URLS  — comma-separated list of 1-3 chosen photo URLs
  NEWSLETTER  — newsletter scope (prevents cross-newsletter rewrites)

The workflow's "Publish to gh-pages" step copies any new GIF in
Beehiiv/Code/output/business_brief_gif_*.gif up to gh-pages/gifs/
right after this script finishes.
"""
import base64
import hashlib
import os
import sys
import time
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import (  # noqa: E402
    NOTION_BUSINESS_BRIEF_DB_ID,
    query_database,
    HEADERS,
)
from gif_maker import create_gif_from_urls  # noqa: E402

GH_OWNER, GH_REPO = "peachyinsurance", "newsletters"
GH_PAGES_BASE = f"https://{GH_OWNER}.github.io/{GH_REPO}/gifs"
OUT_DIR       = Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"


def publish_to_gh_pages(image_bytes: bytes, path: str) -> str:
    """Commit `image_bytes` to gh-pages at `path` via the GitHub Contents API
    and return the permanent public URL (or "" on failure).

    Why not write a file + a separate 'git checkout gh-pages' workflow step:
    that dance was unreliable (branch-switch conflicts, and it raced with the
    review-app gh-pages deploy that runs right after), so the GIF URL got
    written to Notion while the file never actually landed — every business
    brief GIF 404'd. A direct Contents-API PUT is atomic and idempotent."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not image_bytes:
        print("  ✗ no GITHUB_TOKEN (or empty bytes) — cannot publish GIF")
        return ""
    api    = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{path}"
    public = f"https://{GH_OWNER}.github.io/{GH_REPO}/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    blob_sha = hashlib.sha1(b"blob %d\0" % len(image_bytes) + image_bytes).hexdigest()
    sha = None
    try:
        g = requests.get(api + "?ref=gh-pages", headers=headers, timeout=15)
        if g.status_code == 200:
            existing = g.json()
            if existing.get("sha") == blob_sha:
                return public  # already up to date
            sha = existing.get("sha")
    except Exception:
        pass
    body = {"message": f"business brief gif: {path} [skip ci]",
            "content": base64.b64encode(image_bytes).decode(), "branch": "gh-pages"}
    if sha:
        body["sha"] = sha
    try:
        p = requests.put(api, headers=headers, json=body, timeout=30)
        if p.status_code in (200, 201):
            return public
        print(f"  ✗ GIF publish failed: HTTP {p.status_code} {p.text[:160]}")
    except Exception as e:
        print(f"  ✗ GIF publish error: {e}")
    return ""


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


def _safe(s: str, n: int = 40) -> str:
    return "".join(c if c.isalnum() else "_" for c in (s or "")).strip("_")[:n] or "business"


def main() -> int:
    source_url  = os.environ.get("SOURCE_URL", "").strip()
    image_urls  = os.environ.get("IMAGE_URLS", "").strip()
    newsletter  = os.environ.get("NEWSLETTER", "").strip()
    if not source_url or not image_urls:
        print(f"✗ Missing SOURCE_URL or IMAGE_URLS "
              f"(got source={bool(source_url)}, urls={bool(image_urls)})")
        return 1
    if not NOTION_BUSINESS_BRIEF_DB_ID:
        print("✗ NOTION_BUSINESS_BRIEF_DB_ID empty")
        return 1

    urls = [u.strip() for u in image_urls.split(",") if u.strip()][:3]
    if not urls:
        print("✗ No usable image URLs after parsing")
        return 1
    print(f"  → {len(urls)} image(s) selected")

    pages = query_database(NOTION_BUSINESS_BRIEF_DB_ID, filters={
        "property": "Source URL",
        "url":      {"equals": source_url},
    })
    if not pages:
        print(f"✗ No Business Brief row found with Source URL = {source_url}")
        return 1
    if newsletter:
        pages = [p for p in pages
                 if ((p["properties"].get("Newsletter", {}).get("select") or {})
                     .get("name", "") == newsletter)] or pages

    for page in pages:
        page_id = page["id"]
        props   = page.get("properties", {}) or {}
        name    = (props.get("Business Name", {}).get("rich_text") or
                   [{}])[0].get("text", {}).get("content", "") or \
                  (props.get("Name", {}).get("title") or
                   [{}])[0].get("text", {}).get("content", "")

        if len(urls) == 1:
            # Single-photo flow: just patch Photo URL.
            if patch_notion_url(page_id, "Photo URL", urls[0]):
                print(f"  ✓ Photo URL → {urls[0][:90]}  ({name})")
            continue

        # Multi-photo flow: build animated GIF cycling through the chosen
        # photos. Hosted on gh-pages so Beehiiv + Notion can hotlink it.
        nl_name = ((props.get("Newsletter", {}).get("select") or {})
                   .get("name") or newsletter or "newsletter")
        safe    = _safe(name)
        gif_fname = f"business_brief_gif_{nl_name}_{safe}.gif"
        print(f"  Building GIF from {len(urls)} photos for {name!r}…")
        gif_bytes = create_gif_from_urls(urls, duration_ms=2200)
        if not gif_bytes:
            print(f"  ✗ GIF build returned empty bytes")
            return 1
        # Publish DIRECTLY to gh-pages so the file is guaranteed live before we
        # point Notion's Photo URL at it. Also keep a local copy for debugging.
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / gif_fname).write_bytes(gif_bytes)
        public = publish_to_gh_pages(gif_bytes, f"gifs/{gif_fname}")
        if not public:
            print(f"  ✗ Could not publish GIF for {name!r} — leaving Photo URL unchanged")
            return 1
        cache_bust = int(time.time())
        gif_url = f"{public}?v={cache_bust}"
        print(f"  ✓ GIF published ({len(gif_bytes):,} bytes) → {public}")
        if patch_notion_url(page_id, "Photo URL", gif_url):
            print(f"  ✓ Photo URL → {gif_url[:90]}…  ({name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
