#!/usr/bin/env python3
"""
Newsletter Automation - Pet Adoption Section
Scrapes Atlanta Humane Society and Petfinder for cats and dogs,
generates blurbs via Claude, scores them, flags defaults,
and writes results to Google Sheets.
"""

import os
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import anthropic
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

NEWSLETTER_NAME = "East_Cobb_Connect"

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT
# ---------------------------------------------------------------------------
CLAUDE_API_KEY          = os.environ["CLAUDE_API_KEY"]
APIFY_API_KEY           = os.environ["APIFY_API_KEY"]
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
# 4. APIFY SCRAPING
# ---------------------------------------------------------------------------
def fetch_with_apify(url: str, retries: int = 2) -> str | None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APIFY_API_KEY}"
    }
    for attempt in range(retries):
        try:
            run_res = requests.post(
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
                    "maxRequestsPerCrawl": 1
                },
                timeout=120
            )
            if run_res.status_code != 200:
                print(f"Apify error {run_res.status_code}: {run_res.text[:200]}")
                if attempt < retries - 1:
                    time.sleep(3)
                    continue
                return None
            items = run_res.json()
            if items and len(items) > 0:
                return items[0].get("html")
            return None
        except requests.exceptions.ReadTimeout:
            print(f"Timeout on attempt {attempt + 1} for {url}")
            if attempt < retries - 1:
                time.sleep(3)
            else:
                print(f"Skipping {url} after {retries} attempts")
                return None

def clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return soup

# ---------------------------------------------------------------------------
# 5. LOAD PREVIOUSLY APPROVED URLS
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
# 6. SCRAPE HUMANE SOCIETY
# ---------------------------------------------------------------------------
def scrape_humane_society(animal_type: str, excluded_urls: set, target: int = 5) -> list[dict]:
    print(f"\n--- Scraping Atlanta Humane Society ({animal_type}s) ---")

    if animal_type == "cat":
        listing_url = (
            "https://atlantahumane.org/adopt/cats/"
            "?PrimaryBreed=0&Location_4=Marietta&PrimaryColor=0"
            "&search=+Search+&ClientID=13&Species=Cat"
        )
    else:
        listing_url = "https://atlantahumane.org/adopt/dogs/"

    html = fetch_with_apify(listing_url)
    if not html:
        print(f"Failed to fetch Humane Society {animal_type} listing page")
        return []

    soup = clean_soup(html)
    links = soup.find_all("a", href=True)
    pet_links = list(set([
        a["href"] for a in links
        if "/adopt/" in a["href"] and "aid=" in a["href"]
        and a["href"] not in excluded_urls
    ]))
    print(f"Found {len(pet_links)} candidate {animal_type} links")

    pets = []

    def fetch_and_parse(url):
        if url in excluded_urls:
            return None
        html = fetch_with_apify(url)
        if html is None:
            return None
        soup = clean_soup(html)
        clean_text = soup.get_text(separator="\n", strip=True)
        return url, soup, clean_text

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_and_parse, url): url for url in pet_links}
        for future in as_completed(futures):
            if len(pets) >= target:
                break
            result = future.result()
            if result is None:
                continue
            url, soup, clean_text = result
            if "Adoption Fee:" in clean_text:
                after_fee = clean_text.split("Adoption Fee:")[1]
                remaining = "\n".join(after_fee.strip().split("\n")[2:]).strip()
                if len(remaining) > 100 and "PetBridge" not in remaining[:150]:
                    photos = [
                        img.get("src") for img in soup.find_all("img")
                        if img.get("src") and "petango.com" in img.get("src")
                    ][:3]
                    pets.append({"url": url, "profile": clean_text, "photos": photos, "animal_type": animal_type})
                    print(f"  ✓ {animal_type}: {url} | {len(photos)} photos")
                else:
                    print(f"  ✗ No description: {url}")

    print(f"Humane Society {animal_type}s: {len(pets)} with descriptions")
    return pets

# ---------------------------------------------------------------------------
# 7. SCRAPE PETFINDER
# ---------------------------------------------------------------------------
def scrape_petfinder(animal_type: str, excluded_urls: set, target: int = 5, max_total_fetched: int = 10, max_pages: int = 3) -> list[dict]:
    print(f"\n--- Scraping Petfinder ({animal_type}s) ---")
    base_url = "https://www.petfinder.com"

    if animal_type == "cat":
        listing_url = "https://www.petfinder.com/search/cats-for-adoption/us/ga/eastcobb/?includeOutOfTown=true&distance=25&page={page}"
        detail_pattern = "/cat/"
    else:
        listing_url = "https://www.petfinder.com/search/dogs-for-adoption/us/ga/eastcobb/?includeOutOfTown=true&distance=25&page={page}"
        detail_pattern = "/dog/"

    # Fetch listing pages in parallel
    all_pet_links = []

    def fetch_listing_page(page):
        html = fetch_with_apify(listing_url.format(page=page))
        if not html:
            return []
        soup = clean_soup(html)
        links = soup.find_all("a", href=True)
        return list(set([
            a["href"] for a in links
            if detail_pattern in a["href"] and "/details/" in a["href"]
            and base_url + a["href"] not in excluded_urls
        ]))

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_listing_page, page): page for page in range(1, max_pages + 1)}
        for future in as_completed(futures):
            page_links = future.result()
            all_pet_links.extend(page_links)
            print(f"  {len(page_links)} links from a listing page ({len(all_pet_links)} total)")

    all_pet_links = list(set(all_pet_links))
    full_links = [base_url + link for link in all_pet_links if base_url + link not in excluded_urls]
    print(f"  {len(full_links)} total candidate links")

    pets = []
    total_fetched = 0

    for url in full_links:
        if len(pets) >= target:
            print(f"  Reached {target} {animal_type}s. Stopping.")
            break
        if total_fetched >= max_total_fetched:
            print(f"  Reached fetch limit. Stopping.")
            break

        html = fetch_with_apify(url)
        total_fetched += 1
        if not html:
            continue

        soup = clean_soup(html)
        clean_text = soup.get_text(separator="\n", strip=True)

        if "Story" in clean_text and len(clean_text) > 500:
            photos = [
                img.get("src") for img in soup.find_all("img")
                if img.get("src")
                and "cloudfront.net" in img.get("src")
                and "Enlarge" not in (img.get("alt") or "")
            ][:3]
            pets.append({"url": url, "profile": clean_text, "photos": photos, "animal_type": animal_type})
            print(f"  ✓ {total_fetched} fetched | {len(pets)} {animal_type}s: {url}")
        else:
            print(f"  ✗ No description: {url}")

    print(f"Petfinder {animal_type}s: {len(pets)} with descriptions from {total_fetched} fetched")
    return pets

# ---------------------------------------------------------------------------
# 8. BUILD COMBINED PROFILES
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
# 9. GENERATE BLURBS VIA CLAUDE
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

Shelter info for Atlanta Humane Society - Marietta:
1565 Industrial Blvd, Marietta, GA 30062
(404) 974-2800 | adoptions@atlantahumane.org

Shelter info for Good Mews Animal Foundation:
3805 Robinson Road NW, Marietta, GA 30067
(770) 499-2287 | adopt@goodmews.org
Mon-Fri 12-6pm, Sat-Sun 11am-5pm
"""
        }]
    )

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    results = json.loads(clean)
    print(f"Generated {len(results)} {animal_type} blurbs")
    return results

# ---------------------------------------------------------------------------
# 10. SCORE ALL BLURBS IN ONE CLAUDE CALL
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
# 11. FLAG DEFAULT WINNERS
# ---------------------------------------------------------------------------
def flag_default_winners(cat_results: list[dict], dog_results: list[dict]) -> tuple[list[dict], list[dict]]:
    week_number = datetime.today().isocalendar()[1]
    odd_week = week_number % 2 != 0

    # Sort by score
    cat_results.sort(key=lambda x: x["total_score"], reverse=True)
    dog_results.sort(key=lambda x: x["total_score"], reverse=True)

    # Initialize all flags
    for r in cat_results + dog_results:
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
# 12. SAVE TO GOOGLE SHEETS
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
# 13. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting newsletter automation — {datetime.today().strftime('%Y-%m-%d')}")

    skill_prompt  = load_skill_prompt()
    approved_urls = get_approved_urls()

    # Scrape cats and dogs in parallel
    print("\nScraping cats and dogs in parallel...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        cat_future = executor.submit(lambda: (
            scrape_humane_society("cat", approved_urls, target=5),
            scrape_petfinder("cat", approved_urls, target=5)
        ))
        dog_future = executor.submit(lambda: (
            scrape_humane_society("dog", approved_urls, target=5),
            scrape_petfinder("dog", approved_urls, target=5)
        ))
        humane_cats, petfinder_cats = cat_future.result()
        humane_dogs, petfinder_dogs = dog_future.result()

    all_cats = petfinder_cats + humane_cats
    all_dogs = petfinder_dogs + humane_dogs
    print(f"\nTotal cats scraped: {len(all_cats)}")
    print(f"Total dogs scraped: {len(all_dogs)}")

    if not all_cats and not all_dogs:
        print("No pets found. Exiting.")
        exit(1)

    # Generate blurbs for cats and dogs in parallel
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

    # Score all 6 in one Claude call
    all_results = cat_results + dog_results
    print(f"\nScoring all {len(all_results)} candidates in one call...")
    all_results = score_blurbs(all_results)

    # Split back into cats and dogs after scoring
    cat_results = [r for r in all_results if r.get("animal_type") == "cat"]
    dog_results = [r for r in all_results if r.get("animal_type") == "dog"]

    # Flag default winners
    cat_results, dog_results = flag_default_winners(cat_results, dog_results)

    # Save to Google Sheets
    final_results = cat_results + dog_results
    save_to_sheets(final_results)

    print(f"\nDone. Saved {len(final_results)} total rows.")
