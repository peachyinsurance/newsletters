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

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT — all values injected by GitHub Actions secrets
# ---------------------------------------------------------------------------

CLAUDE_API_KEY          = os.environ["CLAUDE_API_KEY"]
ZYTE_API_KEY            = os.environ["ZYTE_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GSHEET_ID               = os.environ["GSHEET_ID"]
GSHEET_TAB              = "Pets"
SKILL_PROMPT_PATH       = Path(__file__).parent / "newsletter-pet-adoption-skill_auto.md"

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


def generate_blurb(pets: list[dict], skill_prompt: str) -> dict:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    combined_profiles = build_combined_profiles(pets)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=skill_prompt,
        messages=[{
            "role": "user",
            "content": f"""
Here are up to 7 adoptable cats from shelters near East Cobb, GA.
Review all of them and pick the one with the best story potential.
Write the East Cobb Connect adoption blurb for that pet only.
Use the pet's actual description -- do not invent details.

Return ONLY a JSON object with no preamble, explanation, or markdown.
Exact format:
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
}}

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
    data = json.loads(clean)
    print(f"Generated blurb for: {data['pet_name']} from {data['shelter_name']}")
    return data

# ---------------------------------------------------------------------------
# 9. SAVE RESULT TO GOOGLE SHEETS
# ---------------------------------------------------------------------------

def save_to_sheets(data: dict) -> None:
    row = [
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
        "available"
    ]
    sheets_service.spreadsheets().values().append(
        spreadsheetId=GSHEET_ID,
        range=f"{GSHEET_TAB}!A:K",
        valueInputOption="RAW",
        body={"values": [row]}
    ).execute()
    print(f"Saved to Google Sheets: {data['pet_name']}")

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

    # Generate blurb via Claude
    result = generate_blurb(fresh_pets, skill_prompt)

    # Save to Google Sheets
    save_to_sheets(result)

    print("\nDone.")
