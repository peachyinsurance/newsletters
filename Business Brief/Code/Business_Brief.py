#!/usr/bin/env python3
"""
Newsletter Automation - Business Brief Section

Searches Brave for non-restaurant local businesses (retail, gyms, salons,
spas, services, boutiques) in each newsletter's coverage area, filters out
restaurants and chain locations, then asks Claude to pick ONE strong
business per newsletter and write a 150-200 word neighbor-style spotlight
in the voice the section is known for.

Pattern mirrors Featured Event: per-newsletter loop, Brave search per
newsletter, aggregator + chain + restaurant filter, URL-validate, Claude
JSON, score + flag default winner, save to Notion. Honors NEWSLETTER env
var for per-newsletter manual dispatch.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from brave_search import search_web, domain_of
from claude_json import call_with_json_output
from notion_helper import (
    save_business_briefs_to_notion,
    get_existing_business_brief_urls,
)
from url_validator import filter_valid_items
from newsletters_config import NEWSLETTERS, filter_by_env

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY        = os.environ["CLAUDE_API_KEY"]
# Optional — kept around so old workflows that still pass it don't
# break, but the Brave fallback path is no longer used. Set this to
# "" or remove from the workflow once you're confident in the Places
# migration.
BRAVE_NEWS_API_KEY    = os.environ.get("BRAVE_NEWS_API_KEY", "")
# REQUIRED — Google Places is now the primary candidate source.
GOOGLE_PLACES_API_KEY = os.environ["GOOGLE_PLACES_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-business-brief-skill_auto.md"

# Places searchNearby radius per newsletter. ~10 miles covers the whole
# East Cobb / Perimeter / Lewisville footprints comfortably.
SEARCH_RADIUS_METERS = 16093  # ≈ 10 miles
# Minimum quality bar so Claude doesn't see businesses no real
# customer has reviewed. Tuned conservatively — adjust if the pool
# is consistently too small.
MIN_RATING       = 4.2
MIN_REVIEW_COUNT = 25

# Whitelist of place types eligible for Business Brief. Conservative
# subset of Google Places API Table A types — the searchNearby endpoint
# rejects the WHOLE request if any single type isn't in Table A, so we
# stick to types we're confident are valid. Full list:
# https://developers.google.com/maps/documentation/places/web-service/place-types
BUSINESS_INCLUDED_TYPES = [
    # Retail
    "clothing_store", "shoe_store", "jewelry_store", "gift_shop",
    "book_store", "furniture_store", "home_goods_store", "florist",
    "pet_store", "bicycle_store", "electronics_store",
    "hardware_store",
    # Beauty / wellness
    "beauty_salon", "hair_care", "spa",
    # Fitness
    "gym",
    # Culture
    "art_gallery",
]

# Explicit excludes — Places sometimes infers a primary type one of these
# overlaps with our included list (e.g. clothing_store + cafe combo).
# Drop anything that looks food-service.
BUSINESS_EXCLUDED_TYPES = [
    "restaurant", "cafe", "bar", "fast_food_restaurant",
    "meal_takeaway", "meal_delivery", "bakery", "night_club",
]

# Chain detection — Places returns chain locations under their corporate
# names. Substring match against displayName (lowercased) covers most
# of them without maintaining a huge hostname list.
CHAIN_NAME_TOKENS = {
    "walmart", "target", "lowe's", "home depot",
    "cvs", "walgreens", "rite aid",
    "best buy", "costco", "sam's club", "kroger",
    "publix", "whole foods", "trader joe's", "aldi",
    "macy's", "kohl's", "nordstrom", "tj maxx", "marshalls",
    "dollar general", "dollar tree", "five below", "family dollar",
    "petsmart", "petco",
    "ulta", "sephora",
    "office depot", "staples",
    "barnes & noble", "barnes and noble",
    "michaels", "hobby lobby", "joann",
    "ikea", "wayfair",
    "at&t", "verizon", "t-mobile",
    "planet fitness", "la fitness", "ymca", "orangetheory", "anytime fitness",
    "supercuts", "great clips", "sport clips", "fantastic sams",
    "massage envy", "european wax center",
}


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Business Brief skill not found at {SKILL_PROMPT_PATH}")


# ---------------------------------------------------------------------------
# 3. GOOGLE PLACES CANDIDATE FETCH
# ---------------------------------------------------------------------------
def fetch_businesses_from_places(newsletter: dict, excluded_urls: set) -> list[dict]:
    """Pull non-restaurant local businesses from Google Places searchNearby
    using the newsletter's lat/lng. Returns candidate dicts shaped like the
    old Brave output (`title`, `url`, `source`, `summary`) so the Claude
    user-prompt builder doesn't need to change.

    Two rank passes (POPULARITY + DISTANCE) maximize coverage. Place IDs
    are deduped between the two passes. Then we filter for:
      - quality bar (rating ≥ MIN_RATING, review count ≥ MIN_REVIEW_COUNT)
      - not a chain (displayName doesn't match any CHAIN_NAME_TOKENS)
      - has a website (without it, source_url would be the Google Maps
        page — not useful for an editorial spotlight)
      - URL not already in the existing Business Brief DB (cross-newsletter
        dedup, passed in as excluded_urls)
    """
    lat = newsletter.get("lat")
    lng = newsletter.get("lng")
    if lat is None or lng is None:
        print(f"  ⚠ No lat/lng for {newsletter['name']} — cannot Places-search")
        return []

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type":     "application/json",
        "X-Goog-Api-Key":   GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.nationalPhoneNumber,places.websiteUri,places.googleMapsUri,"
            "places.regularOpeningHours,places.rating,places.userRatingCount,"
            "places.priceLevel,places.photos,places.primaryTypeDisplayName,"
            "places.editorialSummary,places.reviews,places.types"
        ),
    }

    all_places: list[dict] = []
    seen_ids: set[str] = set()
    for rank_pref in ["POPULARITY", "DISTANCE"]:
        payload = {
            "includedTypes":   BUSINESS_INCLUDED_TYPES,
            "excludedTypes":   BUSINESS_EXCLUDED_TYPES,
            "maxResultCount":  20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": SEARCH_RADIUS_METERS,
                }
            },
            "rankPreference": rank_pref,
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code != 200:
                print(f"  ⚠ Places searchNearby {rank_pref} HTTP {r.status_code}: {r.text[:200]}")
                continue
            for p in (r.json() or {}).get("places", []):
                pid = p.get("id") or ""
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_places.append(p)
            time.sleep(1)
        except Exception as e:
            print(f"  ⚠ Places error ({rank_pref}): {e}")
    print(f"  Places returned {len(all_places)} unique businesses (both passes)")

    candidates: list[dict] = []
    for place in all_places:
        name    = (place.get("displayName") or {}).get("text", "") or ""
        website = (place.get("websiteUri") or "").strip()
        address = place.get("formattedAddress", "") or ""
        rating  = place.get("rating", 0) or 0
        reviews = place.get("userRatingCount", 0) or 0
        types   = place.get("types", []) or []
        primary = (place.get("primaryTypeDisplayName") or {}).get("text", "")
        editorial = (place.get("editorialSummary") or {}).get("text", "")

        if not name:
            continue
        if not website:
            continue
        if website in excluded_urls:
            continue
        # Chain filter — substring match on the business name
        name_low = name.lower()
        if any(tok in name_low for tok in CHAIN_NAME_TOKENS):
            print(f"  ✗ Chain skipped: {name}")
            continue
        # Defensive: Places sometimes mixes a restaurant type into the
        # types array even with excludedTypes set. Belt-and-suspenders.
        if any(t in BUSINESS_EXCLUDED_TYPES for t in types):
            print(f"  ✗ Restaurant-tagged skipped: {name} ({types[:3]})")
            continue
        # Quality bar
        if rating < MIN_RATING or reviews < MIN_REVIEW_COUNT:
            print(f"  ✗ Below quality bar: {name} ({rating}★, {reviews} reviews)")
            continue

        # Build a one-paragraph "summary" Claude can use to score. Pulls
        # whatever Places gives us — editorial blurb, primary type,
        # rating signal, address.
        summary_parts = []
        if editorial:
            summary_parts.append(editorial)
        if primary:
            summary_parts.append(f"Type: {primary}.")
        summary_parts.append(f"Rating: {rating}★ ({reviews} reviews).")
        if address:
            summary_parts.append(f"Address: {address}.")
        # First couple of reviews as additional editorial color
        for rev in (place.get("reviews") or [])[:2]:
            txt = ((rev or {}).get("text") or {}).get("text", "") or ""
            if txt:
                summary_parts.append(f"Review: {txt[:200]}")

        candidates.append({
            "title":   name,
            "url":     website,
            "source":  domain_of(website),
            "summary": " ".join(summary_parts)[:1200],
            # extras Claude doesn't see but we keep for downstream use:
            "_place_id":         place.get("id", ""),
            "_address":          address,
            "_rating":           rating,
            "_review_count":     reviews,
            "_primary_type":     primary,
            "_google_maps_uri":  place.get("googleMapsUri", "") or "",
        })

    # Sort by review count desc as a soft popularity signal — Claude
    # still scores on editorial fit, but presenting busier places first
    # nudges the picker toward businesses readers might actually know.
    candidates.sort(key=lambda c: -(c.get("_review_count") or 0))
    print(f"  {len(candidates)} candidates pass filters")
    return candidates


# ---------------------------------------------------------------------------
# 4. FILTERS (legacy — kept for compatibility but no longer in the active path)
# ---------------------------------------------------------------------------
def is_aggregator(url: str) -> bool:
    return False


def is_chain(url: str) -> bool:
    return False


def is_restaurant_directory(url: str) -> bool:
    return False


def filter_candidates(candidates: list[dict]) -> list[dict]:
    """Drop aggregator/chain/restaurant-directory hostnames before sending to Claude."""
    kept = []
    for c in candidates:
        url = c.get("url", "")
        if is_aggregator(url) or is_chain(url) or is_restaurant_directory(url):
            continue
        kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# 5. CLAUDE PROMPT BUILDER
# ---------------------------------------------------------------------------
def build_claude_user_prompt(newsletter: dict, candidates: list[dict]) -> str:
    # Strip the `_` private extras we kept for downstream use so they
    # don't bloat the prompt and confuse Claude.
    public = [{k: v for k, v in c.items() if not k.startswith("_")}
              for c in candidates]
    indexed = [{**c, "candidate_index": i} for i, c in enumerate(public, 1)]
    candidates_json = json.dumps(indexed, indent=2)

    today = datetime.today()
    return f"""
publication_date: {today.strftime('%Y-%m-%d')}
newsletter_name: {newsletter['name']}
display_area: {newsletter['display_area']}
search_areas: {json.dumps(newsletter['search_areas'])}

Below are Google Places candidates for non-restaurant local businesses
near {newsletter['display_area']}. They are already filtered to retail /
beauty / fitness / services types and meet a rating + review-count
quality bar. Aggregators, chains, and restaurants are already excluded.

Pick the THREE best businesses (per the skill's `businesses: [3 entries]`
rule) and write each spotlight per the voice and structure rules. Use
`candidate_index` to reference the source — do NOT include raw URLs in
your output. Each `summary` field includes the Places editorial blurb +
rating signal + sample reviews to give you editorial context.

Candidates:
{candidates_json}
"""


# ---------------------------------------------------------------------------
# 6. SCORE + FLAG WINNER
# ---------------------------------------------------------------------------
def flag_default_winner(results: list[dict]) -> list[dict]:
    """Mark the highest-relevance pick as the default winner."""
    for r in results:
        r["default_winner"] = ""
    if results:
        results.sort(key=lambda x: -(int(x.get("relevance_score", 0) or 0)))
        results[0]["default_winner"] = "yes"
        print(f"  Default winner: {results[0].get('name')} (relevance {results[0].get('relevance_score')})")
    return results


def fetch_google_places_photos(business_name: str, city: str = "",
                               address: str = "",
                               max_photos: int = 5) -> list[str]:
    """Look up `business_name` in Google Places (Text Search) and return
    up to `max_photos` direct CDN URLs from the matched place. Reviewers
    can browse the gallery in the review app and pick one for the email.

    Returns [] on any miss / error. Owner-uploaded business photos are
    far more reliable than Brave image-search results.

    Query strategy: name + city (or first 4 words of address as fallback
    locator) so we don't accidentally match a chain location somewhere
    else in the country."""
    if not GOOGLE_PLACES_API_KEY or not business_name:
        return []

    locator = city or " ".join((address or "").split()[:4])
    query = f"{business_name} {locator}".strip()

    try:
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "Content-Type":     "application/json",
                "X-Goog-Api-Key":   GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": "places.id,places.displayName,places.photos",
            },
            json={"textQuery": query, "maxResultCount": 3},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"    · Places text search HTTP {resp.status_code} for {business_name!r}")
            return []
        places = (resp.json() or {}).get("places") or []
        if not places:
            print(f"    · Places: no match for {query!r}")
            return []
        photos = (places[0].get("photos") or [])[:max_photos]
        if not photos:
            print(f"    · Places match but no photos for {business_name!r}")
            return []

        # Resolve each photo reference to a direct CDN URL.
        urls: list[str] = []
        for entry in photos:
            ref = (entry or {}).get("name") or ""
            if not ref:
                continue
            try:
                media = requests.get(
                    f"https://places.googleapis.com/v1/{ref}/media",
                    params={
                        "maxHeightPx":      800,
                        "skipHttpRedirect": "true",
                        "key":              GOOGLE_PLACES_API_KEY,
                    },
                    timeout=10,
                )
                if media.status_code != 200:
                    continue
                url = (media.json() or {}).get("photoUri") or ""
                if url and url not in urls:
                    urls.append(url)
            except Exception:
                continue
        if urls:
            print(f"    ✓ Places: {len(urls)} photo(s) for {business_name!r}")
        return urls
    except Exception as e:
        print(f"    · Places photo fetch error for {business_name!r}: {e}")
        return []


def fetch_google_places_photo(business_name: str, city: str = "",
                              address: str = "") -> str:
    """Single-photo convenience wrapper — kept for backwards compat."""
    urls = fetch_google_places_photos(business_name, city, address, max_photos=1)
    return urls[0] if urls else ""


# ---------------------------------------------------------------------------
# 7. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Business Brief automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    for newsletter in filter_by_env():
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Cross-newsletter dedup set — don't re-feature a business already
        # featured (or rejected) in any newsletter's Business Brief DB.
        existing_urls: set[str] = set()
        for nl in NEWSLETTERS:
            existing_urls |= get_existing_business_brief_urls(nl["name"])

        # Google Places searchNearby — replaces Brave + manual filter waterfall.
        # Places guarantees active businesses with valid websiteUri, and the
        # includedTypes / excludedTypes parameters drop restaurants + most
        # chains before we ever see them. URL validation step is no longer
        # needed since websiteUri is owner-verified.
        candidates = fetch_businesses_from_places(newsletter, excluded_urls=existing_urls)
        if not candidates:
            print(f"  No Places candidates for {newsletter['name']}. Skipping.")
            continue

        # Cap to keep prompt reasonable. Sort is review-count desc, so the
        # top N are the most-reviewed (busiest) qualifying businesses.
        candidates = candidates[:30]

        # Claude
        print(f"  Sending {len(candidates)} candidates to Claude...")
        user_prompt = build_claude_user_prompt(newsletter, candidates)
        try:
            response = call_with_json_output(
                api_key=CLAUDE_API_KEY,
                system=skill_prompt,
                user_content=user_prompt,
            )
        except Exception as e:
            print(f"  ✗ Claude error: {e}")
            continue

        # Skill returns either a dict {newsletter_name, businesses, all_scored, dropped_candidates}
        # or a bare list (defensive). Normalize to a list of business dicts.
        if isinstance(response, dict):
            picks = response.get("businesses", [])
        elif isinstance(response, list):
            picks = response
        else:
            picks = []

        if not picks:
            print(f"  Claude found no qualifying businesses for {newsletter['name']}. Skipping.")
            continue

        # Attach real URLs from candidate_index
        candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}
        validated = []
        for r in picks:
            idx = r.get("candidate_index")
            try:
                idx = int(idx) if idx is not None else None
            except Exception:
                idx = None
            source = candidates_by_index.get(idx) if idx is not None else None
            if not source:
                print(f"  ✗ Rejecting business with invalid candidate_index {idx}: {r.get('name', '?')}")
                continue
            r["source_url"] = source.get("url", "")
            r["source"]     = source.get("source", "") or domain_of(source.get("url", ""))
            r.pop("candidate_index", None)
            validated.append(r)

        if not validated:
            continue

        # Look up each picked business in Google Places Text Search and
        # attach a small gallery of photos. The review app shows the
        # gallery so reviewers can pick which one ships. photo_url is
        # the current selection (first photo by default); image_candidates
        # is the JSON-encoded gallery the tile renders for swap.
        for pick in validated:
            photos = fetch_google_places_photos(
                business_name=pick.get("name", ""),
                city=pick.get("city", ""),
                address=pick.get("address", ""),
                max_photos=5,
            )
            if photos:
                pick["photo_url"] = photos[0]
                pick["image_candidates"] = photos

        validated = flag_default_winner(validated)
        save_business_briefs_to_notion(validated, newsletter["name"])

        # Local JSON backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"business_brief_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(validated, indent=2), encoding="utf-8")
        print(f"  Saved JSON backup to {json_file}")

    print(f"\nAll newsletters complete.")
