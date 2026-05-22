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
BRAVE_NEWS_API_KEY    = os.environ["BRAVE_NEWS_API_KEY"]
# Optional — when set, we look up each picked business in Google Places
# (Text Search) and save its first usable photo URL to the row. Same
# secret restaurants already use. If absent, photos just stay empty
# and the existing Brief render falls back to no-image.
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-business-brief-skill_auto.md"

QUERIES_PER_NEWSLETTER = 6
MAX_RESULTS_PER_QUERY  = 10
PAUSE_BETWEEN_BRAVE    = 0.5
TARGET_BRIEFS          = 1   # one business per newsletter (Claude returns 3-5 candidates ranked, we save the top)

# Aggregator/directory blocklist — irrelevant for primary-source business spotlights
AGGREGATOR_BLOCKLIST = {
    "yelp.com",
    "tripadvisor.com",
    "yellowpages.com",
    "manta.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    "quora.com",
    "linkedin.com",
    "groupon.com",
    "bbb.org",
    "youtube.com",
    "thumbtack.com",
    "houzz.com",
    "angi.com",
    "homeadvisor.com",
    "patch.com",
    "eventbrite.com",
}

# Major chain hostnames — filter out their corporate pages so Claude doesn't
# try to spotlight a Walmart or Home Depot. Local mom-and-pop businesses
# whose website happens to mention these chains are unaffected (we match
# the candidate URL hostname, not the article body).
CHAIN_HOSTS = {
    "walmart.com", "target.com", "lowes.com", "homedepot.com",
    "cvs.com", "walgreens.com", "riteaid.com",
    "bestbuy.com", "costco.com", "samsclub.com", "kroger.com",
    "publix.com", "wholefoodsmarket.com", "traderjoes.com", "aldi.us",
    "macys.com", "kohls.com", "nordstrom.com", "tjmaxx.com", "marshalls.com",
    "dollargeneral.com", "dollartree.com", "fivebelow.com", "familydollar.com",
    "petsmart.com", "petco.com",
    "ulta.com", "sephora.com",
    "officedepot.com", "staples.com",
    "barnesandnoble.com",
    "michaels.com", "hobbylobby.com", "joann.com",
    "ikea.com", "wayfair.com",
    "att.com", "verizon.com", "t-mobile.com",
    "fitness19.com", "planetfitness.com", "lafitness.com", "ymca.org",
    "supercuts.com", "greatclips.com", "sportclips.com",
}

# Restaurant-like hostnames or words to filter — restaurants belong to a
# separate section. We hard-filter known restaurant directories; the skill
# also tells Claude to drop food-service candidates as a second gate.
RESTAURANT_DOMAINS = {
    "opentable.com", "ubereats.com", "doordash.com", "grubhub.com",
    "seamless.com", "menupages.com", "menuism.com", "zomato.com",
    "allmenus.com",
}


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Business Brief skill not found at {SKILL_PROMPT_PATH}")


# ---------------------------------------------------------------------------
# 3. SEARCH QUERY BUILDERS
# ---------------------------------------------------------------------------
def build_queries(newsletter: dict) -> list[str]:
    """Build Brave search queries for non-restaurant local businesses.

    Rotates across the newsletter's `search_areas` (concrete town names) for
    relevance, mirroring the Weekend Planner pattern. Categories targeted:
    boutiques, retail shops, gyms, salons, spas, services."""
    areas = newsletter["search_areas"]

    def area(i: int) -> str:
        return areas[i % len(areas)]

    return [
        f"{area(0)} small business spotlight retail",
        f"{area(1)} local boutique shop",
        f"{area(2)} gym yoga pilates studio",
        f"{area(0)} salon spa beauty",
        f"{area(1)} local services tailor framer cobbler",
        f"best new businesses {area(2)}",
    ]


# ---------------------------------------------------------------------------
# 4. FILTERS
# ---------------------------------------------------------------------------
def is_aggregator(url: str) -> bool:
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in AGGREGATOR_BLOCKLIST)


def is_chain(url: str) -> bool:
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in CHAIN_HOSTS)


def is_restaurant_directory(url: str) -> bool:
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in RESTAURANT_DOMAINS)


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
    indexed = [{**c, "candidate_index": i} for i, c in enumerate(candidates, 1)]
    candidates_json = json.dumps(indexed, indent=2)

    today = datetime.today()
    return f"""
publication_date: {today.strftime('%Y-%m-%d')}
newsletter_name: {newsletter['name']}
display_area: {newsletter['display_area']}
search_areas: {json.dumps(newsletter['search_areas'])}

Below are pre-filtered Brave Search candidates for non-restaurant local
businesses near {newsletter['display_area']}. Aggregator, chain, and
restaurant-directory domains are already removed. Drop any remaining
restaurants, chain locations, or candidates without enough specifics for
a 150-200 word recommendation.

Pick the ONE best business and write the spotlight per the skill's voice
and structure rules. Use `candidate_index` to reference the source — do
NOT include raw URLs in your output.

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

        # Brave search
        queries = build_queries(newsletter)
        query_specs = [{"q": q} for q in queries]
        candidates = search_web(
            query_specs=query_specs,
            api_key=BRAVE_NEWS_API_KEY,
            trusted_domains=None,
            max_per_query=MAX_RESULTS_PER_QUERY,
            pause_between=PAUSE_BETWEEN_BRAVE,
        )
        if not candidates:
            print(f"  No Brave results for {newsletter['name']}. Skipping.")
            continue

        # Filter aggregators + chains + restaurant directories
        before = len(candidates)
        candidates = filter_candidates(candidates)
        print(f"  {len(candidates)} candidates after aggregator/chain/restaurant filter (dropped {before - len(candidates)})")
        if not candidates:
            continue

        # Cross-newsletter URL dedup — don't re-feature a business already published anywhere
        existing_urls = set()
        for nl in NEWSLETTERS:
            existing_urls |= get_existing_business_brief_urls(nl["name"])
        if existing_urls:
            before = len(candidates)
            candidates = [c for c in candidates if c["url"] not in existing_urls]
            print(f"  Filtered {before - len(candidates)} previously-used URLs (cross-newsletter dedup)")
        if not candidates:
            print(f"  All candidates were previously used. Skipping {newsletter['name']}.")
            continue

        # URL validation
        candidates, rejected = filter_valid_items(
            candidates,
            critical_fields=["url"],
            optional_fields=[],
            label_field="title",
        )
        if rejected:
            print(f"  Dropped {len(rejected)} candidates with dead URLs")
        if not candidates:
            continue

        # Cap to keep prompt reasonable
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
