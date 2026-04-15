#!/usr/bin/env python3
"""
Newsletter Automation - Featured Event Section
Searches for local events via Brave Search API,
uses Claude to evaluate and pick the best featured event,
and saves results to Notion.
"""
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

import requests
import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import save_events_to_notion

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-featured-event-skill_auto.md"

MAX_RESULTS_PER_QUERY = 10
TARGET_EVENTS         = 3   # return top 3, flag 1 as winner

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "display_area": "East Cobb",
        "search_areas": ["East Cobb", "Marietta GA", "Roswell GA"],
        "demographics": {
            "median_income":    "$118,000",
            "median_age":       "42",
            "family_skew":      "Mix of established families and empty nesters. Many kids are teens or college-age.",
            "homeownership":    "78%",
            "education":        "65% bachelor's degree or higher",
        },
    },
    {
        "name":         "Perimeter_Post",
        "display_area": "Perimeter",
        "search_areas": ["Dunwoody GA", "Sandy Springs GA", "Brookhaven GA"],
        "demographics": {
            "median_income":    "$105,000",
            "median_age":       "38",
            "family_skew":      "Mix of young professionals, young families, and empty nesters. More adult-skewing than East Cobb.",
            "homeownership":    "55%",
            "education":        "70% bachelor's degree or higher",
        },
    },
]


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        "You are a local newsletter writer picking featured events. "
        "Write warm, neighbor-style event blurbs that make readers want to go."
    )


# ---------------------------------------------------------------------------
# 3. FETCH EVENTS VIA BRAVE SEARCH API
# ---------------------------------------------------------------------------
def build_search_queries(display_area: str, search_areas: list[str]) -> list[str]:
    """Build event search queries per the skill's guidance."""
    today = datetime.today()
    month_year = today.strftime("%B %Y")

    queries = []
    for area in search_areas:
        queries.append(f"{area} events this weekend")
        queries.append(f"{area} events next week")
        queries.append(f"{area} things to do {month_year}")
    # Broader queries
    queries.append(f"{display_area} concerts shows festivals {month_year}")
    queries.append(f"events near {display_area} Georgia {month_year}")
    queries.append(f"{display_area} Eventbrite events {month_year}")
    return queries


def fetch_events_brave(search_areas: list[str], display_area: str) -> list[dict]:
    """Fetch event candidates via Brave Search API."""
    headers = {
        "Accept":              "application/json",
        "Accept-Encoding":     "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }

    queries = build_search_queries(display_area, search_areas)
    all_results = []
    seen_urls = set()

    for query in queries:
        print(f"  Searching: {query}")
        try:
            # Try news endpoint first for timely results
            res = requests.get(
                "https://api.search.brave.com/res/v1/news/search",
                headers=headers,
                params={"q": query, "count": MAX_RESULTS_PER_QUERY, "freshness": "pw"},
                timeout=30,
            )
            if res.status_code == 200:
                results = res.json().get("results", [])
                print(f"    News: {len(results)} results")
                for item in results:
                    url = item.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    all_results.append({
                        "title":   item.get("title", ""),
                        "url":     url,
                        "source":  item.get("meta_url", {}).get("hostname", "") if isinstance(item.get("meta_url"), dict) else "",
                        "date":    item.get("age", "") or item.get("page_age", ""),
                        "summary": item.get("description", ""),
                    })

            # Also try web search for Eventbrite / venue pages
            res2 = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params={"q": query, "count": MAX_RESULTS_PER_QUERY, "freshness": "pm"},
                timeout=30,
            )
            if res2.status_code == 200:
                web_results = res2.json().get("web", {}).get("results", [])
                print(f"    Web:  {len(web_results)} results")
                for item in web_results:
                    url = item.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    all_results.append({
                        "title":   item.get("title", ""),
                        "url":     url,
                        "source":  item.get("meta_url", {}).get("hostname", "") if isinstance(item.get("meta_url"), dict) else "",
                        "date":    "",
                        "summary": item.get("description", ""),
                    })

        except Exception as e:
            print(f"    Brave API error: {e}")

        time.sleep(0.5)

    # Deduplicate by title similarity
    unique = []
    seen_titles = set()
    for r in all_results:
        title_key = r["title"].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(r)

    print(f"  {len(unique)} unique event candidates after dedup")
    return unique


# ---------------------------------------------------------------------------
# 4. CLAUDE: EVALUATE AND WRITE BLURBS
# ---------------------------------------------------------------------------
def evaluate_and_write_events(
    candidates: list[dict],
    demographics: dict,
    display_area: str,
    newsletter_name: str,
    skill_prompt: str,
) -> list[dict]:
    """Send candidates + demographics to Claude. Returns top events with blurbs."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    candidates_json = json.dumps(candidates, indent=2)
    demo_summary = (
        f"Median household income: {demographics['median_income']}\n"
        f"Median age: {demographics['median_age']}\n"
        f"Family skew: {demographics['family_skew']}\n"
        f"Homeownership rate: {demographics['homeownership']}\n"
        f"Education level: {demographics['education']}"
    )

    today = datetime.today()
    pub_context = f"Today is {today.strftime('%A, %B %d, %Y')}. The newsletter publishes this week."

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6-20250620",
                max_tokens=4000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""
{pub_context}

Newsletter: {newsletter_name.replace('_', ' ')}
Coverage area: {display_area}

Audience demographics:
{demo_summary}

Below are event candidates found via web search. Many of these may not be actual events —
they could be news articles, ads, or irrelevant pages. Filter aggressively.

Your job:
1. Identify which candidates are real, upcoming, specific events (not recurring weekly things
   like farmers markets, unless it's a special edition).
2. Evaluate each real event using the four factors from your instructions:
   demographic fit, uniqueness/can't-miss factor, family vs adult skew, and ticket price relative to income.
3. Pick the top {TARGET_EVENTS} events. For each, write a blurb following your instructions exactly.
4. Score each event 1-10 on: demographic_fit, uniqueness, audience_match.

Return ONLY a JSON array with no preamble, explanation, or markdown fences. Exact format:
[
  {{
    "event_name": "Event Name",
    "date": "Saturday, May 10",
    "time": "7:00 PM",
    "venue": "Venue Name, City",
    "price": "$25" or "Free",
    "blurb": "Full blurb text following the skill instructions...",
    "source_url": "https://...",
    "ticket_url": "https://... or null",
    "demographic_fit_score": 8,
    "uniqueness_score": 9,
    "audience_match_score": 7,
    "scoring_notes": "Why this event fits this audience..."
  }}
]

If fewer than {TARGET_EVENTS} real events qualify, return fewer. If none qualify, return an empty array [].

Candidates:
{candidates_json}
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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    results = json.loads(clean)

    # Calculate total scores
    for r in results:
        r["total_score"] = (
            r.get("demographic_fit_score", 0) +
            r.get("uniqueness_score", 0) +
            r.get("audience_match_score", 0)
        )
        r["newsletter_name"] = newsletter_name

    # Sort by total score descending
    results.sort(key=lambda x: x["total_score"], reverse=True)

    for r in results:
        print(f"  {r['event_name']}: {r['total_score']}/30 "
              f"(demo:{r.get('demographic_fit_score',0)} "
              f"unique:{r.get('uniqueness_score',0)} "
              f"match:{r.get('audience_match_score',0)})")

    return results


# ---------------------------------------------------------------------------
# 5. FLAG DEFAULT WINNER
# ---------------------------------------------------------------------------
def flag_default_winner(results: list[dict]) -> list[dict]:
    for r in results:
        r["default_winner"] = ""
    if results:
        results[0]["default_winner"] = "yes"
        print(f"  Default winner: {results[0]['event_name']} ({results[0]['total_score']}/30)")
    return results


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Featured Event automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Fetch event candidates
        candidates = fetch_events_brave(
            search_areas=newsletter["search_areas"],
            display_area=newsletter["display_area"],
        )

        if not candidates:
            print(f"  No event candidates found for {newsletter['name']}. Skipping.")
            continue

        # Claude evaluates and writes blurbs
        print(f"\n  Sending {len(candidates)} candidates to Claude...")
        results = evaluate_and_write_events(
            candidates=candidates,
            demographics=newsletter["demographics"],
            display_area=newsletter["display_area"],
            newsletter_name=newsletter["name"],
            skill_prompt=skill_prompt,
        )

        if not results:
            print(f"  Claude found no qualifying events for {newsletter['name']}. Skipping.")
            continue

        # Flag winner
        results = flag_default_winner(results)

        # Save to Notion
        save_events_to_notion(results, newsletter["name"])

        # Save local JSON backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"events_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"  Saved JSON to {json_file}")

        print(f"\n  Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
