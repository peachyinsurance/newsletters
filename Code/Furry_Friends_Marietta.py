#!/usr/bin/env python3
"""
Newsletter Automation - Pet Adoption Section
Scrapes Atlanta Humane Society and Petfinder, generates blurb via Claude,
and writes the result to Google Sheets.
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
# 1. ENVIRONMENT — all values injected by GitHub Actions secrets
# ---------------------------------------------------------------------------

CLAUDE_API_KEY          = os.environ["CLAUDE_API_KEY"]
ZYTE_API_KEY            = os.environ["ZYTE_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GSHEET_ID               = os.environ["GSHEET_ID"]
GSHEET_TAB              = "Pets"
SKILL_PROMPT_PATH       = Path(__file__).parent.parent / "Skills" / "newsletter-pet-adoption-skill_auto.md"

# ---------------------------------------------------------------------------
# 2. GOOGLE AUTH (service account — headless, no browser required)
# ---------------------------------------------------------------------------

creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets_service = build("sheets", "v4", credentials=creds)

# ---------------------------------------------------------------------------
# 3. LOAD SKILL PROMPT FROM REPO
# ---------------------------------------------------------------------------

def load_skill_prompt() -> str:
    if not SKILL_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Skill prompt not found at {SKILL_PROMPT_PATH}. "
            "Make sure newsletter-pet-adoption-skill_auto.md is in the repo root."
        )
    prompt = SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    print(f"Loaded skill prompt ({len(prompt)} chars)")
    return prompt

# ---------------------------------------------------------------------------
# 4. SCRAPING HELPERS
# ---------------------------------------------------------------------------

def fetch_with_zyte(url: str, retries: int = 2) -> str | None:
    for attempt in range(retries):
        try:
            response = requests.post(
                "https://api.zyte.com/v1/extract",
                auth=(ZYTE_API_KEY, ""),
                json={"url": url, "browserHtml": True},
                timeout=120
            )
            return response.json()["browserHtml"]
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
# 5. SCRAPE ATLANTA HUMANE SOCIETY
# ---------------------------------------------------------------------------

def scrape_humane_society() -> list[dict]:
    print("\n--- Scraping Atlanta Humane Society ---")
    listing_url = (
        "https://atlantahumane.org/adopt/cats/"
        "?PrimaryBreed=0&Location_4=Marietta&PrimaryColor=0"
        "&search=+Search+&ClientID=13&Species=Cat"
    )
    html = fetch_with_zyte(listing_url)
    if not html:
        print("Failed to fetch Humane Society listing page")
        return []

    soup = clean_soup(html)
    links = soup.find_all("a", href=True)
    pet_links = list(set([
        a["href"] for a in links
        if "/adopt/" in a["href"] and "aid=" in a["href"]
    ]))
    print(f"Found {len(pet_links)} Marietta cat links")

    pets = []

    def fetch_and_parse(url):
        html = fetch_with_zyte(url)
        if html is None:
            return None
        soup = clean_soup(html)
        clean_text = soup.get_text(separator="\n", strip=True)
        return url, soup, clean_text

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_and_parse, url): url for url in pet_links}
        for future in as_completed(futures):
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
                    ]
                    pets.append({"url": url, "profile": clean_text, "photos": photos})
                    print(f"  ✓ Has description: {url} | {len(photos)} photos")
                else:
                    print(f"  ✗ No description: {url}")

    print(f"Humane Society: {len(pets)} cats with descriptions")
    return pets

# ---------------------------------------------------------------------------
# 6. SCRAPE PETFINDER
# ---------------------------------------------------------------------------

def scrape_petfinder(
    max_with_description: int = 5,
    max_total_fetched: int = 10,
    max_pages: int = 3
) -> list[dict]:
    print("\n--- Scraping Petfinder ---")
    base_url = "https://www.petfinder.com"
    listing_url = (
        "https://www.petfinder.com/search/cats-for-adoption/us/ga/eastcobb/"
        "?includeOutOfTown=true&distance=25&page={page}"
    )

    all_pet_links = []
    for page in range(1, max_pages + 1):
        print(f"  Fetching listing page {page}...")
        html = fetch_with_zyte(listing_url.format(page=page))
        if not html:
            break
        soup = clean_soup(html)
        links = soup.find_all("a", href=True)
        page_links = list(set([
            a["href"] for a in links
            if "/cat/" in a["href"] and "/details/" in a["href"]
        ]))
        all_pet_links.extend(page_links)
        print(f"    {len(page_links)} links on page {page} ({len(all_pet_links)} total)")
        time.sleep(1)

    all_pet_links = list(set(all_pet_links))
    full_links = [base_url + link for link in all_pet_links]
    print(f"  {len(full_links)} total candidate links")

    pets = []
    total_fetched = 0

    for url in full_links:
        if len(pets) >= max_with_description:
            print("  Reached description limit. Stopping.")
            break
        if total_fetched >= max_total_fetched:
            print("  Reached fetch limit. Stopping.")
            break

        html = fetch_with_zyte(url)
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
            pets.append({"url": url, "profile": clean_text, "photos": photos})
            print(f"  ✓ {total_fetched} fetched | {len(pets)} with description: {url}")
        else:
            print(f"  ✗ No description: {url}")

        time.sleep(1)

    print(f"Petfinder: {len(pets)} cats with descriptions from {total_fetched} fetched")
    return pets

# ---------------------------------------------------------------------------
# 7. LOAD FEATURED HISTORY FROM GOOGLE SHEETS
# ---------------------------------------------------------------------------

def get_featured_urls() -> set[str]:
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=GSHEET_ID,
        range=f"{GSHEET_TAB}!A:A"  # source_url is column A
    ).execute()
    rows = result.get("values", [])
    urls = {row[0] for row in rows[1:] if row}  # skip header row
    print(f"Loaded {len(urls)} previously featured URLs from Sheets")
    return urls

# ---------------------------------------------------------------------------
# 8. GENERATE BLURB VIA CLAUDE
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

def generate_blurb(pets: list[dict], skill_prompt: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    combined_profiles = build_combined_profiles(pets)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=skill_prompt,
        messages=[{
            "role": "user",
            "content": f"""
Here are adoptable cats from shelters near East Cobb, GA.
Pick the TOP 3 with the best story potential and write a blurb for each.
Use the pet's actual description -- do not invent details.

Return ONLY a JSON array with exactly 3 objects, no preamble or markdown.
Exact format:
[
  {{
    "pet_name": "Patrick Star",
    "shelter_name": "Good Mews Animal Foundation",
    "blurb": "Full blurb text here...",
    "shelter_address": "3805 Robinson Road NW, Marietta, GA 30067",
    "shelter_phone": "(770) 499-2287",
    "shelter_email": "adopt@goodmews.org",
    "shelter_hours": "Mon-Fri 12-6pm, Sat-Sun 11am-5pm",
    "source_url": "https://...",
    "photo_url": "https://... or null"
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
    print(f"Generated {len(results)} blurbs")
    return results

# ---------------------------------------------------------------------------
# 8A. EVALUATE CLAUDE BLURB AND ADD SCORE
# ---------------------------------------------------------------------------

def score_blurbs(results: list[dict], pets: list[dict]) -> list[dict]:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    scoring_input = ""
    for i, result in enumerate(results, 1):
        scoring_input += f"""
--- Candidate {i} ---
Pet Name: {result['pet_name']}
Shelter: {result['shelter_name']}
Blurb: {result['blurb']}
Source URL: {result['source_url']}

"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""
You are evaluating pet adoption blurbs for a local newsletter editor.
Score each candidate on a 0-10 scale for each of the following criteria:

1. Adoptability: How easy and appealing is this pet to adopt? Consider compatibility, special needs, and fit for a typical family.
2. Interesting Story: How compelling and unique is this pet's backstory? Does it have personality details that make readers want to meet them?
3. Time at Shelter: Based on clues in the blurb (returned pet, long wait, came from another shelter, etc.), estimate how long this pet has been waiting. Longer wait = higher score.

Return ONLY a JSON array with no preamble or markdown. Exact format:
[
  {{
    "pet_name": "Patrick Star",
    "source_url": "https://...",
    "adoptability_score": 8,
    "story_score": 7,
    "shelter_time_score": 5,
    "total_score": 20,
    "scoring_notes": "• Strong adoption candidate with broad family appeal\\n• Returned pet with a clear sympathetic backstory readers will connect with\\n• Has been waiting longer than average based on shelter history clues"
  }},
  {{...}},
  {{...}}
]

Rules for scoring_notes:
- Exactly 3 bullet points per candidate
- Format: • [point]\\n• [point]\\n• [point]
- Each bullet is a concise exec-level reason why this cat should be featured this week
- Focus on newsletter appeal, reader connection, and urgency to adopt
- Write for a newsletter editor making a quick decision, not a shelter worker

Candidates to score:
{scoring_input}
"""
        }]
    )

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    scores = json.loads(clean)

    # Merge scores back into results
    score_map = {s["source_url"]: s for s in scores}
    for result in results:
        score_data = score_map.get(result["source_url"], {})
        result["adoptability_score"] = score_data.get("adoptability_score", 0)
        result["story_score"]        = score_data.get("story_score", 0)
        result["shelter_time_score"] = score_data.get("shelter_time_score", 0)
        result["total_score"]        = score_data.get("total_score", 0)
        result["scoring_notes"]      = score_data.get("scoring_notes", "")

    # Sort by total score descending
    results.sort(key=lambda x: x["total_score"], reverse=True)

    for r in results:
        print(f"  {r['pet_name']}: {r['total_score']}/30 | adoptability: {r['adoptability_score']} | story: {r['story_score']} | shelter time: {r['shelter_time_score']}")
        print(f"  Notes: {r['scoring_notes'][:80]}...")

    return results

# ---------------------------------------------------------------------------
# 9. SAVE RESULT TO GOOGLE SHEETS
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
            data.get("scoring_notes", "")
        ])
    sheets_service.spreadsheets().values().append(
        spreadsheetId=GSHEET_ID,
        range=f"{GSHEET_TAB}!A:R",
        valueInputOption="RAW",
        body={"values": rows}
    ).execute()
    print(f"Saved {len(rows)} rows to Google Sheets")
  
# ---------------------------------------------------------------------------
# 10. MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting newsletter automation — {datetime.today().strftime('%Y-%m-%d')}")

    # Load skill prompt from repo
    skill_prompt = load_skill_prompt()

    # Scrape both sources
    humane_pets    = scrape_humane_society()
    petfinder_pets = scrape_petfinder()
    all_pets       = petfinder_pets + humane_pets
    print(f"\nTotal pets scraped: {len(all_pets)}")

    if not all_pets:
        print("No pets found. Exiting.")
        exit(1)

    # Filter out previously featured pets
    featured_urls = get_featured_urls()
    fresh_pets = [p for p in all_pets if p["url"] not in featured_urls]
    print(f"Fresh pets after filtering history: {len(fresh_pets)}")

    if not fresh_pets:
        print("All scraped pets have been featured before. Exiting.")
        exit(1)
    
    # Generate blurbs via Claude
    results = generate_blurb(fresh_pets, skill_prompt)
    
    # Score blurbs via Claude
    results = score_blurbs(results, fresh_pets)
    
    # Save to Google Sheets
    save_to_sheets(results)

    print("\nDone.")
