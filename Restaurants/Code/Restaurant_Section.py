#!/usr/bin/env python3
"""
Newsletter Automation - Restaurant Section
Uses Google Places API to find local restaurants near each newsletter zip,
generates blurbs via Claude, scores them with festive awareness,
and saves results to Notion.
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))

import json
import math
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from notion_helper import get_featured_place_ids, save_restaurants_to_notion

import requests
import anthropic

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT
# ---------------------------------------------------------------------------
CLAUDE_API_KEY          = os.environ["CLAUDE_API_KEY"]
GOOGLE_PLACES_API_KEY   = os.environ["GOOGLE_PLACES_API_KEY"]
SKILL_PROMPT_PATH       = Path(__file__).parent.parent.parent / "Skills" / "newsletter-restaurant-blurb-skill.md"
SEARCH_RADIUS_METERS    = 8047  # 5 miles in meters
MAX_CANDIDATES          = 30    # fetch before filtering
TARGET_TOP_5            = 5
MAX_SAME_CUISINE        = 2
LOOKBACK_WEEKS          = 8

NEWSLETTERS = [
    {"name": "East_Cobb_Connect", "zip": "30062", "lat": 33.9773, "lng": -84.5130},
    {"name": "Perimeter_Post",    "zip": "30328", "lat": 33.9207, "lng": -84.3882},
]

# ---------------------------------------------------------------------------
# 2. KNOWN CHAINS TO EXCLUDE
# ---------------------------------------------------------------------------
KNOWN_CHAINS = {
    "mcdonald's", "starbucks", "chick-fil-a", "subway", "burger king",
    "wendy's", "taco bell", "chipotle", "panera bread", "olive garden",
    "applebee's", "chili's", "ihop", "denny's", "waffle house",
    "cracker barrel", "buffalo wild wings", "red lobster", "outback steakhouse",
    "texas roadhouse", "longhorn steakhouse", "cheesecake factory", "the cheesecake factory",
    "pf chang's", "domino's", "pizza hut", "papa john's", "little caesars", "five guys",
    "shake shack", "in-n-out", "sonic", "dairy queen", "dunkin",
    "popeyes", "raising cane's", "wingstop", "zaxby's", "hardee's",
    "arby's", "jersey mike's", "jimmy john's", "firehouse subs",
    "moe's southwest grill", "qdoba", "panda express", "jason's deli",
    "noodles & company", "first watch", "eggs up grill", "metro diner",
    "dave & buster's", "dave & busters", "golden corral", "twin peaks",
    "bahama breeze", "fogo de chão", "fogo de chao", "main event",
    "puttshack", "inspire brands", "pappadeaux", "pappadeaux seafood kitchen",
    "pappasito's", "pappasito's cantina", "pappasitos","main event", "fogo de chão"
}

# ---------------------------------------------------------------------------
# 3. FESTIVE CALENDAR
# ---------------------------------------------------------------------------
def get_festive_boosts() -> list[dict]:
    """Return list of upcoming holiday boosts based on today's date."""
    today = datetime.today()
    year  = today.year

    holidays = [
        {"name": "Valentine's Day",  "date": datetime(year, 2, 14),  "cuisines": ["french", "italian"],           "window": 21},
        {"name": "Mardi Gras",       "date": datetime(year, 3, 4),   "cuisines": ["cajun", "creole", "southern"], "window": 14},
        {"name": "St. Patrick's Day","date": datetime(year, 3, 17),  "cuisines": ["irish"],                       "window": 14},
        {"name": "Cinco de Mayo",    "date": datetime(year, 5, 5),   "cuisines": ["mexican"],                     "window": 21},
        {"name": "Fourth of July",   "date": datetime(year, 7, 4),   "cuisines": ["american", "bbq", "barbecue"], "window": 14},
        {"name": "Oktoberfest",      "date": datetime(year, 10, 1),  "cuisines": ["german"],                      "window": 30},
        {"name": "Lunar New Year",   "date": datetime(year, 1, 29),  "cuisines": ["chinese", "vietnamese", "korean", "japanese"], "window": 21},
        {"name": "Thanksgiving",     "date": datetime(year, 11, 27), "cuisines": ["american", "southern"],        "window": 14},
        {"name": "Christmas",        "date": datetime(year, 12, 25), "cuisines": ["italian", "french", "american"], "window": 21},
    ]

    active_boosts = []
    for holiday in holidays:
        days_until = (holiday["date"] - today).days
        if 0 <= days_until <= holiday["window"]:
            boost = round(10 * (1 - days_until / holiday["window"]))
            active_boosts.append({
                "name":     holiday["name"],
                "cuisines": holiday["cuisines"],
                "boost":    max(boost, 3),
                "days_until": days_until
            })

    return active_boosts

def get_festive_score(cuisine_type: str, boosts: list[dict]) -> tuple[int, str]:
    """Return festive score and reason for a cuisine type."""
    cuisine_lower = cuisine_type.lower()
    for boost in boosts:
        for c in boost["cuisines"]:
            if c in cuisine_lower or cuisine_lower in c:
                reason = f"{boost['name']} in {boost['days_until']} days"
                return boost["boost"], reason
    return 5, "No upcoming holiday boost"

# ---------------------------------------------------------------------------
# 4. GOOGLE AUTH
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    # Default prompt if skill file doesn't exist
    return """You are a local newsletter writer covering community restaurants.
Write warm, neighbor-style restaurant blurbs that feel like a trusted friend recommending a place.
Focus on what makes the restaurant special, the vibe, and 1-2 must-try dishes.
Keep it conversational, no em dashes, eighth-grade readability."""

# ---------------------------------------------------------------------------
# 6. FETCH RESTAURANTS FROM GOOGLE PLACES API
# ---------------------------------------------------------------------------

def fetch_restaurants(lat: float, lng: float, excluded_place_ids: set, newsletter_name: str) -> list[dict]:
    print(f"\n--- Fetching restaurants near {lat},{lng} ---")

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type":     "application/json",
        "X-Goog-Api-Key":   GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.websiteUri,places.googleMapsUri,places.regularOpeningHours,places.rating,places.userRatingCount,places.priceLevel,places.photos,places.primaryTypeDisplayName,places.editorialSummary,places.reviews"
    }

    all_places = []
    for rank_pref in ["POPULARITY", "DISTANCE"]:
        payload = {
            "includedTypes":    ["restaurant"],
            "maxResultCount":   20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": SEARCH_RADIUS_METERS
                }
            },
            "rankPreference": rank_pref
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                places = response.json().get("places", [])
                all_places.extend(places)
                print(f"  {len(places)} results by {rank_pref}")
            time.sleep(1)
        except Exception as e:
            print(f"  Places API error ({rank_pref}): {e}")

    # Deduplicate by place ID
    seen = set()
    unique_places = []
    for place in all_places:
        pid = place.get("id", "")
        if pid not in seen:
            seen.add(pid)
            unique_places.append(place)

    print(f"Found {len(unique_places)} unique restaurants from Places API")
    # ... rest of function stays the same, just replace `places` with `unique_places`
    restaurants = []
    for place in unique_places:
        place_id = place.get("id", "")
        name     = place.get("displayName", {}).get("text", "")
    
        # Skip non-restaurants
        primary_type = place.get("primaryTypeDisplayName", {}).get("text", "").lower()
        non_food_keywords = ["corporate office", "golf course", "miniature golf", "entertainment"]
        if any(kw in primary_type for kw in non_food_keywords):
            print(f"  ✗ Not a restaurant: {name}")
            continue
        
        # Check if any chain name is contained within the restaurant name
        if any(chain in name.lower() for chain in KNOWN_CHAINS):
            print(f"  ✗ Chain excluded: {name}")
            continue

        # Skip previously featured
        if place_id in excluded_place_ids:
            print(f"  ✗ Previously featured: {name}")
            continue

        # Skip low rated
        rating = place.get("rating", 0)
        reviews = place.get("userRatingCount", 0)
        if rating < 4.0 or reviews < 50:
            print(f"  ✗ Low rating/reviews: {name} ({rating} stars, {reviews} reviews)")
            continue

        # Get cuisine type
        cuisine = place.get("primaryTypeDisplayName", {}).get("text", "Restaurant")

        # Get up to 3 photo URLs -- resolve to direct CDN URLs
        photos_raw = place.get("photos", [])
        photo_urls = []
        for photo_entry in photos_raw[:3]:
            photo_ref = photo_entry.get("name", "")
            if photo_ref:
                try:
                    photo_api_url = f"https://places.googleapis.com/v1/{photo_ref}/media?maxHeightPx=800&skipHttpRedirect=true&key={GOOGLE_PLACES_API_KEY}"
                    photo_res = requests.get(photo_api_url, timeout=10)
                    if photo_res.status_code == 200:
                        resolved = photo_res.json().get("photoUri", "")
                        if resolved:
                            photo_urls.append(resolved)
                except Exception:
                    pass
        photo_url = photo_urls[0] if photo_urls else ""
        if photo_urls:
            print(f"    ✓ {len(photo_urls)} photos resolved")
                    
        # Get hours
        hours_data = place.get("regularOpeningHours", {})
        hours      = ", ".join(hours_data.get("weekdayDescriptions", [])) if hours_data else ""

        # Get editorial summary or top review
        summary = place.get("editorialSummary", {}).get("text", "")
        if not summary:
            reviews_list = place.get("reviews", [])
            if reviews_list:
                summary = reviews_list[0].get("text", {}).get("text", "")

        restaurants.append({
            "place_id":        place_id,
            "name":            name,
            "cuisine":         cuisine,
            "address":         place.get("formattedAddress", ""),
            "phone":           place.get("nationalPhoneNumber", ""),
            "website":         place.get("websiteUri", ""),
            "maps_url":        place.get("googleMapsUri", ""),
            "rating":          rating,
            "review_count":    reviews,
            "price_level":     place.get("priceLevel", ""),
            "photo_url":       photo_url,
            "photo_urls":      photo_urls,
            "hours":           hours,
            "summary":         summary,
            "newsletter_name": newsletter_name
        })
        print(f"  ✓ {name} | {cuisine} | {rating}★ ({reviews} reviews)")

    print(f"Qualified restaurants: {len(restaurants)}")
    return restaurants

# ---------------------------------------------------------------------------
# 8. ENFORCE CUISINE DIVERSITY
# ---------------------------------------------------------------------------
def enforce_cuisine_diversity(restaurants: list[dict], max_same: int = MAX_SAME_CUISINE) -> list[dict]:
    """Limit to max_same restaurants per cuisine type."""
    cuisine_counts = {}
    selected       = []

    for r in restaurants:
        cuisine = r["cuisine"].lower()
        count   = cuisine_counts.get(cuisine, 0)
        if count < max_same:
            selected.append(r)
            cuisine_counts[cuisine] = count + 1
        else:
            print(f"  ✗ Cuisine limit reached for {r['cuisine']}: {r['name']}")

        if len(selected) >= TARGET_TOP_5:
            break

    return selected

# ---------------------------------------------------------------------------
# 9. GENERATE RESTAURANT BLURBS VIA CLAUDE
# ---------------------------------------------------------------------------
def generate_restaurant_blurbs(restaurants: list[dict], skill_prompt: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    combined = ""
    for i, r in enumerate(restaurants, 1):
        combined += f"""
--- Restaurant {i} ---
Name: {r['name']}
Cuisine: {r['cuisine']}
Address: {r['address']}
Rating: {r['rating']} stars ({r['review_count']} reviews)
Price Level: {r['price_level']}
Hours: {r['hours']}
Website: {r['website']}
Google Maps: {r['maps_url']}
Summary/Review: {r['summary'][:500] if r['summary'] else 'Not available'}

"""

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""
Write a neighbor-style restaurant blurb for each of these local restaurants.
Each blurb should feel like a trusted neighbor recommending a place -- warm, specific, and conversational.
Mention the vibe, 1-2 must-try dishes or drinks, and what kind of occasion it's good for.
No em dashes. No AI-sounding language. Eighth-grade readability.

Return ONLY a JSON array with no preamble or markdown. Exact format:
[
  {{
    "place_id": "ChIJ...",
    "restaurant_name": "Name",
    "cuisine_type": "Italian",
    "blurb": "Full blurb here...",
    "address": "123 Main St",
    "phone": "(770) 555-1234",
    "hours": "Mon-Fri 11am-9pm...",
    "website_url": "https://...",
    "google_maps_url": "https://maps.google.com/...",
    "rating": 4.5,
    "review_count": 234,
    "price_level": "PRICE_LEVEL_MODERATE"
  }}
]

Restaurants:
{combined}
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

    raw    = next(block.text for block in response.content if block.type == "text")
    clean  = raw.strip().removeprefix("```json").removesuffix("```").strip()
    results = json.loads(clean)

    # Map photo_url back -- try place_id first, then name
    photo_map_by_id   = {r["place_id"]: r["photo_url"] for r in restaurants}
    photo_map_by_name = {r["name"]: r["photo_url"] for r in restaurants}

    for result in results:
        photo_url = photo_map_by_id.get(result["place_id"], "")
        if not photo_url:
            photo_url = photo_map_by_name.get(result["restaurant_name"], "")
        result["photo_url"] = photo_url
        print(f"  {result['restaurant_name']} photo_url: {'✓' if photo_url else 'EMPTY'}")

    print(f"Generated {len(results)} restaurant blurbs")
    return results

# ---------------------------------------------------------------------------
# 10. SCORE RESTAURANTS VIA CLAUDE
# ---------------------------------------------------------------------------
def score_restaurants(results: list[dict]) -> list[dict]:
    client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    boosts  = get_festive_boosts()

    if boosts:
        print(f"Active festive boosts: {[b['name'] for b in boosts]}")

    scoring_input = ""
    for i, r in enumerate(results, 1):
        festive_score, festive_reason = get_festive_score(r.get("cuisine_type", ""), boosts)
        r["festive_score"]  = festive_score
        r["festive_reason"] = festive_reason
        scoring_input += f"""
        --- Restaurant {i} ---
        place_id: {r.get('place_id', '')}
        Name: {r['restaurant_name']}
        Cuisine: {r.get('cuisine_type', '')}
        Blurb: {r['blurb']}
        Rating: {r.get('rating', '')} stars ({r.get('review_count', '')} reviews)
        Festive relevance: {festive_reason} (pre-scored: {festive_score}/10)
        
        """

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=[{
                    "role": "user",
                    "content": f"""
        You are scoring local restaurant blurbs for a community newsletter editor.
        Score each on 0-10 for:

        1. Appeal: How exciting and interesting is this restaurant for newsletter readers?
        2. Uniqueness: How different is it from typical chain options in the area?
        3. Neighborhood Fit: How well does it fit a suburban Atlanta community (families, professionals)?

        The festive score has already been calculated separately -- do NOT score it.

        CRITICAL: Return the exact place_id value provided above for each restaurant.
        Do not modify, slugify, or shorten the place_id. Copy it exactly as given.

        Return ONLY a JSON array with no preamble or markdown:
        ...

        Restaurants to score:
        {scoring_input}
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

    raw    = next(block.text for block in response.content if block.type == "text")
    clean  = raw.strip().removeprefix("```json").removesuffix("```").strip()
    scores = json.loads(clean)

    print(f"  Raw scores sample: {scores[0] if scores else 'EMPTY'}")
    print(f"  Score results: {[s.get('place_id') for s in scores]}")
    print(f"  Blurb place_ids: {[r.get('place_id') for r in results]}")

    score_map = {s["place_id"]: s for s in scores}
    for result in results:
        s = score_map.get(result["place_id"], {})
        result["appeal_score"]           = s.get("appeal_score") or s.get("appeal", 0)
        result["uniqueness_score"]       = s.get("uniqueness_score") or s.get("uniqueness", 0)
        result["neighborhood_fit_score"] = s.get("neighborhood_fit_score") or s.get("neighborhood_fit", 0)
        result["scoring_notes"]          = s.get("scoring_notes", "")
        result["total_score"]            = (
            result["appeal_score"] +
            result["uniqueness_score"] +
            result["neighborhood_fit_score"] +
            result["festive_score"]
        )

    results.sort(key=lambda x: x["total_score"], reverse=True)

    for r in results:
        print(f"  {r['restaurant_name']}: {r['total_score']}/40 | appeal: {r['appeal_score']} | unique: {r['uniqueness_score']} | fit: {r['neighborhood_fit_score']} | festive: {r['festive_score']}")

    return results

# ---------------------------------------------------------------------------
# 11. FLAG DEFAULT WINNER
# ---------------------------------------------------------------------------
def flag_default_winner(results: list[dict]) -> list[dict]:
    for r in results:
        r["default_winner"] = ""
    if results:
        results[0]["default_winner"] = "yes"
        print(f"Default winner: {results[0]['restaurant_name']} ({results[0]['total_score']}/40)")
    return results

# ---------------------------------------------------------------------------
# 12. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting restaurant automation — {datetime.today().strftime('%Y-%m-%d')}")

    skill_prompt = load_skill_prompt()

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']}")
        print(f"{'='*60}")

        excluded = get_featured_place_ids(newsletter["name"])

        # Fetch restaurants
        restaurants = fetch_restaurants(
            lat=newsletter["lat"],
            lng=newsletter["lng"],
            excluded_place_ids=excluded,
            newsletter_name=newsletter["name"]
        )

        if not restaurants:
            print(f"No restaurants found for {newsletter['name']}. Skipping.")
            continue

        # Enforce cuisine diversity and limit to top 5
        restaurants = enforce_cuisine_diversity(restaurants)
        print(f"\nTop {len(restaurants)} restaurants after cuisine filter")

        # Generate blurbs
        results = generate_restaurant_blurbs(restaurants, skill_prompt)

        # Score
        results = score_restaurants(results)

        # Select top 5
        results = results[:TARGET_TOP_5]

        # Flag default winner
        results = flag_default_winner(results)

        # Generate GIFs for each restaurant (3 photos per restaurant)
        print(f"\n  Creating GIFs for {len(results)} restaurants...")
        try:
            sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
            from gif_maker import create_gif_from_urls
            from pathlib import Path as _Path
            output_dir = _Path(__file__).parent / "output"
            output_dir.mkdir(exist_ok=True)

            # Build photo_urls map from the original restaurant data (by place_id AND name)
            photo_map_id = {r["place_id"]: r.get("photo_urls", []) for r in restaurants}
            photo_map_name = {r["name"]: r.get("photo_urls", []) for r in restaurants}

            for result in results:
                pid = result.get("place_id", "")
                rname = result.get("restaurant_name", "")
                photos = photo_map_id.get(pid, []) or photo_map_name.get(rname, [])
                if not photos:
                    print(f"    {rname}: no photos, skipping GIF")
                    continue
                if len(photos) == 1:
                    print(f"    {rname}: 1 photo, using static image")
                    continue
                label = f"🍽️ {rname}"
                gif_bytes = create_gif_from_urls(photos, labels=[label] * len(photos))
                if gif_bytes:
                    slug = rname.lower().replace(" ", "_").replace("'", "")[:30]
                    gif_filename = f"rest_{newsletter['name']}_{slug}_{datetime.today().strftime('%Y%m%d')}.gif"
                    gif_path = output_dir / gif_filename
                    gif_path.write_bytes(gif_bytes)
                    result["gif_url"] = f"https://couch2coders.github.io/NewsletterAutomation/gifs/{gif_filename}"
                    result["gif_filename"] = gif_filename
                    print(f"    ✓ {rname} GIF: {len(photos)} frames, {len(gif_bytes):,} bytes")
        except Exception as e:
            print(f"  ✗ GIF creation failed: {e}")

        # Log GIF URLs before save
        for r in results:
            rname = r.get("restaurant_name", "")
            gif = r.get("gif_url", "")
            print(f"    {rname}: gif_url = {gif[:60] if gif else 'EMPTY'}")

        # Save
        save_restaurants_to_notion(results, newsletter["name"])
        print(f"Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
