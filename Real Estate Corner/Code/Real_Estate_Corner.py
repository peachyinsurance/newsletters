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
from datetime import datetime
from pathlib import Path

import requests
import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import create_page, query_database, safe_str, HEADERS as NOTION_HEADERS

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


def parse_listing(raw: dict) -> dict:
    """Parse a raw API listing into a clean dict."""
    loc = raw.get("location", {}).get("address", {})
    desc = raw.get("description", {})
    href = raw.get("href", "")
    # Fix double-prefixed URLs
    if href.startswith("https://www.realtor.com"):
        listing_url = href
    elif href.startswith("/"):
        listing_url = f"https://www.realtor.com{href}"
    else:
        listing_url = href

    # Get full-size photo (API returns small thumbnails ending in 's.jpg')
    photo = raw.get("primary_photo", {}).get("href", "")
    if photo and "l-m" in photo:
        import re
        photo = re.sub(r's\.jpg$', 'od.jpg', photo)
        photo = photo.replace("http://", "https://")
    elif photo:
        photo = photo.replace("http://", "https://")

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
        "photo_url":   photo,
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

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=3000,
        system=skill_prompt,
        messages=[{
            "role": "user",
            "content": f"""
Write a Real Estate Corner section for the {newsletter_display} area newsletter.
There are 3 listings, one per price tier. Write a short, neighbor-style blurb for each.

Return ONLY a JSON array with exactly 3 objects, no preamble or markdown.
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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    results = json.loads(clean)
    print(f"  Generated {len(results)} real estate blurbs")
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
    """Save real estate listings to Notion database."""
    if not NOTION_RE_DB_ID:
        print("  No NOTION_RE_DB_ID set, skipping Notion save")
        return

    for listing in results:
        properties = {
            "Name":           {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {listing.get('tier', '')} - {listing.get('address', '')}"}}]},
            "Tier":           {"select": {"name": listing.get("tier", "")}},
            "Price":          {"number": listing.get("price", 0)},
            "Address":        {"rich_text": [{"text": {"content": safe_str(listing.get("address", ""))}}]},
            "Beds":           {"number": listing.get("beds", 0)},
            "Baths":          {"number": listing.get("baths", 0)},
            "Sqft":           {"number": listing.get("sqft", 0)},
            "Headline":       {"rich_text": [{"text": {"content": safe_str(listing.get("headline", ""))}}]},
            "Blurb":          {"rich_text": [{"text": {"content": safe_str(listing.get("blurb", ""))[:2000]}}]},
            "Photo URL":      {"url": listing.get("photo_url") or None},
            "Listing URL":    {"url": listing.get("listing_url") or None},
            "Newsletter":     {"select": {"name": newsletter_name}},
            "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":         {"select": {"name": "approved"}},
        }
        create_page(NOTION_RE_DB_ID, properties)
        print(f"  ✓ Saved: {listing.get('tier')} - {listing.get('address')}")


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

        # Generate GIF from the tier photos (use tier_listings which has the actual photo URLs)
        print(f"\n  Creating GIF from {len(tier_listings)} listing photos...")
        gif_urls = []
        gif_labels = []
        for listing in tier_listings:
            photo = listing.get("photo_url", "")
            if photo:
                gif_urls.append(photo)
                price = listing.get("price", 0)
                tier = listing.get("tier", "")
                beds = listing.get("beds", 0)
                baths = listing.get("baths", 0)
                tier_emoji = {"Starter": "🏠", "Sweet Spot": "🏡", "Showcase": "🏰"}.get(tier, "🏠")
                gif_labels.append(f"{tier_emoji} {tier}  •  ${price:,}  •  {beds}bd/{baths}ba")

        gif_path = None
        if gif_urls:
            try:
                sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
                from gif_maker import create_gif_from_urls
                gif_bytes = create_gif_from_urls(gif_urls, labels=gif_labels)
                if gif_bytes:
                    output_dir = Path(__file__).parent / "output"
                    output_dir.mkdir(exist_ok=True)
                    gif_path = output_dir / f"re_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.gif"
                    gif_path.write_bytes(gif_bytes)
                    print(f"  ✓ GIF saved to {gif_path} ({len(gif_bytes):,} bytes)")
            except Exception as e:
                print(f"  ✗ GIF creation failed: {e}")

        # Save to Notion
        save_real_estate_to_notion(results, newsletter["name"])

    print(f"\nAll newsletters complete.")
