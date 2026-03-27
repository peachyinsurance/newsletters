#!/usr/bin/env python3
"""
Newsletter Automation - Pet Adoption Section
Uses RescueGroups.org API to find adoptable cats and dogs near East Cobb,
generates blurbs via Claude, scores them, flags defaults,
and writes results to Google Sheets.
"""

import os
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import anthropic
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

NEWSLETTER_NAME     = "East_Cobb_Connect"
ANCHOR_ZIP          = "30062"
SEARCH_RADIUS_MILES = 25

RESCUEGROUPS_API_KEY = os.environ["RESCUE_GROUP_API_KEY"]
print(f"API key loaded: {RESCUEGROUPS_API_KEY[:5]}...")  # shows first 5 chars only

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT
# ---------------------------------------------------------------------------
CLAUDE_API_KEY          = os.environ["CLAUDE_API_KEY"]
RESCUEGROUPS_API_KEY    = os.environ["RESCUE_GROUP_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GSHEET_ID               = os.environ["GSHEET_ID"]
GSHEET_TAB              = "Pets"
SKILL_PROMPT_PATH       = Path(__file__).parent.parent / "Skills" / "newsletter-pet-adoption-skill_auto.md"

# ---------------------------------------------------------------------------
# 2. GOOGLE AUTH
# ---------------------------------------------------------------------------
creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets_service = build("sheets", "v4", credentials=creds)

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
def get_approved_urls() -> set[str]:
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=GSHEET_ID,
        range=f"{GSHEET_TAB}!A:K"
    ).execute()
    rows = result.get("values", [])
    approved = set()
    for row in rows[1:]:
        if len(row) >= 11 and row[10] == "approved":
            approved.add(row[0])
    print(f"Loaded {len(approved)} previously approved URLs to exclude")
    return approved

# ---------------------------------------------------------------------------
# 5. FETCH PETS FROM RESCUEGROUPS API
# ---------------------------------------------------------------------------
def fetch_rescuegroups(species: str, excluded_urls: set, target: int = 5) -> list[dict]:
    print(f"\n--- Fetching {species}s from RescueGroups API ---")

    # Use views in URL: available + cats or dogs
    url = f"https://api.rescuegroups.org/v5/public/animals/search/available/{species.lower()}s/"

    headers = {
    "Authorization": RESCUEGROUPS_API_KEY,
    "Content-Type": "application/vnd.api+json"
    }

    payload = {
        "data": {
            "filterRadius": {
                "miles": SEARCH_RADIUS_MILES,
                "postalcode": ANCHOR_ZIP
            }
        }
    }

    params = {
        "limit": 50,
        "include[]": ["pictures", "orgs"]
    }

    try:
        response = requests.post(url, headers=headers, json=payload, params=params, timeout=30)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text[:500]}")
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        return []
    except Exception as e:
        print(f"RescueGroups API error: {e}")
        return []

    animals  = data.get("data", [])
    included = data.get("included", [])
    print(f"Found {len(animals)} {species}s within {SEARCH_RADIUS_MILES} miles of {ANCHOR_ZIP}")


    
    # Build org lookup from included data
    org_lookup = {}
    photo_lookup = {}
    for item in included:
        if item.get("type") == "orgs":
            org_id = item["id"]
            attrs  = item.get("attributes", {})
            org_lookup[org_id] = {
                "name":    attrs.get("name", ""),
                "address": f"{attrs.get('street', '')} {attrs.get('city', '')} {attrs.get('state', '')} {attrs.get('postalcode', '')}".strip(),
                "phone":   attrs.get("phone", ""),
                "email":   attrs.get("email", ""),
                "hours":   attrs.get("hours", "")
            }
        if item.get("type") == "pictures":
            pic_id  = item["id"]
            pic_url = item.get("attributes", {}).get("large", {}).get("url", "")
            if pic_url:
                photo_lookup[pic_id] = pic_url
    
    pets = []
    for animal in animals:
        if len(pets) >= target:
            break

        attrs = animal.get("attributes", {})
        print(f"  Animal URL field: {attrs.get('url', 'NO URL FIELD')}")
        print(f"  Animal slug: {attrs.get('slug', 'NO SLUG')}")
        attrs     = animal.get("attributes", {})
        relations = animal.get("relationships", {})

        animal_id  = animal.get("id", "")

        source_url = attrs.get("url") or f"https://www.rescuegroups.org/animals/detail/{animal_id}/"
        
        if source_url in excluded_urls:
            print(f"  Skipping previously approved: {source_url}")
            continue

        description = attrs.get("descriptionText") or attrs.get("descriptionHtml", "")
        if not description or len(description.strip()) < 50:
            continue

        org_id   = relations.get("orgs", {}).get("data", [{}])[0].get("id", "") if relations.get("orgs", {}).get("data") else ""
        org_info = org_lookup.get(org_id, {})

        photo_ids = [p.get("id") for p in relations.get("pictures", {}).get("data", [])]
        photos    = [photo_lookup[pid] for pid in photo_ids if pid in photo_lookup][:3]

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
            "url":         source_url,
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
def flag_default_winners(cat_results: list[dict], dog_results: list[dict]) -> tuple[list[dict], list[dict]]:
    week_number = datetime.today().isocalendar()[1]
    odd_week    = week_number % 2 != 0

    cat_results.sort(key=lambda x: x["total_score"], reverse=True)
    dog_results.sort(key=lambda x: x["total_score"], reverse=True)

    for r in cat_results + dog_results:
        r["cat_default"]    = ""
        r["dog_default"]    = ""
        r["default_winner"] = ""

    if cat_results:
        cat_results[0]["cat_default"] = "yes"
        print(f"Cat default: {cat_results[0]['pet_name']} ({cat_results[0]['total_score']}/30)")

    if dog_results:
        dog_results[0]["dog_default"] = "yes"
        print(f"Dog default: {dog_results[0]['pet_name']} ({dog_results[0]['total_score']}/30)")

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
def save_to_sheets(results: list[dict]) -> None:
    rows = []
    for data in results:
        rows.append([
            data["source_url"],
            data["pet_name"],
            data["shelter_name"],
            data["blurb"],
            data["shelter_address"],
            data["shelter_phone"],
            data["shelter_email"],
            data["shelter_hours"],
            data.get("photo_url") or "",
            datetime.today().strftime("%Y-%m-%d"),
            "pending",
            "pet_blurb",
            NEWSLETTER_NAME,
            data.get("total_score", ""),
            data.get("adoptability_score", ""),
            data.get("story_score", ""),
            data.get("shelter_time_score", ""),
            data.get("scoring_notes", ""),
            data.get("default_winner", ""),
            data.get("cat_default", ""),
            data.get("dog_default", ""),
            data.get("animal_type", "")
        ])
    sheets_service.spreadsheets().values().append(
        spreadsheetId=GSHEET_ID,
        range=f"{GSHEET_TAB}!A:V",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()
    print(f"Saved {len(rows)} rows to Google Sheets")

# ---------------------------------------------------------------------------
# 11. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting newsletter automation — {datetime.today().strftime('%Y-%m-%d')}")

    skill_prompt  = load_skill_prompt()
    approved_urls = get_approved_urls()

    # Fetch cats and dogs from RescueGroups
    all_cats = fetch_rescuegroups("Cat", approved_urls, target=5)
    all_dogs = fetch_rescuegroups("Dog", approved_urls, target=5)

    print(f"\nTotal cats: {len(all_cats)}")
    print(f"Total dogs: {len(all_dogs)}")

    if not all_cats and not all_dogs:
        print("No pets found. Exiting.")
        exit(1)

    # Generate blurbs in parallel
    print("\nGenerating blurbs in parallel...")
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

    # Score all in one Claude call
    all_results = cat_results + dog_results
    print(f"\nScoring all {len(all_results)} candidates...")
    all_results = score_blurbs(all_results)

    # Split back by type
    cat_results = [r for r in all_results if r.get("animal_type") == "cat"]
    dog_results = [r for r in all_results if r.get("animal_type") == "dog"]

    # Flag default winners
    cat_results, dog_results = flag_default_winners(cat_results, dog_results)

    # Save to Google Sheets
    final_results = cat_results + dog_results
    save_to_sheets(final_results)

    print(f"\nDone. Saved {len(final_results)} total rows.")
