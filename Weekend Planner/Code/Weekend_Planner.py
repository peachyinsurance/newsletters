#!/usr/bin/env python3
"""
Newsletter Automation - Weekend Planner Section

Builds the Weekend Planner section for each newsletter:
  - 3 newsletters (East Cobb Connect, Perimeter Post, Lewisville Lake Lookout)
  - 2 audiences (Family, Adult)
  - 3 days (Friday, Saturday, Sunday)
  -> 18 (newsletter, audience, day) combos per run

For each combo: Brave web search -> aggregator-domain blocklist filter ->
URL-validate -> Claude evaluates with the weekend-planner skill as system
prompt -> save 5-8 strong events as Notion rows. The assemble script later
renders rows into the inline pipe-separated format.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from brave_search import search_web, domain_of
from claude_json import call_with_json_output
from notion_helper import (
    save_weekend_events_to_notion,
    get_existing_weekend_event_urls,
)
from url_validator import filter_valid_items

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-weekend-planner-skill_auto.md"

TARGET_PER_BUCKET = 8        # upper bound passed to Claude (it picks 5-8)
QUERIES_PER_BUCKET = 4       # Brave queries per (audience, day) combo
MAX_RESULTS_PER_QUERY = 10
PAUSE_BETWEEN_BRAVE = 0.5    # rate-limit buffer

AGGREGATOR_BLOCKLIST = {
    "eventbrite.com",
    "allevents.in",
    "patch.com",
    "yelp.com",
    "tripadvisor.com",
    "facebook.com",
    "meetup.com",
    "reddit.com",
    "groupon.com",
    "youtube.com",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "yellowpages.com",
    "thingstodo.com",
    "10best.com",
    "viator.com",
    "events12.com",
    "eventcrazy.com",
    # Real-estate domains pollute area-based queries
    "redfin.com",
    "zillow.com",
    "trulia.com",
}

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "display_area": "East Cobb",
        "search_areas": ["East Cobb GA", "Marietta GA", "Roswell GA"],
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
    {
        "name":         "Lewisville_Lake_Lookout",
        "display_area": "Lewisville Lake",
        "search_areas": ["Lewisville TX", "Flower Mound TX", "The Colony TX",
                         "Little Elm TX", "Highland Village TX", "Lake Dallas TX"],
        "demographics": {
            "median_income":    "$95,000",
            "median_age":       "36",
            "family_skew":      "Strongly family-heavy with mixed income brackets — middle-income diverse suburbs (Lewisville, Little Elm, The Colony), affluent suburbs (Flower Mound, Highland Village), lake-lifestyle communities (Lake Dallas, Hickory Creek), plus a college-adjacent younger skew near UNT/TWU.",
            "homeownership":    "65%",
            "education":        "50% bachelor's degree or higher",
        },
    },
]

DAYS = ["Friday", "Saturday", "Sunday"]
AUDIENCES = ["Family", "Adult"]


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Weekend Planner skill not found at {SKILL_PROMPT_PATH}")


# ---------------------------------------------------------------------------
# 3. WEEKEND DATE MATH
# ---------------------------------------------------------------------------
def target_weekend_dates(today: datetime | None = None) -> dict:
    """Return ISO dates for the upcoming Friday/Saturday/Sunday at least 7 days out.
    If run on Wednesday, this returns the weekend 9-11 days out (= the weekend after
    the next Thursday issue)."""
    today = today or datetime.today()
    days_until_friday = (4 - today.weekday()) % 7  # 4 == Friday in Python's weekday()
    days_until_friday += 7  # always look one Friday ahead
    friday = (today + timedelta(days=days_until_friday)).date()
    return {
        "Friday":   friday.isoformat(),
        "Saturday": (friday + timedelta(days=1)).isoformat(),
        "Sunday":   (friday + timedelta(days=2)).isoformat(),
    }


# ---------------------------------------------------------------------------
# 4. SEARCH QUERY BUILDERS
# ---------------------------------------------------------------------------
def build_queries(newsletter: dict, audience: str, day: str, target_date_iso: str) -> list[str]:
    """Build ~4 Brave search queries for one (newsletter, audience, day) combo."""
    primary = newsletter["display_area"]
    target_dt = datetime.fromisoformat(target_date_iso)
    date_label = target_dt.strftime("%B %d %Y")
    month_year = target_dt.strftime("%B %Y")

    if audience == "Family":
        return [
            f"{primary} family events {day} {date_label}",
            f"{primary} kids activities {day} {month_year}",
            f"{primary} family things to do weekend {month_year}",
            f"{primary} library museum park {day}",
        ]
    else:  # Adult
        return [
            f"{primary} live music {day} {month_year}",
            f"{primary} brewery distillery winery {day}",
            f"{primary} concerts shows nightlife {day} {month_year}",
            f"{primary} adult things to do {day} {date_label}",
        ]


# ---------------------------------------------------------------------------
# 5. AGGREGATOR FILTER
# ---------------------------------------------------------------------------
def is_aggregator(url: str) -> bool:
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in AGGREGATOR_BLOCKLIST)


def filter_aggregators(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates if not is_aggregator(c.get("url", ""))]


# ---------------------------------------------------------------------------
# 6. CLAUDE PROMPT BUILDER
# ---------------------------------------------------------------------------
def build_claude_user_prompt(
    newsletter: dict,
    audience: str,
    day: str,
    target_date_iso: str,
    candidates: list[dict],
) -> str:
    d = newsletter["demographics"]
    demo_summary = (
        f"Median household income: {d['median_income']}\n"
        f"Median age: {d['median_age']}\n"
        f"Family skew: {d['family_skew']}\n"
        f"Homeownership rate: {d['homeownership']}\n"
        f"Education level: {d['education']}"
    )

    # Tag candidates with 1-based candidate_index for safe URL attachment post-Claude
    indexed = [{**c, "candidate_index": i} for i, c in enumerate(candidates, 1)]
    candidates_json = json.dumps(indexed, indent=2)

    target_dt = datetime.fromisoformat(target_date_iso)
    date_label = target_dt.strftime("%A, %B %d, %Y")

    return f"""
Newsletter: {newsletter['name'].replace('_', ' ')} ({newsletter['display_area']})
Audience: {audience}
Day: {day} ({date_label})

Audience demographics:
{demo_summary}

Anchor towns: {', '.join(newsletter['search_areas'])}

Below are pre-filtered Brave Search candidates (aggregator domains already removed).
Filter for events that are real, primary-source-verified, on the target date, and a fit for the {audience.lower()} audience.

Pick {TARGET_PER_BUCKET} or fewer strong events. For each, return JSON per the skill's output schema. Use `candidate_index` to reference the source URL — do NOT include raw URLs in the output.

Set every event's `audience` to "{audience}" and `day` to "{day}" and `date` to "{target_date_iso}".

Candidates:
{candidates_json}
"""


# ---------------------------------------------------------------------------
# 7. PROCESS ONE BUCKET (audience × day)
# ---------------------------------------------------------------------------
def process_bucket(
    newsletter: dict,
    audience: str,
    day: str,
    target_date_iso: str,
    skill_prompt: str,
    existing_urls: set,
) -> list[dict]:
    """Run search + Claude for one bucket. Returns list of event dicts ready for save."""
    print(f"\n  [{audience} / {day} / {target_date_iso}]")

    queries = build_queries(newsletter, audience, day, target_date_iso)
    query_specs = [{"q": q} for q in queries]

    # Brave search — no allowlist filter, we apply blocklist after
    candidates = search_web(
        query_specs=query_specs,
        api_key=BRAVE_NEWS_API_KEY,
        trusted_domains=None,
        max_per_query=MAX_RESULTS_PER_QUERY,
        pause_between=PAUSE_BETWEEN_BRAVE,
    )
    if not candidates:
        print(f"    No Brave results")
        return []

    # Drop aggregator hostnames
    before = len(candidates)
    candidates = filter_aggregators(candidates)
    print(f"    {len(candidates)} candidates after aggregator filter (dropped {before - len(candidates)})")
    if not candidates:
        return []

    # Drop URLs we've already published in any prior run for this newsletter
    if existing_urls:
        before = len(candidates)
        candidates = [c for c in candidates if c["url"] not in existing_urls]
        print(f"    {len(candidates)} candidates after prior-run dedup (dropped {before - len(candidates)})")
    if not candidates:
        return []

    # Validate URLs (drop dead links before spending Claude tokens)
    candidates, rejected = filter_valid_items(
        candidates,
        critical_fields=["url"],
        optional_fields=[],
        label_field="title",
    )
    if rejected:
        print(f"    Dropped {len(rejected)} candidates with dead URLs")
    if not candidates:
        return []

    # Cap candidates to keep prompts reasonable (Claude doesn't need 100 candidates)
    candidates = candidates[:30]

    # Claude
    user_prompt = build_claude_user_prompt(newsletter, audience, day, target_date_iso, candidates)
    try:
        results = call_with_json_output(
            api_key=CLAUDE_API_KEY,
            system=skill_prompt,
            user_content=user_prompt,
        )
    except Exception as e:
        print(f"    ✗ Claude error: {e}")
        return []

    if not results:
        print(f"    Claude found no qualifying events")
        return []

    # Attach real URLs from candidate_index
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
            print(f"    ✗ Rejecting event with invalid candidate_index {idx}: {r.get('event_name', '?')}")
            continue
        r["source_url"] = source.get("url", "")
        r.pop("candidate_index", None)

        # Force audience/day/date in case Claude drifted
        r["audience"] = audience
        r["day"] = day
        r["date"] = target_date_iso
        validated.append(r)

    print(f"    ✓ {len(validated)} events accepted")
    for r in validated:
        print(f"      - {r.get('emoji', '')} {r.get('event_name', '?')}")
    return validated


# ---------------------------------------------------------------------------
# 8. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Weekend Planner automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    weekend = target_weekend_dates()
    print(f"Target weekend: Fri {weekend['Friday']} / Sat {weekend['Saturday']} / Sun {weekend['Sunday']}")

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        existing_urls = get_existing_weekend_event_urls(newsletter["name"])
        print(f"  {len(existing_urls)} existing URLs in Notion (cross-run dedup)")

        all_events: list[dict] = []
        for audience in AUDIENCES:
            for day in DAYS:
                bucket_events = process_bucket(
                    newsletter=newsletter,
                    audience=audience,
                    day=day,
                    target_date_iso=weekend[day],
                    skill_prompt=skill_prompt,
                    existing_urls=existing_urls,
                )
                all_events.extend(bucket_events)
                # Track new URLs to avoid re-using within this run across buckets
                for ev in bucket_events:
                    if ev.get("source_url"):
                        existing_urls.add(ev["source_url"])
                time.sleep(0.5)

        if not all_events:
            print(f"\n  No events accepted for {newsletter['name']}. Skipping save.")
            continue

        print(f"\n  Saving {len(all_events)} total events for {newsletter['name']}...")
        save_weekend_events_to_notion(all_events, newsletter["name"])

        # Local JSON backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"weekend_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(all_events, indent=2), encoding="utf-8")
        print(f"  Saved JSON backup to {json_file}")

    print(f"\nAll newsletters complete.")
