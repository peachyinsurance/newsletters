#!/usr/bin/env python3
"""
Newsletter Automation - Local Lowdown Section
Scrapes Google News via Apify for local news stories,
then uses Claude to select the best 3-5 and write the section.
Saves results to Notion.

To add a new newsletter, just add an entry to NEWSLETTERS with search terms.
No hardcoded news sources needed — Google News finds them automatically.
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
from notion_helper import HEADERS as NOTION_HEADERS, save_lowdown_to_notion
from url_validator import validate_url
from newsletters_config import NEWSLETTERS, filter_by_env
from event_date_filter import brave_freshness_for_issue, effective_today

NOTION_API_KEY = os.environ["NOTION_API_KEY"]

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY      = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY  = os.environ["BRAVE_NEWS_API_KEY"]

from voice_helper import with_voice  # noqa: E402
SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-local-lowdown-skill_auto.md"

MAX_ARTICLES = 50  # Brave News API max per query — pull the full window
MIN_ARTICLES = 15  # minimum eligible articles before sending to Claude
MIN_STORIES  = 3   # minimum stories Claude must select, otherwise retry
MAX_STORIES  = 5   # cap on stories saved (target range is 3-5)
MAX_RETRIES  = 3   # how many fetch rounds to attempt
RETRY_TERMS_PER_ROUND = 3  # how many retry terms to use per round

# Topics to exclude — keep the newsletter PG and community-focused.
# The Local Lowdown should feel like good news + civic updates, not a crime
# blotter or obituary column. When in doubt, drop it.
EXCLUDED_KEYWORDS = {
    # Violent / serious crime
    "murder", "homicide", "killed", "kills", "killing", "stabbed", "stabbing",
    "shooting", "shooter", "shot dead", "gunfire", "gunman", "gunshot",
    "manslaughter", "assault", "assaulted", "rape", "raped", "sexual assault",
    "domestic violence", "arson", "robbery", "robbed", "carjacking",
    "kidnapping", "kidnapped", "abduction", "abducted", "hostage",
    "skeletal remains", "body found", "death investigation", "homicide",
    "human trafficking", "child porn", "molested", "molestation",
    "abuse", "abused", "abuser", "predator", "groomed", "grooming",
    # Drugs
    "drug bust", "drug trafficking", "overdose", "fentanyl", "meth lab",
    # Arrests / courts (negative tone)
    "arrested", "arrest of", "charged with", "indicted", "indictment",
    "arraigned", "arraign", "convicted", "sentenced", "plead guilty",
    "pleaded guilty", "guilty plea", "fugitive", "wanted suspect",
    "police chase", "manhunt", "standoff", "swat",
    # Fatal accidents / disasters
    "fatal", "fatally", "deadly", "dies in", "killed in", "died in",
    "found dead", "pronounced dead", "tragic death", "tragedy", "tragic",
    "drowned", "drowning", "house fire", "deadly fire", "wildfire victim",
    "plane crash", "fatal crash", "head-on crash", "rollover crash",
    "pedestrian killed", "cyclist killed", "hit-and-run", "hit and run",
    # Public health negatives
    "outbreak", "salmonella", "e. coli", "listeria", "recall hazard",
    "suicide", "self-harm",
    # Lawsuits / scandals
    "lawsuit", "sued", "sues", "scandal", "fraud", "embezzle", "embezzlement",
    "scam", "scammed", "ponzi", "indicted", "investigation into",
    "misconduct", "harassment", "fired for", "resigns amid", "steps down amid",
    # Partisan politics
    "trump", "biden", "desantis", "gop", "democrat", "republican",
    "partisan", "impeach",
}

# Domains we never source from: metered/soft paywalls that slip past automated
# detection, plus low-quality news AGGREGATORS that republish others' reporting
# (we want original local sources, not rewrites).
BLOCKED_DOMAINS = {
    "mdjonline.com",
    "ajc.com",
    "cobbcourier.com",   # aggregator — republishes other outlets' stories
}

# Pure event-aggregator domains (no real news content). These get dropped entirely.
# Note: sites like East Cobb News / Patch publish BOTH news and event roundups, so they
# are NOT in this list — individual event-list articles are caught by the title filter below.
AGGREGATOR_DOMAINS = {
    "cobbcountyevents.com",
    "atlantaonthecheap.com",
    "macaronikid.com",
    "mommypoppins.com",
    "accessatlanta.com",
}

# Title/summary markers that indicate an event list or calendar article (not news).
# We want the Local Lowdown to feel like news, not "10 things to do this weekend."
EVENT_ROUNDUP_MARKERS = (
    "weekend events", "things to do", "free events", "events this week",
    "events this weekend", "things to do this week", "things to do this weekend",
    "upcoming events", "calendar of events", "what's happening", "whats happening",
    "what to do this", "fun things to do", "free family events",
    "events calendar", "community calendar", "weekly events",
    "roundup of events", "weekend roundup", "your weekend guide",
    "guide to events", "things to do in",
)

# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a local newsletter writer. Select 3-5 timely local news stories and write concise summaries."


# ---------------------------------------------------------------------------
# 3. FETCH NEWS VIA BRAVE SEARCH API
# ---------------------------------------------------------------------------
def is_paywalled(url: str) -> bool:
    """Check if a URL is behind a paywall.
    Detects: hard paywalls (403), soft paywalls (JS popups), and metered paywalls."""
    try:
        r = requests.get(url, timeout=8, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; newsletter-bot)"})

        # Blocked or auth required
        if r.status_code in (401, 403, 451):
            return True

        # Redirected to a login/subscribe page
        final_url = r.url.lower()
        if any(kw in final_url for kw in ["login", "signin", "subscribe", "paywall", "register", "create-account"]):
            return True

        # Check HTML for paywall indicators (catches soft/JS paywalls too)
        content = r.text[:10000].lower()
        paywall_signals = [
            # Hard paywall text
            "subscribe to read", "subscribers only", "premium content",
            "create a free account", "sign in to continue",
            "to continue reading", "exclusive to subscribers",
            # Soft/JS paywall indicators
            "paywall", "metered", "leaky-paywall", "piano-paywall",
            "tp-modal", "subscriber-overlay", "regwall",
            "blox-paywall", "tnt-paywall", "lee-paywall",
            # Common local news paywall systems (Lee Enterprises, Blox CMS, TownNews)
            "townnews.com/static/paywall", "bloxcms", "lee-enterprises",
            "data-paywall", "data-meter", "pw-content-gate",
            # Generic modal/gate patterns
            "subscribe-modal", "subscription-required", "article-gate",
        ]
        if any(signal in content for signal in paywall_signals):
            return True

    except Exception:
        pass  # If we can't check, assume it's fine

    return False


def fetch_news_brave(search_terms: list[str]) -> list[dict]:
    """Fetch recent news articles via Brave Search News API. Returns real source URLs.

    Freshness anchors to ISSUE_DATE via `brave_freshness_for_issue()` when
    set (10-day lookback from that Thursday), otherwise 'pw' (past week)."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }
    freshness = brave_freshness_for_issue()  # honors ISSUE_DATE env

    all_articles = []
    seen_urls = set()

    for query in search_terms:
        print(f"  Searching Brave News for: {query} (freshness={freshness})")
        try:
            res = requests.get(
                "https://api.search.brave.com/res/v1/news/search",
                headers=headers,
                params={
                    "q": query,
                    "count": MAX_ARTICLES,
                    "freshness": freshness,
                },
                timeout=30,
            )
            if res.status_code != 200:
                print(f"  Brave API error {res.status_code}: {res.text[:300]}")
                continue

            data = res.json()
            results = data.get("results", [])
            print(f"  Brave returned {len(results)} articles")

            for item in results:
                url = item.get("url", "")
                if not url or url in seen_urls:
                    continue

                # Skip crime, violence, and partisan politics
                title = item.get("title", "")
                desc = item.get("description", "")
                text_to_check = f"{title} {desc}".lower()
                matched_keyword = next((kw for kw in EXCLUDED_KEYWORDS if kw in text_to_check), None)
                if matched_keyword:
                    print(f"    ✗ Skipping excluded topic ({matched_keyword}): {title[:60]}")
                    continue

                # Check blocked domains (metered paywalls that slip past detection)
                hostname = item.get("meta_url", {}).get("hostname", "") if isinstance(item.get("meta_url"), dict) else ""
                if any(domain in url.lower() or domain in hostname.lower() for domain in BLOCKED_DOMAINS):
                    print(f"    ✗ Skipping blocked domain ({hostname}): {url}")
                    continue

                # Skip pure event-aggregator sites (no real news content)
                if any(domain in url.lower() or domain in hostname.lower() for domain in AGGREGATOR_DOMAINS):
                    print(f"    ✗ Skipping event-aggregator site ({hostname}): {url}")
                    continue

                # Skip articles that are event lists / "things to do" roundups — we want NEWS
                title_lower = title.lower()
                matched_roundup = next((m for m in EVENT_ROUNDUP_MARKERS if m in title_lower), None)
                if matched_roundup:
                    print(f"    ✗ Skipping event roundup ('{matched_roundup}'): {title[:60]}")
                    continue

                # Check for paywall
                if is_paywalled(url):
                    print(f"    ✗ Skipping paywalled ({hostname}): {url}")
                    continue

                seen_urls.add(url)

                all_articles.append({
                    "title":   title,
                    "url":     url,
                    "source":  item.get("meta_url", {}).get("hostname", "") if isinstance(item.get("meta_url"), dict) else "",
                    "date":    item.get("age", "") or item.get("page_age", ""),
                    "summary": item.get("description", ""),
                })

        except Exception as e:
            print(f"  Brave API error: {e}")

    # Deduplicate by title
    unique = []
    seen_titles = set()
    for a in all_articles:
        title_key = a["title"].lower().strip()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(a)

    print(f"  {len(unique)} unique articles after dedup")
    return unique


# ---------------------------------------------------------------------------
# 4. CLAUDE: SELECT AND WRITE LOCAL LOWDOWN
# ---------------------------------------------------------------------------
def write_local_lowdown(articles: list[dict], newsletter_name: str, display_area: str,
                        skill_prompt: str, pub_date: str) -> dict:
    """Use Claude to select best stories and write the Local Lowdown section."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    # Tag each article with a 1-based index so Claude can reference them safely
    indexed_articles = [{**a, "article_index": i} for i, a in enumerate(articles, 1)]
    articles_json = json.dumps(indexed_articles, indent=2)

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
Here are recent local news articles scraped from Google News for the {display_area} area.

Newsletter: {newsletter_name}
Publication date: {pub_date}
Coverage area: {display_area}

Select the best 3-5 stories and write the Local Lowdown section.

CRITICAL rule about source URLs:
- Do NOT return raw URLs. For each story, return a field "source_article_indexes": [1, 3]
  listing the article_index values of the articles that informed that story.
- We will build the source_urls from the original article list using those indexes.
- Do not invent URLs or sources.

Return ONLY valid JSON, no preamble or markdown fences.

Articles:
{articles_json}
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

    stories = result.get("stories", [])

    # Rebuild source_urls ONLY from the original articles using Claude's indexes.
    # Discard whatever URLs Claude may have tried to provide (anti-hallucination).
    articles_by_index = {i: a for i, a in enumerate(articles, 1)}
    for story in stories:
        indexes = story.get("source_article_indexes") or []
        # Backwards compat: if Claude used source_urls with urls matching our list, honor those
        if not indexes and story.get("source_urls"):
            for src in story.get("source_urls", []):
                url = src.get("url", "")
                match_idx = next((i for i, a in articles_by_index.items() if a["url"] == url), None)
                if match_idx:
                    indexes.append(match_idx)

        rebuilt = []
        seen = set()
        for idx in indexes:
            try:
                idx = int(idx)
            except Exception:
                continue
            if idx in seen:
                continue
            seen.add(idx)
            article = articles_by_index.get(idx)
            if not article:
                print(f"    ✗ Claude referenced unknown article_index {idx} — skipping")
                continue
            url = article.get("url", "")
            if not url:
                continue
            if is_paywalled(url):
                print(f"    ✗ Removed paywalled source: {article.get('source', '')} ({url})")
                continue
            if not validate_url(url):
                print(f"    ✗ Removed dead source URL: {article.get('source', '')} ({url})")
                continue
            rebuilt.append({
                "url":   url,
                "label": article.get("source", "") or article.get("title", "")[:40],
            })
        story["source_urls"] = rebuilt
        # Drop the transient field from output
        story.pop("source_article_indexes", None)

    # Enforce 3-5 target range: cap at MAX_STORIES if Claude returns too many
    if len(stories) > MAX_STORIES:
        print(f"  Claude returned {len(stories)} stories, trimming to top {MAX_STORIES}")
        result["stories"] = stories[:MAX_STORIES]
        stories = result["stories"]

    print(f"  Claude selected {len(stories)} stories")
    for s in stories:
        print(f"    {s.get('emoji', '')} {s.get('headline', '')}")

    return result


# ---------------------------------------------------------------------------
# 5. WRITE TO NOTION CURRENT EDITION PAGE
# ---------------------------------------------------------------------------

def notion_search_page(title: str) -> str | None:
    """Search for an existing page by title. Returns page_id or None."""
    r = requests.post(
        "https://api.notion.com/v1/search",
        headers=NOTION_HEADERS,
        json={"query": title, "filter": {"value": "page", "property": "object"}},
        timeout=30,
    )
    r.raise_for_status()
    for result in r.json().get("results", []):
        page_title = result.get("properties", {}).get("title", {}).get("title", [])
        if page_title and page_title[0].get("text", {}).get("content", "") == title:
            if not result.get("archived", False):
                return result["id"]
    return None


def notion_get_blocks(page_id: str) -> list[dict]:
    """Get all child blocks of a page."""
    blocks = []
    has_more = True
    cursor = None
    while has_more:
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=NOTION_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        blocks += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return blocks


def find_section_and_replace(page_id: str, heading_text: str, new_blocks: list[dict]) -> bool:
    """Find a section by heading text, clear its content, insert new blocks."""
    blocks = notion_get_blocks(page_id)

    found_heading = False
    heading_id = None
    section_block_ids = []

    for block in blocks:
        block_type = block.get("type", "")
        if not found_heading:
            if block_type.startswith("heading_"):
                rich_text = block.get(block_type, {}).get("rich_text", [])
                text = "".join(t.get("text", {}).get("content", "") for t in rich_text)
                if heading_text.lower() in text.lower():
                    found_heading = True
                    heading_id = block["id"]
                    continue
        else:
            if block_type.startswith("heading_") or block_type == "divider":
                break
            section_block_ids.append(block["id"])

    if not heading_id:
        print(f"  Could not find heading '{heading_text}' on page")
        return False

    # Delete old content
    for bid in section_block_ids:
        requests.delete(f"https://api.notion.com/v1/blocks/{bid}", headers=NOTION_HEADERS, timeout=30)

    # Insert new blocks after the heading
    if new_blocks:
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": new_blocks, "after": heading_id},
            timeout=30,
        )
        if not r.ok:
            print(f"  Failed to insert blocks: {r.text[:300]}")
            return False

    return True


def paragraph_block(text: str, bold: bool = False) -> dict:
    annotations = {"bold": bold} if bold else {}
    return {
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}, "annotations": annotations}]},
    }


def save_results(result: dict, newsletter_name: str) -> None:
    """Save to Notion database. The assembler handles writing to the Current Edition page."""
    # Save to database (assembler reads from here)
    save_lowdown_to_notion(result, newsletter_name)

    # Save local files
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    json_file = output_dir / f"lowdown_{newsletter_name}_{datetime.today().strftime('%Y%m%d')}.json"
    json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  ✓ Saved JSON to {json_file}")


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Local Lowdown automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()
    pub_date = effective_today().strftime("%Y-%m-%d")  # honors ISSUE_DATE

    for newsletter in filter_by_env():
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Search for local news via Brave — retry with broader terms if not enough
        articles = fetch_news_brave(newsletter["lowdown_search_terms"])

        if len(articles) < MIN_ARTICLES:
            retry_terms = newsletter.get("lowdown_retry_terms", [])
            for attempt in range(1, MAX_RETRIES + 1):
                if len(articles) >= MIN_ARTICLES or not retry_terms:
                    break
                print(f"\n  Retry {attempt}/{MAX_RETRIES} — only {len(articles)} eligible articles, need {MIN_ARTICLES}")
                # Use next batch of retry terms
                extra_terms = retry_terms[:RETRY_TERMS_PER_ROUND]
                retry_terms = retry_terms[RETRY_TERMS_PER_ROUND:]
                print(f"  Broadening search with: {extra_terms}")
                extra_articles = fetch_news_brave(extra_terms)
                # Deduplicate against what we already have
                existing_urls = {a["url"] for a in articles}
                existing_titles = {a["title"].lower().strip() for a in articles}
                for a in extra_articles:
                    if a["url"] not in existing_urls and a["title"].lower().strip() not in existing_titles:
                        articles.append(a)
                        existing_urls.add(a["url"])
                        existing_titles.add(a["title"].lower().strip())
                print(f"  Now have {len(articles)} eligible articles")

        if not articles:
            print(f"  No articles found for {newsletter['name']}. Skipping.")
            continue

        # Add coverage area to each article for Claude
        for a in articles:
            a["coverage_area"] = newsletter["name"]

        # Claude selects and writes — retry with more articles if too few stories selected
        result = None
        retry_terms = newsletter.get("lowdown_retry_terms", [])[:]
        for claude_attempt in range(MAX_RETRIES + 1):
            print(f"\n  Sending {len(articles)} articles to Claude...")
            result = write_local_lowdown(
                articles=articles,
                newsletter_name=newsletter["name"],
                display_area=newsletter["display_area"],
                skill_prompt=skill_prompt,
                pub_date=pub_date,
            )

            stories_count = len(result.get("stories", []))
            if stories_count >= MIN_STORIES:
                break

            if claude_attempt >= MAX_RETRIES or not retry_terms:
                print(f"  Warning: only {stories_count} stories after all retries, saving anyway")
                break

            print(f"\n  Only {stories_count} stories selected (need {MIN_STORIES}), fetching more articles...")
            extra_terms = retry_terms[:RETRY_TERMS_PER_ROUND]
            retry_terms = retry_terms[RETRY_TERMS_PER_ROUND:]
            print(f"  Broadening search with: {extra_terms}")
            extra_articles = fetch_news_brave(extra_terms)
            existing_urls = {a["url"] for a in articles}
            existing_titles = {a["title"].lower().strip() for a in articles}
            added = 0
            for a in extra_articles:
                if a["url"] not in existing_urls and a["title"].lower().strip() not in existing_titles:
                    a["coverage_area"] = newsletter["name"]
                    articles.append(a)
                    existing_urls.add(a["url"])
                    existing_titles.add(a["title"].lower().strip())
                    added += 1
            print(f"  Added {added} new articles, total now {len(articles)}")

        # Save
        save_results(result, newsletter["name"])
        print(f"\n  Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
