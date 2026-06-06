#!/usr/bin/env python3
"""
Newsletter Automation - Featured Event Section

Pipeline:
  1. Pull upcoming events from the Weekend Events Notion DB (populated by
     per-newsletter scrapers in Weekend Events/Code/<Newsletter>/).
  2. Date-window filter: only events whose start_date is between this
     week's upcoming Friday and 14 days later.
  3. Claude pass 1 — title-only: pick the top 10 events by title alone.
  4. Claude pass 2 — full eval: feed those 10 (title + description) into
     the existing scoring/blurb prompt and pick the winners.
  5. Image lookup: scraped image from the DB row is the primary; we
     additionally pull og:image candidates and Brave Image Search results
     to build a gallery the reviewer can swap between.

Saves results to the Featured Event Notion DB (NOTION_EVENTS_DB_ID).
"""
import os
import sys
import json
import time
from datetime import datetime, date, timedelta

from pathlib import Path

import requests
import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import (
    save_events_to_notion,
    get_existing_event_urls,
    query_database,
    update_page,
    NOTION_WEEKEND_EVENTS_DB_ID,
    NOTION_EVENTS_DB_ID,
)
from newsletters_config import NEWSLETTERS, filter_by_env
# Shared event-date filtering (Friday floor + parsing) lives in
# NewsletterCreation/Code/event_date_filter.py — used by Featured Event,
# Free Events, and Weekend Planner.
from event_date_filter import (
    section_date_window,
    filter_events_to_window,
)

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

from voice_helper import with_voice  # noqa: E402
SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-featured-event-skill_auto.md"

MAX_RESULTS_PER_QUERY = 10
# Pass-1 (title only) narrows the pool to TOP_N_TITLES candidates before
# we spend tokens feeding Claude full descriptions. Pass-2 then evaluates
# and writes blurbs for the survivors, returning CANDIDATE_EVENTS picks.
TOP_N_TITLES          = 10
CANDIDATE_EVENTS      = 8   # ask Claude for up to this many in pass 2
TARGET_EVENTS         = CANDIDATE_EVENTS  # legacy alias used in the prompt
FINAL_EVENTS          = 3   # keep this many after the date-floor filter
# Hard floor on picks per newsletter. Section is never empty — if the
# initial Claude call returns fewer, we re-call with relaxed guardrails.
MIN_PICKS_PER_NL      = 3
# Date window for Notion event lookup: upcoming Friday → +14 days.
DATE_WINDOW_DAYS      = 14

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
# 3a. FETCH EVENTS FROM NOTION (Weekend Events DB)
# ---------------------------------------------------------------------------
def _rich_text(prop: dict) -> str:
    """Concatenate all rich_text/title runs in a Notion property."""
    if not isinstance(prop, dict):
        return ""
    chunks = prop.get("rich_text") or prop.get("title") or []
    return "".join(c.get("plain_text", "") for c in chunks).strip()


# Newsletter tags that pull from a shared event pool. ECC_PP is written
# by the Sandy Springs scraper (visitsandysprings.org sits geographically
# between East Cobb and Perimeter coverage areas), so both newsletters
# OR-include it at query time.
SHARED_NEWSLETTER_TAGS = {
    "East_Cobb_Connect":       ["East_Cobb_Connect", "ECC_PP"],
    "Perimeter_Post":          ["Perimeter_Post",    "ECC_PP"],
    "Lewisville_Lake_Lookout": ["Lewisville_Lake_Lookout"],
}


def fetch_events_from_notion(newsletter_name: str,
                             window_start: date,
                             window_end: date) -> list[dict]:
    """Query the Weekend Events Notion DB for rows tagged with this
    newsletter (or a shared tag like ECC_PP) whose Date falls in
    [window_start, window_end] inclusive.

    Returns candidate dicts shaped like the old Brave output so the
    downstream Claude eval can consume them unchanged."""
    if not NOTION_WEEKEND_EVENTS_DB_ID:
        print("  ⚠ NOTION_WEEKEND_EVENTS_DB_ID not set — skipping Notion fetch")
        return []
    tags = SHARED_NEWSLETTER_TAGS.get(newsletter_name, [newsletter_name])
    if len(tags) == 1:
        newsletter_clause = {"property": "Newsletter", "select": {"equals": tags[0]}}
    else:
        newsletter_clause = {"or": [
            {"property": "Newsletter", "select": {"equals": t}} for t in tags
        ]}
    filters = {
        "and": [
            newsletter_clause,
            {"property": "Date", "date": {"on_or_after": window_start.isoformat()}},
            {"property": "Date", "date": {"on_or_before": window_end.isoformat()}},
        ]
    }
    # Status filter: exclude rows already picked by Featured Event /
    # Weekend Planner, and human-rejected / archived rows. The Notion
    # `select` filter requires one `does_not_equal` per status.
    filters["and"].extend([
        {"property": "Status", "select": {"does_not_equal": "featured"}},
        {"property": "Status", "select": {"does_not_equal": "wp_used"}},
        {"property": "Status", "select": {"does_not_equal": "rejected"}},
        {"property": "Status", "select": {"does_not_equal": "archived"}},
    ])
    print(f"  Newsletter filter: {tags}  (excluding featured/wp_used/rejected/archived)")
    pages = query_database(NOTION_WEEKEND_EVENTS_DB_ID, filters=filters) or []
    candidates: list[dict] = []
    for p in pages:
        props = p.get("properties", {})
        title = _rich_text(props.get("Event Name")) or _rich_text(props.get("Name"))
        url   = (props.get("Source URL", {}).get("url") or "").strip()
        if not title or not url:
            continue
        date_prop = (props.get("Date") or {}).get("date") or {}
        start_str = date_prop.get("start") or ""
        try:
            event_start = date.fromisoformat(start_str[:10]) if start_str else None
        except ValueError:
            event_start = None
        # query_database with a date filter is authoritative, but verify in
        # Python so a schema-induced unfiltered fallback (see query_database)
        # doesn't smuggle past or out-of-window events through.
        if event_start and not (window_start <= event_start <= window_end):
            continue
        candidates.append({
            "title":       title,
            "url":         url,
            "summary":     _rich_text(props.get("Description")),
            "image_url":   (props.get("Image URL", {}).get("url") or "").strip(),
            "venue":       _rich_text(props.get("Location")),
            "address":     _rich_text(props.get("Address")),
            "dates":       _rich_text(props.get("Dates")),
            "time":        _rich_text(props.get("Time")),
            "start_date":  event_start.isoformat() if event_start else "",
            "source":      "weekend_events_db",
            # Page ID so we can PATCH the source row's Status after pick.
            "notion_page_id": p.get("id"),
        })
    candidates.sort(key=lambda c: c.get("start_date", ""))
    return candidates


# ---------------------------------------------------------------------------
# 3b. CLAUDE PASS 1 — pick top N titles
# ---------------------------------------------------------------------------
def claude_pick_top_titles(candidates: list[dict],
                           demographics: dict,
                           display_area: str,
                           newsletter_name: str,
                           n: int = TOP_N_TITLES) -> list[dict]:
    """Send Claude the titles only and ask it to pick the top N that best
    fit the audience. Returns the selected candidate dicts in Claude's
    preference order."""
    if len(candidates) <= n:
        return candidates
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    title_list = "\n".join(f"[{i}] {c['title']}" for i, c in enumerate(candidates, 1))
    demo_summary = (
        f"Median household income: {demographics['median_income']}\n"
        f"Median age: {demographics['median_age']}\n"
        f"Family skew: {demographics['family_skew']}\n"
        f"Homeownership rate: {demographics['homeownership']}\n"
        f"Education level: {demographics['education']}"
    )
    prompt = f"""You're picking events to feature in a local newsletter for {newsletter_name.replace('_', ' ')} ({display_area}).

Audience demographics:
{demo_summary}

Below is a list of upcoming events (titles only). Pick the {n} most interesting,
specific, can't-miss events for this audience. Prefer distinctive happenings
(festivals, concerts, premieres, special exhibits) over generic recurring
listings (weekly farmers markets, ongoing open mics) unless the title clearly
flags a special edition.

Return ONLY a JSON array of the {n} candidate indices, in order from best to
worst pick. Example: [12, 3, 27, ...]. Do not include any other text.

Events:
{title_list}
"""
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Claude pass-1 error (attempt {attempt + 1}): {e}")
                time.sleep(5 * (attempt + 1))
            else:
                raise
    raw = next(b.text for b in response.content if b.type == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        idxs = json.loads(raw)
    except json.JSONDecodeError:
        # Salvage: extract first [...] block
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0:
            print(f"  ✗ pass-1 returned non-JSON; falling back to first {n}\n     raw: {raw[:200]}")
            return candidates[:n]
        try:
            idxs = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as e:
            print(f"  ✗ pass-1 JSON salvage failed: {e}; falling back to first {n}")
            return candidates[:n]
    picked: list[dict] = []
    seen: set[int] = set()
    for raw_i in idxs:
        try:
            i = int(raw_i)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(candidates) and i not in seen:
            seen.add(i)
            picked.append(candidates[i - 1])
        if len(picked) >= n:
            break
    if not picked:
        print(f"  ⚠ pass-1 produced no valid indices; falling back to first {n}")
        return candidates[:n]
    return picked


# ---------------------------------------------------------------------------
# 3c. FETCH EVENTS VIA BRAVE SEARCH API (legacy — kept for populate_cache.py)
# ---------------------------------------------------------------------------
def build_search_queries(display_area: str, search_areas: list[str]) -> list[str]:
    """Build event search queries targeting the NEXT weekend's specific
    date range. Featured Event runs ahead of publish day, so generic
    "this weekend" queries return current-week listicles whose events
    are already past by the time we filter. Targeting the upcoming
    Friday-Sunday by name pulls listicles for the right weekend.
    """
    today = datetime.today()
    month_year = today.strftime("%B %Y")

    # Compute the upcoming Fri-Sat-Sun. If today is past Fri (Sat/Sun/Mon),
    # we jump to the NEXT Friday. Most weekly newsletters target the
    # upcoming weekend not the current one.
    from datetime import timedelta
    weekday = today.weekday()   # Mon=0..Sun=6
    days_to_fri = (4 - weekday) % 7
    if days_to_fri == 0:
        days_to_fri = 7         # already on Friday → use NEXT Friday
    next_fri = today + timedelta(days=days_to_fri)
    next_sat = next_fri + timedelta(days=1)
    next_sun = next_fri + timedelta(days=2)
    fri_label = next_fri.strftime("%B %-d")    # e.g. "May 22"
    sun_label = next_sun.strftime("%-d, %Y")   # e.g. "24, 2026"
    date_range = f"{fri_label}-{sun_label}"    # "May 22-24, 2026"
    fri_full   = next_fri.strftime("%B %-d %Y")  # "May 22 2026"

    queries = [
        f"{display_area} things to do {date_range}",
        f"{display_area} events {fri_full}",
        f"{display_area} weekend events {date_range}",
        f"{display_area} {month_year} festivals concerts",
    ]
    # One broader area-based query keeps coverage when the display_area
    # is ambiguous (e.g. "Perimeter" in Georgia).
    if search_areas:
        queries.append(f"{search_areas[0]} things to do {date_range}")
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
    quota_exhausted = False  # set True on HTTP 402 — stop hammering the API
    all_results = []
    seen_urls = set()

    # Web-only — Brave Web Search returns ≥80% of what /news/search does
    # for these queries, at half the API cost. Dropping news cut Brave
    # spend ~2x without measurable quality loss.
    for query in queries:
        if quota_exhausted:
            print(f"  ⏭ Skipping remaining queries — Brave quota exhausted (402)")
            break
        print(f"  Searching: {query}")
        try:
            res = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params={"q": query, "count": MAX_RESULTS_PER_QUERY, "freshness": "pm"},
                timeout=30,
            )
            if res.status_code == 402:
                print(f"    Web:  HTTP 402 — Brave quota exhausted; aborting search loop")
                quota_exhausted = True
                break
            if res.status_code != 200:
                print(f"    Web:  HTTP {res.status_code} — {res.text[:160]}")
                time.sleep(0.5)
                continue
            web_results = res.json().get("web", {}).get("results", [])
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
                    "date":    item.get("age", "") or item.get("page_age", ""),
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
    floor: date,
    ceiling: date | None = None,
) -> list[dict]:
    """Send candidates + demographics to Claude. Returns top events with blurbs.

    `floor` is the earliest acceptable event date. `ceiling` is the latest
    when an issue_date override is in effect (Thu..next-Wed); None falls
    back to the open-ended floor-only behavior."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    # Tag each candidate with a 1-based index for safe URL matching post-Claude.
    # IMPORTANT: strip `article_text` / `primary_text` / `original_url` from
    # what Claude sees. Those fields are kept on the candidate dict for our
    # date-filter use, but when Claude sees an aggregator listicle's full
    # body text, it picks multiple events from that one candidate and
    # assigns them all the same candidate_index → all events get the same
    # source URL after our index-→-URL attach step. Forcing Claude to see
    # only title+url+summary makes 1 candidate map to ≤1 event.
    INTERNAL_FIELDS = {"article_text", "primary_text", "original_url",
                       "drilled", "source_url_aggregator", "page_age"}
    indexed_candidates = []
    for i, c in enumerate(candidates, 1):
        clean = {k: v for k, v in c.items() if k not in INTERNAL_FIELDS}
        clean["candidate_index"] = i
        indexed_candidates.append(clean)
    candidates_json = json.dumps(indexed_candidates, indent=2)
    # Log a sample of what Claude is being asked to choose from
    print(f"  ⓘ Sending {len(indexed_candidates)} candidate(s) to Claude (sample):")
    for c in indexed_candidates[:6]:
        print(f"     [{c['candidate_index']:>2}] {c.get('title', '')[:70]}  →  {c.get('url', '')[:80]}")
    demo_summary = (
        f"Median household income: {demographics['median_income']}\n"
        f"Median age: {demographics['median_age']}\n"
        f"Family skew: {demographics['family_skew']}\n"
        f"Homeownership rate: {demographics['homeownership']}\n"
        f"Education level: {demographics['education']}"
    )

    today = datetime.today()
    if ceiling is not None:
        # issue_date override — strict issue_date..next-Wed window.
        pub_context = (
            f"Today is {today.strftime('%A, %B %d, %Y')}. "
            f"The newsletter is being prepared for an issue dated "
            f"{floor.strftime('%A, %B %d, %Y')}. "
            f"IMPORTANT: only consider events occurring between "
            f"{floor.strftime('%A, %B %d, %Y')} and "
            f"{ceiling.strftime('%A, %B %d, %Y')} (inclusive). "
            f"Events earlier or later than this window are out of scope for this issue."
        )
    else:
        pub_context = (
            f"Today is {today.strftime('%A, %B %d, %Y')}. "
            f"The newsletter publishes this week. "
            f"IMPORTANT: only consider events occurring on or after "
            f"{floor.strftime('%A, %B %d, %Y')} "
            f"(the upcoming Friday) — anything earlier in the week is past by send time."
        )

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system=with_voice(skill_prompt),
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
    # Claude occasionally appends commentary, multiple arrays, or trailing
    # whitespace after the JSON. Extract the FIRST top-level JSON array
    # via balanced-bracket scanning so we don't choke on "Extra data".
    try:
        results = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("[")
        if start < 0:
            print(f"  ✗ No JSON array in Claude response. First 500 chars:\n{clean[:500]}")
            return []
        depth, end = 0, -1
        in_str = False
        esc = False
        for i in range(start, len(clean)):
            ch = clean[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end < 0:
            print(f"  ✗ Couldn't find balanced JSON array. First 500 chars:\n{clean[:500]}")
            return []
        try:
            results = json.loads(clean[start:end])
            print(f"  ⓘ Recovered JSON array from chars {start}..{end} "
                  f"(stripped {len(clean) - (end - start)} chars of extra output)")
        except json.JSONDecodeError as e:
            print(f"  ✗ JSON parse failed even after extraction: {e}")
            print(f"     First 500 chars of array:\n{clean[start:start+500]}")
            return []

    # Diagnostic: log what Claude actually returned before validation.
    if isinstance(results, list):
        print(f"  ⓘ Claude returned {len(results)} pick(s) from {len(candidates)} candidate(s)")
        for i, r in enumerate(results[:8]):
            print(f"     [{i+1}] candidate_index={r.get('candidate_index')!r}  "
                  f"event_name={(r.get('event_name') or '')[:60]!r}  "
                  f"venue={(r.get('venue') or '')[:50]!r}")
    else:
        print(f"  ⚠ Claude returned non-list: {type(results).__name__}")
        return []

    # Attach real URLs from candidates using the index Claude returned.
    # Discard any URLs Claude may have provided directly.
    candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}
    validated = []
    # Dedup: Claude sometimes returns the same candidate_index for multiple
    # events (e.g., one Atlanta Dream candidate ending up labeled both
    # 'Atlanta Dream vs Aces' and 'Marcus King Band' because both names
    # appeared in the same source article). Each source URL should map
    # to at MOST one event. Keep the first occurrence per index.
    seen_indices: set[int] = set()
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
        if idx in seen_indices:
            print(f"  ✗ Rejecting duplicate candidate_index {idx}: {r.get('event_name', '?')} "
                  f"(same source URL already used by an earlier event)")
            continue

        # Venue-vs-host sanity check: if the event's name+venue tokens have
        # no DISTINCTIVE overlap with the source URL, Claude probably
        # conflated two events from the same source article (e.g., labeling
        # an Atlanta Dream URL as Marcus King's source). Drop it.
        #
        # Distinctive = non-generic. Common nouns ('band', 'festival',
        # 'concert', 'theater', etc.) and common geo words ('atlanta',
        # 'georgia') match too many unrelated URLs and cause false positives.
        candidate_url = source.get("url", "") or ""
        event_name    = r.get("event_name", "") or ""
        venue         = r.get("venue", "") or ""
        from urllib.parse import urlparse as _up
        host = (_up(candidate_url).hostname or "").lower().removeprefix("www.")
        import re as _re_venue

        VENUE_STOP_TOKENS = {
            # Common event-type words
            "band", "festival", "event", "events", "show", "shows", "tour",
            "live", "music", "concert", "concerts", "party", "annual",
            "weekend", "evening", "night", "presents", "feat", "ticket",
            "tickets", "presents",
            # Venue-type words
            "theatre", "theater", "arena", "stadium", "venue", "center",
            "centre", "park", "fest",
            # Geo (covers our newsletter regions and metros)
            "atlanta", "georgia", "metro", "north", "south", "east", "west",
            "downtown", "uptown", "midtown",
            "dallas", "texas", "fort", "worth",
            "cobb", "marietta", "roswell", "alpharetta",
            "dunwoody", "brookhaven", "sandy", "springs",
            "lewisville", "denton", "flower", "mound",
        }
        def _tokens(s: str) -> set[str]:
            tokens = set(_re_venue.findall(r"[a-z]{4,}", s.lower()))
            return tokens - VENUE_STOP_TOKENS

        event_tokens = _tokens(event_name) | _tokens(venue)
        # For URL matching, use the lowercased URL as a flat string so an
        # event token like 'greek' matches 'mariettagreekfestival.com'
        # (one un-hyphenated token) via substring containment. Pure-token
        # intersection misses these compound domain names.
        url_lower = candidate_url.lower()

        # Skip the check for fully-generic event hosts (Eventbrite, Ticketmaster
        # etc.) — those won't contain event-name tokens by design.
        GENERIC_TICKETING_HOSTS = ("eventbrite.com", "ticketmaster.com", "axs.com",
                                    "stubhub.com", "seatgeek.com", "tixr.com",
                                    "bigtickets.com")
        skip_check = any(host == g or host.endswith("." + g) for g in GENERIC_TICKETING_HOSTS)
        url_has_event_token = any(tok in url_lower for tok in event_tokens)
        if not skip_check and event_tokens and not url_has_event_token:
            print(f"  ✗ Rejecting venue/host mismatch: '{event_name}' at '{venue}' "
                  f"vs URL '{candidate_url[:60]}' (no distinctive tokens in URL; "
                  f"event_keywords={sorted(event_tokens)[:5]})")
            continue

        seen_indices.add(idx)
        r["source_url"] = candidate_url
        r["ticket_url"] = source.get("ticket_url", "") or ""
        # Notion page ID of the source Weekend Events row — used after
        # save_events_to_notion to PATCH that row's Status to 'featured'
        # so Weekend Planner doesn't re-pick the same event.
        if source.get("notion_page_id"):
            r["notion_page_id"] = source["notion_page_id"]
        # Carry the scraped image forward as the primary; the image-lookup
        # stage will add og:image / Brave Image candidates to the gallery
        # but keep this one as the default.
        if source.get("image_url"):
            r["image_url"] = source["image_url"]
        # Fallbacks for downstream renderers (event body GIF, header image)
        # when Claude didn't echo the venue/address/time fields verbatim.
        if not r.get("venue") and source.get("venue"):
            r["venue"] = source["venue"]
        if not r.get("address") and source.get("address"):
            r["address"] = source["address"]
        if not r.get("time") and source.get("time"):
            r["time"] = source["time"]
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

    # Hard date window: drop anything outside [floor, ceiling]. With no
    # ceiling, only the past-event floor is enforced (current behavior).
    # Belt-and-suspenders against Claude leaking out-of-window picks past
    # the prompt-level instruction.
    results = filter_events_to_window(results, floor, ceiling)

    # Dedup by event_name — Claude sometimes returns the same event twice
    # when two candidate indices point at the same festival via different
    # aggregator articles (e.g. eastcobbnews + Fox5 both write up Marietta
    # Greek Festival). Keep the higher-scored copy.
    by_name: dict[str, dict] = {}
    for r in results:
        key = (r.get("event_name") or "").strip().lower()
        if not key:
            continue
        existing = by_name.get(key)
        if existing is None or (r.get("total_score", 0) > existing.get("total_score", 0)):
            by_name[key] = r
    if len(by_name) != len(results):
        print(f"  ⓘ Deduplicated {len(results) - len(by_name)} same-name event(s)")
    results = list(by_name.values())

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
    """Flag the default winner — the highest-scored event that actually has a
    usable image, so the newsletter never leads with a blank hero. `results`
    is pre-sorted by total_score desc, so the first entry carrying an image (or
    image gallery) is the highest-scored one with a photo. Falls back to the
    top-scored event if none has an image.

    Call this AFTER the image-population stage so `image_url`/`image_candidates`
    reflect what was actually found."""
    for r in results:
        r["default_winner"] = ""
    if not results:
        return results
    winner = next((r for r in results
                   if (r.get("image_url") or r.get("image_candidates"))), None)
    if winner is None:
        winner = results[0]
        print(f"  Default winner: {winner['event_name']} "
              f"({winner['total_score']}/30) — no candidate had an image")
    elif winner is not results[0]:
        print(f"  Default winner: {winner['event_name']} "
              f"({winner['total_score']}/30) — preferred over higher-scored "
              f"'{results[0]['event_name']}' which had no image")
    else:
        print(f"  Default winner: {winner['event_name']} ({winner['total_score']}/30)")
    winner["default_winner"] = "yes"
    return results


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Featured Event automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    # section_date_window() honors ISSUE_DATE transparently:
    #   With ISSUE_DATE set: (issue_date, issue_date + 6 days)
    #   Without:             (upcoming_friday(), None)
    floor, ceiling = section_date_window()
    window_end = ceiling if ceiling is not None else floor + timedelta(days=DATE_WINDOW_DAYS)

    for newsletter in filter_by_env():
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")
        print(f"  Date window: {floor} → {window_end}"
              + ("  (strict — ISSUE_DATE override)" if ceiling else f"  ({DATE_WINDOW_DAYS} days)"))

        candidates = fetch_events_from_notion(
            newsletter_name = newsletter["name"],
            window_start    = floor,
            window_end      = window_end,
        )
        print(f"  Pulled {len(candidates)} events from Weekend Events DB")
        if not candidates:
            print(f"  No candidates for {newsletter['name']} in window. Skipping.")
            continue

        # Cross-newsletter URL dedup — union of existing event URLs across both newsletters.
        # Re-fetched per iteration so Newsletter 2 sees Newsletter 1's freshly-saved winners.
        existing_urls: set[str] = set()
        for nl in NEWSLETTERS:
            existing_urls |= get_existing_event_urls(nl["name"])
        if existing_urls:
            before = len(candidates)
            candidates = [c for c in candidates if c["url"] not in existing_urls]
            print(f"  Filtered {before - len(candidates)} previously-used URLs (union across both newsletters)")
        if not candidates:
            print(f"  All candidates were previously used for {newsletter['name']}. Skipping.")
            continue

        # Per-occurrence pool: the Weekend Events DB now stores one row per
        # occurrence, so a multi-day event surfaces as several candidates with
        # the same title. Collapse to the earliest in-window occurrence per
        # title (candidates are pre-sorted by start_date asc, so first-seen ==
        # earliest) — restores the prior one-candidate-per-event input so
        # Claude's Pass-1 picks aren't spent on duplicate days, and the chosen
        # row's URL/date match the soonest occurrence.
        _seen_titles: set[str] = set()
        _collapsed: list[dict] = []
        for c in candidates:
            key = " ".join((c.get("title") or "").lower().split())
            if key and key in _seen_titles:
                continue
            if key:
                _seen_titles.add(key)
            _collapsed.append(c)
        if len(_collapsed) != len(candidates):
            print(f"  Collapsed {len(candidates) - len(_collapsed)} duplicate "
                  f"occurrence(s) to earliest per title")
        candidates = _collapsed

        # Pass 1 — title-only Claude filter. Narrows the pool to the top
        # TOP_N_TITLES titles before pass 2 spends tokens on descriptions.
        print(f"\n  Pass 1 (titles only): sending {len(candidates)} titles to Claude → top {TOP_N_TITLES}")
        top_titles = claude_pick_top_titles(
            candidates       = candidates,
            demographics     = newsletter["demographics"],
            display_area     = newsletter["display_area"],
            newsletter_name  = newsletter["name"],
            n                = TOP_N_TITLES,
        )
        print(f"  Pass 1 selected {len(top_titles)} candidate(s):")
        for i, c in enumerate(top_titles, 1):
            print(f"     [{i:>2}] {c['title'][:80]}")

        # Pass 2 — full eval: send title + description for scoring + blurbs.
        print(f"\n  Pass 2 (full eval): sending {len(top_titles)} candidates to Claude...")
        results = evaluate_and_write_events(
            candidates=top_titles,
            demographics=newsletter["demographics"],
            display_area=newsletter["display_area"],
            newsletter_name=newsletter["name"],
            skill_prompt=skill_prompt,
            floor=floor,
            ceiling=ceiling,
        )

        if not results:
            print(f"  Claude found no qualifying events for {newsletter['name']}. Skipping.")
            continue

        # Populate image_url for each event in two stages:
        #   Stage 1: scrape the source URL's og:image / JSON-LD / body-image
        #            (includes root-domain fallback). Free, no API cost.
        #   Stage 2: if Stage 1 found nothing, Brave Image Search using the
        #            event name + display area as the query. Picks the best
        #            non-stock, non-affiliate result. Uses your existing
        #            Brave subscription ($0.005/request).
        try:
            sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'Free Events', 'Code'))
            from Free_Events import fetch_event_image, fetch_event_images, _image_looks_real
            from brave_search import search_images

            BRAVE_IMAGE_SKIP_HOSTS = (
                # Stock photo / watermark sites Claude shouldn't surface
                "shutterstock.com", "gettyimages.com", "istockphoto.com",
                "alamy.com", "depositphotos.com", "dreamstime.com",
                "stock.adobe.com", "123rf.com",
                # Affiliate / ad CDNs (consistent with og:image SKIP_TOKENS)
                "grouponcdn.com", "jdoqocy.com", "dpbolvw.net",
                "tkqlhce.com", "anrdoezrs.net",
            )

            def _brave_image_fallback(event_name: str) -> str:
                """Last-resort image find via Brave Image Search. Returns first OK URL."""
                imgs = _brave_image_candidates(event_name, max_results=5)
                return imgs[0] if imgs else ""

            def _brave_image_candidates(event_name: str, max_results: int = 8) -> list[str]:
                """Brave Image Search → list of usable image URLs. Same filter as
                the singular fallback but returns all that pass."""
                if not BRAVE_NEWS_API_KEY:
                    return []
                query = f"{event_name} {newsletter['display_area']}"
                results = search_images(query, BRAVE_NEWS_API_KEY, max_results=max_results)
                if not results:
                    return []
                out: list[str] = []
                for img in results:
                    candidate = img.get("image_url") or img.get("thumbnail") or ""
                    if not candidate:
                        continue
                    cl = candidate.lower()
                    if any(host in cl for host in BRAVE_IMAGE_SKIP_HOSTS):
                        continue
                    if _image_looks_real(candidate):
                        out.append(candidate)
                return out

            # Track image URLs we've already assigned to an event in THIS
            # batch. If the same image URL gets scraped for multiple events,
            # it's almost certainly a sitewide widget / affiliate banner
            # (e.g., DreamHack banner appearing on multiple aggregator
            # pages' og:image) — not the actual event's photo. Force
            # Stage-2 fallback for subsequent events.
            used_image_urls: set[str] = set()

            import re as _re_img

            def _normalize_img(u: str) -> str:
                """Normalize an image URL for dedup. Beyond stripping query
                strings, this collapses WordPress-style size variants of the
                same image so `foo-300x300.jpg`, `foo-1024x768.jpg`,
                `foo-scaled.jpg`, `foo-e1739815584876.jpg` and `foo.jpg`
                all map to the same key.

                Patterns handled (all WP / common CMS conventions):
                  -WIDTHxHEIGHT  (e.g. -300x300, -1024x768)
                  -scaled        (full-size variant)
                  -edited / -rotated / -cropped
                  -e<digits>     (timestamp suffix the WP editor adds)
                """
                u = u.split("?")[0].split("#")[0].rstrip("/").lower()
                # Split extension off so we can clean the stem
                m = _re_img.match(r"^(.*)(\.[a-z]{2,4})$", u)
                if m:
                    stem, ext = m.group(1), m.group(2)
                else:
                    stem, ext = u, ""
                # Strip recognized WP/CMS size & edit suffixes (repeatedly,
                # to handle stacked variants like `foo-scaled-1024x768.jpg`)
                while True:
                    new = _re_img.sub(
                        r"(-\d+x\d+|-scaled|-edited|-rotated|-cropped|-e\d{6,})$",
                        "", stem,
                    )
                    if new == stem:
                        break
                    stem = new
                return stem + ext

            MAX_GALLERY = 8  # max candidate images saved per event

            from urllib.parse import urljoin as _urljoin

            def _absolutize(maybe_relative: str, base_url: str) -> str:
                """Absolutize relative image URLs against the source URL.
                Old Sandy Springs Notion rows (saved before the scraper's
                absolutize fix) carry paths like '/imager/cmsimages/…'
                which crash the header builder's requests.get. urljoin is
                idempotent on already-absolute URLs."""
                if not maybe_relative:
                    return ""
                if maybe_relative.startswith(("http://", "https://", "//")):
                    return maybe_relative
                if not base_url:
                    return maybe_relative
                return _urljoin(base_url, maybe_relative)

            for r in results:
                if r.get("image_url") and r.get("image_candidates"):
                    continue
                url = r.get("source_url") or r.get("ticket_url") or ""
                event_name = r.get("event_name", "")

                # Primary: image scraped by the Weekend Events scraper (lives
                # on r["image_url"] from the source candidate). The reviewer
                # sees this first; the rest of the gallery is for swapping.
                scraped = _absolutize((r.get("image_url") or "").strip(), url)

                # Stage 1: scrape ALL plausible images from the source page
                page_imgs = fetch_event_images(url, max_results=MAX_GALLERY) if url else []
                page_imgs = [_absolutize(u, url) for u in page_imgs]
                # Drop any already used by another event in this batch
                page_imgs = [u for u in page_imgs if _normalize_img(u) not in used_image_urls]
                # Stage 2: Brave Image Search — pull a few extras so the
                # reviewer has alternatives even when page-scrape worked
                brave_imgs = _brave_image_candidates(event_name, max_results=MAX_GALLERY)
                brave_imgs = [u for u in brave_imgs if _normalize_img(u) not in used_image_urls]

                # Combined gallery: scraped image first (primary), then
                # page-scrape results, then Brave. Dedup by normalized URL.
                gallery: list[str] = []
                seen_norm: set[str] = set()
                ordered_sources = ([scraped] if scraped else []) + page_imgs + brave_imgs
                for u in ordered_sources:
                    if not u:
                        continue
                    n = _normalize_img(u)
                    if n in seen_norm:
                        continue
                    seen_norm.add(n)
                    gallery.append(u)
                    if len(gallery) >= MAX_GALLERY:
                        break

                if gallery:
                    r["image_url"] = gallery[0]
                    r["image_candidates"] = gallery
                    # Reserve EVERY image in the gallery so no other event in
                    # this batch ends up offering the same options (e.g.,
                    # atlantaparent.com sidebar widgets appear on every event
                    # page and would otherwise show up in 3-4 galleries).
                    for u in gallery:
                        used_image_urls.add(_normalize_img(u))
                    print(f"  ↳ image gallery for {event_name[:50]} ({len(gallery)} candidates, default: {gallery[0][:60]})")
                else:
                    print(f"  · no images found for {event_name[:50]} (both stages failed)")
        except Exception as e:
            print(f"  ⚠ image lookup skipped ({e}) — events will save without image_url")

        # Flag the default winner AFTER images are resolved, so we lead with
        # the highest-scored event that actually has a photo (not a blank hero).
        results = flag_default_winner(results)

        # Pre-build the Canva header composite for every event so the
        # review app shows a preview immediately (no reviewer click needed).
        # The picker can regenerate later if the reviewer swaps images.
        try:
            from header_image_maker import build_header_image
            from pathlib import Path as _Path
            header_out_dir = _Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"
            header_out_dir.mkdir(parents=True, exist_ok=True)
            for idx, r in enumerate(results):
                photo = r.get("image_url") or ""
                title = r.get("event_name") or ""
                if not photo:
                    continue
                try:
                    png = build_header_image(title=title, photo_url=photo)
                except Exception as e:
                    print(f"    · header build failed for {title[:50]}: {e}")
                    continue
                if not png:
                    continue
                # Per-event filename so multiple candidates each have their own preview
                safe = "".join(c if c.isalnum() else "_" for c in title)[:40] or f"event_{idx}"
                fname = f"Newsletter_Header_image_{newsletter['name']}_{safe}.png"
                (header_out_dir / fname).write_bytes(png)
                # Cache-bust so browsers/Notion don't serve a stale composite
                # when the same filename gets rewritten on subsequent runs.
                cache_bust = int(datetime.today().timestamp())
                r["header_image_url"] = (
                    f"https://peachyinsurance.github.io/newsletters/gifs/{fname}?v={cache_bust}"
                )
                print(f"    ✓ built header preview: {fname}")
        except Exception as e:
            print(f"  ⚠ header pre-build skipped ({e})")

        # Pre-build the Canva-style "event body GIF" for each event:
        # animated frames of up to 4 candidate photos composited into the
        # body template's chroma blob, with title / location / address /
        # date text overlays repeated on every frame.
        try:
            from header_image_maker import build_event_body_gif
            from pathlib import Path as _Path
            gif_out_dir = _Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"
            gif_out_dir.mkdir(parents=True, exist_ok=True)
            for idx, r in enumerate(results):
                # Build a frame list: chosen image first, then alternates
                # (so frame 1 always matches the static fallback).
                main = r.get("image_url") or ""
                cands = r.get("image_candidates") or []
                frame_urls: list[str] = []
                if main:
                    frame_urls.append(main)
                for u in cands:
                    if u not in frame_urls:
                        frame_urls.append(u)
                    if len(frame_urls) >= 4:
                        break
                if not frame_urls:
                    continue
                title = r.get("event_name") or ""
                try:
                    gif_bytes = build_event_body_gif(
                        title         = title,
                        location_name = r.get("venue")   or "",
                        address       = r.get("address") or "",
                        date          = r.get("date")    or "",
                        photo_urls    = frame_urls,
                    )
                except Exception as e:
                    print(f"    · GIF build failed for {title[:50]}: {e}")
                    continue
                if not gif_bytes:
                    continue
                safe = "".join(c if c.isalnum() else "_" for c in title)[:40] or f"event_{idx}"
                fname = f"event_gif_{newsletter['name']}_{safe}.gif"
                (gif_out_dir / fname).write_bytes(gif_bytes)
                cache_bust = int(datetime.today().timestamp())
                r["gif_url"] = f"https://peachyinsurance.github.io/newsletters/gifs/{fname}?v={cache_bust}"
                print(f"    ✓ built event body GIF: {fname} ({len(frame_urls)} frames, {len(gif_bytes):,} bytes)")
        except Exception as e:
            print(f"  ⚠ event GIF build skipped ({e})")

        # Save to Notion
        save_events_to_notion(results, newsletter["name"])

        # Mark each picked source row in the Weekend Events DB as
        # 'featured' so the next Weekend Planner run skips them.
        marked = 0
        for r in results:
            page_id = r.get("notion_page_id")
            if not page_id:
                continue
            try:
                update_page(page_id, {"Status": {"select": {"name": "featured"}}})
                marked += 1
            except Exception as e:
                print(f"  ⚠ couldn't mark '{r.get('event_name','?')[:40]}' as featured: {e}")
        if marked:
            print(f"  ↳ Marked {marked} Weekend Events row(s) as Status='featured'")

        # ── Refresh headers for ALL still-active Featured Event rows ────
        # This run only built fresh header PNGs for the events it just
        # picked. Older picks (chosen in previous Featured Event runs but
        # still showing in the review-app / Notion UI) still have their
        # original PNGs on gh-pages — those were built before the latest
        # template edit, so they show the old red-box design. Walk every
        # Notion row whose Status isn't rejected/old and rebuild its
        # header PNG against the CURRENT template. The workflow's
        # gh-pages publish step picks up everything in the output dir.
        if NOTION_EVENTS_DB_ID:
            try:
                from header_image_maker import build_header_image as _build_hdr
                refresh_filter = {
                    "and": [
                        {"property": "Newsletter",
                         "select": {"equals": newsletter["name"]}},
                        {"property": "Status",
                         "select": {"does_not_equal": "rejected"}},
                        {"property": "Status",
                         "select": {"does_not_equal": "approved - old"}},
                    ]
                }
                active_pages = query_database(NOTION_EVENTS_DB_ID,
                                              filters=refresh_filter) or []
                print(f"\n  Refreshing headers for {len(active_pages)} "
                      f"active Featured Event row(s)…")
                hdr_out = Path(__file__).parent.parent.parent / "Beehiiv" / "Code" / "output"
                hdr_out.mkdir(parents=True, exist_ok=True)
                refreshed = 0
                for p in active_pages:
                    props = p.get("properties", {})
                    ev_name = (_rich_text(props.get("Event Name"))
                               or _rich_text(props.get("Name")))
                    img_url = (props.get("Image URL", {}).get("url") or "").strip()
                    if not ev_name or not img_url:
                        continue
                    try:
                        png = _build_hdr(title=ev_name, photo_url=img_url)
                    except Exception as e:
                        print(f"    · skipped '{ev_name[:50]}': {e}")
                        continue
                    if not png:
                        continue
                    safe = "".join(c if c.isalnum() else "_"
                                   for c in ev_name)[:40] or "event"
                    fname = f"Newsletter_Header_image_{newsletter['name']}_{safe}.png"
                    (hdr_out / fname).write_bytes(png)
                    refreshed += 1
                print(f"  ↳ Rebuilt {refreshed} header PNG(s) "
                      f"against the current template")
            except Exception as e:
                print(f"  ⚠ Header refresh stage skipped ({e})")

        # Save local JSON backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"events_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"  Saved JSON to {json_file}")

        print(f"\n  Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
