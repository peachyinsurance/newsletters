#!/usr/bin/env python3
"""
Quick test: fetch one Petfinder search page via Apify and dump the structure.
No Notion or Claude calls — just scrape and print.

Usage: APIFY_API_KEY=your_key python Code/test_petfinder_scrape.py
"""
import os
import sys
import json
import requests
from bs4 import BeautifulSoup

APIFY_API_KEY = os.environ.get("APIFY_API_KEY")
if not APIFY_API_KEY:
    print("Set APIFY_API_KEY env var first")
    sys.exit(1)

TEST_URL = "https://www.petfinder.com/search/dogs-for-adoption/us/ga/30062/"

print(f"Fetching: {TEST_URL}")
res = requests.post(
    "https://api.apify.com/v2/acts/apify~web-scraper/run-sync-get-dataset-items",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APIFY_API_KEY}",
    },
    json={
        "startUrls": [{"url": TEST_URL}],
        "pageFunction": """
async function pageFunction(context) {
    return {
        url: context.request.url,
        html: document.documentElement.outerHTML
    };
}
""",
        "maxConcurrency": 1,
        "maxRequestsPerCrawl": 1,
    },
    timeout=120,
)

print(f"Status: {res.status_code}")
if res.status_code not in (200, 201):
    print(f"Error: {res.text[:500]}")
    sys.exit(1)

items = res.json()
html = items[0].get("html", "")
print(f"HTML length: {len(html)} chars")

soup = BeautifulSoup(html, "html.parser")

# Check __NEXT_DATA__
next_tag = soup.find("script", id="__NEXT_DATA__")
if next_tag:
    print("\n=== __NEXT_DATA__ found ===")
    nd = json.loads(next_tag.string)
    pp = nd.get("props", {}).get("pageProps", {})
    print(f"pageProps keys: {list(pp.keys())}")
    for k, v in pp.items():
        if isinstance(v, dict):
            print(f"  [{k}] dict keys: {list(v.keys())[:15]}")
            # Go one level deeper
            for k2, v2 in v.items():
                if isinstance(v2, list) and len(v2) > 0:
                    print(f"    [{k}][{k2}] list len={len(v2)}, first item type={type(v2[0]).__name__}")
                    if isinstance(v2[0], dict):
                        print(f"      first item keys: {list(v2[0].keys())[:15]}")
                elif isinstance(v2, dict):
                    print(f"    [{k}][{k2}] dict keys: {list(v2.keys())[:10]}")
        elif isinstance(v, list):
            print(f"  [{k}] list len={len(v)}")
            if v and isinstance(v[0], dict):
                print(f"    first item keys: {list(v[0].keys())[:15]}")
        else:
            print(f"  [{k}] = {str(v)[:120]}")
else:
    print("\n=== No __NEXT_DATA__ ===")

# Check for pet links
print("\n=== Pet links in HTML ===")
all_links = soup.select("a[href]")
pet_links = [a.get("href") for a in all_links if a.get("href") and ("/cat/" in a.get("href") or "/dog/" in a.get("href"))]
print(f"Found {len(pet_links)} pet links")
for link in pet_links[:10]:
    print(f"  {link}")

# Check for common card patterns
print("\n=== Card-like elements ===")
for selector in [
    "[data-test*='pet']", "[data-test*='Pet']", "[data-test*='animal']",
    "[class*='petCard']", "[class*='PetCard']", "[class*='AnimalCard']",
    "[class*='animal']", "article", "[role='article']",
]:
    found = soup.select(selector)
    if found:
        print(f"  {selector}: {len(found)} matches")
        if found[0].get("class"):
            print(f"    first class: {found[0].get('class')}")

# Inspect the pet link elements closely
print("\n=== Pet card structure (first 3 unique) ===")
seen_hrefs = set()
count = 0
for link in soup.select("a[href]"):
    href = link.get("href", "")
    if ("/cat/" not in href and "/dog/" not in href) or "/search/" in href:
        continue
    if href in seen_hrefs:
        continue
    seen_hrefs.add(href)
    count += 1
    if count > 3:
        break
    print(f"\n--- Link {count}: {href} ---")
    # Show the link's parent structure
    parent = link.parent
    grandparent = parent.parent if parent else None
    # Show what's inside the <a> tag
    print(f"  <a> inner text: {link.get_text(strip=True)[:150]}")
    print(f"  <a> classes: {link.get('class')}")
    imgs = link.select("img")
    print(f"  <a> contains {len(imgs)} img(s)")
    for img in imgs[:1]:
        print(f"    img src: {(img.get('src') or img.get('data-src', ''))[:120]}")
        print(f"    img alt: {img.get('alt', '')}")
    # Show parent element
    if parent:
        print(f"  parent tag: <{parent.name}> class={parent.get('class')}")
        # Show siblings of the <a> tag
        for sib in parent.children:
            if sib.name and sib != link:
                print(f"  sibling: <{sib.name}> class={sib.get('class')} text={sib.get_text(strip=True)[:100]}")
    if grandparent:
        print(f"  grandparent tag: <{grandparent.name}> class={grandparent.get('class')}")
        # Check grandparent for name/breed info
        for el in grandparent.select("h2, h3, span, p, div"):
            txt = el.get_text(strip=True)
            if txt and len(txt) < 80:
                print(f"    text element: <{el.name}> class={el.get('class')} -> {txt}")
