#!/usr/bin/env python3
"""
Newsletter Automation - Free Events Section
Scrapes Brave Search for free events in the coverage area for the next 7 days,
then uses Claude to select 3-5 and write short labeled blurbs.
Saves the full section to Notion.
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
from notion_helper import save_free_events_to_notion, get_used_free_event_urls
from url_validator import validate_url

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-free-events-skill_auto.md"

MAX_RESULTS_PER_QUERY = 10
MIN_CANDIDATES        = 10  # fewer than this triggers broader fallback
TARGET_EVENTS         = 5

# Keep content friendly to the newsletter: drop obviously off-topic or unsafe items
EXCLUDED_KEYWORDS = {
    "shooting", "murder", "assault", "arrest", "overdose",
    "gun violence", "protest march",
}

# Paywalled/metered sources that sneak past validation
BLOCKED_DOMAINS = {
    "mdjonline.com",
    "ajc.com",
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Social / link-shorteners we don't want as primary candidates
SOCIAL_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "t.co", "bit.ly", "tinyurl.com", "lnkd.in",
}

# Generic anchor text that tells us nothing — skip these when extracting aggregator links
# to avoid pairing wrong URLs with wrong events.
GENERIC_ANCHOR_TEXT = {
    "click here", "here", "click", "more", "more info", "more information",
    "read more", "learn more", "register", "register here", "sign up",
    "tickets", "get tickets", "buy tickets", "details", "visit", "visit site",
    "website", "link", "see more", "view", "more details", "info", "rsvp",
}

# Aggregator / round-up / syndication sites. Kept as candidates themselves AND
# scraped for primary-source links, so both appear in the pool.
AGGREGATOR_DOMAINS = {
    "eastcobbnews.com",
    "patch.com",
    "eastcobber.com",
    "atlantaparent.com",
    "atlantaonthecheap.com",
    "macaronikid.com",
    "mommypoppins.com",
    "northfulton.com",
    "accessatlanta.com",
    "cobbcountyevents.com",
    "morningstar.com",
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "accesswire.com",
    "finance.yahoo.com",
    "news.yahoo.com",
    "streetinsider.com",
}

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "display_area": "East Cobb",
        "search_areas": ["East Cobb GA", "Marietta GA", "Kennesaw GA"],
    },
    {
        "name":         "Perimeter_Post",
        "display_area": "Perimeter",
        "search_areas": ["Dunwoody GA", "Sandy Springs GA", "Perimeter Atlanta"],
    },
]


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a local newsletter writer. Select 3-5 free events for the next 7 days and write short blurbs."


# ---------------------------------------------------------------------------
# 3. FETCH CANDIDATES VIA BRAVE SEARCH
# ---------------------------------------------------------------------------
def _build_exclusions() -> str:
    """Build -site: operators for paywalled domains only.
    Aggregators are ALLOWED through — we scrape them for primary-source links
    AND keep the aggregator URL itself as a valid candidate."""
    return " " + " ".join(f"-site:{d}" for d in sorted(BLOCKED_DOMAINS))


def search_brave(query: str) -> list[dict]:
    """One Brave news search — returns normalized candidates.
    Appends -site: operators to exclude paywall and aggregator domains at the source."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }
    full_query = query + _build_exclusions()
    try:
        res = requests.get(
            "https://api.search.brave.com/res/v1/news/search",
            headers=headers,
            params={"q": full_query, "count": MAX_RESULTS_PER_QUERY, "freshness": "pw"},
            timeout=30,
        )
        if res.status_code != 200:
            print(f"    Brave error {res.status_code}: {res.text[:200]}")
            return []
        raw_results = res.json().get("results", [])
    except Exception as e:
        print(f"    Brave error: {e}")
        return []

    normalized = []
    dropped_paywall = 0
    dropped_excluded = 0
    for item in raw_results:
        url = item.get("url", "")
        hostname = item.get("meta_url", {}).get("hostname", "") if isinstance(item.get("meta_url"), dict) else ""
        if not url:
            continue
        url_l, host_l = url.lower(), hostname.lower()
        if any(d in url_l or d in host_l for d in BLOCKED_DOMAINS):
            dropped_paywall += 1
            continue
        title = item.get("title", "") or ""
        desc  = item.get("description", "") or ""
        txt   = f"{title} {desc}".lower()
        if any(k in txt for k in EXCLUDED_KEYWORDS):
            dropped_excluded += 1
            continue
        normalized.append({
            "title":   title,
            "url":     url,
            "source":  hostname,
            "date":    item.get("age", "") or item.get("page_age", ""),
            "summary": desc,
        })
    print(f"    → {len(raw_results)} raw, {len(normalized)} kept"
          + (f", {dropped_paywall} paywall" if dropped_paywall else "")
          + (f", {dropped_excluded} excluded kw" if dropped_excluded else ""))
    return normalized


def _hostname(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _host_in(host: str, domains: set) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def expand_aggregator(aggregator_url: str) -> list[dict]:
    """Fetch an aggregator page, extract primary-source links from its body.
    Each extracted link becomes a separate candidate (anchor text = title,
    parent paragraph = summary). Generic anchor text ('click here', 'register')
    is skipped to avoid wrong-URL/wrong-event pairings."""
    try:
        from bs4 import BeautifulSoup
    except Exception as e:
        print(f"    ⚠ bs4 not available ({e}) — skipping aggregator expansion")
        return []

    try:
        r = requests.get(
            aggregator_url,
            headers={"User-Agent": BROWSER_UA},
            timeout=8,
            allow_redirects=True,
        )
        if r.status_code >= 400 or not r.text:
            print(f"    ✗ Aggregator fetch failed ({r.status_code}): {aggregator_url[:60]}")
            return []
    except Exception as e:
        print(f"    ✗ Aggregator fetch error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.find("article") or soup.find("main") or soup
    aggregator_host = _hostname(aggregator_url)

    candidates = []
    seen = set()
    skipped_generic = 0
    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(aggregator_url, href)
        if not href.startswith("http"):
            continue

        host = _hostname(href)
        if not host or host == aggregator_host:
            continue
        if _host_in(host, AGGREGATOR_DOMAINS):  # don't chain aggregators
            continue
        if _host_in(host, BLOCKED_DOMAINS):
            continue
        if _host_in(host, SOCIAL_DOMAINS):
            continue

        url_clean = href.rstrip("/")
        if url_clean in seen:
            continue

        anchor_text = (a.get_text(strip=True) or "")[:200]
        # Skip links with generic anchor text — can't reliably associate with event
        if len(anchor_text) < 4 or anchor_text.strip().lower() in GENERIC_ANCHOR_TEXT:
            skipped_generic += 1
            continue

        seen.add(url_clean)
        parent = a.find_parent(["p", "li", "div"])
        summary = (parent.get_text(" ", strip=True) if parent else anchor_text)[:500]

        candidates.append({
            "title":   anchor_text,
            "url":     href,
            "source":  host,
            "date":    "",
            "summary": summary,
        })

    label = f"{aggregator_host}"
    extras = f"({skipped_generic} generic skipped)" if skipped_generic else ""
    print(f"    ↳ Extracted {len(candidates)} primary sources from {label} {extras}".rstrip())
    return candidates


def fetch_candidates(search_areas: list[str], excluded_urls: set | None = None) -> list[dict]:
    """Build a pool of free-event candidates from multiple targeted queries.
    Brave queries have -site: operators appended to exclude paywall + aggregator domains.
    Previously featured URLs are also excluded."""
    if excluded_urls is None:
        excluded_urls = set()

    queries = []
    for area in search_areas:
        # Strip " GA" / " Atlanta" suffixes so we can quote the city/area name directly
        city = area.replace(" GA", "").replace(" Atlanta", "").strip()
        # Quote the area to force it as a required phrase in results
        queries.append(f'"free" events "{city}" Georgia')
        queries.append(f'"free" things to do "{city}"')
        queries.append(f'"free" family "{city}" Georgia')

    seen = set()
    candidates = []
    excluded_count = 0
    expanded_from_aggregators = 0
    scraped_aggregators = set()  # don't scrape the same aggregator URL twice

    def _add_item(item: dict) -> bool:
        u = item["url"].rstrip("/")
        if u in excluded_urls:
            return False
        t = item["title"].lower().strip()
        if u in seen or (t and t in seen):
            return False
        seen.add(u)
        if t:
            seen.add(t)
        candidates.append(item)
        return True

    for q in queries:
        print(f"  Searching Brave: {q}")
        results = search_brave(q)
        for item in results:
            u = item["url"].rstrip("/")
            if u in excluded_urls:
                excluded_count += 1
                continue

            # Always try to add the original item (aggregator or primary — doesn't matter)
            _add_item(item)

            # If it's from an aggregator, also scrape for primary-source links and add those
            host = (item.get("source") or "").lower()
            if _host_in(host, AGGREGATOR_DOMAINS) and u not in scraped_aggregators:
                scraped_aggregators.add(u)
                print(f"  🔗 Aggregator: {host} — scraping for primary sources")
                for p in expand_aggregator(item["url"]):
                    if _add_item(p):
                        expanded_from_aggregators += 1

    if excluded_count:
        print(f"  Excluded {excluded_count} previously featured URLs")
    if expanded_from_aggregators:
        print(f"  Added {expanded_from_aggregators} primary sources from aggregator pages")
    print(f"  {len(candidates)} unique candidates after dedup")
    return candidates


# ---------------------------------------------------------------------------
# 4. CLAUDE SELECTS + WRITES
# ---------------------------------------------------------------------------
def write_free_events(candidates: list[dict], newsletter_name: str, display_area: str,
                      skill_prompt: str, pub_date: str) -> dict:
    """Ask Claude to pick 3-5 real free events and write blurbs.
    URLs are attached from source data — Claude returns candidate_index only."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    indexed = [{**c, "candidate_index": i} for i, c in enumerate(candidates, 1)]
    candidates_json = json.dumps(indexed, indent=2)

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""Find 3-5 actually-free events happening in the next 7 days near the {display_area} area.

Newsletter: {newsletter_name}
Publication date: {pub_date}
Coverage area: {display_area}

CRITICAL: Do NOT return raw URLs. Return "candidate_index" for each event — we will attach the source URL from the candidate list using that index.

Mix family-friendly and adults-only events when they qualify, and label each.

Return ONLY valid JSON, no preamble or markdown fences.

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

    raw = next((block.text for block in response.content if block.type == "text"), "")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    if not (clean.startswith("[") or clean.startswith("{")):
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            clean = clean[start:end + 1]
    try:
        result = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"  ✗ Failed to parse Claude JSON: {e}")
        print(f"  Raw response (first 500 chars): {raw[:500]}")
        return {"newsletter_name": newsletter_name, "events": []}

    events = result.get("events", [])
    dropped = result.get("dropped_candidates", [])
    print(f"  Claude returned {len(events)} events, dropped {len(dropped)} candidates")
    # Log why Claude dropped things — useful for diagnosing when Claude is too strict
    for d in dropped[:10]:
        print(f"    • dropped idx {d.get('candidate_index', '?')}: {d.get('reason', '')[:120]}")

    # Parse publication date once for filtering
    from datetime import date, timedelta
    try:
        pub = datetime.strptime(pub_date, "%Y-%m-%d").date()
    except Exception:
        pub = date.today()
    window_end = pub + timedelta(days=14)  # allow 2-week lookahead to reduce false drops

    # Attach real URLs from candidates using index. Reject events with invalid data.
    candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}
    validated = []
    for ev in events:
        # 1. Index → real URL
        idx = ev.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        source = candidates_by_index.get(idx) if idx is not None else None
        if not source:
            print(f"    ✗ Rejecting event with invalid candidate_index {idx}: {ev.get('name', '?')}")
            continue

        # 2. Event date is on or after pub_date, and within a reasonable window
        date_str = (ev.get("event_date") or "").strip()
        if not date_str:
            print(f"    ✗ Dropping event with no event_date: {ev.get('name', '?')}")
            continue
        try:
            ev_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            print(f"    ✗ Dropping event with unparseable event_date '{date_str}': {ev.get('name', '?')}")
            continue
        if ev_date < pub:
            print(f"    ✗ Dropping past event ({ev_date}): {ev.get('name', '?')}")
            continue
        if ev_date > window_end:
            print(f"    ✗ Dropping event outside 7-day window ({ev_date}): {ev.get('name', '?')}")
            continue

        # 3. URL is live
        url = source.get("url", "")
        if not url or not validate_url(url):
            print(f"    ✗ Dropping event with dead/missing URL: {ev.get('name', '?')}")
            continue

        ev["source_url"] = url
        ev["source"]     = source.get("source", "")
        ev.pop("candidate_index", None)
        validated.append(ev)

    result["events"] = validated
    print(f"  Claude selected {len(validated)} free events (after date + URL validation)")
    for ev in validated:
        print(f"    {ev.get('emoji', '')} {ev.get('name', '')} — {ev.get('event_date', '?')} ({ev.get('audience', '?')})")

    return result


# ---------------------------------------------------------------------------
# 5. SAVE
# ---------------------------------------------------------------------------
def save_results(result: dict, newsletter_name: str) -> None:
    save_free_events_to_notion(result, newsletter_name)

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    json_file = output_dir / f"free_events_{newsletter_name}_{datetime.today().strftime('%Y%m%d')}.json"
    json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  ✓ Saved JSON to {json_file}")


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Free Events automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()
    pub_date = datetime.today().strftime("%Y-%m-%d")

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        excluded = get_used_free_event_urls(newsletter["name"])
        candidates = fetch_candidates(newsletter["search_areas"], excluded_urls=excluded)
        if not candidates:
            print(f"  No candidates found for {newsletter['name']}. Skipping.")
            continue

        print(f"\n  Sending {len(candidates)} candidates to Claude...")
        result = write_free_events(
            candidates=candidates,
            newsletter_name=newsletter["name"],
            display_area=newsletter["display_area"],
            skill_prompt=skill_prompt,
            pub_date=pub_date,
        )

        if not result.get("events"):
            print(f"  No qualifying free events for {newsletter['name']}. Skipping.")
            continue

        save_results(result, newsletter["name"])
        print(f"  Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
