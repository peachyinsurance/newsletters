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

import re as _re


def _image_looks_real(url: str) -> bool:
    """HEAD/GET-validate that a URL actually returns an image (not a 404 page,
    redirect-to-login, or 1px tracker pixel). Returns True if content-type is
    image/* AND payload is reasonably sized (> 5 KB)."""
    if not url:
        return False
    try:
        r = requests.get(
            url, timeout=8, allow_redirects=True, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (newsletter-automation)"},
        )
        if r.status_code != 200:
            return False
        ct = (r.headers.get("Content-Type") or "").lower()
        if not ct.startswith("image/"):
            return False
        # Size hint: use Content-Length if present, else read the first chunk
        size = int(r.headers.get("Content-Length") or 0)
        if size == 0:
            chunk = next(r.iter_content(8192), b"")
            size = len(chunk)
        return size >= 5_000
    except Exception:
        return False


def _absolutize(url: str, base_url: str) -> str:
    """Resolve //, relative, and absolute URLs against the page's base URL."""
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{url}"
    return url  # leave alone if we can't resolve


def fetch_event_image(source_url: str) -> str:
    """Pull a reliable hero image URL from the source page. Tries (in order):
       1. og:image / twitter:image / image_src meta tags
       2. JSON-LD structured data (schema.org Event.image)
       3. First reasonably-large <img> in the page body (>= 400px width)

    Each candidate is HEAD-validated to confirm it actually serves an image
    >5 KB before returning. Skips obvious logos, favicons, trackers, etc.
    Best-effort; returns empty string if nothing reliable is found.
    """
    if not source_url:
        return ""
    try:
        r = requests.get(
            source_url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (newsletter-automation)"},
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.text:
            return ""
        html = r.text
    except Exception:
        return ""

    SKIP_TOKENS = ("logo", "favicon", "sprite", "icon-", "/icons/",
                   "placeholder", "spacer", "tracker", "pixel.gif",
                   "1x1", "blank.gif", "transparent.png")

    candidates: list[str] = []

    # 1. Meta tags (og:image, twitter:image, image_src)
    head = html[:200_000]  # meta tags live in <head> — cap for speed
    meta_patterns = [
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
    ]
    for pat in meta_patterns:
        m = _re.search(pat, head, _re.IGNORECASE)
        if m:
            candidates.append(m.group(1).strip())

    # 2. JSON-LD: schema.org Event objects often carry an `image` field
    for ld_match in _re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
        html, _re.IGNORECASE | _re.DOTALL,
    ):
        try:
            blob = json.loads(ld_match.group(1).strip())
        except Exception:
            continue
        items = blob if isinstance(blob, list) else [blob]
        for item in items:
            if not isinstance(item, dict):
                continue
            img = item.get("image")
            if isinstance(img, str):
                candidates.append(img)
            elif isinstance(img, list):
                for x in img:
                    if isinstance(x, str):
                        candidates.append(x)
                    elif isinstance(x, dict) and isinstance(x.get("url"), str):
                        candidates.append(x["url"])
            elif isinstance(img, dict) and isinstance(img.get("url"), str):
                candidates.append(img["url"])

    # 3. First reasonably-large <img> in the page body — last-resort fallback
    #    when meta tags are absent. Match <img ... width="N" ... src="...">
    #    where N >= 400 (large enough to be a hero photo, not an icon).
    body_img_patterns = [
        r'<img[^>]+width=["\']?(\d+)["\']?[^>]+src=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]+width=["\']?(\d+)["\']?',
    ]
    for pat in body_img_patterns:
        for m in _re.finditer(pat, html, _re.IGNORECASE):
            groups = m.groups()
            # Different group order in the two patterns
            if pat.startswith(r'<img[^>]+width'):
                w_str, url_str = groups[0], groups[1]
            else:
                url_str, w_str = groups[0], groups[1]
            try:
                w = int(w_str)
            except ValueError:
                continue
            if w >= 400:
                candidates.append(url_str)

    # Validate each candidate (in priority order) until one passes
    seen: set = set()
    for url in candidates:
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if url.startswith("data:"):
            continue
        url = _absolutize(url, source_url)
        ul = url.lower()
        if any(skip in ul for skip in SKIP_TOKENS):
            continue
        if _image_looks_real(url):
            print(f"      ✓ free-event image: {url[:80]}…")
            return url

    print(f"      · no reliable free-event image found ({len(candidates)} candidates rejected)")
    return ""

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
    # Travel guides / listicle aggregators
    "tripster.com",
    "tripadvisor.com",
    "thrillist.com",
    "timeout.com",
    "yelp.com",
    "discoveratlanta.com",
    "atlantatrails.com",
    "exploregeorgia.org",
}

# URL path / title hints that mean "listicle / guide page" — used after Claude
# picks a winner to detect "still aggregator-shaped" URLs and trigger drill-down.
LISTICLE_PATH_HINTS = (
    "/travelguide/", "/guide/", "/things-to-do", "/top-", "/best-",
    "/list/", "/listicle/", "/roundup/",
)
LISTICLE_TITLE_HINTS = (
    "things to do", "top ", "best ", "free things", "free events", "guide to",
    "what to do", "places to", "events in",
)

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "display_area": "East Cobb",
        "search_areas": ["East Cobb GA", "Marietta GA", "Kennesaw GA"],
        "lat":          33.9773,
        "lng":          -84.5130,
    },
    {
        "name":         "Perimeter_Post",
        "display_area": "Perimeter",
        "search_areas": ["Dunwoody GA", "Sandy Springs GA", "Perimeter Atlanta"],
        "lat":          33.9462,
        "lng":          -84.3346,
    },
]

# Evergreen "free thing" search radius (~10 miles)
EVERGREEN_RADIUS_METERS = 16093

# Google Places types worth featuring as a year-round free outing.
# All are typically free to enter; we additionally filter out anything with
# explicit `priceLevel` indicating fees.
EVERGREEN_PLACE_TYPES = [
    "park",
    "library",
    "museum",
    "tourist_attraction",
    "garden",
    "hiking_area",
    "dog_park",
    "playground",
]

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")


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
    """Brave WEB search (broader index than news). Returns normalized candidates.
    Appends -site: operators to exclude paywall domains."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }
    full_query = query + _build_exclusions()
    try:
        res = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params={"q": full_query, "count": MAX_RESULTS_PER_QUERY, "freshness": "pm"},
            timeout=30,
        )
        if res.status_code != 200:
            print(f"    Brave error {res.status_code}: {res.text[:200]}")
            return []
        # Web search returns results under data.web.results
        web = res.json().get("web", {}) or {}
        raw_results = web.get("results", []) or []
    except Exception as e:
        print(f"    Brave error: {e}")
        return []

    normalized = []
    dropped_paywall = 0
    dropped_excluded = 0
    for item in raw_results:
        url = item.get("url", "")
        # web search exposes hostname under meta_url.hostname too
        meta = item.get("meta_url") or {}
        hostname = meta.get("hostname", "") if isinstance(meta, dict) else ""
        if not hostname:
            hostname = _hostname(url)
        if not url:
            continue
        url_l, host_l = url.lower(), hostname.lower()
        if any(d in url_l or d in host_l for d in BLOCKED_DOMAINS):
            dropped_paywall += 1
            continue
        title = item.get("title", "") or ""
        desc  = item.get("description", "") or item.get("snippet", "") or ""
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
    """Ask Claude to score up to 5 free-event candidates on time sensitivity.
    Then combine with a deterministic source_quality score (based on
    AGGREGATOR_DOMAINS) to pick the single Free Event of the Week.
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
                    "content": f"""Evaluate the candidates below and pick the single best free event for this week's {display_area} newsletter.

Newsletter: {newsletter_name}
Publication date: {pub_date}
Coverage area: {display_area}

RETURN:
- `events`: array of EXACTLY ONE event — your #1 pick
- `all_scored`: array of up to 5 candidates, ranked best-to-worst, each with `time_sensitivity_score` (1-10) per the rubric
- `dropped_candidates`: anything you ruled out entirely and why

CRITICAL: Do NOT return raw URLs. Return `candidate_index` for each entry — we attach the source URL from the candidate list using that index. A downstream step adds a source_quality bonus (based on whether the URL is from an aggregator) and may re-rank, so giving us your full top-5 with scores is more useful than just returning one.

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

    dropped = result.get("dropped_candidates", [])
    print(f"  Claude dropped {len(dropped)} candidates")
    for d in dropped[:10]:
        print(f"    • dropped idx {d.get('candidate_index', '?')}: {d.get('reason', '')[:120]}")

    from datetime import date, timedelta
    try:
        pub = datetime.strptime(pub_date, "%Y-%m-%d").date()
    except Exception:
        pub = date.today()
    window_end = pub + timedelta(days=14)

    candidates_by_index = {i: c for i, c in enumerate(candidates, 1)}

    # Past-tense / recap markers in the article that strongly suggest the event already happened.
    PAST_TENSE_MARKERS = (
        "was held", "was hosted", "took place", "has taken place", "has happened",
        "concluded", "ended last", "wrapped up", "recap", "attendees enjoyed",
        "turnout was", "drew a crowd", "last saturday", "last sunday", "last weekend",
        "last friday", "last monday", "last tuesday", "last wednesday", "last thursday",
        "last week", "last month", "went home with", "winners announced",
    )

    def _looks_past_tense(source: dict) -> str:
        """Return a matching past-tense phrase if found in the article title/summary, else ''."""
        txt = f"{source.get('title', '')} {source.get('summary', '')}".lower()
        for marker in PAST_TENSE_MARKERS:
            if marker in txt:
                return marker
        return ""

    # Build scoreboard from all_scored (fall back to events if all_scored missing)
    scoreboard_input = result.get("all_scored") or result.get("events", [])
    scoreboard = []
    for ev in scoreboard_input:
        idx = ev.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        source = candidates_by_index.get(idx) if idx is not None else None
        if not source:
            print(f"    ✗ Dropping scored entry with invalid candidate_index {idx}: {ev.get('name', '?')}")
            continue

        url  = source.get("url", "")
        host = (source.get("source") or "").lower()
        time_score = int(ev.get("time_sensitivity_score", 0) or 0)
        is_aggregator = _host_in(host, AGGREGATOR_DOMAINS)
        source_score = 3 if is_aggregator else 10
        total = time_score + source_score

        scoreboard.append({
            "ev":            ev,
            "source_url":    url,
            "source_host":   host,
            "time_score":    time_score,
            "source_score":  source_score,
            "total":         total,
            "is_aggregator": is_aggregator,
        })

    # Sort: total DESC (primary), event_date DESC (tiebreaker — pick latest)
    def _event_date(s):
        try:
            return datetime.strptime((s["ev"].get("event_date") or "").strip(), "%Y-%m-%d").date()
        except Exception:
            return date.min
    scoreboard.sort(key=lambda s: (s["total"], _event_date(s)), reverse=True)

    print(f"  Scoreboard ({len(scoreboard)} candidates):")
    for i, s in enumerate(scoreboard, 1):
        ev = s["ev"]
        tag = "aggregator" if s["is_aggregator"] else "primary"
        print(f"    {i}. \"{ev.get('name', '?')}\" {ev.get('event_date', '')}"
              f"  time={s['time_score']} source={s['source_score']} total={s['total']}  ({tag})")

    # Pick the highest that passes date + URL + past-tense validation
    winner = None
    for s in scoreboard:
        ev = s["ev"]
        date_str = (ev.get("event_date") or "").strip()
        try:
            ev_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            print(f"    ✗ Skipping '{ev.get('name', '?')}': unparseable event_date '{date_str}'")
            continue
        if ev_date < pub:
            print(f"    ✗ Skipping '{ev.get('name', '?')}': past event ({ev_date})")
            continue
        if ev_date > window_end:
            print(f"    ✗ Skipping '{ev.get('name', '?')}': outside 14-day window ({ev_date})")
            continue
        # Article text looks like a recap — likely Claude hallucinated a future date
        idx = None
        # Find the index we used when building this scoreboard entry so we can re-look up the source
        for i, c in candidates_by_index.items():
            if c.get("url", "") == s["source_url"]:
                idx = i
                break
        src = candidates_by_index.get(idx) if idx else None
        if src:
            past_marker = _looks_past_tense(src)
            if past_marker:
                print(f"    ✗ Skipping '{ev.get('name', '?')}': article text looks past-tense ('{past_marker}')")
                continue
        if not s["source_url"] or not validate_url(s["source_url"]):
            print(f"    ✗ Skipping '{ev.get('name', '?')}': dead/missing URL")
            continue
        winner = s
        break

    if not winner:
        print(f"  No qualifying free event for {newsletter_name}")
        result["events"] = []
        return result

    ev = winner["ev"]
    raw_url = winner["source_url"]
    # Try to drill down: if the winner's URL is a listicle/guide page, find the
    # specific event link inside it and use that instead of the generic guide URL.
    refined_url = drill_down_aggregator_url(raw_url, ev.get("name", ""))
    ev["source_url"] = refined_url
    ev["source"]     = winner["source_host"]
    ev.pop("candidate_index", None)
    ev["time_sensitivity_score"] = winner["time_score"]
    ev["source_quality_score"]   = winner["source_score"]
    ev["total_score"]            = winner["total"]
    ev["image_url"]              = fetch_event_image(ev["source_url"])

    result["events"] = [ev]
    print(f"  🏆 Winner: {ev.get('emoji', '')} {ev.get('name', '')} "
          f"({ev.get('audience', '?')})  total={winner['total']}")
    return result


# ---------------------------------------------------------------------------
# 4a. DRILL-DOWN INTO LISTICLES
# ---------------------------------------------------------------------------
def _looks_like_listicle(url: str, page_title: str = "") -> bool:
    """Heuristic: is this URL a listicle / guide page (rather than a single event)?"""
    ul = (url or "").lower()
    tl = (page_title or "").lower()
    if any(h in ul for h in LISTICLE_PATH_HINTS):
        return True
    if any(h in tl for h in LISTICLE_TITLE_HINTS):
        return True
    return False


def _normalize_for_match(s: str) -> set[str]:
    """Tokenize a string into a set of meaningful words for fuzzy matching."""
    s = (s or "").lower()
    s = _re.sub(r"[^\w\s]", " ", s)
    stopwords = {"the", "a", "an", "and", "or", "of", "in", "at", "on", "for", "to",
                 "with", "from", "by", "is", "are", "this", "that", "it", "its",
                 "free", "event", "events", "things", "do"}
    return {w for w in s.split() if len(w) > 2 and w not in stopwords}


def drill_down_aggregator_url(source_url: str, event_name: str) -> str:
    """If `source_url` is a listicle / guide page AND `event_name` is specific,
    fetch the page and find a link whose anchor text most closely matches the
    event name. Returns the better URL if found, else returns source_url unchanged.

    This is the "double-click" behavior: instead of linking the reader to a
    generic 'Free Things to Do in Atlanta' page, link them to the specific
    event listed on that page.
    """
    if not source_url or not event_name:
        return source_url
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return source_url

    try:
        r = requests.get(
            source_url, timeout=8, allow_redirects=True,
            headers={"User-Agent": BROWSER_UA},
        )
        if r.status_code >= 400 or not r.text:
            return source_url
    except Exception:
        return source_url

    soup = BeautifulSoup(r.text, "html.parser")
    page_title = (soup.title.string if soup.title else "") or ""

    # Only drill down if this looks like a listicle. If it's a single-event page,
    # the URL is already specific — leave it.
    if not _looks_like_listicle(source_url, page_title):
        return source_url

    print(f"    🔍 Drill-down: '{source_url[:60]}…' looks like a listicle. "
          f"Searching for '{event_name[:40]}'…")

    event_tokens = _normalize_for_match(event_name)
    if not event_tokens:
        return source_url

    aggregator_host = _hostname(source_url)
    body = soup.find("article") or soup.find("main") or soup

    best_url = source_url
    best_overlap = 0
    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(source_url, href)
        if not href.startswith("http"):
            continue
        host = _hostname(href)
        # Allow same-host (a deeper path on this site) OR external — both are fine.
        # But skip social shares.
        if _host_in(host, SOCIAL_DOMAINS) or _host_in(host, BLOCKED_DOMAINS):
            continue
        # Skip the listicle URL itself
        if href.rstrip("/") == source_url.rstrip("/"):
            continue

        anchor_text = (a.get_text(strip=True) or "")[:200]
        # Also consider the link's title attribute and surrounding heading
        title_attr = a.get("title", "") or ""
        # Walk up to find a nearest heading for context (h2/h3 typical in listicles)
        nearby = ""
        parent = a
        for _ in range(4):
            parent = parent.parent if parent else None
            if not parent:
                break
            heading = parent.find(["h2", "h3", "h4"])
            if heading:
                nearby = heading.get_text(strip=True)
                break

        candidate_text = " ".join([anchor_text, title_attr, nearby])
        candidate_tokens = _normalize_for_match(candidate_text)
        if not candidate_tokens:
            continue
        overlap = len(event_tokens & candidate_tokens)
        if overlap > best_overlap and overlap >= 2:
            best_overlap = overlap
            best_url = href

    if best_url != source_url:
        print(f"    ✓ Drill-down match (overlap={best_overlap}): {best_url[:80]}")
        return best_url
    print("    · No strong drill-down match — keeping original URL")
    return source_url


# ---------------------------------------------------------------------------
# 4b. EVERGREEN FALLBACK (Google Places — parks, libraries, museums, etc.)
# ---------------------------------------------------------------------------
def fetch_evergreen_freebie(lat: float, lng: float, display_area: str,
                            excluded_urls: set) -> dict | None:
    """When no time-sensitive free event is available, fall back to a year-round
    free public facility from Google Places (parks, libraries, museums, etc.).
    Returns a dict shaped like a free event, or None if Places lookup fails or
    every candidate has already been featured.
    """
    if not GOOGLE_PLACES_API_KEY:
        print("  ⚠ Evergreen fallback: GOOGLE_PLACES_API_KEY not set — skipping.")
        return None

    print(f"\n  🌿 Evergreen fallback: searching Google Places near {lat},{lng} for free public facilities…")
    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type":     "application/json",
        "X-Goog-Api-Key":   GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.googleMapsUri,places.websiteUri,places.rating,"
            "places.userRatingCount,places.primaryTypeDisplayName,"
            "places.editorialSummary,places.priceLevel,places.types"
        ),
    }
    payload = {
        "includedTypes":    EVERGREEN_PLACE_TYPES,
        "maxResultCount":   20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": EVERGREEN_RADIUS_METERS,
            }
        },
        "rankPreference": "POPULARITY",
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
    except Exception as e:
        print(f"  ⚠ Evergreen Places error: {e}")
        return None
    if res.status_code != 200:
        print(f"  ⚠ Evergreen Places returned {res.status_code}: {res.text[:200]}")
        return None

    places = res.json().get("places", []) or []
    print(f"  Got {len(places)} evergreen candidates from Places API")

    # Filter: must be highly rated, plenty of reviews, no obvious paid label
    qualified = []
    for p in places:
        name    = (p.get("displayName") or {}).get("text", "")
        rating  = p.get("rating") or 0
        reviews = p.get("userRatingCount") or 0
        link    = p.get("websiteUri") or p.get("googleMapsUri") or ""
        # Reject if priceLevel suggests a paid attraction (e.g., MODERATE+).
        # Free public facilities either omit priceLevel or report PRICE_LEVEL_FREE.
        plevel = (p.get("priceLevel") or "").upper()
        if plevel and plevel not in ("", "PRICE_LEVEL_FREE", "PRICE_LEVEL_INEXPENSIVE"):
            continue
        if rating < 4.2 or reviews < 50:
            continue
        if not link:
            continue
        if link in excluded_urls:
            continue  # already featured before
        qualified.append({
            "place_id": p.get("id", ""),
            "name":     name,
            "type":     (p.get("primaryTypeDisplayName") or {}).get("text", ""),
            "address":  p.get("formattedAddress", ""),
            "rating":   rating,
            "reviews":  reviews,
            "url":      link,
            "summary":  (p.get("editorialSummary") or {}).get("text", ""),
        })

    if not qualified:
        print("  ⚠ No qualified evergreen candidates found.")
        return None

    qualified.sort(key=lambda q: (q["rating"], q["reviews"]), reverse=True)
    pick = qualified[0]
    print(f"  ⭐ Evergreen pick: {pick['name']} ({pick['rating']}★, {pick['reviews']} reviews)")

    # Generate a Claude blurb so it reads like a real recommendation
    blurb = _claude_evergreen_blurb(pick, display_area)

    return {
        "emoji":   "🌳",
        "name":    pick["name"],
        "when":    "Open year-round",
        "venue":   pick["address"],
        "audience": "all ages",
        "blurb":   blurb,
        "source":  "Google Places",
        "source_url":   pick["url"],
        "image_url":    fetch_event_image(pick["url"]),
        "time_sensitivity_score": 0,  # not time-sensitive
        "source_quality_score":   8,  # primary, high quality
        "total_score":            8,
        "is_evergreen": True,
    }


def _claude_evergreen_blurb(pick: dict, display_area: str) -> str:
    """Short neighborly blurb for an evergreen free spot."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    prompt = f"""Write a 2-3 sentence neighbor-style recommendation for this free, year-round
spot in the {display_area} area newsletter. Conversational, warm. No em dashes.
Eighth-grade reading level. Don't repeat the name verbatim more than once.
Mention what makes it worth visiting and a hint of what to do there.

Place: {pick['name']}
Type: {pick['type']}
Address: {pick['address']}
Rating: {pick['rating']}★ ({pick['reviews']} reviews)
Editorial summary (may be empty): {pick['summary']}

Return ONLY the blurb text. No preamble, no markdown, no quotes."""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return text.strip()
    except Exception as e:
        print(f"  ⚠ Claude blurb error: {e}")
        # Fallback to summary or a generic line
        return pick.get("summary") or f"A neighborhood favorite worth a free visit."


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
            # No time-sensitive event qualified — fall back to an evergreen
            # public freebie (park / library / museum) from Google Places.
            evergreen = fetch_evergreen_freebie(
                lat=newsletter["lat"],
                lng=newsletter["lng"],
                display_area=newsletter["display_area"],
                excluded_urls=excluded,
            )
            if not evergreen:
                print(f"  No qualifying free events or evergreen fallback for {newsletter['name']}. Skipping.")
                continue
            result["events"] = [evergreen]
            print(f"  🏆 Evergreen winner: {evergreen.get('emoji', '')} {evergreen.get('name', '')}")

        save_results(result, newsletter["name"])
        print(f"  Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
