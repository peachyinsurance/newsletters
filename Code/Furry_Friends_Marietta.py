#!/usr/bin/env python3
"""
Newsletter Automation - Pet Adoption Section
Uses RescueGroups.org API to find adoptable cats and dogs near East Cobb,
generates blurbs via Claude, scores them, flags defaults,
and writes results to Google Sheets.
"""

import os
import re
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math

import requests
import anthropic
from notion_helper import get_approved_pet_urls, save_pets_to_notion

NEWSLETTERS = [
    {
        "name":   "East_Cobb_Connect",
        "zip":    "30062",
        "radius": 10
    },
    {
        "name":   "Perimeter_Post",
        "zip":    "30346",
        "radius": 10
    }
]

# NEWSLETTER_NAME     = "East_Cobb_Connect"
# ANCHOR_ZIP          = "30062"
# SEARCH_RADIUS_MILES = 25

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT
# ---------------------------------------------------------------------------
CLAUDE_API_KEY          = os.environ["CLAUDE_API_KEY"]
RESCUEGROUPS_API_KEY    = os.environ["RESCUE_GROUP_API_KEY"]
SKILL_PROMPT_PATH       = Path(__file__).parent.parent / "Skills" / "newsletter-pet-adoption-skill_auto.md"


# ---------------------------------------------------------------------------
# 3. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if not SKILL_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Skill prompt not found at {SKILL_PROMPT_PATH}.")
    prompt = SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    print(f"Loaded skill prompt ({len(prompt)} chars)")
    return prompt

# ---------------------------------------------------------------------------
# 4. LOAD PREVIOUSLY APPROVED URLS
# ---------------------------------------------------------------------------
approved_urls = get_approved_pet_urls()

# ---------------------------------------------------------------------------
# 5. FETCH PETS FROM RESCUEGROUPS API
# ---------------------------------------------------------------------------

def extract_url_from_description(description: str) -> str:
    """Extract the first https URL from the description text."""
    urls = re.findall(r'https?://[^\s<>"]+', description)
    # Filter out tracker and facebook URLs
    for url in urls:
        if "tracker.rescuegroups" not in url and "facebook.com" not in url:
            return url.rstrip('/')
    return ""
    
def fetch_rescuegroups(species: str, excluded_urls: set, anchor_zip: str, radius_miles: int, target: int = 5) -> list[dict]:
    print(f"\n--- Fetching {species}s from RescueGroups API ---")

    url = f"https://api.rescuegroups.org/v5/public/animals/search/available/{species.lower()}s/"

    headers = {
        "Authorization": RESCUEGROUPS_API_KEY,
        "Content-Type": "application/vnd.api+json"
    }

    payload = {
        "data": {
            "filterRadius": {
            "postalcode": anchor_zip,
            "miles": radius_miles
            }
        },
        "fields": {
            "orgs": ["name", "street", "city", "state", "postalcode", "phone", "email", "url", "adoptionProcess"]
        },
        "include": ["pictures", "orgs"]
    }

    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            if response.status_code != 200:
                print(f"Status: {response.status_code} | {response.text[:200]}")
                return []
            data = response.json()
            break
        except requests.exceptions.ReadTimeout:
            print(f"  Timeout on attempt {attempt + 1}, retrying...")
            time.sleep(5)
    else:
        print(f"  Failed after 3 attempts")
        return []

    animals  = data.get("data", [])
    included = data.get("included", [])
    
    # Build org lookup from included data
    org_lookup = {}
    photo_lookup = {}
    
    for item in included:
        if item.get("type") == "orgs":
            org_id     = item["id"]
            item_attrs = item.get("attributes", {})
            org_lookup[org_id] = {
                "name":            item_attrs.get("name", ""),
                "address":         f"{item_attrs.get('street', '')} {item_attrs.get('city', '')} {item_attrs.get('state', '')} {item_attrs.get('postalcode', '')}".strip(),
                "phone":           item_attrs.get("phone", ""),
                "email":           item_attrs.get("email", ""),
                "hours":           item_attrs.get("hours", ""),
                "url":             item_attrs.get("url", ""),
                "adoptionProcess": item_attrs.get("adoptionProcess", "")
            }
        if item.get("type") == "pictures":
            pic_id     = item["id"]
            item_attrs = item.get("attributes", {})
            pic_url    = item_attrs.get("large") if isinstance(item_attrs.get("large"), str) else item_attrs.get("large", {}).get("url", "")
            if not pic_url:
                pic_url = item_attrs.get("original", "") or item_attrs.get("small", "")
            if pic_url:
                photo_lookup[pic_id] = pic_url

    for org_id, org_data in org_lookup.items():
        print(f"  Org {org_id}: {org_data}")
        break  # just first org
    
    pets = []
    for animal in animals:
        if len(pets) >= target:
            break
    
        attrs     = animal.get("attributes", {})
        relations = animal.get("relationships", {})
        animal_id = animal.get("id", "")
        
        description = attrs.get("descriptionText") or attrs.get("descriptionHtml", "")
        if not description or len(description.strip()) < 50:
            continue
        
        org_id   = relations.get("orgs", {}).get("data", [{}])[0].get("id", "") if relations.get("orgs", {}).get("data") else ""
        org_info = org_lookup.get(org_id, {})
        
        desc_html  = attrs.get("descriptionHtml", "")
        desc_url   = extract_url_from_description(desc_html)
        org_url    = org_info.get("url", "")
        source_url = f"https://rescuegroups.org/animals/detail/{animal_id}/"
        listing_url = org_url or desc_url or f"https://www.google.com/search?q={org_info.get('name', '').replace(' ', '+')}+adopt+{attrs.get('name', '').replace(' ', '+')}"
        
        if source_url in excluded_urls:
            print(f"  Skipping previously approved: {source_url}")
            continue
            
        photo_ids = [p.get("id") for p in relations.get("pictures", {}).get("data", [])]
        photos    = [photo_lookup[pid] for pid in photo_ids if pid in photo_lookup][:3]
        print(f"  Photo lookup size: {len(photo_lookup)}")
        print(f"  First animal photo_ids: {photo_ids[:3]}")
        print(f"  First animal photos: {photos[:1]}")
        print(f"  Photo lookup size: {len(photo_lookup)}")
        print(f"  Org lookup size: {len(org_lookup)}")
    
        profile = f"""
    Name: {attrs.get('name', 'Unknown')}
    Species: {species}
    Breed: {attrs.get('breedPrimary', '')}
    Age: {attrs.get('ageString', '')}
    Gender: {attrs.get('sex', '')}
    Size: {attrs.get('sizeGroup', '')}
    Description: {description}
    Good with kids: {attrs.get('isKidsOk', 'Unknown')}
    Good with dogs: {attrs.get('isDogsOk', 'Unknown')}
    Good with cats: {attrs.get('isCatsOk', 'Unknown')}
    House trained: {attrs.get('isHousetrained', 'Unknown')}
    Spayed/Neutered: {attrs.get('isAltered', 'Unknown')}
    Vaccinated: {attrs.get('isCurrentVaccinations', 'Unknown')}
    Shelter: {org_info.get('name', '')}
    Address: {org_info.get('address', '')}
    Phone: {org_info.get('phone', '')}
    Email: {org_info.get('email', '')}
    Hours: {org_info.get('hours', '')}
    """.strip()
        
        pets.append({
        "url":         source_url,        # unique per pet (for internal tracking)
        "listing_url": org_url,           # org website (for readers to click)
        "profile":     profile,
        "photos":      photos,
        "animal_type": species.lower(),
        "org_info":    org_info
        })

        print(f"  ✓ {attrs.get('name', 'Unknown')} | {org_info.get('name', 'Unknown org')} | {len(photos)} photos")
    
    print(f"RescueGroups {species}s: {len(pets)} with descriptions")
    return pets

# ---------------------------------------------------------------------------
# 6. BUILD COMBINED PROFILES
# ---------------------------------------------------------------------------
def build_combined_profiles(pets: list[dict]) -> str:
    combined = ""
    for i, pet in enumerate(pets, 1):
        combined += f"""
--- Pet {i} ---
Source URL: {pet['url']}
Photos: {', '.join(pet['photos'][:2]) if pet['photos'] else 'None'}
Profile:
{pet['profile'][:2000]}

"""
    return combined

# ---------------------------------------------------------------------------
# 7. GENERATE BLURBS VIA CLAUDE
# ---------------------------------------------------------------------------
def generate_blurb(pets: list[dict], skill_prompt: str, animal_type: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    combined_profiles = build_combined_profiles(pets)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=skill_prompt,
        messages=[{
            "role": "user",
            "content": f"""
Here are adoptable {animal_type}s from shelters near East Cobb, GA.
Pick the TOP 3 with the best story potential and write a blurb for each.
Use the pet's actual description -- do not invent details.

Return ONLY a JSON array with exactly 3 objects, no preamble or markdown.
Exact format:
[
  {{
    "pet_name": "Name",
    "shelter_name": "Shelter Name",
    "blurb": "Full blurb text here...",
    "shelter_address": "address",
    "shelter_phone": "phone",
    "shelter_email": "email",
    "shelter_hours": "hours",
    "source_url": "https://...",
    "photo_url": "https://... or null",
    "animal_type": "{animal_type}"
  }},
  {{...}},
  {{...}}
]

{combined_profiles}
"""
        }]
    )

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    results = json.loads(clean)
    
    # Map photo and listing_url back from original scraped data
    photo_map   = {p["url"]: p["photos"][0] if p["photos"] else "" for p in pets}
    listing_map = {p["url"]: p.get("listing_url", "") for p in pets}
    
    for result in results:
        result["photo_url"]   = photo_map.get(result["source_url"], "")
        result["listing_url"] = listing_map.get(result["source_url"], "")
    
    print(f"Generated {len(results)} {animal_type} blurbs")
    return results

    
# ---------------------------------------------------------------------------
# 8. SCORE ALL BLURBS IN ONE CLAUDE CALL
# ---------------------------------------------------------------------------
def score_blurbs(results: list[dict]) -> list[dict]:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    scoring_input = ""
    for i, result in enumerate(results, 1):
        scoring_input += f"""
--- Candidate {i} ({result.get('animal_type', 'unknown')}) ---
Pet Name: {result['pet_name']}
Shelter: {result['shelter_name']}
Blurb: {result['blurb']}
Source URL: {result['source_url']}

"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""
You are evaluating pet adoption blurbs for a local newsletter editor.
Score each candidate on a 0-10 scale for each criteria:

1. Adoptability: How easy and appealing is this pet to adopt?
2. Interesting Story: How compelling and unique is the backstory?
3. Time at Shelter: Longer wait = higher score based on clues in the blurb.

Return ONLY a JSON array with no preamble or markdown. Exact format:
[
  {{
    "pet_name": "Name",
    "source_url": "https://...",
    "adoptability_score": 8,
    "story_score": 7,
    "shelter_time_score": 5,
    "total_score": 20,
    "scoring_notes": "• Strong adoption candidate\\n• Compelling backstory\\n• Has been waiting a long time"
  }}
]

Rules for scoring_notes:
- Exactly 3 bullet points per candidate
- Format: • [point]\\n• [point]\\n• [point]
- Each bullet is a concise exec-level reason why this pet should be featured
- Focus on newsletter appeal, reader connection, and urgency to adopt

Candidates to score:
{scoring_input}
"""
        }]
    )

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    scores = json.loads(clean)

    score_map = {s["source_url"]: s for s in scores}
    for result in results:
        score_data = score_map.get(result["source_url"], {})
        result["adoptability_score"] = score_data.get("adoptability_score", 0)
        result["story_score"]        = score_data.get("story_score", 0)
        result["shelter_time_score"] = score_data.get("shelter_time_score", 0)
        result["total_score"]        = score_data.get("total_score", 0)
        result["scoring_notes"]      = score_data.get("scoring_notes", "")

    for r in results:
        print(f"  {r['pet_name']} ({r.get('animal_type','?')}): {r['total_score']}/30")

    return results

# ---------------------------------------------------------------------------
# 9. FLAG DEFAULT WINNERS
# ---------------------------------------------------------------------------


#-----------getting week number for even/odd weeks-------------
def get_week_number():
    now = datetime.today()
    start_of_year = datetime(now.year, 1, 1)
    # Exact match of JavaScript formula:
    # Math.ceil(((now - startOfYear) / 86400000 + startOfYear.getDay() + 1) / 7)
    days_diff = (now - start_of_year).total_seconds() / 86400
    jan1_weekday = start_of_year.weekday()
    # JavaScript getDay() is 0=Sunday, Python weekday() is 0=Monday
    # Convert Python weekday to JS getDay()
    jan1_js_day = (jan1_weekday + 1) % 7
    week_num = math.ceil((days_diff + jan1_js_day + 1) / 7)
    return week_num
    
#-----------selecting default winners-------------
def flag_default_winners(cat_results: list[dict], dog_results: list[dict]) -> tuple[list[dict], list[dict]]:
    print(f"Cat results: {[r['pet_name'] for r in cat_results]}")
    print(f"Dog results: {[r['pet_name'] for r in dog_results]}")
    
    week_number = get_week_number()
    odd_week    = week_number % 2 != 0
    print(f"Week number: {week_number} | Odd: {odd_week}")

    cat_results.sort(key=lambda x: x["total_score"], reverse=True)
    dog_results.sort(key=lambda x: x["total_score"], reverse=True)

    # Initialize all flags to empty
    for r in cat_results:
        r["cat_default"]    = ""
        r["dog_default"]    = ""
        r["default_winner"] = ""

    for r in dog_results:
        r["cat_default"]    = ""
        r["dog_default"]    = ""
        r["default_winner"] = ""

    # Flag top cat
    if cat_results:
        cat_results[0]["cat_default"] = "yes"
        print(f"Cat default: {cat_results[0]['pet_name']} ({cat_results[0]['total_score']}/30)")

    # Flag top dog
    if dog_results:
        dog_results[0]["dog_default"] = "yes"
        print(f"Dog default: {dog_results[0]['pet_name']} ({dog_results[0]['total_score']}/30)")

    # Flag overall default winner based on week number
    if odd_week and cat_results:
        cat_results[0]["default_winner"] = "yes"
        print(f"Week {week_number} (odd) — overall default: {cat_results[0]['pet_name']} (cat)")
    elif not odd_week and dog_results:
        dog_results[0]["default_winner"] = "yes"
        print(f"Week {week_number} (even) — overall default: {dog_results[0]['pet_name']} (dog)")

    return cat_results, dog_results

# ---------------------------------------------------------------------------
# 10. SAVE TO GOOGLE SHEETS
# ---------------------------------------------------------------------------
save_pets_to_notion(final_results, newsletter["name"])

# ---------------------------------------------------------------------------
# 11. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting newsletter automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt  = load_skill_prompt()
    approved_urls = get_approved_pet_urls()

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']}")
        print(f"{'='*60}")

        # Fetch cats and dogs
        all_cats = fetch_rescuegroups("Cat", approved_urls, newsletter["zip"], newsletter["radius"], target=5)
        all_dogs = fetch_rescuegroups("Dog", approved_urls, newsletter["zip"], newsletter["radius"], target=5)

        print(f"\nTotal cats: {len(all_cats)}")
        print(f"Total dogs: {len(all_dogs)}")

        if not all_cats and not all_dogs:
            print(f"No pets found for {newsletter['name']}. Skipping.")
            continue

        # Generate blurbs in parallel
        cat_results, dog_results = [], []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            if all_cats:
                futures["cat"] = executor.submit(generate_blurb, all_cats, skill_prompt, "cat")
            if all_dogs:
                futures["dog"] = executor.submit(generate_blurb, all_dogs, skill_prompt, "dog")
            if "cat" in futures:
                cat_results = futures["cat"].result()
            if "dog" in futures:
                dog_results = futures["dog"].result()

        # Score all in one call
        all_results = cat_results + dog_results
        print(f"\nScoring all {len(all_results)} candidates for {newsletter['name']}...")
        all_results = score_blurbs(all_results)

        # Split back by type
        cat_results = [r for r in all_results if r.get("animal_type") == "cat"]
        dog_results = [r for r in all_results if r.get("animal_type") == "dog"]

        # Flag default winners
        cat_results, dog_results = flag_default_winners(cat_results, dog_results)

        # Save to Google Sheets
        final_results = cat_results + dog_results
        save_pets_to_notion(final_results, newsletter["name"])
        print(f"Done with {newsletter['name']}. Saved {len(final_results)} rows.")

    print(f"\nAll newsletters complete.")
