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

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from brave_search import search_web, domain_of
from claude_json import call_with_json_output
from notion_helper import (
    save_weekend_events_to_notion,
    query_database,
    update_page,
    NOTION_WEEKEND_EVENTS_DB_ID,
)
from newsletters_config import NEWSLETTERS, filter_by_env
from event_date_filter import upcoming_friday, filter_candidates_by_date, filter_candidates_in_date_range, effective_today

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-weekend-planner-skill_auto.md"

TARGET_PER_AUDIENCE = 20     # Soft target per audience. The pool depth IS the section length
MIN_PER_AUDIENCE    = 20     # now (Claude can't reject), so this is informational. If Adult
                             # ends up short, family events get promoted (one-way) in main.
MIN_PER_DAY         = 5      # Surfaced as a warning when the scraper pool is thin on a given
                             # day. Not enforced (no gap-fill anymore) — surfacing is the
                             # signal to add scraper coverage upstream.
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
    """Return ISO dates for the UPCOMING Friday/Saturday/Sunday.

    Always looks FORWARD: if today is Sat/Sun, advances to NEXT Friday rather
    than snapping back to the in-progress weekend (which would have already
    passed for newsletter-prep purposes).

    When `today` is None, anchors to `effective_today()` which honors the
    ISSUE_DATE env override. So passing ISSUE_DATE=05/21/2026 (a Thursday)
    automatically targets the May 22–24 weekend with no other code changes."""
    if today is None:
        today = datetime.combine(effective_today(), datetime.min.time())
    weekday = today.weekday()  # Mon=0 ... Sun=6
    # Mon-Fri → days to this Friday (0 if today is Friday).
    # Sat/Sun → days to NEXT Friday (6 or 5).
    days_until_friday = (4 - weekday) % 7
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
# ---------------------------------------------------------------------------
# Notion candidate pool — replaces the Brave-search flow. One pull per
# newsletter, cached for the run so all audiences share the same pool.
# ---------------------------------------------------------------------------
# Mirror of Featured_Event.py's SHARED_NEWSLETTER_TAGS so ECC also
# pulls ECC_PP-tagged events (Sandy Springs is shared territory).
_SHARED_NEWSLETTER_TAGS = {
    "East_Cobb_Connect":       ["East_Cobb_Connect", "ECC_PP"],
    "Perimeter_Post":          ["Perimeter_Post",    "ECC_PP"],
    "Lewisville_Lake_Lookout": ["Lewisville_Lake_Lookout"],
}

# Two-tier pool strategy (added 2026-05-20):
#   Primary  — only status='approved'. Use these picks as-is.
#   Fallback — if the approved pool has fewer than MIN_PER_DAY events
#              for any day, expand to ALL non-archived rows (so pending /
#              featured / wp_used / rejected all become eligible too).
# This lets editors pre-approve picks in Notion when they're available
# while still falling back to the raw scraper output when approval volume
# is thin. `archived` is the only hard-excluded status (past events).
_FALLBACK_EXCLUDE_STATUSES = ("archived",)


def _rich_text_value(prop) -> str:
    if not isinstance(prop, dict):
        return ""
    chunks = prop.get("rich_text") or prop.get("title") or []
    return "".join(c.get("plain_text", "") for c in chunks).strip()


def fetch_weekend_events_from_notion(newsletter_name: str,
                                     target_weekend: dict,
                                     status_equals: str | None = None,
                                     status_excludes: tuple[str, ...] = (),
                                     ) -> list[dict]:
    """Query the Weekend Events Notion DB for rows tagged with this
    newsletter (or the shared ECC_PP tag) whose Date falls in
    [Friday, Sunday] of the target weekend.

    Pass either `status_equals` (only rows with exactly that Status) or
    `status_excludes` (rows whose Status is none of those) — not both.

    Returns dicts shaped like Brave search_web output (url, title,
    description, age) so the downstream Claude eval is shape-agnostic.
    Each row also carries `notion_page_id` so we can PATCH its Status
    after a pick."""
    if not NOTION_WEEKEND_EVENTS_DB_ID:
        print("  ⚠ NOTION_WEEKEND_EVENTS_DB_ID not set — no Notion pool")
        return []
    tags = _SHARED_NEWSLETTER_TAGS.get(newsletter_name, [newsletter_name])
    if len(tags) == 1:
        nl_clause = {"property": "Newsletter", "select": {"equals": tags[0]}}
    else:
        nl_clause = {"or": [
            {"property": "Newsletter", "select": {"equals": t}} for t in tags
        ]}
    friday = target_weekend["Friday"]
    sunday = target_weekend["Sunday"]
    status_clauses: list = []
    if status_equals:
        status_clauses.append({"property": "Status", "select": {"equals": status_equals}})
    for s in status_excludes:
        status_clauses.append({"property": "Status", "select": {"does_not_equal": s}})
    filters = {
        "and": [
            nl_clause,
            {"property": "Date", "date": {"on_or_after": friday}},
            {"property": "Date", "date": {"on_or_before": sunday}},
        ] + status_clauses
    }
    pages = query_database(NOTION_WEEKEND_EVENTS_DB_ID, filters=filters) or []
    # Map the target weekend ISO dates → day labels so we can pre-fill
    # `days` directly from each row's structured Date / Dates fields.
    # Without this, downstream determine_event_days() re-scans the title
    # text for date mentions — and most scraped rows don't repeat the
    # date in their summary, so it returns [] and the pick gets dropped
    # even though we already know the date from Notion.
    weekend_day_by_iso = {
        target_weekend["Friday"]:   "Friday",
        target_weekend["Saturday"]: "Saturday",
        target_weekend["Sunday"]:   "Sunday",
    }
    # Lazy import — event_image_scraper lives in NewsletterCreation/Code,
    # already on sys.path via the image-fetch step further down.
    try:
        sys.path.append(os.path.join(os.path.dirname(__file__),
                                     '..', '..', 'NewsletterCreation', 'Code'))
        from event_image_scraper import is_cancelled_event
    except Exception:
        is_cancelled_event = lambda t, d="": False  # noqa: E731

    out: list[dict] = []
    dropped_cancelled = 0
    for p in pages:
        props = p.get("properties", {})
        title = _rich_text_value(props.get("Event Name")) or _rich_text_value(props.get("Name"))
        url   = (props.get("Source URL", {}).get("url") or "").strip()
        if not title or not url:
            continue
        date_prop = (props.get("Date") or {}).get("date") or {}
        start_str = (date_prop.get("start") or "")[:10]
        description = _rich_text_value(props.get("Description"))
        if is_cancelled_event(title, description):
            dropped_cancelled += 1
            continue
        venue   = _rich_text_value(props.get("Location"))
        address = _rich_text_value(props.get("Address"))
        # Pre-fill `days` from structured Notion fields. Start with the
        # row's primary Date, then merge any ISO dates parsed out of the
        # `Dates` rich-text field (for recurring events).
        days: list[str] = []
        if start_str in weekend_day_by_iso:
            days.append(weekend_day_by_iso[start_str])
        dates_text = _rich_text_value(props.get("Dates"))
        if dates_text:
            import re as _re_iso
            for iso in _re_iso.findall(r"\d{4}-\d{2}-\d{2}", dates_text):
                label = weekend_day_by_iso.get(iso)
                if label and label not in days:
                    days.append(label)
        if days:
            order = ["Friday", "Saturday", "Sunday"]
            days = sorted(days, key=order.index)
        out.append({
            "title":       title,
            "url":         url,
            "description": description,
            "summary":     description[:600],
            "age":         start_str,
            "date":        start_str,
            "venue":       venue,
            "address":     address,
            "image_url":   (props.get("Image URL", {}).get("url") or "").strip(),
            "notion_page_id": p.get("id"),
            "_from_notion": True,
            "days":        days,
        })
    status_label = (f"status={status_equals}" if status_equals
                    else f"status not in {status_excludes}" if status_excludes
                    else "any status")
    cancel_note = (f", dropped {dropped_cancelled} cancelled"
                   if dropped_cancelled else "")
    print(f"  Notion pool: {len(out)} candidate(s) in window "
          f"({friday} → {sunday}) for {tags}, {status_label}{cancel_note}")
    return out


# Per-run cache: avoid re-querying Notion for each audience.
_NOTION_POOL_CACHE: dict[tuple[str, str], list[dict]] = {}


def get_notion_pool(newsletter_name: str,
                    target_weekend: dict) -> list[dict]:
    """Cached wrapper around fetch_weekend_events_from_notion. The
    Notion query runs once per (newsletter, weekend).

    Two-tier strategy (added 2026-05-20):
      1. Query for status='approved' first.
      2. If the approved pool covers every day with at least MIN_PER_DAY
         candidates, use it as-is.
      3. Otherwise fall back to ALL non-archived rows so we don't ship
         a thin section. Editors who want strict approved-only behavior
         should make sure ≥{MIN_PER_DAY} events per day are approved
         before the WP run.
    """
    key = (newsletter_name, target_weekend.get("Friday", ""))
    if key not in _NOTION_POOL_CACHE:
        approved = fetch_weekend_events_from_notion(
            newsletter_name, target_weekend,
            status_equals="approved",
        )
        per_day = {d: sum(1 for c in approved if d in (c.get("days") or [])) for d in DAYS}
        thin_days = [d for d in DAYS if per_day[d] < MIN_PER_DAY]
        if not thin_days:
            print(f"  ✓ Approved pool is sufficient: per-day {per_day}")
            _NOTION_POOL_CACHE[key] = approved
        else:
            print(f"  ⚠ Approved pool thin on {thin_days} (per-day {per_day}); "
                  f"falling back to all non-archived events")
            _NOTION_POOL_CACHE[key] = fetch_weekend_events_from_notion(
                newsletter_name, target_weekend,
                status_excludes=_FALLBACK_EXCLUDE_STATUSES,
            )
    return list(_NOTION_POOL_CACHE[key])


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

    # Aggregator handling: ONLY expand aggregator URLs that look like
    # listicles. Tag archives, business directories, news landing pages
    # and bare homepages get kept as single candidates instead of being
    # expanded into their sidebar/related-stories noise.
    try:
        from aggregator_drilldown import expand_listicle as _expand_listicle
    except Exception:
        _expand_listicle = None

    # Listicle title/URL markers (mirrors Featured Event).
    _LISTICLE_MARKERS = (
        "things to do", "things-to-do", "5 things", "10 things",
        "weekend checklist", "weekend events", "weekend roundup",
        "weekend guide", "your weekend", "events this weekend",
        "events this week", "events you absolutely need",
        "out and about", "what to do this", "what's happening",
        "upcoming events", "calendar of events", "events calendar",
        "fun things to do", "guide to events", "things to do in",
    )
    _NON_LISTICLE_URL_PATTERNS = (
        "/tag/", "/tags/", "/category/", "/categories/",
        "/author/", "/authors/", "/archives/", "/archive/",
        "/business/listing/", "/businesses/",
        "/news/local", "/news/police", "/news/crime",
    )

    def _is_listicle(c: dict) -> bool:
        blob = f"{c.get('title','')} {c.get('url','')}".lower()
        return any(m in blob for m in _LISTICLE_MARKERS)

    def _is_landing_or_archive(url: str) -> bool:
        from urllib.parse import urlparse as _up
        p = _up(url)
        path = (p.path or "").lower().rstrip("/")
        if not path:
            return True
        return any(pat in path for pat in _NON_LISTICLE_URL_PATTERNS)

    # Expand any candidate that looks like a listicle (title/URL marker),
    # regardless of host. Government calendars (cobbcounty.gov) and city
    # tourism sites also publish event roundups. Drop only aggregator
    # landing/archive URLs (they have no event content).
    expanded_count = 0
    expanded_total = 0
    kept_single    = 0
    dropped_count  = 0
    keep_pool = []
    for c in candidates:
        url = c.get("url", "")
        # Step 1: aggregator landing/archive → drop entirely.
        if is_aggregator(url) and _is_landing_or_archive(url):
            dropped_count += 1
            continue
        # Step 2: anything that looks like a listicle → expand.
        if _expand_listicle is not None and _is_listicle(c):
            events = _expand_listicle(url, listicle_title=c.get("title", ""))
            if events:
                expanded_count += 1
                expanded_total += len(events)
                keep_pool.extend(events)
            else:
                # No event links found; keep the original URL so date filter
                # can still consider it.
                keep_pool.append(c)
            continue
        # Step 3: everything else → keep as single candidate.
        keep_pool.append(c)
        if is_aggregator(url):
            kept_single += 1
    if expanded_count:
        print(f"    [{label}] expanded {expanded_count} listicle(s) into {expanded_total} candidates")
    if kept_single:
        print(f"    [{label}] kept {kept_single} aggregator(s) as single candidate (not a listicle)")
    if dropped_count:
        print(f"    [{label}] dropped {dropped_count} aggregator(s) that yielded no events")
    candidates = keep_pool

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
        # Generous max_tokens because the response is a JSON array of
        # up to 20 events × ~200 tokens of structured content each.
        # The default 4000 routinely truncated the array mid-event,
        # breaking JSON parsing and forcing the fallback gap-fill path
        # for both audiences on every run.
        results = call_with_json_output(
            api_key=CLAUDE_API_KEY,
            system=skill_prompt,
            user_content=user_prompt,
            max_tokens=12000,
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


def call_claude_for_pool(
    candidates: list[dict],
    newsletter: dict,
    target_weekend: dict,
    skill_prompt: str,
) -> list[dict]:
    """One Claude call over the entire weekend pool. Claude's only jobs:
    classify each event as `audience: "Family"` or `"Adult"` and write a
    blurb. EVERY candidate must appear in the output — the pipeline already
    pre-filtered by date (Notion window query) and dedup, so Claude has
    no rejection power here.

    If Claude still drops candidates (e.g. truncated output, classification
    miss), the missing ones are backfilled code-side with audience='Family'
    (the safe default for local-event scrapers) and a fallback blurb drawn
    from the candidate's own description. This guarantees the scraper pool
    IS the published curation.

    HARD venue/city exclusion (newsletter["excluded_venues"|"excluded_cities"])
    still applies post-pass — out-of-range events are dropped regardless of
    what Claude returns.
    """
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

    excluded_venues_raw = newsletter.get("excluded_venues") or []
    excluded_cities_raw = newsletter.get("excluded_cities") or []
    exclusion_block = ""
    if excluded_venues_raw or excluded_cities_raw:
        lines = ["",
                 "OUT OF RANGE — these venues/cities are outside coverage.",
                 "Still classify them and write a blurb (the pipeline drops",
                 "them in post — don't pre-skip):"]
        if excluded_venues_raw:
            lines.append("Venues:")
            for v in excluded_venues_raw:
                lines.append(f"  - {v}")
        if excluded_cities_raw:
            lines.append("Cities:")
            for c in excluded_cities_raw:
                lines.append(f"  - {c}")
        exclusion_block = "\n".join(lines)

    user_prompt = f"""
Newsletter: {newsletter['name'].replace('_', ' ')} ({newsletter['display_area']})
Target weekend: Fri {target_weekend['Friday']} / Sat {target_weekend['Saturday']} / Sun {target_weekend['Sunday']}

Audience demographics:
{demo_summary}

Anchor towns: {', '.join(newsletter['search_areas'])}
{exclusion_block}

The {len(candidates)} candidates below have already been scraped from
local event calendars AND pre-filtered for date (every one runs during
this target weekend). Pre-filtering is done. Your job is NOT to filter.

YOUR JOB:
1. Classify each event as either `audience: "Family"` or `audience: "Adult"`.
   - Family: kid-friendly, all-ages, daytime, libraries, parks, festivals,
     markets, museums, family workshops, community events.
   - Adult: 21+, nightlife, bars/breweries/wineries, late-night shows,
     adult-only classes, professional networking, mature themes.
   - When in doubt → Family. Most local events are family-appropriate.
2. Write `event_name`, `emoji`, and a short blurb per the skill schema.

INCLUDE EVERY CANDIDATE. The scraper-driven pool IS the curation. There
is no downstream filtering. Return exactly {len(candidates)} entries —
one per `candidate_index` from 1 to {len(candidates)}, no skips. A
borderline / vague / recurring / generic event still gets classified and
blurbed — do NOT drop it for being weak. The four old hard-skip cases
(cancelled, wrong-audience, out-of-range, duplicate) no longer apply
here — out-of-range is handled by the pipeline post-pass, and dedup
already happened upstream.

CRITICAL: each event belongs to ONE audience (Family OR Adult). Don't
emit the same `candidate_index` twice.

Use `candidate_index` to reference URLs. Do NOT include raw URLs in
the output.

Candidates:
{candidates_json}
"""

    try:
        # Generous max_tokens because the response is a JSON array of up
        # to ~50+ events x ~200 tokens each. Bumped above the previous
        # 12000 because the pool can now be much larger (whole weekend
        # pool in one call, not per-audience).
        results = call_with_json_output(
            api_key=CLAUDE_API_KEY,
            system=skill_prompt,
            user_content=user_prompt,
            max_tokens=16000,
        )
    except Exception as e:
        print(f"    ✗ Claude error: {e}")
        results = []

    candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}
    excluded_venues = [v.lower() for v in excluded_venues_raw]
    excluded_cities = [c.lower() for c in excluded_cities_raw]

    def _is_out_of_range(haystack: str) -> str:
        if not (excluded_venues or excluded_cities):
            return ""
        hl = haystack.lower()
        for v in excluded_venues:
            if v in hl:
                return v
        for c in excluded_cities:
            if c in hl:
                return c
        return ""

    validated_by_idx: dict[int, dict] = {}
    for r in results or []:
        idx = r.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        source = candidates_by_index.get(idx) if idx is not None else None
        if not source:
            print(f"    ✗ Discarding entry with invalid candidate_index "
                  f"{idx}: {r.get('event_name', '?')}")
            continue
        haystack = " | ".join(str(r.get(k, "") or "") for k in
                              ("venue", "address", "event_name"))
        hit = _is_out_of_range(haystack)
        if hit:
            print(f"    ✗ Dropping out-of-range event "
                  f"'{r.get('event_name','?')[:50]}' (matched: {hit})")
            continue
        aud = (r.get("audience") or "").strip().capitalize()
        if aud not in ("Family", "Adult"):
            aud = "Family"
        r["audience"] = aud
        r["source_url"] = source.get("url", "")
        r["_source_candidate"] = source
        r.pop("candidate_index", None)
        validated_by_idx[idx] = r

    # Backfill any candidates Claude dropped. The whole point of this
    # flow is "every event gets through" — if Claude skipped or the
    # response truncated, we synthesize a row from the source candidate.
    missing = [(i, c) for i, c in candidates_by_index.items()
               if i not in validated_by_idx]
    if missing:
        print(f"    ⚠ Claude returned {len(validated_by_idx)}/"
              f"{len(candidates_by_index)} — backfilling {len(missing)} "
              f"from source descriptions")
        for idx, c in missing:
            haystack = " | ".join(str(c.get(k, "") or "") for k in
                                  ("venue", "address", "title"))
            hit = _is_out_of_range(haystack)
            if hit:
                print(f"    ✗ Dropping out-of-range skipped event "
                      f"'{c.get('title','?')[:50]}' (matched: {hit})")
                continue
            desc = (c.get("description") or "").strip()
            blurb = desc[:400] if desc else c.get("title", "")
            validated_by_idx[idx] = {
                "event_name":  c.get("title", "Event"),
                "emoji":       "📅",
                "summary":     blurb,
                "venue":       c.get("venue", ""),
                "address":     c.get("address", ""),
                "audience":    "Family",
                "source_url":  c.get("url", ""),
                "_source_candidate": c,
                "_backfilled": True,
            }

    validated = list(validated_by_idx.values())
    validated = prefer_primary_source(validated)
    # Diagnostic: if any classified row reached this point without a
    # source_url we're about to ship URL-less rows to Notion. Log loudly
    # so the failure mode is visible in the run output.
    missing_url = [v for v in validated if not v.get("source_url")]
    if missing_url:
        print(f"    ⚠ {len(missing_url)} classified event(s) have no source_url "
              f"after Claude pool pass — this is a bug; URLs will be blank in Notion:")
        for v in missing_url[:5]:
            print(f"        · {v.get('event_name','?')[:60]}  "
                  f"(candidate had url={v.get('_source_candidate',{}).get('url','')!r})")
    return validated


def process_pool(
    newsletter: dict,
    target_weekend: dict,
    skill_prompt: str,
) -> list[dict]:
    """One Notion pull, one Claude call, every event kept. Returns a flat
    list of classified picks with `audience` set on each. Day-expansion
    and within-run dedup happen in the main loop."""
    print(f"\n  ━━━ Weekend pool ━━━")
    candidates = get_notion_pool(newsletter["name"], target_weekend)
    if not candidates:
        print(f"    No Notion candidates for this weekend")
        return []

    pool_per_day = {d: sum(1 for c in candidates if d in (c.get("days") or []))
                    for d in DAYS}
    print(f"    Pool depth per day: " +
          ", ".join(f"{d}={pool_per_day[d]}" for d in DAYS))
    for d in DAYS:
        if pool_per_day[d] < MIN_PER_DAY:
            print(f"    ⚠ {d}: pool has only {pool_per_day[d]} candidates "
                  f"(< {MIN_PER_DAY} required) — scraper coverage gap")

    picks = call_claude_for_pool(candidates, newsletter, target_weekend, skill_prompt)
    from collections import Counter
    by_aud = Counter(p["audience"] for p in picks)
    print(f"    Claude classified {len(picks)} event(s): " +
          ", ".join(f"{a}={by_aud.get(a,0)}" for a in AUDIENCES))
    for p in picks:
        tag = " [backfilled]" if p.get("_backfilled") else ""
        print(f"      - [{p['audience']}] {p.get('emoji','')} "
              f"{p.get('event_name','?')[:60]}{tag}")
    return picks


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

    # target_weekend_dates() auto-honors the ISSUE_DATE env override via
    # effective_today(). No explicit threading required.
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

        # Single-pool architecture: one Notion query, one Claude call.
        # Claude classifies each candidate Family-or-Adult and writes a
        # blurb. Every candidate gets through — no rejection. The
        # save-time dedup in save_weekend_events_to_notion (date-scoped
        # to the same target weekend) handles same-week re-runs, so we
        # don't pre-filter the pool by URL here.
        picks = process_pool(
            newsletter=newsletter,
            target_weekend=weekend,
            skill_prompt=skill_prompt,
        )

        all_events: list[dict] = []
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

        # One-way Family → Adult backfill. If Adult is short of the
        # TARGET, take the lowest-ranked Family events and re-tag them
        # as Adult — Family events are generally acceptable for Adult
        # audiences too, just less optimal. Reverse (Adult → Family) is
        # NOT done because adult-only events (bars, nightlife, 21+)
        # shouldn't end up in the Family section.
        #
        # FAMILY_FLOOR caps how much we'll drain Family to fill Adult.
        # Set to half the target so Family never gets gutted, but the
        # gate is loose enough that an Adult-short run actually gets
        # backfilled (previously this required Family > MIN, which with
        # MIN=TARGET=20 meant Family had to over-hit before promotion
        # could even start).
        FAMILY_FLOOR = MIN_PER_AUDIENCE // 2
        from collections import Counter
        per_audience = Counter(ev["audience"] for ev in all_events)
        adult_count  = per_audience.get("Adult",  0)
        family_count = per_audience.get("Family", 0)
        if adult_count < TARGET_PER_AUDIENCE and family_count > FAMILY_FLOOR:
            need = TARGET_PER_AUDIENCE - adult_count
            # Use Family events that have a `total_score` if Claude
            # ranked them; otherwise just take the last `need` Family
            # entries (assumed to be lowest priority).
            family_events = [e for e in all_events if e["audience"] == "Family"]
            family_events.sort(key=lambda e: e.get("total_score", 0))
            moved = 0
            for ev in family_events:
                if moved >= need:
                    break
                if family_count - 1 < FAMILY_FLOOR:
                    break
                ev["audience"] = "Adult"
                ev["audience_promoted_from"] = "Family"
                family_count -= 1
                adult_count  += 1
                moved += 1
                print(f"  ↳ Promoted Family → Adult: {ev.get('event_name','?')[:50]}")
            if moved:
                print(f"  ↳ Family→Adult backfill moved {moved} event(s) "
                      f"(Family now {family_count}, Adult now {adult_count}, "
                      f"family floor={FAMILY_FLOOR})")

        per_audience = Counter(ev["audience"] for ev in all_events)
        for aud in AUDIENCES:
            count = per_audience.get(aud, 0)
            mark = "✓" if count >= MIN_PER_AUDIENCE else "⚠"
            print(f"  {mark} {aud}: {count} rows (min {MIN_PER_AUDIENCE})")

        # Per-event image scrape: cheap og:image / JSON-LD fetch from the
        # source URL. Used by the assemble script to render a small
        # thumbnail next to each event in the Notion landing page.
        try:
            sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'Free Events', 'Code'))
            from Free_Events import fetch_event_image  # noqa: E402
            seen_imgs: set[str] = set()
            print(f"\n  Fetching event images...")
            for ev in all_events:
                if ev.get("image_url"):
                    continue
                url = ev.get("source_url") or ""
                if not url:
                    continue
                try:
                    img = fetch_event_image(url)
                except Exception:
                    img = ""
                if not img:
                    continue
                # Skip if another event in this batch already claimed this
                # image (avoids sitewide-widget bleed across events).
                norm = img.split("?")[0].rstrip("/").lower()
                if norm in seen_imgs:
                    continue
                seen_imgs.add(norm)
                ev["image_url"] = img
            with_img = sum(1 for e in all_events if e.get("image_url"))
            print(f"  ✓ {with_img}/{len(all_events)} events have an image")
        except Exception as e:
            print(f"  ⚠ event image fetch skipped ({e})")

        # One photo per (audience, day) — at most six images render in the
        # Weekend Planner section (Friday/Saturday/Sunday × Family/Adult).
        # Also enforce no URL reuse across slots: if Family Friday and
        # Family Saturday would both grab the same recurring event's photo,
        # only the first slot gets it and Saturday falls through to the
        # next-best event with a still-unused image.
        used_image_urls: set[str] = set()
        for aud in AUDIENCES:
            for day in ("Friday", "Saturday", "Sunday"):
                in_slot = [e for e in all_events
                           if e.get("audience") == aud
                           and e.get("day") == day
                           and e.get("image_url")]
                in_slot.sort(key=lambda e: e.get("total_score", 0), reverse=True)
                kept = None
                for ev in in_slot:
                    if ev["image_url"] not in used_image_urls:
                        kept = ev
                        used_image_urls.add(ev["image_url"])
                        break
                cleared = 0
                for ev in in_slot:
                    if ev is not kept:
                        ev.pop("image_url", None)
                        cleared += 1
                if kept:
                    print(f"  ↳ {aud} {day}: keeping image on "
                          f"'{kept.get('event_name','?')[:50]}', cleared {cleared} other(s)")
                elif in_slot:
                    print(f"  ↳ {aud} {day}: no unique image available "
                          f"(all {len(in_slot)} candidates' images already used elsewhere)")

        # Half-size each surviving image. Notion's image_block API has no
        # width control — display size is the file's pixel size, capped at
        # column width. So to render images half-size we downscale the file,
        # save locally, and swap the URL to a stable gh-pages location.
        # The workflow's "Publish Weekend Planner images" step commits these
        # to gh-pages/gifs/ where Notion can fetch them.
        try:
            import io as _io
            from PIL import Image as _PILImage
            img_out_dir = Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"
            img_out_dir.mkdir(parents=True, exist_ok=True)
            MAX_DIM = 600   # px — half of the typical 1200-px source
            for ev in all_events:
                src_url = ev.get("image_url")
                if not src_url:
                    continue
                try:
                    r = requests.get(src_url, timeout=15,
                                     headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code != 200 or not r.content:
                        continue
                    img = _PILImage.open(_io.BytesIO(r.content))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.thumbnail((MAX_DIM, MAX_DIM))  # in-place; preserves aspect
                    slug = "".join(
                        c if c.isalnum() else "_"
                        for c in (ev.get("event_name") or "evt")
                    )[:40] or "evt"
                    fname = (f"wp_event_{newsletter['name']}_"
                             f"{ev.get('audience','x')}_{slug}.jpg")
                    out_path = img_out_dir / fname
                    img.save(out_path, "JPEG", quality=85, optimize=True)
                    # Cache-bust so Notion picks up the new file each run.
                    cache_bust = int(datetime.today().timestamp())
                    ev["image_url"] = (
                        f"https://peachyinsurance.github.io/newsletters/gifs/"
                        f"{fname}?v={cache_bust}"
                    )
                    print(f"    ✓ resized & localized: {fname} "
                          f"({img.size[0]}x{img.size[1]}, {out_path.stat().st_size:,} bytes)")
                except Exception as e:
                    print(f"    · resize skipped for {ev.get('event_name','?')[:40]}: {e}")
        except Exception as e:
            print(f"  ⚠ event image resize stage skipped ({e})")

        # (Canva-style banner removed at user request — Weekend Planner
        # renders as a plain section without the templated header image.)

        print(f"\n  Saving {len(all_events)} total events for {newsletter['name']}...")
        save_weekend_events_to_notion(all_events, newsletter["name"],
                                      target_weekend=weekend)

        # Mark each picked source row in the Weekend Events DB as
        # 'wp_used' so the next Featured Event / Weekend Planner run
        # skips them. We dedup page_ids first so a multi-day event
        # (one source row → N rendered rows) only gets PATCHed once.
        page_ids_to_mark: set[str] = set()
        for ev in all_events:
            cand = ev.get("_source_candidate") or {}
            pid = cand.get("notion_page_id") or ev.get("notion_page_id")
            if pid:
                page_ids_to_mark.add(pid)
        marked = 0
        for pid in page_ids_to_mark:
            try:
                update_page(pid, {"Status": {"select": {"name": "wp_used"}}})
                marked += 1
            except Exception as e:
                print(f"  ⚠ couldn't mark page {pid[:8]}… as wp_used: {e}")
        if marked:
            print(f"  ↳ Marked {marked} Weekend Events row(s) as Status='wp_used'")

        # Local JSON backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"weekend_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(all_events, indent=2), encoding="utf-8")
        print(f"  Saved JSON backup to {json_file}")

    print(f"\nAll newsletters complete.")
