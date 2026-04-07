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
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
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


APIFY_SCRAPER_TIMEOUT = 300  # seconds for the single combined run


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

def fetch_all_html_apify(urls: list[str]) -> dict[str, str]:
    """Fetch ALL URLs in a single Apify web-scraper run. Returns {url: html} dict."""
    if not urls:
        return {}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APIFY_API_KEY}",
    }
    print(f"  Starting single Apify run for {len(urls)} URLs (concurrency=5)...")
    try:
        res = requests.post(
            "https://api.apify.com/v2/acts/apify~web-scraper/run-sync-get-dataset-items",
            headers=headers,
            json={
                "startUrls": [{"url": u} for u in urls],
                "pageFunction": """
async function pageFunction(context) {
    return {
        url: context.request.url,
        html: document.documentElement.outerHTML
    };
}
""",
                "maxConcurrency": 5,
                "maxRequestsPerCrawl": len(urls),
            },
            timeout=APIFY_SCRAPER_TIMEOUT,
        )
        if res.status_code not in (200, 201):
            print(f"  Apify error {res.status_code}: {res.text[:200]}")
            return {}
        items = res.json()
        result = {}
        for item in items:
            u = item.get("url", "")
            h = item.get("html", "")
            if u and h:
                result[u] = h
        print(f"  Apify returned {len(result)} pages")
        return result
    except requests.exceptions.ReadTimeout:
        print(f"  Apify timeout after {APIFY_SCRAPER_TIMEOUT}s")
        return {}


def fetch_html_apify(url: str, retries: int = 2) -> str | None:
    """Fetch a single page's rendered HTML via Apify web-scraper."""
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


def parse_search_html(html: str, species: str) -> list[dict]:
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

    # Parse DOM: pet cards are in grandparent div.tw-h-[450px] > div > a[href]
    print("  Parsing DOM for pet cards...")
    seen_hrefs = set()
    all_links = soup.select("a[href]")
    for link in all_links:
        href = link.get("href", "")
        if not href or "/search/" in href:
            continue
        if "/cat/" not in href and "/dog/" not in href:
            continue
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        pet_url = f"https://www.petfinder.com{href}" if href.startswith("/") else href

        # Card data lives in the grandparent container
        card = link.parent.parent if link.parent else None
        if not card:
            continue

        # Name: div.tw-font-extrabold
        name_el = card.select_one("div.tw-font-extrabold")
        name = name_el.get_text(strip=True) if name_el else ""

        # Age + Gender: first span in the info area (e.g. "Young • Male")
        age_gender = ""
        info_div = card.select_one("div.tw-text-primary-600")
        if info_div:
            first_span = info_div.select_one("span")
            if first_span:
                age_gender = first_span.get_text(strip=True)

        age, gender = "", ""
        if "•" in age_gender:
            parts = [p.strip() for p in age_gender.split("•")]
            age = parts[0] if len(parts) > 0 else ""
            gender = parts[1] if len(parts) > 1 else ""

        # Breed: span.tw-truncate
        breed_el = card.select_one("span.tw-truncate")
        breed = breed_el.get_text(strip=True) if breed_el else ""

        # Photo: img inside the link
        img_el = link.select_one("img")
        photo = ""
        if img_el:
            photo = img_el.get("src") or img_el.get("data-src") or ""

        # Alt text has extra info (e.g. "Luke, ADOPTABLE, Young • Male, German Shepherd Dog")
        alt_text = img_el.get("alt", "") if img_el else ""

        if name:
            pets.append({
                "name": name, "url": pet_url, "species": species,
                "breed": breed, "age": age, "gender": gender, "size": "",
                "description": alt_text,  # use alt text as basic description from search page
                "photos": [photo] if photo else [],
                "org_name": "", "org_address": "", "org_phone": "", "org_email": "",
            })

    print(f"  DOM parsing found {len(pets)} pets")
    return pets


def clean_text(text: str) -> str:
    """Fix double-encoded UTF-8 and clean up special characters."""
    import html as html_module
    if not text:
        return ""
    # Decode HTML entities (e.g. &amp; &#39;)
    text = html_module.unescape(text)
    # Fix double-encoded UTF-8 (e.g. Ã¢ÂÂ → ')
    try:
        text = text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    # Replace common problematic characters
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u00a0", " ")
    return text.strip()


def parse_detail_html(html: str) -> dict:
    """Parse a single pet detail page HTML into a detail dict."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    detail = {}

    next_tag = soup.find("script", id="__NEXT_DATA__")
    if next_tag:
        try:
            nd = json.loads(next_tag.string)
            pp = nd.get("props", {}).get("pageProps", {})
            animal = pp.get("animal") or pp.get("pet") or {}
            if animal:
                detail["description"] = clean_text(animal.get("description", ""))
                contact = animal.get("contact", {})
                org_addr = contact.get("address", {})
                addr_parts = [org_addr.get("address1", ""), org_addr.get("city", ""),
                              org_addr.get("state", ""), org_addr.get("postcode", "")]
                detail["org_address"] = " ".join(p for p in addr_parts if p).strip()
                detail["org_phone"] = contact.get("phone", "")
                detail["org_email"] = contact.get("email", "")
                detail["org_name"] = animal.get("organization_id", "")
                photos = []
                for p in (animal.get("photos") or []):
                    url = p.get("large") or p.get("full") or p.get("medium") or ""
                    if url:
                        photos.append(url)
                if photos:
                    detail["photos"] = photos
                detail["size"] = animal.get("size", "")
                detail["age"] = animal.get("age", "")
                detail["gender"] = animal.get("gender", "")
                detail["breed"] = (animal.get("breeds") or {}).get("primary", "")
                return detail
        except Exception as e:
            print(f"    Detail parse error: {e}")

    desc_el = soup.select_one("[data-test='Pet_Story_Section'], [class*='description'], [class*='Description']")
    if desc_el:
        detail["description"] = clean_text(desc_el.get_text(strip=True))
    return detail


def fetch_petfinder_apify(species: str, excluded_urls: set, state: str, zip_code: str,
                          target: int = 5, _html_cache: dict = None) -> list[dict]:
    """Build pet profiles from pre-fetched HTML cache."""
    print(f"\n--- Building {species} profiles from cache ---")

    search_url = f"https://www.petfinder.com/search/{species.lower()}s-for-adoption/us/{state}/{zip_code}/"
    cache = _html_cache or {}

    search_html = cache.get(search_url, "")
    if not search_html:
        print(f"  No cached HTML for {search_url}")
        return []

    raw_items = parse_search_html(search_html, species.lower())

    candidates = []
    for item in raw_items:
        pet_url = item.get("url", "").rstrip("/")
        if not pet_url:
            continue
        if pet_url in excluded_urls:
            print(f"  ✗ Skipping previously approved: {item.get('name')}")
            continue
        candidates.append(item)
    candidates = candidates[:target * 2]
    print(f"  {len(candidates)} candidates (need {target})")

    if not candidates:
        return []

    pets = []
    for item in candidates:
        if len(pets) >= target:
            break

        source_url = item["url"].rstrip("/")
        name = item.get("name", "Unknown")

        detail_html = cache.get(source_url, "")
        detail = parse_detail_html(detail_html) if detail_html else {}

        description = detail.get("description") or item.get("description") or ""
        if not description or len(description.strip()) < 30:
            print(f"  ✗ No description for {name}, skipping")
            continue

        breed       = detail.get("breed") or item.get("breed", "")
        age         = detail.get("age") or item.get("age", "")
        gender      = detail.get("gender") or item.get("gender", "")
        size        = detail.get("size") or item.get("size", "")
        photos      = detail.get("photos") or item.get("photos", [])
        org_name    = detail.get("org_name") or item.get("org_name", "")
        org_address = detail.get("org_address") or item.get("org_address", "")
        org_phone   = detail.get("org_phone") or item.get("org_phone", "")
        org_email   = detail.get("org_email") or item.get("org_email", "")

        org_info = {
            "name": org_name, "address": org_address,
            "phone": org_phone, "email": org_email, "hours": "",
        }

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
            "listing_url": source_url,
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

    # ── PHASE 1: Scrape all search pages in ONE Apify call ─────────────
    search_urls = []
    for nl in NEWSLETTERS:
        for species in ["cats", "dogs"]:
            search_urls.append(f"https://www.petfinder.com/search/{species}-for-adoption/us/{nl['state']}/{nl['zip']}/")

    print(f"\n{'='*60}")
    print(f"Phase 1: Scraping {len(search_urls)} search pages")
    print(f"{'='*60}")
    html_cache = fetch_all_html_apify(search_urls)

    # ── PHASE 2: Parse search pages, collect detail URLs ───────────────
    print(f"\nPhase 2: Parsing search pages and collecting detail URLs")
    detail_urls = []
    search_results = {}  # key = (newsletter_name, species) -> list of pet dicts

    for nl in NEWSLETTERS:
        for species in ["Cat", "Dog"]:
            search_url = f"https://www.petfinder.com/search/{species.lower()}s-for-adoption/us/{nl['state']}/{nl['zip']}/"
            search_html = html_cache.get(search_url, "")
            if not search_html:
                print(f"  No HTML for {search_url}")
                search_results[(nl["name"], species)] = []
                continue

            pets = parse_search_html(search_html, species.lower())
            # Filter excluded and take candidates
            candidates = []
            for p in pets:
                url = p.get("url", "").rstrip("/")
                if url and url not in approved_urls:
                    candidates.append(p)
            candidates = candidates[:7]  # take up to 7 per species/newsletter (need 5, buffer for missing descriptions)
            search_results[(nl["name"], species)] = candidates
            for c in candidates:
                detail_urls.append(c["url"].rstrip("/"))
            print(f"  {nl['name']} {species}: {len(candidates)} candidates")

    # Deduplicate and remove already-approved URLs before scraping
    detail_urls = list(dict.fromkeys(detail_urls))
    detail_urls = [u for u in detail_urls if u not in approved_urls]
    print(f"\n  Total detail pages to scrape: {len(detail_urls)} (excluded {len(approved_urls)} approved)")

    # ── PHASE 3: Scrape detail pages in batches of 10 ────────────────
    BATCH_SIZE = 10
    if detail_urls:
        print(f"\n{'='*60}")
        print(f"Phase 3: Scraping {len(detail_urls)} detail pages in batches of {BATCH_SIZE}")
        print(f"{'='*60}")
        for i in range(0, len(detail_urls), BATCH_SIZE):
            batch = detail_urls[i:i + BATCH_SIZE]
            print(f"\n  Batch {i // BATCH_SIZE + 1}: {len(batch)} URLs")
            batch_cache = fetch_all_html_apify(batch)
            html_cache.update(batch_cache)
    else:
        print("\n  No detail pages to scrape")

    # ── PHASE 4: Process each newsletter ───────────────────────────────
    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']}")
        print(f"{'='*60}")

        all_cats = fetch_petfinder_apify("Cat", approved_urls, newsletter["state"], newsletter["zip"],
                                         target=5, _html_cache=html_cache)
        all_dogs = fetch_petfinder_apify("Dog", approved_urls, newsletter["state"], newsletter["zip"],
                                         target=5, _html_cache=html_cache)

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
