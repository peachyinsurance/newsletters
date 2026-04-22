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

# Aggregator / round-up / syndication sites. Results from these domains are scraped
# to extract the primary-source links they reference; the aggregator URL itself is dropped.
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
    "morningstar.com",
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "accesswire.com",
    "finance.yahoo.com",
    "news.yahoo.com",
    "streetinsider.com",
}

# Social / link-shorteners we never want as candidates (even if an aggregator points to them)
SOCIAL_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "t.co", "bit.ly", "tinyurl.com", "lnkd.in",
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

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
def search_brave(query: str) -> list[dict]:
    """One Brave news search — returns normalized candidates."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }
    try:
        res = requests.get(
            "https://api.search.brave.com/res/v1/news/search",
            headers=headers,
            params={"q": query, "count": MAX_RESULTS_PER_QUERY, "freshness": "pw"},
            timeout=30,
        )
        if res.status_code != 200:
            print(f"    Brave error {res.status_code}: {res.text[:200]}")
            return []
        results = res.json().get("results", [])
    except Exception as e:
        print(f"    Brave error: {e}")
        return []

    normalized = []
    for item in results:
        url = item.get("url", "")
        hostname = item.get("meta_url", {}).get("hostname", "") if isinstance(item.get("meta_url"), dict) else ""
        if not url:
            continue
        if any(d in url.lower() or d in hostname.lower() for d in BLOCKED_DOMAINS):
            continue
        title = item.get("title", "") or ""
        desc  = item.get("description", "") or ""
        txt   = f"{title} {desc}".lower()
        if any(k in txt for k in EXCLUDED_KEYWORDS):
            continue
        normalized.append({
            "title":   title,
            "url":     url,
            "source":  hostname,
            "date":    item.get("age", "") or item.get("page_age", ""),
            "summary": desc,
        })
    return normalized


def _hostname(url: str) -> str:
    """Extract hostname from a URL, lowercased, no www."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return host.lower().removeprefix("www.")
    except Exception:
        return ""


def _is_aggregator(url: str, hostname: str = "") -> bool:
    host = (hostname or _hostname(url)).lower()
    return any(d == host or host.endswith("." + d) for d in AGGREGATOR_DOMAINS)


def _is_blocked(url: str, hostname: str = "") -> bool:
    host = (hostname or _hostname(url)).lower()
    return any(d == host or host.endswith("." + d) for d in BLOCKED_DOMAINS)


def _is_social(url: str, hostname: str = "") -> bool:
    host = (hostname or _hostname(url)).lower()
    return any(d == host or host.endswith("." + d) for d in SOCIAL_DOMAINS)


def expand_aggregator(aggregator_url: str) -> list[dict]:
    """Fetch an aggregator page and extract primary-source links from its article body.
    Returns candidate dicts (title, url, source, summary). On any failure returns []."""
    try:
        from bs4 import BeautifulSoup
    except Exception as e:
        print(f"    ⚠ bs4 not available ({e}) — skipping aggregator expansion")
        return []

    try:
        r = requests.get(aggregator_url, headers={"User-Agent": BROWSER_UA}, timeout=8, allow_redirects=True)
        if r.status_code >= 400 or not r.text:
            print(f"    ✗ Aggregator fetch failed ({r.status_code}): {aggregator_url[:60]}")
            return []
    except Exception as e:
        print(f"    ✗ Aggregator fetch error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    # Focus on the main article body; fall back to full page if neither exists
    body = soup.find("article") or soup.find("main") or soup

    aggregator_host = _hostname(aggregator_url)
    candidates = []
    seen = set()

    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        # Resolve relative URLs against the aggregator URL
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(aggregator_url, href)
        if not href.startswith("http"):
            continue

        host = _hostname(href)
        if not host or host == aggregator_host:
            continue
        if _is_aggregator(href, host):  # don't chain aggregators
            continue
        if _is_blocked(href, host):
            continue
        if _is_social(href, host):
            continue

        url_clean = href.rstrip("/")
        if url_clean in seen:
            continue
        seen.add(url_clean)

        # Title = anchor text. Summary = parent paragraph's text if available.
        anchor_text = (a.get_text(strip=True) or "")[:200]
        if len(anchor_text) < 3:
            continue  # skip blank / tiny link labels
        parent = a.find_parent(["p", "li", "div"])
        summary = (parent.get_text(" ", strip=True) if parent else anchor_text)[:500]

        candidates.append({
            "title":   anchor_text,
            "url":     href,
            "source":  host,
            "date":    "",
            "summary": summary,
        })

    print(f"    ↳ Extracted {len(candidates)} primary sources from {aggregator_host}")
    return candidates


def fetch_candidates(search_areas: list[str], excluded_urls: set | None = None) -> list[dict]:
    """Build a pool of free-event candidates from multiple targeted queries.
    When Brave returns a known aggregator URL, we scrape its primary-source links
    instead of passing the aggregator page to Claude.
    Excludes any URL that was previously featured."""
    if excluded_urls is None:
        excluded_urls = set()

    queries = []
    for area in search_areas:
        queries.append(f'"free" events {area} this week')
        queries.append(f'"free" things to do {area}')
        queries.append(f'free family events {area}')
    # Broader fallback
    queries.append("free events metro Atlanta this week")

    seen = set()
    candidates = []
    excluded_count = 0
    expanded_from_aggregators = 0

    def _add_item(item: dict) -> bool:
        """Add a single candidate, with dedup + exclusion check. Returns True if added."""
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

            # If this is an aggregator, scrape it for primary sources and replace
            if _is_aggregator(item["url"], item.get("source", "")):
                print(f"  🔗 Aggregator detected: {item.get('source', '')} — scraping for primary sources")
                primaries = expand_aggregator(item["url"])
                for p in primaries:
                    if _add_item(p):
                        expanded_from_aggregators += 1
                continue  # never add the aggregator URL itself

            _add_item(item)

    if excluded_count:
        print(f"  Excluded {excluded_count} previously featured URLs")
    if expanded_from_aggregators:
        print(f"  Extracted {expanded_from_aggregators} primary sources from aggregator pages")
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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    result = json.loads(clean)

    events = result.get("events", [])
    # Attach real URLs from candidates using index. Reject events with invalid index.
    candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}
    validated = []
    for ev in events:
        idx = ev.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        source = candidates_by_index.get(idx) if idx is not None else None
        if not source:
            print(f"    ✗ Rejecting event with invalid candidate_index {idx}: {ev.get('name', '?')}")
            continue
        url = source.get("url", "")
        if not url or not validate_url(url):
            print(f"    ✗ Dropping event with dead/missing URL: {ev.get('name', '?')}")
            continue
        ev["source_url"] = url
        ev["source"]     = source.get("source", "")
        ev.pop("candidate_index", None)
        validated.append(ev)

    result["events"] = validated
    print(f"  Claude selected {len(validated)} free events")
    for ev in validated:
        print(f"    {ev.get('emoji', '')} {ev.get('name', '')} ({ev.get('audience', '?')})")

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
