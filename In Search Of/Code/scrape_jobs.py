#!/usr/bin/env python3
"""Generic job-source scraper for the In Search Of section.

Walks the per-newsletter source list in `job_sources.py`, fetches each
URL, extracts a snippet + image + page title, and upserts a row into the
In Search Of Notion DB with Status='pending' for reviewer approval.

Why generic instead of per-site scrapers: jobs sites vary wildly (ATS
platforms, Squarespace, NEOGOV, custom CMSes). A bespoke parser per site
breaks every time the site is redesigned. A generic OG-metadata scraper
captures enough for Claude to write a thin-but-honest blurb, and the
no-fabrication rule in the skill keeps it accurate. Sites that need
richer extraction (e.g. parsing the actual list of open roles) can get
their own bespoke wrappers later in `In Search Of/Code/<newsletter>/`.

Run:
    NEWSLETTER=East_Cobb_Connect python "In Search Of/Code/scrape_jobs.py"
"""
from __future__ import annotations

import os
import re
import sys
import time
from html import unescape

import requests

# Path hack: notion_save lives one level up; notion_helper is shared infra.
sys.path.append(os.path.join(os.path.dirname(__file__), "_shared"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_save import existing_source_urls, save_job  # noqa: E402
from job_sources import sources_for  # noqa: E402


NOTION_IN_SEARCH_OF_DB_ID = os.environ.get("NOTION_IN_SEARCH_OF_DB_ID", "")
NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch(url: str) -> str:
    """Fetch HTML with browser headers. Returns empty string on failure
    (logged) so the scraper keeps going to the next source."""
    cffi_get = None
    try:
        from curl_cffi import requests as _cffi
        cffi_get = lambda u: _cffi.get(u, impersonate="chrome120",
                                       timeout=20, allow_redirects=True)
    except ImportError:
        pass

    for attempt in range(3):
        try:
            if cffi_get is not None:
                r = cffi_get(url)
            else:
                r = requests.get(url, headers=_BROWSER_HEADERS,
                                 timeout=20, allow_redirects=True)
            if 200 <= r.status_code < 300:
                return r.text
            print(f"    ⚠ {url} → HTTP {r.status_code}")
            return ""
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            print(f"    ✗ fetch failed for {url}: {e}")
    return ""


# Meta-tag extractors. We use small regexes (BeautifulSoup is overkill
# when we only need 4 fields). All return "" when not found.
_META_PATTERNS = {
    "og:description": [
        re.compile(r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
        re.compile(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:description["\']', re.IGNORECASE),
    ],
    "description": [
        re.compile(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
        re.compile(r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']description["\']', re.IGNORECASE),
    ],
    "og:image": [
        re.compile(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
        re.compile(r'<meta\s+name=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
    ],
    "og:title": [
        re.compile(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
    ],
    "title": [
        re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE),
    ],
}


def _extract(html: str, key: str) -> str:
    for pat in _META_PATTERNS.get(key, []):
        m = pat.search(html)
        if m:
            return unescape(m.group(1).strip())
    return ""


def scrape_one(source: dict) -> dict | None:
    """Scrape one source. Returns a row dict ready for save_job, or None
    on hard failure (no usable content extracted)."""
    url = source["url"]
    html = _fetch(url)
    if not html:
        return None

    snippet = _extract(html, "og:description") or _extract(html, "description")
    image   = _extract(html, "og:image")
    title   = _extract(html, "og:title") or _extract(html, "title")

    # Snippet is the load-bearing field for Claude. If we got nothing
    # usable, drop with a warning — the row would be pure boilerplate.
    if not snippet and not title:
        print(f"    ⚠ no usable content extracted from {url}")
        return None

    # Fall back to title when snippet is missing entirely.
    if not snippet:
        snippet = title[:500]

    return {
        "employer":          source["employer"],
        "scraped_snippet":   snippet[:2000],
        "job_listings_url":  url,
        "image_url":         image,
        "city":              source.get("city", ""),
        "is_resource_hint":  bool(source.get("is_resource_hint")),
    }


def main() -> int:
    if not NOTION_IN_SEARCH_OF_DB_ID:
        print("✗ NOTION_IN_SEARCH_OF_DB_ID not set in env")
        return 1

    sources = sources_for(NEWSLETTER)
    if not sources:
        print(f"⚠ no job sources configured for {NEWSLETTER}; nothing to scrape")
        return 0

    print(f"In Search Of scrape — {NEWSLETTER}")
    print(f"  → Notion DB:  {NOTION_IN_SEARCH_OF_DB_ID[:8]}…")
    print(f"  → Sources:    {len(sources)}")

    # Per-newsletter URL lookup so multi-newsletter sources (e.g.
    # governmentjobs.com appears under both ECC and LLL) get separate
    # rows per newsletter.
    existing = existing_source_urls(NOTION_IN_SEARCH_OF_DB_ID, newsletter=NEWSLETTER)
    print(f"  → Existing:   {len(existing)} URLs already in DB for {NEWSLETTER}")

    saved = 0
    updated = 0
    dropped = 0
    for src in sources:
        url = src["url"]
        print(f"\n  • {src['employer']} ({url})")
        row = scrape_one(src)
        if not row:
            dropped += 1
            continue
        page_id = existing.get(url)
        ok = save_job(NOTION_IN_SEARCH_OF_DB_ID, row, NEWSLETTER,
                      page_id=page_id)
        if not ok:
            dropped += 1
            continue
        if page_id:
            updated += 1
            print(f"    ↻ updated existing row")
        else:
            saved += 1
            print(f"    ✓ saved as pending")
        time.sleep(0.5)  # be polite

    print(f"\nDone. {saved} new, {updated} updated, {dropped} dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
