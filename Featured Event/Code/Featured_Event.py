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
from aggregator_drilldown import is_aggregator_url, expand_listicle

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

    # Trimmed from 12 → 5 queries per newsletter to cut Brave spend ~2.5x.
    # These five capture the same hits the full set did in past runs:
    # Brave's index dedup means the per-area triplets and the
    # "Eventbrite/concerts/things-to-do" variants all return overlapping URLs.
    queries = [
        f"{display_area} events this weekend",
        f"{display_area} events next week",
        f"{display_area} {month_year} festivals concerts",
    ]
    # One broader area-based query keeps coverage when the display_area
    # is ambiguous (e.g. "Perimeter" in Georgia).
    if search_areas:
        queries.append(f"{search_areas[0]} things to do {month_year}")
    queries.append(f"events near {display_area} Georgia {month_year}")
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
) -> list[dict]:
    """Send candidates + demographics to Claude. Returns top events with blurbs."""
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
        # Round 1 typically yields 30-80 valid candidates per newsletter, so
        # rounds 2/3 are wasted spend in normal operation. We only fire them
        # when round 1 was thin (<3 valid) — likely indicates a bad-search-
        # term week or geo. MIN_VALID_FOR_RETRY is the round-1 floor that
        # triggers a retry; MIN_VALID_CANDIDATES is the warning threshold.
        MIN_VALID_CANDIDATES = 8
        MIN_VALID_FOR_RETRY  = 3
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
        brave_dead = False  # set True if a round returns 0 candidates (quota or hard outage)
        for round_idx, extra in enumerate(broader_query_sets, 1):
            if brave_dead:
                print(f"  ⏭ Skipping round {round_idx} — Brave returned no results last round")
                break
            print(f"\n  --- Candidate round {round_idx} (floor: {floor}) ---")

            # Brave cache lives in the repo at Featured Event/Code/brave_cache/
            # so it's available in CI and local runs without setup. Iterate
            # on filtering/drill/Claude logic without burning Brave quota.
            # To refresh: delete the cache file, or set BRAVE_CACHE_REFRESH=1
            # to force-overwrite, or set BRAVE_CACHE_DISABLE=1 to bypass.
            cache_disabled = bool(os.environ.get("BRAVE_CACHE_DISABLE"))
            cache_dir = os.environ.get(
                "BRAVE_CACHE_DIR",
                str(Path(__file__).parent / "brave_cache"),
            )
            cache_path = None
            new_pool = None
            if not cache_disabled:
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                safe_name = newsletter["name"].replace("/", "_")
                cache_path = Path(cache_dir) / f"{safe_name}_round{round_idx}.json"
                if cache_path.exists() and not os.environ.get("BRAVE_CACHE_REFRESH"):
                    try:
                        new_pool = json.loads(cache_path.read_text(encoding="utf-8"))
                        print(f"  ↺ Loaded {len(new_pool)} candidates from cache: {cache_path}")
                    except Exception as e:
                        print(f"  ⚠ Cache read failed ({e}); falling back to Brave")
                        new_pool = None

            if new_pool is None:
                new_pool = fetch_events_brave(
                    search_areas=newsletter["search_areas"],
                    display_area=newsletter["display_area"],
                    exclude_urls=excluded_urls,
                    extra_queries=extra,
                )
                if cache_path is not None and new_pool:
                    cache_path.write_text(json.dumps(new_pool, indent=2), encoding="utf-8")
                    print(f"  ✓ Saved {len(new_pool)} candidates to cache: {cache_path}")

            if not new_pool:
                brave_dead = True
            # NOTE: deliberately NOT URL-validating here. HEAD/GET-validation
            # gives false positives on bot-protected event pages — local news
            # sites (eastcobbnews.com, 11alive.com, discoveratlanta.com,
            # artsatl.org, etc.) return 403/404 to non-browser User-Agents.
            # We rely on aggregator drill-down + date filter as the gates.

            # Drill aggregator URLs into their primary embedded source. e.g.
            # eastcobbnews.com/taste-of-east-cobb-announces-2026-… → tasteofeastcobb.com
            # That swap is important because the aggregator's article text may
            # be timeless ("returns for 36th annual event") while the official
            # site shows the actual date — which our date filter can then
            # check against `primary_text`.
            #
            # Aggregator candidates whose drill FAILS (e.g. AJC "10 events"
            # listicle with generic "Get tickets" anchor text) are DROPPED
            # from the pool entirely. Otherwise the aggregator URL would
            # remain as the candidate's source_url and end up in Notion.
            # Listicle detection: roundup pages like "5 things to do",
            # "weekend checklist", "things to do this weekend" cover many
            # events. Drilling them collapses everything to one URL, which
            # then gets reused for unrelated events Claude picks from the
            # article body. Drop these from the pool entirely.
            LISTICLE_MARKERS = (
                "things to do", "things-to-do", "5 things", "10 things",
                "weekend checklist", "weekend events", "weekend roundup",
                "weekend guide", "your weekend", "events this weekend",
                "events this week", "events you absolutely need",
                "out and about", "what to do this", "what's happening",
                "upcoming events", "calendar of events", "events calendar",
                "fun things to do", "guide to events", "things to do in",
            )

            def _is_listicle(c):
                blob = f"{c.get('title','')} {c.get('url','')}".lower()
                return any(m in blob for m in LISTICLE_MARKERS)

            # Unified handling: every aggregator URL gets expanded. This
            # covers both multi-event listicles and "single-event" articles
            # whose pages also link to several related/sibling events.
            # If expansion yields nothing we keep the original aggregator
            # URL so the date filter and Claude still see the candidate.
            expanded_count = 0
            expanded_total_links = 0
            kept_original = 0
            keep_pool = []
            for c in new_pool:
                url = c.get("url", "")
                if is_aggregator_url(url):
                    print(f"  ↳ aggregator detected, expanding: {url}")
                    expanded = expand_listicle(url)
                    if expanded:
                        expanded_count += 1
                        expanded_total_links += len(expanded)
                        for sub in expanded:
                            print(f"      ↳ + extracted event: {sub['title'][:80]} → {sub['url']}")
                        keep_pool.extend(expanded)
                    else:
                        kept_original += 1
                        print(f"      ↳ no event links found, keeping original: {url}")
                        keep_pool.append(c)
                else:
                    keep_pool.append(c)
            new_pool = keep_pool
            if expanded_count:
                print(f"  ↳ expanded {expanded_count} aggregator pages into {expanded_total_links} new candidates")
            if kept_original:
                print(f"  ↳ kept {kept_original} aggregator candidates with original URL (no expansion)")

            # Date-floor filter — scan title + summary + article body
            # (always present for aggregator candidates) + primary_text
            # (when drill-down found a clean primary URL). The article body
            # is critical for cases like the Marietta History Center event,
            # where the venue's website doesn't list the event but the
            # aggregator's article spells out 'Saturday, June 27th'.
            kept, past_urls = filter_candidates_by_date(
                new_pool, floor,
                text_keys=("title", "summary", "article_text", "primary_text"),
            )
            excluded_urls.update(past_urls)
            # Merge (dedup by URL) into the surviving candidate set
            seen = {c["url"] for c in candidates}
            for c in kept:
                if c.get("url") and c["url"] not in seen:
                    candidates.append(c)
                    seen.add(c["url"])
            print(f"  ↳ pool size after round {round_idx}: {len(candidates)} valid candidates")
            # Stop after round 1 unless we got VERY thin — protects against
            # burning Brave credits when the round-1 pool is healthy.
            if round_idx == 1 and len(candidates) >= MIN_VALID_FOR_RETRY:
                print(f"  ↳ round-1 pool sufficient ({len(candidates)} ≥ {MIN_VALID_FOR_RETRY}) — skipping retry rounds")
                break
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

            def _normalize_img(u: str) -> str:
                """Strip query params for dedup so the same image with
                different cache-bust query strings still matches."""
                return u.split("?")[0].split("#")[0].rstrip("/").lower()

            MAX_GALLERY = 8  # max candidate images saved per event

            for r in results:
                if r.get("image_url") and r.get("image_candidates"):
                    continue
                url = r.get("source_url") or r.get("ticket_url") or ""
                event_name = r.get("event_name", "")

                # Stage 1: scrape ALL plausible images from the source page
                page_imgs = fetch_event_images(url, max_results=MAX_GALLERY) if url else []
                # Drop any already used by another event in this batch
                page_imgs = [u for u in page_imgs if _normalize_img(u) not in used_image_urls]
                # Stage 2: Brave Image Search — pull a few extras so the
                # reviewer has alternatives even when page-scrape worked
                brave_imgs = _brave_image_candidates(event_name, max_results=MAX_GALLERY)
                brave_imgs = [u for u in brave_imgs if _normalize_img(u) not in used_image_urls]

                # Combined gallery: page results first (more reliable),
                # then Brave. Dedup by normalized URL.
                gallery: list[str] = []
                seen_norm: set[str] = set()
                for u in page_imgs + brave_imgs:
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
                    used_image_urls.add(_normalize_img(gallery[0]))
                    print(f"  ↳ image gallery for {event_name[:50]} ({len(gallery)} candidates, default: {gallery[0][:60]})")
                else:
                    print(f"  · no images found for {event_name[:50]} (both stages failed)")
        except Exception as e:
            print(f"  ⚠ image lookup skipped ({e}) — events will save without image_url")

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
