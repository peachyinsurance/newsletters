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
from notion_helper import save_events_to_notion, get_existing_event_urls
from url_validator import filter_valid_items
from newsletters_config import NEWSLETTERS, filter_by_env
# Shared event-date filtering (Friday floor + parsing) lives in
# NewsletterCreation/Code/event_date_filter.py — used by Featured Event,
# Free Events, and Weekend Planner.
from event_date_filter import (
    upcoming_friday as _upcoming_friday,
    filter_candidates_by_date,
    filter_past_events as _filter_past_events,
)

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-featured-event-skill_auto.md"

MAX_RESULTS_PER_QUERY = 10
# Claude is asked for a larger pool so the date-floor filter has headroom —
# we then keep the top FINAL_EVENTS by score after dropping past-dated ones.
CANDIDATE_EVENTS      = 8   # ask Claude for this many
TARGET_EVENTS         = CANDIDATE_EVENTS  # legacy alias used in the prompt
FINAL_EVENTS          = 3   # keep this many after the date-floor filter

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


def fetch_events_brave(search_areas: list[str], display_area: str,
                       exclude_urls: set[str] | None = None,
                       extra_queries: list[str] | None = None) -> list[dict]:
    """Fetch event candidates via Brave Search API.

    `exclude_urls` — URLs we already pulled and rejected (past-dated etc.);
    they're skipped so a re-pull yields fresh candidates.
    `extra_queries` — additional queries to broaden the search on retries
    (e.g. swap 'this weekend' → 'next two weeks')."""
    headers = {
        "Accept":              "application/json",
        "Accept-Encoding":     "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }
    exclude_urls = exclude_urls or set()
    queries = build_search_queries(display_area, search_areas)
    if extra_queries:
        queries = list(extra_queries) + queries
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
                    if not url or url in seen_urls or url in exclude_urls:
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
                    if not url or url in seen_urls or url in exclude_urls:
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

    # Tag each candidate with a 1-based index for safe URL matching post-Claude
    indexed_candidates = [{**c, "candidate_index": i} for i, c in enumerate(candidates, 1)]
    candidates_json = json.dumps(indexed_candidates, indent=2)
    demo_summary = (
        f"Median household income: {demographics['median_income']}\n"
        f"Median age: {demographics['median_age']}\n"
        f"Family skew: {demographics['family_skew']}\n"
        f"Homeownership rate: {demographics['homeownership']}\n"
        f"Education level: {demographics['education']}"
    )

    today = datetime.today()
    earliest = _upcoming_friday(today.date())
    pub_context = (
        f"Today is {today.strftime('%A, %B %d, %Y')}. "
        f"The newsletter publishes this week. "
        f"IMPORTANT: only consider events occurring on or after "
        f"{earliest.strftime('%A, %B %d, %Y')} "
        f"(the upcoming Friday) — anything earlier in the week is past by send time."
    )

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

CRITICAL rule about URLs:
- Do NOT return raw URLs (source_url, ticket_url).
- Instead return "candidate_index": N for each selected event, referencing the candidate it came from.
- We will attach the real source_url from the candidate list using that index. Do not invent URLs.

Return ONLY a JSON array with no preamble, explanation, or markdown fences. Exact format:
[
  {{
    "candidate_index": 3,
    "event_name": "Event Name",
    "date": "Saturday, May 10",
    "time": "7:00 PM",
    "venue": "Venue Name, City",
    "price": "$25" or "Free",
    "blurb": "Full blurb text following the skill instructions...",
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

    # Attach real URLs from candidates using the index Claude returned.
    # Discard any URLs Claude may have provided directly.
    candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}
    validated = []
    for r in results:
        idx = r.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        source = candidates_by_index.get(idx) if idx is not None else None
        if not source:
            print(f"  ✗ Rejecting event with invalid candidate_index {idx}: {r.get('event_name', '?')}")
            continue
        r["source_url"] = source.get("url", "")
        r["ticket_url"] = source.get("ticket_url", "") or ""
        r.pop("candidate_index", None)

        # Calculate total score
        r["total_score"] = (
            r.get("demographic_fit_score", 0) +
            r.get("uniqueness_score", 0) +
            r.get("audience_match_score", 0)
        )
        r["newsletter_name"] = newsletter_name
        validated.append(r)
    results = validated

    # Hard floor: drop anything dated before this week's Friday (the
    # newsletter's earliest publish date). Belt-and-suspenders against
    # Claude leaking past events past the prompt-level instruction.
    results = _filter_past_events(results, _upcoming_friday())

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

    for newsletter in filter_by_env():
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Fetch event candidates → date-floor filter → backfill if needed.
        # We want at least MIN_VALID_CANDIDATES candidates surviving the
        # date filter so Claude has real choices. If we fall short, re-pull
        # with broader queries while excluding URLs we already rejected.
        MIN_VALID_CANDIDATES = 8
        floor = _upcoming_friday()
        excluded_urls: set[str] = set()
        broader_query_sets = [
            None,                                            # round 1: defaults
            [f"{newsletter['display_area']} events next two weeks",
             f"{newsletter['display_area']} upcoming events"],
            [f"{newsletter['display_area']} festivals this month",
             f"{newsletter['display_area']} concerts this month",
             f"things to do near {newsletter['display_area']}"],
        ]
        candidates: list[dict] = []
        for round_idx, extra in enumerate(broader_query_sets, 1):
            print(f"\n  --- Candidate round {round_idx} (floor: {floor}) ---")
            new_pool = fetch_events_brave(
                search_areas=newsletter["search_areas"],
                display_area=newsletter["display_area"],
                exclude_urls=excluded_urls,
                extra_queries=extra,
            )
            # URL-validate the new pool before paying for Claude
            new_pool, rejected = filter_valid_items(
                new_pool,
                critical_fields=["url"],
                optional_fields=[],
                label_field="title",
            )
            if rejected:
                print(f"  Dropped {len(rejected)} new candidates with dead URLs")
                excluded_urls.update(r.get("url", "") for r in rejected if r.get("url"))
            # Date-floor filter on the new pool
            kept, past_urls = filter_candidates_by_date(new_pool, floor)
            excluded_urls.update(past_urls)
            # Merge (dedup by URL) into the surviving candidate set
            seen = {c["url"] for c in candidates}
            for c in kept:
                if c.get("url") and c["url"] not in seen:
                    candidates.append(c)
                    seen.add(c["url"])
            print(f"  ↳ pool size after round {round_idx}: {len(candidates)} valid candidates")
            if len(candidates) >= MIN_VALID_CANDIDATES:
                break

        if not candidates:
            print(f"  No future-dated event candidates for {newsletter['name']}. Skipping.")
            continue
        if len(candidates) < MIN_VALID_CANDIDATES:
            print(f"  ⚠ Only {len(candidates)} valid candidates after retries — proceeding anyway")

        # Cross-newsletter URL dedup — union of existing event URLs across both newsletters.
        # Re-fetched per iteration so Newsletter 2 sees Newsletter 1's freshly-saved winners.
        existing_urls = set()
        for nl in NEWSLETTERS:
            existing_urls |= get_existing_event_urls(nl["name"])
        if existing_urls:
            before = len(candidates)
            candidates = [c for c in candidates if c["url"] not in existing_urls]
            print(f"  Filtered {before - len(candidates)} previously-used URLs (union across both newsletters)")
        if not candidates:
            print(f"  All candidates were previously used for {newsletter['name']}. Skipping.")
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
