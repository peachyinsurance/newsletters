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
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from brave_search import search_web, domain_of
from claude_json import call_with_json_output
from notion_helper import (
    save_weekend_events_to_notion,
    get_existing_weekend_event_urls,
)
from newsletters_config import NEWSLETTERS, filter_by_env
from event_date_filter import upcoming_friday, filter_candidates_by_date, filter_candidates_in_date_range

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-weekend-planner-skill_auto.md"

TARGET_PER_AUDIENCE = 18     # upper bound Claude picks per audience (Family OR Adult); ~6/day × 3 days
MIN_PER_AUDIENCE    = 9      # below this, we fire a retry pass with broader queries (~3/day)
MAX_RESULTS_PER_QUERY = 15
PAUSE_BETWEEN_BRAVE = 0.5    # rate-limit buffer

# Backfill if Claude returns fewer than MIN_PER_AUDIENCE picks for an audience.
RETRY_RESULTS_PER_QUERY = 20      # Brave hard-caps `count` at 20; sending >20 gets HTTP 422
CANDIDATE_CAP            = 120    # max candidates sent to Claude per audience (pooled across 3 days)

AGGREGATOR_BLOCKLIST = {
    # Kept blocked: review sites, social, listicles, real-estate noise.
    # Removed in May 2026: eventbrite.com, allevents.in, meetup.com —
    # those are where 60-80% of legitimate small-venue events live.
    # We now accept them as candidates and drill the picked event for a
    # primary-source URL in `prefer_primary_source()` below.
    "patch.com",
    "yelp.com",
    "tripadvisor.com",
    "facebook.com",
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
    # Listicle / "things to do this weekend" roundup hubs. These pages list
    # many events in one article — when Claude picks an event mentioned
    # inside, the candidate_index URL points to the roundup, not the
    # actual event, so the event text and link end up disconnected.
    "mommypoppins.com",
    "thrillist.com",
    "timeout.com",
    "365atlanta.com",
    "accessatlanta.com",
    "365thingsindallas.com",
    # Real-estate domains pollute area-based queries
    "redfin.com",
    "zillow.com",
    "trulia.com",
}

# URL-path patterns that signal a listicle/roundup even on a domain we
# don't blanket-block. Same problem as the listicle hubs above: candidate
# URL is the roundup, not the event Claude writes about. Checked on the
# URL path, case-insensitive.
LISTICLE_URL_HINTS = (
    "/things-to-do",
    "/things_to_do",
    "/best-of",
    "/best-",
    "/top-",
    "/guide-to-",
    "/guide/",
    "/roundup",
    "/listicle",
    "/weekend-guide",
    "/what-to-do",
    "/events-this-weekend",
    "/things-to-do-this-weekend",
)

# Domains we still treat as "aggregators" for prefer-primary-source logic.
# When Claude picks an event whose URL is on one of these, we drill its
# article body for an embedded primary-source link (official venue/
# organizer page) and swap if a good candidate exists.
AGGREGATOR_DRILL_HOSTS = {
    "eventbrite.com",
    "allevents.in",
    "meetup.com",
}

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
    """Return ISO dates for the UPCOMING Friday/Saturday/Sunday — i.e. THIS
    week's weekend, not next-next. If today is already Sat/Sun, snaps to
    that same weekend's Friday so the run still targets the day-of."""
    today = today or datetime.today()
    weekday = today.weekday()  # Mon=0 ... Sun=6
    # If today is Mon-Fri, days_until_friday is forward to Friday (0 if today is Friday).
    # If today is Sat/Sun, snap BACK to this weekend's Friday.
    if weekday <= 4:
        days_until_friday = 4 - weekday
    else:  # Sat or Sun
        days_until_friday = 4 - weekday  # negative — yields this weekend's Friday
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
    """Build 2 Brave search queries per (newsletter, audience, day) combo.

    Trimmed from 4 → 2 to cut Brave spend ~2x — Brave's index dedupes
    heavily across similar local-area queries, so the four-query variant
    returned mostly overlapping URLs. Two well-chosen queries capture the
    same hits at half the cost.

    Queries rotate across `search_areas` (concrete towns) instead of
    `display_area` because display areas like 'Perimeter' or 'Lewisville
    Lake' return too much off-topic content (physics seminars, etc.)."""
    target_dt = datetime.fromisoformat(target_date_iso)
    date_label = target_dt.strftime("%B %d %Y")
    month_year = target_dt.strftime("%B %Y")

    areas = newsletter["search_areas"]

    def area(i: int) -> str:
        return areas[i % len(areas)]

    if audience == "Family":
        return [
            f"{area(0)} family events {day} {date_label}",
            f"{area(1)} kids things to do {month_year}",
            f"{area(2)} family weekend activities {month_year}",
        ]
    else:  # Adult
        return [
            f"{area(0)} live music {day} {date_label}",
            f"{area(1)} concerts nightlife weekend {month_year}",
            f"{area(2)} brewery bar event {day} {month_year}",
        ]


def build_fallback_queries(newsletter: dict, audience: str, day: str, target_date_iso: str) -> list[str]:
    """Broader fallback queries for the retry pass (trimmed 4 → 2). Rotation
    starts at the back half of `search_areas` so the retry pool covers
    different towns than the primary pass."""
    target_dt = datetime.fromisoformat(target_date_iso)
    month_year = target_dt.strftime("%B %Y")

    areas = newsletter["search_areas"]
    offset = len(areas) // 2  # start fallback rotation at the midpoint

    def area(i: int) -> str:
        return areas[(i + offset) % len(areas)]

    if audience == "Family":
        return [
            f"{area(0)} weekend events {month_year}",
            f"{area(1)} community events {month_year}",
            f"{area(2)} kids fun things to do {month_year}",
        ]
    else:  # Adult
        return [
            f"{area(0)} nightlife weekend {month_year}",
            f"what's happening {area(1)} {day}",
            f"{area(2)} bars venues live music {month_year}",
        ]


# ---------------------------------------------------------------------------
# 5. AGGREGATOR FILTER
# ---------------------------------------------------------------------------
def is_aggregator(url: str) -> bool:
    host = domain_of(url)
    if any(host == d or host.endswith("." + d) for d in AGGREGATOR_BLOCKLIST):
        return True
    # Listicle URL-path heuristic: catches "things-to-do" / "best-of" /
    # weekend-guide patterns on domains we don't blanket-block.
    path = url.lower()
    if any(hint in path for hint in LISTICLE_URL_HINTS):
        return True
    return False


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

Below are pre-filtered candidates. They have ALREADY been screened for:
  • domain quality (review sites, social, listicles, real-estate noise removed)
  • date range (only candidates whose page text mentions a date inside the target weekend Fri-Sun remain, with unparseable-date candidates kept as borderline)
  • duplicate URLs

Your job is to PICK the best {TARGET_PER_BUCKET} for this audience+day and WRITE the
one-line description per event per the skill's schema. Do NOT re-filter for date,
relevance, or geography — that's already been done. If a candidate looks off, that's
a signal the pre-filter missed something — feel free to skip, but the working
assumption is every candidate in this list is a valid option.

When the same event appears under both an aggregator URL (e.g. Eventbrite) AND a primary
source URL (the venue's own page), pick the primary by `candidate_index`. We drill
aggregator picks for an embedded primary link in a post-pass.

Use `candidate_index` to reference the source URL — do NOT include raw URLs in the output.

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
    target_range: tuple[date, date] | None = None,
    target_weekend: dict | None = None,
) -> list[dict]:
    """One pass: Brave -> aggregator filter -> dedup. NO URL validation.

    Why no validation: HEAD-request validation gives false positives on
    bot-protected event-calendar pages (visitlewisville.com/events/,
    llela.org/visit/llela-events-calendar, playlewisville.com/programs/
    activities-calendar, etc.). Those are real, human-reachable pages that
    return 403/404 to non-browser User-Agents. Killing them pre-Claude
    starves the candidate pool of the best primary sources we have.

    Trade-off: Claude may occasionally see a candidate whose page is
    actually dead. The skill's primary-source rule is the quality gate —
    Claude rejects news-article URLs and stale roundups by content, not
    by URL reachability."""
    query_specs = [{"q": q} for q in queries]
    # Freshness window: only consider pages Brave indexed in the past 8
    # weeks. Stale event roundups ("Best Atlanta events of January") are
    # almost always reposted/syndicated old content. Restricting to recent
    # crawl-dates drastically cuts the rate of past-event candidates that
    # we'd otherwise have to date-extract and reject.
    today = datetime.today().date()
    freshness_window = f"{(today - timedelta(weeks=8)).isoformat()}to{today.isoformat()}"
    candidates = search_web(
        query_specs=query_specs,
        api_key=BRAVE_NEWS_API_KEY,
        trusted_domains=None,
        max_per_query=max_per_query,
        pause_between=PAUSE_BETWEEN_BRAVE,
        freshness=freshness_window,
    )
    if not candidates:
        print(f"    [{label}] No Brave results")
        return []

    before = len(candidates)
    candidates = filter_aggregators(candidates)
    if before - len(candidates):
        print(f"    [{label}] dropped {before - len(candidates)} aggregators")

    if excluded_urls:
        before = len(candidates)
        candidates = [c for c in candidates if c["url"] not in excluded_urls]
        if before - len(candidates):
            print(f"    [{label}] dropped {before - len(candidates)} already-seen URLs")

    # Strict date filter: candidates must mention a date IN the target
    # weekend (Fri-Sun of the current week). If no target_range is given,
    # fall back to the past-only floor.
    before = len(candidates)
    if target_range is not None:
        start, end = target_range
        candidates, dropped_urls = filter_candidates_in_date_range(candidates, start, end)
        if dropped_urls:
            excluded_urls.update(dropped_urls)
            print(f"    [{label}] dropped {before - len(candidates)} candidates outside {start}..{end}")
    else:
        candidates, past_urls = filter_candidates_by_date(candidates, upcoming_friday())
        if past_urls:
            excluded_urls.update(past_urls)
            print(f"    [{label}] dropped {before - len(candidates)} past-only candidates")

    # Pipeline-side day tagging: figure out which target-weekend day(s)
    # each candidate maps to BEFORE sending to Claude. Drops candidates
    # whose text doesn't pin to Fri/Sat/Sun (the recurring-event fallback
    # inside determine_event_days still rescues "every Friday" / "weekly"
    # wording). After this, each survivor has a `days` list and Claude
    # doesn't need to figure out the date — the pipeline already did.
    if target_weekend is not None:
        before = len(candidates)
        tagged = []
        for c in candidates:
            days = determine_event_days(c, target_weekend)
            if days:
                c["days"] = days
                tagged.append(c)
        candidates = tagged
        if before - len(candidates):
            print(f"    [{label}] dropped {before - len(candidates)} candidates with no target-weekend day match")

    print(f"    [{label}] {len(candidates)} candidates ready for Claude")
    return candidates[:CANDIDATE_CAP]


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
    # Prefer primary sources: if Claude picked an Eventbrite / Meetup /
    # AllEvents URL, drill the page to find an official venue or organizer
    # link and swap. Falls back to the aggregator URL on miss.
    validated = prefer_primary_source(validated)
    return validated


def prefer_primary_source(events: list[dict]) -> list[dict]:
    """For each event whose source_url is on a known aggregator (Eventbrite,
    Meetup, AllEvents), fetch the page and substitute the official primary
    URL when one is found. Keeps the aggregator URL as fallback in
    `source_url_aggregator` for audit / debugging."""
    try:
        sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
        from aggregator_drilldown import find_primary_url, _hostname
    except Exception as e:
        print(f"    ⚠ aggregator_drilldown unavailable ({e}) — keeping aggregator URLs")
        return events
    for r in events:
        url = r.get("source_url", "")
        if not url:
            continue
        host = _hostname(url)
        if not any(host == d or host.endswith("." + d) for d in AGGREGATOR_DRILL_HOSTS):
            continue
        primary = find_primary_url(url, title=r.get("event_name", ""))
        if primary:
            print(f"    ↳ swapped aggregator URL to primary: {host} → {_hostname(primary)} ({r.get('event_name', '')[:50]})")
            r["source_url_aggregator"] = url
            r["source_url"] = primary
    return events


def determine_event_days(candidate: dict, target_weekend: dict) -> list[str]:
    """Read the candidate's title + summary for date mentions and map them
    to the Friday/Saturday/Sunday of the target weekend.

    Hard rule: the candidate text MUST mention a date inside the target
    weekend (Fri/Sat/Sun). If no parsed date matches, return [] so the
    caller drops the pick — DON'T default to Saturday, which used to ship
    past events labeled as this Saturday.

    target_weekend = {'Friday': '2026-05-15', 'Saturday': '2026-05-16',
                      'Sunday': '2026-05-17'}
    """
    from datetime import date as _date
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..',
                                 'NewsletterCreation', 'Code'))
    from event_date_filter import extract_dates_from_text

    text = " ".join(str(candidate.get(k, "") or "") for k in
                    ("title", "summary", "full_text"))
    parsed = extract_dates_from_text(text)
    weekend_dates = {
        _date.fromisoformat(target_weekend["Friday"]):   "Friday",
        _date.fromisoformat(target_weekend["Saturday"]): "Saturday",
        _date.fromisoformat(target_weekend["Sunday"]):   "Sunday",
    }
    days_found = []
    for d in parsed:
        label = weekend_dates.get(d)
        if label and label not in days_found:
            days_found.append(label)
    if days_found:
        order = ["Friday", "Saturday", "Sunday"]
        return sorted(days_found, key=order.index)

    # Recurring-event fallback: if the candidate text describes a weekly /
    # ongoing pattern (e.g. "every Friday", "Fridays", "weekly", "every
    # weekend"), map those weekday words onto the target weekend. This
    # rescues legitimate recurring events whose snippets don't include a
    # specific calendar date.
    import re as _re
    low = text.lower()
    recurring_signals = [
        "every friday", "every saturday", "every sunday",
        "fridays", "saturdays", "sundays",
        "every weekend", "every week", "weekly",
        "each friday", "each saturday", "each sunday",
        "every other friday", "every other saturday", "every other sunday",
        "ongoing", "recurring",
    ]
    if any(sig in low for sig in recurring_signals):
        weekday_map = {
            "friday": "Friday",
            "saturday": "Saturday",
            "sunday": "Sunday",
        }
        recurring_days = []
        for word, label in weekday_map.items():
            # match the weekday word standalone or with "s"/"every"/"each"
            if _re.search(rf"\b{word}s?\b", low):
                recurring_days.append(label)
        if "every weekend" in low or "weekends" in low:
            for label in ("Friday", "Saturday", "Sunday"):
                if label not in recurring_days:
                    recurring_days.append(label)
        if recurring_days:
            order = ["Friday", "Saturday", "Sunday"]
            return sorted(set(recurring_days), key=order.index)

    return []


def call_claude_for_audience(
    candidates: list[dict],
    newsletter: dict,
    audience: str,
    target_weekend: dict,
    skill_prompt: str,
) -> list[dict]:
    """Ask Claude to pick the best events for one audience across the whole
    weekend. Returns validated event dicts WITHOUT day/date assignment —
    those are derived from each candidate's text after Claude picks.
    URLs reattached from candidate_index.

    Two anchor dates passed in the prompt so Claude has full context, but
    Claude is told the candidate set was already date-filtered for the target
    weekend and should not re-filter."""
    if not candidates:
        return []

    indexed = [{**c, "candidate_index": i} for i, c in enumerate(candidates, 1)]
    candidates_json = json.dumps(indexed, indent=2)

    d = newsletter["demographics"]
    demo_summary = (
        f"Median household income: {d['median_income']}\n"
        f"Median age: {d['median_age']}\n"
        f"Family skew: {d['family_skew']}\n"
        f"Homeownership rate: {d['homeownership']}\n"
        f"Education level: {d['education']}"
    )

    user_prompt = f"""
Newsletter: {newsletter['name'].replace('_', ' ')} ({newsletter['display_area']})
Audience: {audience}
Target weekend: Fri {target_weekend['Friday']} / Sat {target_weekend['Saturday']} / Sun {target_weekend['Sunday']}

Audience demographics:
{demo_summary}

Anchor towns: {', '.join(newsletter['search_areas'])}

The candidates below have ALREADY been screened by the pipeline for:
domain quality, date range, duplicates, AND target-weekend day mapping.
Each candidate has a `days` field listing which of Fri/Sat/Sun it runs
on — that's the source of truth. Do NOT try to figure out the date or
day yourself. The pipeline already did it.

Your job is to PICK the best {TARGET_PER_AUDIENCE} or fewer for the
{audience.lower()} audience and WRITE the event description. Aim for at
least one pick covering each of Friday, Saturday, and Sunday if the
candidate pool supports it.

CRITICAL: each event belongs to ONLY ONE audience (Family OR Adult). Do
not include events that would feel equally appropriate for the OTHER
audience — pick the audience it fits best, leave it for that audience's
pool.

Use `candidate_index` to reference URLs — do NOT include raw URLs in
the output.

Candidates:
{candidates_json}
"""

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
        r["audience"] = audience
        # Attach the source candidate so we can later derive days from its text
        r["_source_candidate"] = source
        r.pop("candidate_index", None)
        validated.append(r)
    validated = prefer_primary_source(validated)
    return validated


def process_audience(
    newsletter: dict,
    audience: str,
    target_weekend: dict,
    skill_prompt: str,
    existing_urls: set,
) -> list[dict]:
    """Pool all 3 days' worth of Brave queries for one audience, send Claude
    one call, return the picked events. Each returned event includes a
    `_source_candidate` field so the caller can extract its days."""
    print(f"\n  ━━━ {audience} pool ━━━")

    target_range = (
        date.fromisoformat(target_weekend["Friday"]),
        date.fromisoformat(target_weekend["Sunday"]),
    )

    # Pool Brave queries across Friday + Saturday + Sunday for this audience.
    # Dedup is handled inside fetch_and_filter_candidates via `seen_urls`
    # within search_web, plus the excluded_urls set we maintain externally.
    all_queries: list[str] = []
    for day in DAYS:
        all_queries.extend(build_queries(newsletter, audience, day, target_weekend[day]))

    candidates = fetch_and_filter_candidates(
        all_queries, MAX_RESULTS_PER_QUERY, existing_urls,
        label=f"{audience.lower()} primary", target_range=target_range,
        target_weekend=target_weekend,
    )

    results = call_claude_for_audience(
        candidates, newsletter, audience, target_weekend, skill_prompt
    )
    print(f"    Primary pass: {len(results)} events accepted")

    # Backfill if below MIN_PER_AUDIENCE
    seen_candidate_urls = {c["url"] for c in candidates}
    if len(results) < MIN_PER_AUDIENCE:
        print(f"    Retrying broader (only {len(results)} events, want ≥{MIN_PER_AUDIENCE})")
        retry_excluded = set(existing_urls) | seen_candidate_urls
        retry_queries: list[str] = []
        for day in DAYS:
            retry_queries.extend(build_fallback_queries(newsletter, audience, day, target_weekend[day]))
        candidates_p2 = fetch_and_filter_candidates(
            retry_queries, RETRY_RESULTS_PER_QUERY, retry_excluded,
            label=f"{audience.lower()} retry", target_range=target_range,
            target_weekend=target_weekend,
        )
        more = call_claude_for_audience(
            candidates_p2, newsletter, audience, target_weekend, skill_prompt
        )
        seen = {r["source_url"] for r in results}
        added = 0
        for r in more:
            if r["source_url"] and r["source_url"] not in seen:
                results.append(r)
                seen.add(r["source_url"])
                added += 1
        seen_candidate_urls |= {c["url"] for c in candidates_p2}
        print(f"    Retry pass added {added} events ({audience} total: {len(results)})")

    # Per-day gap fill — hard rule: each of Fri/Sat/Sun must have at least
    # one pick per audience. After primary + retry, check which days the
    # current picks actually cover (via the source candidate's text) and
    # fire targeted queries for any day with zero coverage.
    def _covered_days(events: list[dict]) -> set[str]:
        covered = set()
        for ev in events:
            cand = ev.get("_source_candidate", {})
            days = cand.get("days") or determine_event_days(cand, target_weekend)
            for d in days:
                covered.add(d)
        return covered

    covered = _covered_days(results)
    missing = [d for d in DAYS if d not in covered]
    if missing:
        print(f"    Day coverage gap: {missing} uncovered — running targeted gap-fill")
        gap_excluded = set(existing_urls) | seen_candidate_urls | {r["source_url"] for r in results}
        seen = {r["source_url"] for r in results}
        for day in missing:
            gap_queries = (
                build_queries(newsletter, audience, day, target_weekend[day])
                + build_fallback_queries(newsletter, audience, day, target_weekend[day])
            )
            gap_target_range = (date.fromisoformat(target_weekend[day]),
                                date.fromisoformat(target_weekend[day]))
            gap_candidates = fetch_and_filter_candidates(
                gap_queries, RETRY_RESULTS_PER_QUERY, gap_excluded,
                label=f"{audience.lower()} gap-fill {day}",
                target_range=gap_target_range,
                target_weekend=target_weekend,
            )
            gap_picks = call_claude_for_audience(
                gap_candidates, newsletter, audience, target_weekend, skill_prompt
            )
            added_for_day = 0
            for r in gap_picks:
                if not r.get("source_url") or r["source_url"] in seen:
                    continue
                cand = r.get("_source_candidate", {})
                pre_tagged = cand.get("days") or determine_event_days(cand, target_weekend)
                day_match = day in pre_tagged
                if not day_match:
                    continue
                results.append(r)
                seen.add(r["source_url"])
                added_for_day += 1
            gap_excluded |= {c["url"] for c in gap_candidates}
            if added_for_day:
                print(f"    ✓ Gap-fill {day}: added {added_for_day}")
            else:
                print(f"    ⚠ Gap-fill {day}: still no coverage after targeted query")

    print(f"    ✓ {len(results)} {audience} events accepted")
    for r in results:
        print(f"      - {r.get('emoji', '')} {r.get('event_name', '?')}")
    return results


def process_bucket(
    newsletter: dict,
    audience: str,
    day: str,
    target_date_iso: str,
    skill_prompt: str,
    existing_urls: set,
) -> list[dict]:
    """Run one bucket with adaptive retry: if first pass yields too few
    events, run a second broader pass and merge."""
    print(f"\n  [{audience} / {day} / {target_date_iso}]")

    # The target weekend's Fri-Sun range — strict. Candidates must mention
    # a date inside this window OR have no parseable date (those are kept
    # as borderline, since many real events use vague wording like 'this
    # weekend' and we don't want to false-drop them).
    weekend = target_weekend_dates()
    target_range = (
        date.fromisoformat(weekend["Friday"]),
        date.fromisoformat(weekend["Sunday"]),
    )

    # Pass 1 — primary queries, normal result count
    primary_queries = build_queries(newsletter, audience, day, target_date_iso)
    candidates_p1 = fetch_and_filter_candidates(
        primary_queries, MAX_RESULTS_PER_QUERY, existing_urls,
        label="primary", target_range=target_range,
        target_weekend=weekend,
    )
    results = call_claude_for_bucket(
        candidates_p1, newsletter, audience, day, target_date_iso, skill_prompt
    )
    print(f"    Primary pass: {len(results)} events accepted")

    # Retry if Claude found too few qualifying events
    if len(results) < MIN_EVENTS_BEFORE_RETRY:
        print(f"    Retrying broader (reason: only {len(results)} events) — {RETRY_RESULTS_PER_QUERY} results/query")

        # Exclude URLs already in pass 1 so the retry pool is fresh
        retry_excluded = set(existing_urls) | {c["url"] for c in candidates_p1}

        fallback_queries = build_fallback_queries(newsletter, audience, day, target_date_iso)
        candidates_p2 = fetch_and_filter_candidates(
            fallback_queries, RETRY_RESULTS_PER_QUERY, retry_excluded,
            label="retry", target_range=target_range,
            target_weekend=weekend,
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

    for newsletter in filter_by_env():
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Normalize URLs before comparing so the same event with different
        # query strings / paths / trailing slashes dedupes correctly.
        # e.g. dreamhack.com/atlanta/tickets/?utm=x → dreamhack.com/atlanta/tickets
        def _normalize_url(u: str) -> str:
            if not u:
                return ""
            from urllib.parse import urlparse
            p = urlparse(u.strip())
            host = (p.hostname or "").lower().removeprefix("www.")
            path = (p.path or "/").rstrip("/").lower()
            return f"{p.scheme}://{host}{path}"

        existing_url_set = get_existing_weekend_event_urls(newsletter["name"])
        # Stored normalized so any URL variant of an already-saved event matches
        existing_norm = {_normalize_url(u) for u in existing_url_set}
        print(f"  {len(existing_norm)} existing URLs in Notion (cross-run dedup)")

        # Audience-pooled architecture:
        #   • Family processed first → list of events with day(s) extracted
        #   • Family URLs AND event-name tokens added to exclusion → Adult
        #     can't repeat any event even via a different URL or rephrasing
        #   • Adult processed second on the remaining candidate space
        # Each picked event expands to N rows (one per day it runs).
        import re as _re_audience

        def _event_name_key(name: str) -> str:
            """Normalize event name for fuzzy cross-audience matching.
            'Marietta Greek Festival' and '36th Annual Marietta Greek Festival'
            both reduce to {'greek', 'festival', 'marietta'} (after dropping
            year/qualifier tokens and short words)."""
            tokens = _re_audience.findall(r"\w+", (name or "").lower())
            STOP = {"the", "and", "for", "with", "at", "an", "a", "of", "on",
                    "to", "annual", "th", "st", "nd", "rd"}
            return frozenset(t for t in tokens
                             if len(t) > 2 and not t.isdigit() and t not in STOP)

        family_name_keys: set[frozenset] = set()

        all_events: list[dict] = []
        for audience in AUDIENCES:  # ["Family", "Adult"] — Family first
            picks = process_audience(
                newsletter=newsletter,
                audience=audience,
                target_weekend=weekend,
                skill_prompt=skill_prompt,
                existing_urls=existing_url_set,
            )
            # Cross-audience name dedup for Adult: drop picks whose name-key
            # significantly overlaps a Family pick's name-key.
            if audience == "Adult" and family_name_keys:
                filtered_picks = []
                for pick in picks:
                    pk = _event_name_key(pick.get("event_name", ""))
                    if not pk:
                        filtered_picks.append(pick)
                        continue
                    overlap_with_family = max(
                        (len(pk & fam_pk) / max(len(pk), 1) for fam_pk in family_name_keys),
                        default=0,
                    )
                    if overlap_with_family >= 0.6:  # ≥60% token overlap = same event
                        print(f"      ✗ cross-audience dedup: dropping Adult '{pick.get('event_name','?')[:50]}' (matches Family pick)")
                        continue
                    filtered_picks.append(pick)
                picks = filtered_picks

            # Expand each pick into one row per day the event runs. The
            # pipeline already tagged each candidate with `days` pre-Claude
            # (see fetch_and_filter_candidates), so we just read it back
            # off the source candidate. If it's missing for any reason,
            # fall back to determine_event_days for safety.
            for pick in picks:
                source_cand = pick.pop("_source_candidate", {})
                days = source_cand.get("days") or determine_event_days(source_cand, weekend)
                if not days:
                    print(f"      ✗ dropped (no date matches target weekend "
                          f"{weekend['Friday']}..{weekend['Sunday']}): "
                          f"{pick.get('event_name','?')[:60]}")
                    continue
                for day_name in days:
                    row = dict(pick)  # shallow copy per day
                    row["day"]  = day_name
                    row["date"] = weekend[day_name]
                    all_events.append(row)
                print(f"      ↳ {pick.get('event_name','?')[:50]} runs: {days}")
            # Track Family's name-keys so Adult can avoid them
            if audience == "Family":
                for pick in picks:
                    family_name_keys.add(_event_name_key(pick.get("event_name", "")))
            # URL-based cross-audience exclusion (belt-and-suspenders alongside
            # name-token matching above)
            for pick in picks:
                for k in ("source_url", "source_url_aggregator"):
                    u = pick.get(k)
                    if u:
                        existing_url_set.add(u)
                        existing_norm.add(_normalize_url(u))
            time.sleep(0.5)

        if not all_events:
            print(f"\n  No events accepted for {newsletter['name']}. Skipping save.")
            continue

        # Final within-run dedup: same (normalized_url, day) tuple is a true
        # duplicate. Same URL on different days of the same audience is OK
        # (multi-day events legitimately appear on each day). Same URL
        # across audiences should NOT happen given the cross-audience
        # exclusion set above — this is belt-and-suspenders.
        seen = set()
        deduped = []
        for ev in all_events:
            key = (_normalize_url(ev.get("source_url", "")),
                   ev.get("audience", ""), ev.get("day", ""))
            if key in seen:
                print(f"  ✗ within-run dedup: dropping {ev.get('event_name','?')[:50]} ({key[2]})")
                continue
            seen.add(key)
            deduped.append(ev)
        if len(deduped) != len(all_events):
            print(f"  ↳ within-run dedup: {len(all_events)} → {len(deduped)} rows")
        all_events = deduped

        # Report final counts per audience to verify mins are met
        from collections import Counter
        per_audience = Counter()
        for ev in all_events:
            per_audience[ev["audience"]] += 1
        for aud in AUDIENCES:
            count = per_audience.get(aud, 0)
            mark = "✓" if count >= MIN_PER_AUDIENCE else "⚠"
            print(f"  {mark} {aud}: {count} rows (min {MIN_PER_AUDIENCE})")

        print(f"\n  Saving {len(all_events)} total events for {newsletter['name']}...")
        save_weekend_events_to_notion(all_events, newsletter["name"])

        # Local JSON backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"weekend_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(all_events, indent=2), encoding="utf-8")
        print(f"  Saved JSON backup to {json_file}")

    print(f"\nAll newsletters complete.")
