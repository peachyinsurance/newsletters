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

NOTION_API_KEY = os.environ["NOTION_API_KEY"]

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY      = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY  = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-local-lowdown-skill_auto.md"

MAX_ARTICLES = 15
MIN_ARTICLES = 5   # minimum eligible articles before sending to Claude
MAX_RETRIES  = 2   # how many fetch rounds to attempt

# Topics to exclude — keep the newsletter PG and community-focused
EXCLUDED_KEYWORDS = {
    "murder", "homicide", "killed", "stabbed", "shooting", "shot dead",
    "manslaughter", "assault", "rape", "sexual assault", "domestic violence",
    "arson", "robbery", "carjacking", "kidnapping", "abduction",
    "skeletal remains", "body found", "death investigation",
    "drug bust", "drug trafficking", "overdose",
    "trump", "biden", "desantis", "GOP", "democrat", "republican",
    "partisan", "impeach", "indictment", "arraign",
}

# Domains with metered/soft paywalls that slip past automated detection
BLOCKED_DOMAINS = {
    "mdjonline.com",
    "ajc.com",
}

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "search_terms": ["East Cobb GA news"],
        "retry_terms":  ["Marietta GA news", "Cobb County GA news"],
        "display_area": "East Cobb",
    },
    {
        "name":         "Perimeter_Post",
        "search_terms": ["Dunwoody Sandy Springs news"],
        "retry_terms":  ["Sandy Springs GA news", "Dunwoody GA news", "Perimeter Atlanta news"],
        "display_area": "Perimeter",
    },
]


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
    """Fetch recent news articles via Brave Search News API. Returns real source URLs."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_NEWS_API_KEY,
    }

    all_articles = []
    seen_urls = set()

    for query in search_terms:
        print(f"  Searching Brave News for: {query}")
        try:
            res = requests.get(
                "https://api.search.brave.com/res/v1/news/search",
                headers=headers,
                params={
                    "q": query,
                    "count": MAX_ARTICLES,
                    "freshness": "pw",  # past week
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
                    print(f"    ✗ Skipping blocked domain: {hostname or url[:50]}")
                    continue

                # Check for paywall
                if is_paywalled(url):
                    source = hostname or url[:50]
                    print(f"    ✗ Skipping paywalled: {source}")
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

    articles_json = json.dumps(articles, indent=2)

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""
Here are recent local news articles scraped from Google News for the {display_area} area.

Newsletter: {newsletter_name}
Publication date: {pub_date}
Coverage area: {display_area}

Select the best 3-5 stories and write the Local Lowdown section.
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

    # Post-filter: remove any paywalled URLs Claude included in source_urls
    for story in stories:
        source_urls = story.get("source_urls", [])
        clean_urls = []
        for src in source_urls:
            url = src.get("url", "")
            if url and not is_paywalled(url):
                clean_urls.append(src)
            else:
                print(f"    ✗ Removed paywalled source from output: {src.get('label', '')} ({url[:50]})")
        story["source_urls"] = clean_urls

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
    pub_date = datetime.today().strftime("%Y-%m-%d")

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Search for local news via Brave — retry with broader terms if not enough
        articles = fetch_news_brave(newsletter["search_terms"])

        if len(articles) < MIN_ARTICLES:
            retry_terms = newsletter.get("retry_terms", [])
            for attempt in range(1, MAX_RETRIES + 1):
                if len(articles) >= MIN_ARTICLES or not retry_terms:
                    break
                print(f"\n  Retry {attempt}/{MAX_RETRIES} — only {len(articles)} eligible articles, need {MIN_ARTICLES}")
                # Use next batch of retry terms
                extra_terms = retry_terms[:2]
                retry_terms = retry_terms[2:]
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

        # Claude selects and writes
        print(f"\n  Sending {len(articles)} articles to Claude...")
        result = write_local_lowdown(
            articles=articles,
            newsletter_name=newsletter["name"],
            display_area=newsletter["display_area"],
            skill_prompt=skill_prompt,
            pub_date=pub_date,
        )

        # Save
        save_results(result, newsletter["name"])
        print(f"\n  Done with {newsletter['name']}.")

    print(f"\nAll newsletters complete.")
