#!/usr/bin/env python3
"""
Newsletter Automation - Pet Adoption Section
Scrapes Petfinder via Apify to find adoptable cats and dogs near each newsletter area,
generates blurbs via Claude, scores them, flags defaults,
and saves results to Notion.
"""

import os
import sys
import re
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))

import requests
import anthropic
from notion_helper import get_approved_pet_urls, save_pets_to_notion
from url_validator import filter_valid_items


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
CLAUDE_API_KEY      = os.environ["CLAUDE_API_KEY"]
RESCUEGROUPS_API_KEY = os.environ.get("RESCUE_GROUP_API_KEY", "")
SKILL_PROMPT_PATH    = Path(__file__).parent.parent.parent / "Skills" / "newsletter-pet-adoption-skill_auto.md"

RESCUEGROUPS_API_BASE = "https://api.rescuegroups.org/v5/public"
RESCUEGROUPS_SEARCH_RADIUS_MILES = 25
RESCUEGROUPS_PAGE_LIMIT = 8   # how many pages to fetch (8 × 25 = 200 candidates)
RESCUEGROUPS_TIMEOUT = 30


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
# Petfinder migrated to client-side rendering + TLS fingerprinting that blocks
# server-side scrapes. Replaced with RescueGroups public API (free, structured
# data: real bios, multi-photo carousel, shelter contact). Only orgs that
# syndicate to RescueGroups are accessible — Mostly Mutts and Barkville for now.
# Custom scrapers for non-RescueGroups shelters (e.g. Fulton County) added
# later as a separate fallback.

from concurrent.futures import ThreadPoolExecutor, as_completed

# Per-species priority list of orgs (and per-org settings).
# Search proceeds in order until target is reached. URL strategy is shelter-specific.
ORG_PLAN = {
    "Cat": [
        {"name_filter": "Mostly Mutts",
         "url_template": "https://mostlymutts.org/adopt/adoptable-cats/{slug}"},
    ],
    "Dog": [
        {"name_filter": "Mostly Mutts",
         "url_template": "https://mostlymutts.org/adopt/adoptable-dogs/{slug}"},
        {"name_filter": "Barkville",
         "url_template": "https://www.barkvilledogrescue.org/adoptabledogs"},
    ],
}

# How many valid candidates to collect per species (per newsletter)
TARGET_PER_SPECIES = 3
# Max API pages to scan per org before giving up (each page = 25)
MAX_PAGES_PER_ORG = 6


def _slugify_name(name: str) -> str:
    """Lowercase, replace non-alphanumerics with hyphens. 'Holly Hobby' → 'holly-hobby'."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip()).strip("-")


def _species_from_slug(rg_slug: str) -> str:
    """Infer species from RescueGroups' animal slug. e.g. 'adopt-jacquelyn-...-dog'."""
    s = (rg_slug or "").lower()
    if s.endswith("-cat") or "-cat-" in s:
        return "Cat"
    if s.endswith("-dog") or "-dog-" in s:
        return "Dog"
    return ""


def _build_pet_url(template: str, pet_name: str) -> str:
    """Render the per-shelter URL template with the pet's name slug.
    For listing-page templates (no `{slug}` placeholder), returns as-is."""
    if not template:
        return ""
    if "{slug}" in template:
        return template.format(slug=_slugify_name(pet_name))
    return template


def _validate_pet_url(url: str, pet_name: str, listing_only: bool = False) -> bool:
    """Confirm the URL is reachable. For per-pet URLs we also check the page mentions
    the pet's name (so we know it isn't a generic 404-but-200 page). For listing-page
    URLs (no per-pet path), just check 200."""
    if not url:
        return False
    try:
        r = requests.get(url, timeout=8, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        if listing_only:
            return True
        return (pet_name or "").lower() in r.text.lower()
    except Exception:
        return False


def _rescuegroups_post(body: dict, params: dict) -> dict:
    """Wrap the API call. Returns parsed JSON, or {} on error."""
    if not RESCUEGROUPS_API_KEY:
        print("  ⚠ RESCUE_GROUP_API_KEY not set")
        return {}
    try:
        r = requests.post(
            f"{RESCUEGROUPS_API_BASE}/animals/search/available",
            headers={"Authorization": RESCUEGROUPS_API_KEY,
                     "Content-Type": "application/vnd.api+json"},
            params=params, json=body, timeout=RESCUEGROUPS_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"  ⚠ RescueGroups API {r.status_code}: {r.text[:150]}")
            return {}
        return r.json()
    except Exception as e:
        print(f"  ⚠ RescueGroups API error: {e}")
        return {}


def _rg_pet_to_pipeline_dict(animal: dict, included_index: dict, species: str,
                              public_url: str) -> dict:
    """Convert one RescueGroups animal record → our pipeline pet dict."""
    attr = animal.get("attributes", {}) or {}
    rels = animal.get("relationships", {}) or {}

    name        = attr.get("name") or "Unknown"
    breed       = attr.get("breedString") or attr.get("breedPrimary") or ""
    age         = attr.get("ageGroup") or ""
    gender      = attr.get("sex") or ""
    size        = attr.get("sizeGroup") or ""
    description = attr.get("descriptionText") or ""

    # Photos via included resources. Some picture fields can be nested dicts
    # (e.g. {'url': '...', 'width': N}), so coerce to a plain string before adding.
    photos: list[str] = []
    seen: set[str] = set()
    for pr in rels.get("pictures", {}).get("data") or []:
        pic = included_index.get((pr.get("type"), str(pr.get("id"))))
        if not pic:
            continue
        pa = pic.get("attributes") or {}
        u = None
        for key in ("url", "large", "original", "medium"):
            v = pa.get(key)
            if isinstance(v, str):
                u = v
                break
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                u = v["url"]
                break
        if u and u not in seen:
            seen.add(u)
            photos.append(u)
    photos = photos[:3]

    # Org info via included resources
    org_attr = {}
    for og in rels.get("orgs", {}).get("data") or []:
        org = included_index.get((og.get("type"), str(og.get("id"))))
        if org:
            org_attr = org.get("attributes") or {}
            break

    org_info = {
        "name":    org_attr.get("name") or "",
        "address": org_attr.get("addressLine1") or "",
        "phone":   org_attr.get("phone") or "",
        "email":   org_attr.get("email") or "",
        "hours":   "",
    }

    profile = (
        f"Name: {name}\n"
        f"Species: {species}\n"
        f"Breed: {breed}\n"
        f"Age: {age}\n"
        f"Gender: {gender}\n"
        f"Size: {size}\n"
        f"Description: {description}\n"
        f"Shelter: {org_info['name']}\n"
        f"Address: {org_info['address']}\n"
        f"Phone: {org_info['phone']}\n"
        f"Email: {org_info['email']}"
    )

    return {
        "url":         public_url,
        "listing_url": public_url,
        "profile":     profile,
        "photos":      photos,
        "animal_type": species.lower(),
        "org_info":    org_info,
        "_rg_id":      animal.get("id"),
        "_pet_name":   name,
    }


def fetch_pets_via_rescuegroups(species: str, excluded_urls: set,
                                target: int = TARGET_PER_SPECIES) -> list[dict]:
    """Fetch up to `target` validated pets of `species` from the configured ORG_PLAN.
    Walks orgs in order, paginates the API, builds each pet's public URL using the
    shelter's pattern, and validates each URL is reachable + mentions the pet's name
    (or just reachable for listing-page templates)."""
    print(f"\n--- Fetching {species}s from RescueGroups ---")
    valid: list[dict] = []
    species_suffix = f"-{species.lower()}"

    for org_cfg in ORG_PLAN.get(species, []):
        if len(valid) >= target:
            break
        org_filter = org_cfg["name_filter"]
        url_template = org_cfg["url_template"]
        listing_only = "{slug}" not in url_template
        print(f"  → {org_filter}")

        for page in range(1, MAX_PAGES_PER_ORG + 1):
            if len(valid) >= target:
                break
            body = {"data": {"filters": [
                {"fieldName": "orgs.name", "operation": "contains", "criteria": org_filter},
            ]}}
            params = {
                "include": "pictures,orgs",
                "fields[animals]":
                    "name,slug,breedString,breedPrimary,ageGroup,sex,sizeGroup,"
                    "descriptionText,pictureCount,url",
                "fields[orgs]": "name,city,state,addressLine1,phone,email,url",
                "page": str(page),
            }
            data = _rescuegroups_post(body, params)
            animals = data.get("data") or []
            included = data.get("included") or []
            inc = {(i.get("type"), str(i.get("id"))): i for i in included}
            meta = data.get("meta") or {}

            for a in animals:
                if len(valid) >= target:
                    break
                attr = a.get("attributes") or {}
                slug = (attr.get("slug") or "").lower()
                # Filter by species via slug suffix
                if not (slug.endswith(species_suffix) or f"{species_suffix}-" in slug):
                    continue
                name = attr.get("name") or ""
                if not name:
                    continue

                # Build the public URL for this pet
                public_url = _build_pet_url(url_template, name)
                # Skip duplicates / previously approved
                if public_url and public_url.rstrip("/") in excluded_urls:
                    continue

                # Validate URL is reachable
                if not _validate_pet_url(public_url, name, listing_only=listing_only):
                    print(f"    ✗ {name} | dead URL: {public_url}")
                    continue

                pet = _rg_pet_to_pipeline_dict(a, inc, species, public_url)
                # Drop pets with too-short bios (Claude can't write a real blurb)
                if len(pet["profile"]) < 80 or len(attr.get("descriptionText") or "") < 30:
                    print(f"    ✗ {name} | bio too short ({len(attr.get('descriptionText') or '')} chars)")
                    continue

                valid.append(pet)
                print(f"    ✓ {name} | {pet['org_info']['name']} | {len(pet['photos'])} photos")

            # Stop if we've exhausted available pages
            if len(animals) < (meta.get("limit") or 25):
                break

    print(f"  → {len(valid)} valid {species} candidates")
    return valid


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

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""
Here are adoptable {animal_type}s from shelters near East Cobb, GA.
Pick the TOP 3 with the best story potential and write a blurb for each.
Use the pet's actual description -- do not invent details.

AUTOMATED PIPELINE — do not return plain English, do not ask clarifying questions,
do not refuse to write. Even if a listing has thin data, write the best blurb you
can using only the verified facts (name, breed, age, gender, photo). Editorial
review happens later. Always return valid JSON.

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
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Claude API error (attempt {attempt + 1}): {e}")
                time.sleep(10 * (attempt + 1))
            else:
                raise

    raw = next((block.text for block in response.content if block.type == "text"), "")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    # If Claude returned prose before/after the JSON, try to extract the array/object
    if not (clean.startswith("[") or clean.startswith("{")):
        start = clean.find("[")
        if start < 0:
            start = clean.find("{")
        end_bracket = clean.rfind("]") if start >= 0 and clean[start] == "[" else clean.rfind("}")
        if start >= 0 and end_bracket > start:
            clean = clean[start:end_bracket + 1]
    try:
        results = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"  ✗ Failed to parse Claude JSON for {animal_type}: {e}")
        print(f"  Raw response (first 500 chars): {raw[:500]}")
        return []

    # Build source lookups
    source_by_url  = {p["url"]: p for p in pets}
    source_by_name = {p["name"]: p for p in pets if p.get("name")}

    # For each result: match to source (by url, fallback to name), then OVERWRITE URL fields.
    # Claude only provides text: pet_name, blurb, animal_type.
    validated = []
    for result in results:
        claude_url = result.get("source_url", "")
        source = source_by_url.get(claude_url)
        if not source:
            source = source_by_name.get(result.get("pet_name", ""))
            if source:
                print(f"  ⚠ Fixing hallucinated source_url for {result.get('pet_name', '?')}: {claude_url} → {source['url']}")
            else:
                print(f"  ✗ Rejecting pet with no matching source: {result.get('pet_name', '?')} / {claude_url}")
                continue

        # Overwrite URL and shelter fields from scraped source data
        result["source_url"]      = source["url"]
        result["listing_url"]     = source.get("listing_url", "") or source["url"]
        result["photo_url"]       = source["photos"][0] if source.get("photos") else ""
        if not result.get("pet_name"):
            result["pet_name"] = source.get("name", "")
        validated.append(result)

    print(f"Generated {len(validated)} {animal_type} blurbs")
    return validated

    
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

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
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
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Claude API error (attempt {attempt + 1}): {e}")
                time.sleep(10 * (attempt + 1))
            else:
                raise

    raw = next((block.text for block in response.content if block.type == "text"), "")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    if not (clean.startswith("[") or clean.startswith("{")):
        start = clean.find("[")
        if start < 0:
            start = clean.find("{")
        end_bracket = clean.rfind("]") if start >= 0 and clean[start] == "[" else clean.rfind("}")
        if start >= 0 and end_bracket > start:
            clean = clean[start:end_bracket + 1]
    try:
        scores = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"  ✗ Failed to parse Claude JSON for pet scoring: {e}")
        print(f"  Raw response (first 500 chars): {raw[:500]}")
        return results  # return unscored results so pipeline continues

    # Validate scores come from real source_urls; fallback to pet_name match
    real_urls = {r["source_url"] for r in results}
    name_to_url = {r["pet_name"]: r["source_url"] for r in results if r.get("pet_name")}
    score_map = {}
    for s in scores:
        url = s.get("source_url", "")
        if url in real_urls:
            score_map[url] = s
        elif s.get("pet_name") in name_to_url:
            fixed = name_to_url[s["pet_name"]]
            print(f"  ⚠ Scoring: fixing hallucinated source_url for {s['pet_name']}: {url} → {fixed}")
            score_map[fixed] = s
        else:
            print(f"  ✗ Scoring: rejecting score with unmatched source_url {url} / {s.get('pet_name', '?')}")

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

    # ── PHASE 1: Per-newsletter, fetch validated cat + dog candidates from
    #            RescueGroups API. URL validation is built into the fetch
    #            (each pet's public-facing page is GET'd and confirmed reachable +
    #            mentions the pet's name). No separate scrape phase needed.
    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']}")
        print(f"{'='*60}")

        all_cats = fetch_pets_via_rescuegroups("Cat", approved_urls,
                                               target=TARGET_PER_SPECIES)
        all_dogs = fetch_pets_via_rescuegroups("Dog", approved_urls,
                                               target=TARGET_PER_SPECIES)

        print(f"\nTotal cats: {len(all_cats)}")
        print(f"Total dogs: {len(all_dogs)}")

        if not all_cats and not all_dogs:
            print(f"No pets found for {newsletter['name']}. Skipping.")
            continue

        # Validate listing URLs before spending on Claude
        print("\n  Validating pet listing URLs...")
        if all_cats:
            all_cats, rejected_cats = filter_valid_items(
                all_cats, critical_fields=["url"], optional_fields=[], label_field="name",
            )
            if rejected_cats:
                print(f"  Dropped {len(rejected_cats)} cats with dead listing URLs")
        if all_dogs:
            all_dogs, rejected_dogs = filter_valid_items(
                all_dogs, critical_fields=["url"], optional_fields=[], label_field="name",
            )
            if rejected_dogs:
                print(f"  Dropped {len(rejected_dogs)} dogs with dead listing URLs")

        if not all_cats and not all_dogs:
            print(f"No pets with valid URLs for {newsletter['name']}. Skipping.")
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
        final_results = cat_results + dog_results

        # Generate GIFs for each pet (3 photos per pet)
        print(f"\n  Creating GIFs for {len(final_results)} pets...")
        try:
            from gif_maker import create_gif_from_urls
            output_dir = Path(__file__).parent / "output"
            output_dir.mkdir(exist_ok=True)

            # Build photo map from raw pet data (source_url -> photos list)
            all_raw_pets = all_cats + all_dogs
            photo_map = {p["url"]: p.get("photos", []) for p in all_raw_pets}

            for result in final_results:
                src_url = result.get("source_url", "")
                photos = photo_map.get(src_url, [])
                pname = result.get("pet_name", "")
                if not photos:
                    print(f"    {pname}: no photos, skipping GIF")
                    continue
                if len(photos) == 1:
                    print(f"    {pname}: 1 photo, using static image")
                    continue
                gif_bytes = create_gif_from_urls(photos[:3], crop_top=True)
                if gif_bytes:
                    slug = pname.lower().replace(" ", "_").replace("'", "")[:30]
                    gif_filename = f"pet_{newsletter['name']}_{slug}_{datetime.today().strftime('%Y%m%d')}.gif"
                    gif_path = output_dir / gif_filename
                    gif_path.write_bytes(gif_bytes)
                    cache_bust = int(datetime.today().timestamp())
                    result["gif_url"] = f"https://peachyinsurance.github.io/newsletters/gifs/{gif_filename}?v={cache_bust}"
                    result["gif_filename"] = gif_filename
                    print(f"    ✓ {pname} GIF: {min(len(photos), 3)} frames, {len(gif_bytes):,} bytes")
        except Exception as e:
            print(f"  ✗ GIF creation failed: {e}")

        # Save
        save_pets_to_notion(final_results, newsletter["name"])
        print(f"Done with {newsletter['name']}. Saved {len(final_results)} rows.")

    print(f"\nAll newsletters complete.")
