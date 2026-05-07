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

# Adaptive-retry thresholds
MIN_EVENTS_BEFORE_RETRY = 3       # fewer than this on first pass -> retry
HIGH_DROP_RATIO_THRESHOLD = 0.5   # >50% URL-validation 404s on first pass -> retry
RETRY_RESULTS_PER_QUERY = 20      # broader pass pulls more candidates per query

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


def build_fallback_queries(newsletter: dict, audience: str, day: str, target_date_iso: str) -> list[str]:
    """Broader fallback queries for the retry pass — same geography, less specific
    angle, to surface candidates the primary queries missed."""
    primary = newsletter["display_area"]
    target_dt = datetime.fromisoformat(target_date_iso)
    month_year = target_dt.strftime("%B %Y")

    if audience == "Family":
        return [
            f"{primary} weekend events {month_year}",
            f"things to do with kids near {primary}",
            f"{primary} community events {month_year}",
            f"{primary} family weekend activities",
        ]
    else:  # Adult
        return [
            f"{primary} nightlife {day} {month_year}",
            f"{primary} bars venues weekend",
            f"what's happening {primary} {day}",
            f"{primary} weekend activities for adults {month_year}",
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
# 7. PROCESS ONE BUCKET (audience × day) — with adaptive retry
# ---------------------------------------------------------------------------
def fetch_and_filter_candidates(
    queries: list[str],
    max_per_query: int,
    excluded_urls: set,
    label: str,
) -> tuple[list[dict], float]:
    """One pass: Brave -> aggregator filter -> dedup -> URL validate.
    Returns (candidates, drop_ratio). drop_ratio = fraction of pre-validation
    candidates rejected as dead URLs (used as one of the retry triggers)."""
    query_specs = [{"q": q} for q in queries]
    candidates = search_web(
        query_specs=query_specs,
        api_key=BRAVE_NEWS_API_KEY,
        trusted_domains=None,
        max_per_query=max_per_query,
        pause_between=PAUSE_BETWEEN_BRAVE,
    )
    if not candidates:
        print(f"    [{label}] No Brave results")
        return [], 0.0

    before = len(candidates)
    candidates = filter_aggregators(candidates)
    if before - len(candidates):
        print(f"    [{label}] dropped {before - len(candidates)} aggregators")

    if excluded_urls:
        before = len(candidates)
        candidates = [c for c in candidates if c["url"] not in excluded_urls]
        if before - len(candidates):
            print(f"    [{label}] dropped {before - len(candidates)} already-seen URLs")

    if not candidates:
        return [], 0.0

    pre_validate = len(candidates)
    candidates, rejected = filter_valid_items(
        candidates,
        critical_fields=["url"],
        optional_fields=[],
        label_field="title",
    )
    drop_ratio = (len(rejected) / pre_validate) if pre_validate else 0.0
    print(f"    [{label}] {len(candidates)} valid URLs, {len(rejected)} dead ({drop_ratio:.0%} 404 ratio)")

    return candidates[:30], drop_ratio


def call_claude_for_bucket(
    candidates: list[dict],
    newsletter: dict,
    audience: str,
    day: str,
    target_date_iso: str,
    skill_prompt: str,
) -> list[dict]:
    """Send candidates to Claude. Returns validated event dicts (URLs reattached
    from candidate_index, audience/day/date forced)."""
    if not candidates:
        return []

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
        return []

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
        r["audience"] = audience
        r["day"] = day
        r["date"] = target_date_iso
        validated.append(r)
    return validated


def process_bucket(
    newsletter: dict,
    audience: str,
    day: str,
    target_date_iso: str,
    skill_prompt: str,
    existing_urls: set,
) -> list[dict]:
    """Run one bucket with adaptive retry: if first pass yields too few events
    OR has a high 404 ratio, run a second broader pass and merge."""
    print(f"\n  [{audience} / {day} / {target_date_iso}]")

    # Pass 1 — primary queries, normal result count
    primary_queries = build_queries(newsletter, audience, day, target_date_iso)
    candidates_p1, drop_ratio_p1 = fetch_and_filter_candidates(
        primary_queries, MAX_RESULTS_PER_QUERY, existing_urls, label="primary"
    )
    results = call_claude_for_bucket(
        candidates_p1, newsletter, audience, day, target_date_iso, skill_prompt
    )
    print(f"    Primary pass: {len(results)} events accepted")

    # Should we retry?
    needs_retry = (
        len(results) < MIN_EVENTS_BEFORE_RETRY
        or drop_ratio_p1 > HIGH_DROP_RATIO_THRESHOLD
    )

    if needs_retry:
        why = []
        if len(results) < MIN_EVENTS_BEFORE_RETRY:
            why.append(f"only {len(results)} events")
        if drop_ratio_p1 > HIGH_DROP_RATIO_THRESHOLD:
            why.append(f"{drop_ratio_p1:.0%} 404 ratio")
        print(f"    Retrying broader (reason: {', '.join(why)}) — {RETRY_RESULTS_PER_QUERY} results/query")

        # Exclude URLs already in pass 1 so the retry pool is fresh
        retry_excluded = set(existing_urls) | {c["url"] for c in candidates_p1}

        fallback_queries = build_fallback_queries(newsletter, audience, day, target_date_iso)
        candidates_p2, _ = fetch_and_filter_candidates(
            fallback_queries, RETRY_RESULTS_PER_QUERY, retry_excluded, label="retry"
        )
        more_results = call_claude_for_bucket(
            candidates_p2, newsletter, audience, day, target_date_iso, skill_prompt
        )

        # Merge, dedup by URL
        seen_urls = {r["source_url"] for r in results}
        added = 0
        for r in more_results:
            if r["source_url"] and r["source_url"] not in seen_urls:
                results.append(r)
                seen_urls.add(r["source_url"])
                added += 1
        print(f"    Retry pass added {added} events (bucket total: {len(results)})")

    print(f"    ✓ {len(results)} events accepted")
    for r in results:
        print(f"      - {r.get('emoji', '')} {r.get('event_name', '?')}")
    return results


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
