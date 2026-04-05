#!/usr/bin/env python3
"""
Newsletter Automation - Pet Adoption Section
Scrapes Petfinder via Apify to find adoptable cats and dogs near each newsletter area,
generates blurbs via Claude, scores them, flags defaults,
and saves results to Notion.
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
        "state":  "ga",
    },
    {
        "name":   "Perimeter_Post",
        "zip":    "30346",
        "state":  "ga",
    },
]

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT
# ---------------------------------------------------------------------------
CLAUDE_API_KEY    = os.environ["CLAUDE_API_KEY"]
APIFY_API_KEY     = os.environ["APIFY_API_KEY"]
SKILL_PROMPT_PATH = Path(__file__).parent.parent / "Skills" / "newsletter-pet-adoption-skill_auto.md"

APIFY_SCRAPER_TIMEOUT = 120  # seconds


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
# 5. FETCH PETS FROM PETFINDER VIA APIFY
# ---------------------------------------------------------------------------

def fetch_html_apify(url: str, retries: int = 2) -> str | None:
    """Fetch a page's rendered HTML via Apify web-scraper."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APIFY_API_KEY}",
    }
    for attempt in range(retries):
        try:
            res = requests.post(
                "https://api.apify.com/v2/acts/apify~web-scraper/run-sync-get-dataset-items",
                headers=headers,
                json={
                    "startUrls": [{"url": url}],
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
                timeout=APIFY_SCRAPER_TIMEOUT,
            )
            if res.status_code not in (200, 201):
                print(f"  Apify error {res.status_code}: {res.text[:200]}")
                if attempt < retries - 1:
                    time.sleep(3)
                    continue
                return None
            items = res.json()
            if items and len(items) > 0:
                return items[0].get("html")
            return None
        except requests.exceptions.ReadTimeout:
            print(f"  Timeout on attempt {attempt + 1} for {url}")
            if attempt < retries - 1:
                time.sleep(3)
    return None


def parse_petfinder_html(html: str, species: str) -> list[dict]:
    """Parse Petfinder search results HTML into pet dicts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    pets = []

    # Try __NEXT_DATA__ first (Next.js embedded JSON with full pet data)
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag:
        try:
            next_data = json.loads(next_data_tag.string)
            # Navigate Next.js data structure
            page_props = next_data.get("props", {}).get("pageProps", {})
            # Try multiple possible paths for animal data
            animals = []
            for path in [
                page_props.get("searchData", {}).get("animals", []),
                page_props.get("animals", []),
                page_props.get("initialState", {}).get("search", {}).get("animals", []),
            ]:
                if path:
                    animals = path
                    break

            print(f"  __NEXT_DATA__: found {len(animals)} animals")
            for a in animals:
                pet_path = a.get("url", "")
                pet_url = f"https://www.petfinder.com{pet_path}" if pet_path and not pet_path.startswith("http") else pet_path

                photos = []
                for p in (a.get("photos") or []):
                    photo_url = p.get("large") or p.get("full") or p.get("medium") or p.get("small") or ""
                    if photo_url:
                        photos.append(photo_url)
                if not photos:
                    crop = a.get("primary_photo_cropped") or {}
                    if isinstance(crop, dict):
                        photo_url = crop.get("large") or crop.get("full") or crop.get("medium") or crop.get("small") or ""
                        if photo_url:
                            photos.append(photo_url)

                contact = a.get("contact", {})
                org_addr = contact.get("address", {})
                address_parts = [org_addr.get("address1", ""), org_addr.get("city", ""),
                                 org_addr.get("state", ""), org_addr.get("postcode", "")]
                address_str = " ".join(p for p in address_parts if p).strip()

                pets.append({
                    "name":        a.get("name", ""),
                    "url":         pet_url,
                    "species":     a.get("species", species),
                    "breed":       (a.get("breeds") or {}).get("primary", ""),
                    "age":         a.get("age", ""),
                    "gender":      a.get("gender", ""),
                    "size":        a.get("size", ""),
                    "description": a.get("description", ""),
                    "photos":      photos,
                    "org_name":    a.get("organization_id", ""),
                    "org_address": address_str,
                    "org_phone":   contact.get("phone", ""),
                    "org_email":   contact.get("email", ""),
                })
            if pets:
                return pets
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  __NEXT_DATA__ parse error: {e}")

    # Fallback: parse DOM with BeautifulSoup
    print("  Falling back to DOM parsing...")
    cards = soup.select('a[href*="/cat/"], a[href*="/dog/"]')
    for card in cards:
        href = card.get("href", "")
        if not href or "/search/" in href:
            continue
        pet_url = f"https://www.petfinder.com{href}" if href.startswith("/") else href
        name_el = card.select_one("h2, h3, [class*='name'], [class*='Name']")
        breed_el = card.select_one("[class*='breed'], [class*='Breed']")
        img_el = card.select_one("img")
        name = name_el.get_text(strip=True) if name_el else ""
        breed = breed_el.get_text(strip=True) if breed_el else ""
        photo = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""

        if name:
            pets.append({
                "name": name, "url": pet_url, "species": species,
                "breed": breed, "age": "", "gender": "", "size": "",
                "description": "", "photos": [photo] if photo else [],
                "org_name": "", "org_address": "", "org_phone": "", "org_email": "",
            })

    print(f"  DOM parsing found {len(pets)} pets")
    return pets


def fetch_petfinder_apify(species: str, excluded_urls: set, state: str, zip_code: str, target: int = 5) -> list[dict]:
    """Scrape Petfinder via Apify and return pet profiles."""
    print(f"\n--- Fetching {species}s from Petfinder via Apify ---")

    search_url = f"https://www.petfinder.com/search/{species.lower()}s-for-adoption/us/{state}/{zip_code}/"
    html = fetch_html_apify(search_url)
    if not html:
        print(f"  Failed to fetch HTML for {search_url}")
        return []
    print(f"  Got {len(html)} chars of HTML")
    raw_items = parse_petfinder_html(html, species.lower())

    pets = []
    for item in raw_items:
        if len(pets) >= target:
            break

        # Build the Petfinder listing URL (source of truth for dedup)
        pet_url = item.get("url") or item.get("petfinderUrl") or ""
        if not pet_url:
            continue

        # Normalize URL for dedup
        source_url = pet_url.rstrip("/")

        if source_url in excluded_urls:
            print(f"  ✗ Skipping previously approved: {source_url}")
            continue

        name        = item.get("name") or item.get("petName") or "Unknown"
        description = item.get("description") or ""
        if not description or len(description.strip()) < 30:
            continue

        breed   = item.get("breed") or item.get("breedPrimary") or item.get("breeds", {}).get("primary", "") or ""
        age     = item.get("age") or ""
        gender  = item.get("gender") or item.get("sex") or ""
        size    = item.get("size") or ""

        # Photos
        photos_raw = item.get("photos") or item.get("images") or []
        photos = []
        for p in photos_raw[:3]:
            if isinstance(p, str):
                photos.append(p)
            elif isinstance(p, dict):
                photos.append(p.get("large") or p.get("full") or p.get("medium") or p.get("small") or "")

        # Organization / shelter info
        org = item.get("organization") or item.get("shelter") or {}
        if isinstance(org, str):
            org = {"name": org}
        org_name    = org.get("name") or item.get("organizationName") or item.get("shelterName") or ""
        org_address = org.get("address") or ""
        if isinstance(org_address, dict):
            parts = [org_address.get("address1", ""), org_address.get("city", ""),
                     org_address.get("state", ""), org_address.get("postcode", "")]
            org_address = " ".join(p for p in parts if p).strip()
        org_phone = org.get("phone") or item.get("phone") or ""
        org_email = org.get("email") or item.get("email") or ""

        org_info = {
            "name":    org_name,
            "address": org_address,
            "phone":   org_phone,
            "email":   org_email,
            "hours":   "",
        }

        listing_url = source_url

        profile = f"""
Name: {name}
Species: {species}
Breed: {breed}
Age: {age}
Gender: {gender}
Size: {size}
Description: {description}
Shelter: {org_name}
Address: {org_address}
Phone: {org_phone}
Email: {org_email}
""".strip()

        pets.append({
            "url":         source_url,
            "listing_url": listing_url,
            "profile":     profile,
            "photos":      [p for p in photos if p],
            "animal_type": species.lower(),
            "org_info":    org_info,
        })

        print(f"  ✓ {name} | {org_name} | {len(photos)} photos")

    print(f"Petfinder {species}s: {len(pets)} with descriptions")
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

        # Fetch cats and dogs from Petfinder via Apify
        all_cats = fetch_petfinder_apify("Cat", approved_urls, newsletter["state"], newsletter["zip"], target=5)
        all_dogs = fetch_petfinder_apify("Dog", approved_urls, newsletter["state"], newsletter["zip"], target=5)

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
