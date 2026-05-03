#!/usr/bin/env python3
"""
Newsletter Automation - Real Estate Corner
Pulls one listing per price tier (Starter, Sweet Spot, Showcase) from Realtor.com
via RapidAPI, generates blurbs via Claude, and saves to Notion.
"""
import os
import sys
import json
import time
import re
from datetime import datetime
from pathlib import Path

import requests
import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import create_page, query_database, safe_str, HEADERS as NOTION_HEADERS
from url_validator import validate_url, filter_valid_items

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY    = os.environ["CLAUDE_API_KEY"]
REALTOR_API_KEY   = os.environ["REALTOR_API_KEY"]
NOTION_API_KEY    = os.environ["NOTION_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-real-estate-skill_auto.md"

REALTOR_HOST = "realtor-search.p.rapidapi.com"

NEWSLETTERS = [
    {
        "name":     "East_Cobb_Connect",
        "location": "city:Marietta, GA",
        "display":  "East Cobb",
        "tiers": [
            {"name": "Starter",    "label": "🏠 Starter Home", "max_price": 400000, "min_price": 0,       "min_beds": 3, "min_baths": 2, "type_filter": None},
            {"name": "Sweet Spot", "label": "🏡 Sweet Spot",   "max_price": 700000, "min_price": 400000,  "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
            {"name": "Showcase",   "label": "🏰 Showcase",     "max_price": None,   "min_price": 1000000, "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
        ],
    },
    {
        "name":     "Perimeter_Post",
        "location": "city:Dunwoody, GA",
        "display":  "Perimeter",
        "tiers": [
            {"name": "Starter",    "label": "🏠 Starter Home", "max_price": 400000, "min_price": 0,       "min_beds": 3, "min_baths": 2, "type_filter": None},
            {"name": "Sweet Spot", "label": "🏡 Sweet Spot",   "max_price": 700000, "min_price": 400000,  "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
            {"name": "Showcase",   "label": "🏰 Showcase",     "max_price": None,   "min_price": 1000000, "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
        ],
    },
]

# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a local newsletter writer. Write short, neighbor-style real estate blurbs."

# ---------------------------------------------------------------------------
# 3. FETCH LISTINGS FROM REALTOR.COM API
# ---------------------------------------------------------------------------
def fetch_listings(location: str, limit: int = 20) -> list[dict]:
    """Fetch listings from Realtor.com API. Returns raw results — filter by price in Python."""
    headers = {
        "Content-Type": "application/json",
        "x-rapidapi-host": REALTOR_HOST,
        "x-rapidapi-key": REALTOR_API_KEY,
    }

    params = {
        "location": location,
        "limit": str(limit),
    }

    try:
        res = requests.get(
            f"https://{REALTOR_HOST}/properties/search-buy",
            headers=headers,
            params=params,
            timeout=30,
        )
        if res.status_code != 200:
            print(f"    API error {res.status_code}: {res.text[:200]}")
            return []

        data = res.json().get("data", {})
        results = data.get("results", [])
        print(f"  Got {len(results)} total listings from API")
        return results

    except Exception as e:
        print(f"    API error: {e}")
        return []


def has_valid_photo(listing: dict) -> bool:
    """Check if a listing has a real photo (not a placeholder)."""
    photo = listing.get("primary_photo", {}).get("href", "")
    if not photo:
        return False
    # 'l-f' in URL = placeholder/coming soon, 'l-m' = real photo
    if "l-f" in photo:
        return False
    return True


def is_available_for_sale(listing: dict) -> bool:
    """Drop pending, under contract, or coming-soon listings — we only feature
    listings a reader can actually shop today."""
    status = (listing.get("status") or "").lower()
    flags = listing.get("flags") or {}
    if status in ("pending", "contingent", "coming_soon", "under_contract", "sold"):
        return False
    if flags.get("is_pending"):
        return False
    if flags.get("is_contingent"):
        return False
    if flags.get("is_coming_soon"):
        return False
    return True


def filter_by_tier(listings: list[dict], min_price: int, max_price: int | None,
                   min_beds: int = 0, min_baths: int = 0,
                   type_filter: str | None = None) -> list[dict]:
    """Filter raw listings by price range, bed/bath minimums, property type, and valid photo."""
    filtered = []
    for r in listings:
        price = r.get("list_price", 0) or 0
        beds = r.get("description", {}).get("beds", 0) or 0
        baths = r.get("description", {}).get("baths", 0) or 0
        prop_type = r.get("description", {}).get("type", "") or ""

        if price < min_price:
            continue
        if max_price and price > max_price:
            continue
        if beds < min_beds or baths < min_baths:
            continue
        if type_filter and prop_type != type_filter:
            continue
        if not has_valid_photo(r):
            continue
        filtered.append(r)
    return filtered


def build_listing_url(raw: dict) -> str:
    """Get listing URL from API href. Returns empty string if missing."""
    href = raw.get("href", "")
    if href.startswith("https://www.realtor.com"):
        return href
    if href.startswith("/"):
        return f"https://www.realtor.com{href}"
    return ""


def parse_listing(raw: dict) -> dict:
    """Parse a raw API listing into a clean dict."""
    loc = raw.get("location", {}).get("address", {})
    desc = raw.get("description", {})
    listing_url = build_listing_url(raw)

    # Get full-size photos (Realtor's API often returns thumbnails).
    # Realtor CDN URLs end with a single-letter size code: s/m/n/l/p/x.jpg → tiny→large.
    # `od.jpg` (or `od-w####_h####_x2.webp`) is the original/full-resolution variant.
    # We rewrite any single-letter size suffix to `od.jpg` to force a hi-res fetch.
    import re as _re

    def _upgrade_photo(url):
        if not url:
            return ""
        url = url.replace("http://", "https://")
        # Realtor's CDN encodes size as a single letter immediately before `.jpg`,
        # following the asset hash digits. Examples we see in the wild:
        #     .../<hash>s.jpg   →  small  (~120x80 thumbnail)
        #     .../<hash>m.jpg   →  medium
        #     .../<hash>l.jpg   →  large
        #     .../<hash>od.jpg  →  original / full-resolution
        # We rewrite any single-letter size code to `od` for full quality.
        # Anchored with a preceding digit so we don't accidentally chop the last
        # letter of a non-Realtor URL or a hash that happens to end in s/m/l.
        url = _re.sub(r"(\d)[smnlpx]\.jpg(\?[^/]*)?$", r"\1od.jpg\2", url)
        # Also handle the parameterized form /od-w480_... → bump width ≥ 1280
        def _bump(m: _re.Match) -> str:
            cur = int(m.group(1))
            return f"w{max(cur, 1280)}"
        url = _re.sub(r"w(\d{2,4})(?=[_./])", _bump, url)
        return url

    primary_photo = _upgrade_photo(raw.get("primary_photo", {}).get("href", ""))

    # Get up to 3 valid photo URLs (scan up to 6 in case some are missing/empty).
    all_photos = []
    seen = set()
    for p in raw.get("photos", [])[:6]:
        url = _upgrade_photo(p.get("href", ""))
        if url and url not in seen:
            all_photos.append(url)
            seen.add(url)
        if len(all_photos) >= 3:
            break
    if not all_photos and primary_photo:
        all_photos = [primary_photo]

    return {
        "price":       raw.get("list_price", 0),
        "address":     f"{loc.get('line', '')} {loc.get('city', '')} {loc.get('state_code', '')} {loc.get('postal_code', '')}".strip(),
        "city":        loc.get("city", ""),
        "zip":         loc.get("postal_code", ""),
        "beds":        desc.get("beds", 0),
        "baths":       desc.get("baths", 0),
        "sqft":        desc.get("sqft") or 0,
        "type":        (desc.get("type") or "").replace("_", " ").title(),
        "lot_sqft":    desc.get("lot_sqft") or 0,
        "year_built":  desc.get("year_built") or "",
        "photo_url":   primary_photo,
        "photos":      all_photos,
        "listing_url": listing_url,
        "list_date":   raw.get("list_date", ""),
        "property_id": raw.get("property_id", ""),
    }


def pick_best_listing(listings: list[dict], target_price: int = 0,
                      min_beds: int = 0, min_baths: int = 0) -> dict | None:
    """Pick the listing closest to the target price (midpoint of range).
    Prefers listings with photos and complete data as tiebreakers."""
    if not listings:
        return None
    parsed = [parse_listing(r) for r in listings]
    # Filter by bed/bath minimums
    if min_beds or min_baths:
        parsed = [p for p in parsed if (p["beds"] or 0) >= min_beds and (p["baths"] or 0) >= min_baths]
    if not parsed:
        return None
    # Score: closeness to target price + data completeness
    scored = []
    for p in parsed:
        score = 0
        if p["photo_url"]:
            score += 3
        if p["sqft"]:
            score += 2
        if p["beds"] and p["baths"]:
            score += 2
        if p["year_built"]:
            score += 1
        # Distance from target price (lower = better), normalized to 0-5 range
        if target_price and p["price"]:
            distance = abs(p["price"] - target_price) / max(target_price, 1)
            price_score = max(0, 5 - (distance * 10))  # 0 distance = 5 points, 50% off = 0
            score += price_score
        scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


# ---------------------------------------------------------------------------
# 4. GENERATE BLURBS VIA CLAUDE
# ---------------------------------------------------------------------------
def generate_blurbs(listings: list[dict], skill_prompt: str, newsletter_display: str) -> list[dict]:
    """Generate blurbs for the three tier listings."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    listings_text = ""
    for listing in listings:
        listings_text += f"""
--- {listing['tier']} ---
Price: ${listing['price']:,}
Address: {listing['address']}
Beds: {listing['beds']} | Baths: {listing['baths']} | Sqft: {listing['sqft']:,} | Type: {listing['type']}
Year Built: {listing['year_built']}
Listing URL: {listing['listing_url']}
Photo: {listing['photo_url']}

"""

    listing_count = len(listings)
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=3000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""
Write a Real Estate Corner section for the {newsletter_display} area newsletter.
There are {listing_count} listing(s) below. Write a short, neighbor-style blurb for each.

Return ONLY a JSON array with exactly {listing_count} object(s), no preamble or markdown.
Exact format:
[
  {{
    "tier": "Starter",
    "headline": "Short catchy headline",
    "blurb": "2-3 sentence blurb about the listing",
    "price": 350000,
    "address": "123 Main St Marietta GA 30062",
    "beds": 3,
    "baths": 2,
    "sqft": 1500,
    "photo_url": "https://...",
    "listing_url": "https://..."
  }}
]

Listings:
{listings_text}
"""
                }]
            )
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Claude API error (attempt {attempt + 1}): {e}")
                time.sleep(10 * (attempt + 1))
            else:
                raise

    # Extract Claude's text. Be defensive: response may have no text blocks at all.
    raw = ""
    if response and response.content:
        for block in response.content:
            if getattr(block, "type", "") == "text":
                raw = block.text
                break

    if not raw.strip():
        print("  ⚠ Claude returned no text; skipping blurb generation for this batch")
        print(f"    Response stop_reason: {getattr(response, 'stop_reason', 'unknown')}")
        return []

    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    # Tolerant JSON extraction: pull the first JSON array out of the response,
    # whether or not Claude wrapped it in prose or fences.
    try:
        results = json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            print("  ⚠ Could not parse Claude's response as JSON. Raw output (first 500 chars):")
            print(f"    {raw[:500]}")
            return []
        try:
            results = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            print(f"  ⚠ JSON salvage failed: {e}. Raw (first 500 chars):")
            print(f"    {raw[:500]}")
            return []

    if not isinstance(results, list):
        print(f"  ⚠ Claude returned non-array result: {type(results).__name__}. Skipping.")
        return []

    print(f"  Generated {len(results)} real estate blurbs (expected {listing_count})")
    return results


# ---------------------------------------------------------------------------
# 5. SAVE TO NOTION
# ---------------------------------------------------------------------------
NOTION_RE_DB_ID = os.environ.get("NOTION_RE_DB_ID", "")


def get_used_listing_urls(newsletter_name: str) -> set:
    """Get listing URLs already used for this newsletter (to prevent repeats)."""
    if not NOTION_RE_DB_ID:
        return set()
    try:
        pages = query_database(NOTION_RE_DB_ID)
        urls = set()
        for page in pages:
            nl = (page["properties"].get("Newsletter", {}).get("select") or {}).get("name", "")
            if nl != newsletter_name:
                continue
            url = page["properties"].get("Listing URL", {}).get("url", "")
            if url:
                urls.add(url)
        print(f"  Loaded {len(urls)} previously used listing URLs to exclude")
        return urls
    except Exception:
        return set()


def cleanup_old_re_listings() -> None:
    """Delete real estate entries older than 8 weeks."""
    if not NOTION_RE_DB_ID:
        return
    from notion_helper import archive_page
    from datetime import timedelta
    cutoff = (datetime.today() - timedelta(weeks=8)).strftime("%Y-%m-%d")
    try:
        pages = query_database(NOTION_RE_DB_ID, filters={
            "property": "Date Generated",
            "date": {"before": cutoff}
        })
    except Exception:
        pages = []
    count = 0
    for page in pages:
        name = page["properties"].get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        archive_page(page["id"])
        print(f"  Archived: {name}")
        count += 1
    if count:
        print(f"  Archived {count} real estate listings older than 8 weeks")


def save_real_estate_to_notion(results: list[dict], newsletter_name: str) -> None:
    """Save real estate listings to Notion database. Replaces existing entries for this newsletter.
    Manually edited rows (Manually Edited = True) are preserved and skipped."""
    if not NOTION_RE_DB_ID:
        print("  No NOTION_RE_DB_ID set, skipping Notion save")
        return

    # Find existing entries. Flip previous auto-generated "approved" rows to "approved - old"
    # so they stay in the database for exclusion (no repeat listings) but clear the slot for new picks.
    # Manually edited rows are preserved with their current status.
    protected_tiers = set()
    try:
        existing = query_database(NOTION_RE_DB_ID)
        existing = [p for p in existing if
                    (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
        flipped = 0
        for page in existing:
            props = page["properties"]
            is_edited = props.get("Manually Edited", {}).get("checkbox", False)
            tier = (props.get("Tier", {}).get("select") or {}).get("name", "")
            status = (props.get("Status", {}).get("select") or {}).get("name", "")
            if is_edited:
                protected_tiers.add(tier)
                print(f"  🔒 Preserving manually edited {tier} listing")
            elif status == "approved":
                # Flip to "approved - old" so it stays for exclusion but is no longer current
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page['id']}",
                    headers=NOTION_HEADERS,
                    json={"properties": {"Status": {"select": {"name": "approved - old"}}}},
                    timeout=30,
                )
                flipped += 1
        if flipped:
            print(f"  Flipped {flipped} previous RE entries to 'approved - old' for {newsletter_name}")
    except Exception as e:
        print(f"  Warning: could not process existing RE entries: {e}")

    for listing in results:
        tier = listing.get("tier", "")
        if tier in protected_tiers:
            print(f"  ⏭ Skipping {tier} (manually edited row kept)")
            continue
        properties = {
            "Name":           {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {tier} - {listing.get('address', '')}"}}]},
            "Tier":           {"select": {"name": tier}},
            "Price":          {"number": listing.get("price", 0)},
            "Address":        {"rich_text": [{"text": {"content": safe_str(listing.get("address", ""))}}]},
            "Beds":           {"number": listing.get("beds", 0)},
            "Baths":          {"number": listing.get("baths", 0)},
            "Sqft":           {"number": listing.get("sqft", 0)},
            "Headline":       {"rich_text": [{"text": {"content": safe_str(listing.get("headline", ""))}}]},
            "Blurb":          {"rich_text": [{"text": {"content": safe_str(listing.get("blurb", ""))[:2000]}}]},
            "Photo URL":      {"url": listing.get("photo_url") or None},
            "GIF URL":        {"url": listing.get("gif_url") or None},
            "Template Image": {"url": listing.get("template_image_url") or None},
            "Listing URL":    {"url": listing.get("listing_url") or None},
            "Newsletter":     {"select": {"name": newsletter_name}},
            "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":         {"select": {"name": "approved"}},
            "Manually Edited": {"checkbox": False},
        }
        create_page(NOTION_RE_DB_ID, properties)
        print(f"  ✓ Saved: {tier} - {listing.get('address')}")


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Real Estate Corner — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    # Cleanup old listings first
    print("\nCleaning up old listings...")
    cleanup_old_re_listings()

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display']})")
        print(f"{'='*60}")

        # Load previously used listings to exclude
        excluded_urls = get_used_listing_urls(newsletter["name"])

        # Fetch all listings once, then filter per tier
        all_listings = fetch_listings(location=newsletter["location"], limit=20)
        if not all_listings:
            print(f"  No listings found for {newsletter['name']}. Skipping.")
            continue

        # Remove previously featured listings
        before_count = len(all_listings)
        all_listings = [r for r in all_listings if
                        r.get("href", "") not in excluded_urls and
                        f"https://www.realtor.com{r.get('href', '')}" not in excluded_urls]
        if len(all_listings) < before_count:
            print(f"  Excluded {before_count - len(all_listings)} previously featured listings")

        # Exclude listings that aren't actually for sale today (pending, contingent, coming soon, etc.)
        before_count = len(all_listings)
        all_listings = [r for r in all_listings if is_available_for_sale(r)]
        if len(all_listings) < before_count:
            print(f"  Excluded {before_count - len(all_listings)} pending / coming-soon listings")

        # Validate listing URLs before tier selection (saves Claude costs on dead listings)
        print(f"\n  Validating {len(all_listings)} listing URLs...")
        valid_listings = []
        for r in all_listings:
            url = build_listing_url(r)
            if not url or validate_url(url):
                valid_listings.append(r)
            else:
                loc = r.get("location", {}).get("address", {})
                addr = f"{loc.get('line', '')} {loc.get('city', '')}".strip()
                print(f"    ✗ Dead listing URL: {addr} ({url[:80]})")
        if len(valid_listings) < len(all_listings):
            print(f"  Dropped {len(all_listings) - len(valid_listings)} listings with dead URLs")
        all_listings = valid_listings
        if not all_listings:
            print(f"  No listings with valid URLs for {newsletter['name']}. Skipping.")
            continue

        tiers = newsletter["tiers"]
        tier_listings = []
        used_ids = set()

        # Adaptive tier ranges
        STEP = 100000
        RANGE_WIDTH = 300000

        starter_cfg = tiers[0]
        sweet_cfg   = tiers[1]
        showcase_cfg = tiers[2]

        starter_max = starter_cfg["max_price"]
        sweet_min   = sweet_cfg["min_price"]
        sweet_max   = sweet_cfg["max_price"]
        showcase_min = showcase_cfg["min_price"]

        # --- STARTER: expand upward until we find a hit ---
        starter_result = None
        for attempt in range(6):
            cur_max = starter_max + (attempt * STEP)
            tier_filtered = filter_by_tier(
                all_listings, starter_cfg["min_price"], cur_max,
                min_beds=starter_cfg["min_beds"], min_baths=starter_cfg["min_baths"],
                type_filter=starter_cfg.get("type_filter"),
            )
            tier_filtered = [r for r in tier_filtered if r.get("property_id") not in used_ids]
            target = cur_max // 2
            starter_result = pick_best_listing(tier_filtered, target_price=target,
                                              min_beds=starter_cfg["min_beds"], min_baths=starter_cfg["min_baths"])
            if starter_result:
                if attempt > 0:
                    print(f"\n  🏠 Starter Home (expanded to <${cur_max//1000}k)")
                else:
                    print(f"\n  🏠 Starter Home (<${starter_max//1000}k)")
                ptype = starter_cfg.get('type_filter') or 'all types'
                print(f"    Filter: {ptype} | {starter_cfg['min_beds']}+bd/{starter_cfg['min_baths']}+ba")
                print(f"    ✓ ${starter_result['price']:,} | {starter_result['address']} | {starter_result['beds']}bd/{starter_result['baths']}ba")
                starter_result["tier"] = "Starter"
                starter_result["tier_label"] = "🏠 Starter Home"
                tier_listings.append(starter_result)
                used_ids.add(starter_result["property_id"])
                delta = attempt * STEP
                sweet_min = sweet_min + delta
                sweet_max = sweet_min + RANGE_WIDTH
                break
        if not starter_result:
            print(f"\n  🏠 Starter Home — no listings found")

        # --- SWEET SPOT ---
        sweet_target = (sweet_min + sweet_max) // 2
        ptype = sweet_cfg.get('type_filter') or 'all types'
        print(f"\n  🏡 Sweet Spot (${sweet_min//1000}k-${sweet_max//1000}k, target ~${sweet_target//1000}k)")
        print(f"    Filter: {ptype}")
        tier_filtered = filter_by_tier(
            all_listings, sweet_min, sweet_max,
            min_beds=sweet_cfg["min_beds"], min_baths=sweet_cfg["min_baths"],
            type_filter=sweet_cfg.get("type_filter"),
        )
        tier_filtered = [r for r in tier_filtered if r.get("property_id") not in used_ids]
        print(f"    {len(tier_filtered)} listings in range")
        sweet_result = pick_best_listing(tier_filtered, target_price=sweet_target)
        if sweet_result:
            sweet_result["tier"] = "Sweet Spot"
            sweet_result["tier_label"] = "🏡 Sweet Spot"
            tier_listings.append(sweet_result)
            used_ids.add(sweet_result["property_id"])
            print(f"    ✓ ${sweet_result['price']:,} | {sweet_result['address']} | {sweet_result['beds']}bd/{sweet_result['baths']}ba")
        else:
            print(f"    ✗ No listings in Sweet Spot range")

        # --- SHOWCASE: shrink down until we find a hit ---
        showcase_result = None
        ptype = showcase_cfg.get('type_filter') or 'all types'
        for attempt in range(4):
            cur_min = showcase_min - (attempt * STEP)
            if cur_min < sweet_max:
                break
            tier_filtered = filter_by_tier(
                all_listings, cur_min, None,
                min_beds=showcase_cfg["min_beds"], min_baths=showcase_cfg["min_baths"],
                type_filter=showcase_cfg.get("type_filter"),
            )
            tier_filtered = [r for r in tier_filtered if r.get("property_id") not in used_ids]
            showcase_result = pick_best_listing(tier_filtered, target_price=cur_min)
            if showcase_result:
                if attempt > 0:
                    print(f"\n  🏰 Showcase (adjusted to ${cur_min//1000}k+)")
                else:
                    print(f"\n  🏰 Showcase ($1M+)")
                print(f"    Filter: {ptype}")
                print(f"    ✓ ${showcase_result['price']:,} | {showcase_result['address']} | {showcase_result['beds']}bd/{showcase_result['baths']}ba")
                showcase_result["tier"] = "Showcase"
                showcase_result["tier_label"] = "🏰 Showcase"
                tier_listings.append(showcase_result)
                used_ids.add(showcase_result["property_id"])
                break
        if not showcase_result:
            print(f"\n  🏰 Showcase — no listings found")

        if not tier_listings:
            print(f"  No listings found for {newsletter['name']}. Skipping.")
            continue

        # Generate blurbs
        print(f"\n  Generating blurbs for {len(tier_listings)} listings...")
        results = generate_blurbs(tier_listings, skill_prompt, newsletter["display"])

        # Merge original listing data into Claude's results (source of truth for URLs/photos).
        # Claude only provides text: headline, blurb. Everything else is overwritten from source.
        tier_data_map = {l["tier"]: l for l in tier_listings}
        validated_results = []
        for r in results:
            tier = r.get("tier", "")
            original = tier_data_map.get(tier)
            if not original:
                print(f"  ✗ Rejecting result with unknown tier: '{tier}'")
                continue
            print(f"  Merge {tier}: Claude addr='{r.get('address', '')[:40]}' | Original addr='{original.get('address', '')[:40]}'")
            r["photo_url"]   = original.get("photo_url", "")
            r["listing_url"] = original.get("listing_url", "")
            r["address"]     = original.get("address", "")
            r["price"]       = original.get("price", 0)
            r["beds"]        = original.get("beds", 0)
            r["baths"]       = original.get("baths", 0)
            r["sqft"]        = original.get("sqft", 0)
            validated_results.append(r)
        results = validated_results

        # Generate template images (animated GIF with border overlay)
        sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        print(f"\n  Creating listing images...")
        try:
            from re_image_maker import generate_re_images
            image_results = generate_re_images(tier_listings, newsletter["name"], str(output_dir))

            # Build image URL map and merge into results
            for img_result in image_results:
                tier = img_result["tier"]
                img_filename = img_result["image_filename"]
                cache_bust = int(datetime.today().timestamp())
                img_url = f"https://peachyinsurance.github.io/newsletters/gifs/{img_filename}?v={cache_bust}"
                for r in results:
                    if r.get("tier") == tier:
                        r["template_image_url"] = img_url
                        break
                # Also store on tier_listings for gh-pages publish
                for listing in tier_listings:
                    if listing.get("tier") == tier:
                        listing["image_filename"] = img_filename
                        break
        except Exception as e:
            print(f"  ✗ Image generation failed: {e}")

        # Save to Notion
        save_real_estate_to_notion(results, newsletter["name"])

    print(f"\nAll newsletters complete.")
