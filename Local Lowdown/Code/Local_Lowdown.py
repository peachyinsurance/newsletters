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
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
APIFY_API_KEY   = os.environ["APIFY_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-local-lowdown-skill_auto.md"

APIFY_NEWS_ACTOR = "automation-lab~google-news-scraper"
APIFY_TIMEOUT    = 120
MAX_ARTICLES     = 15  # per query — Claude only picks 3-5, no need for more

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "search_terms": ["East Cobb GA news"],
        "display_area": "East Cobb",
    },
    {
        "name":         "Perimeter_Post",
        "search_terms": ["Dunwoody Sandy Springs news"],
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
# 3. SCRAPE GOOGLE NEWS VIA APIFY
# ---------------------------------------------------------------------------
def resolve_google_news_url(url: str, title: str = "", source: str = "") -> str:
    """Resolve Google News redirect URLs to actual article URLs.
    Falls back to a Google search link for the article title + source."""
    if "news.google.com" not in url:
        return url

    # Try HTTP redirect first (works sometimes)
    try:
        r = requests.get(url, allow_redirects=True, timeout=10, stream=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.close()
        final = r.url
        if "news.google.com" not in final and "google.com/rss" not in final:
            return final
    except Exception:
        pass

    # Fallback: build a Google search URL for the article
    # This gives readers a direct path to find the original article
    if title:
        from urllib.parse import quote
        search_query = f"{title} {source}".strip()
        return f"https://www.google.com/search?q={quote(search_query)}"

    return url


def fetch_news_apify(search_terms: list[str]) -> list[dict]:
    """Fetch recent news articles from Google News via Apify."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APIFY_API_KEY}",
    }

    all_articles = []
    seen_urls = set()

    # Run one Apify call with all queries
    print(f"  Searching Google News for: {search_terms}")
    try:
        res = requests.post(
            f"https://api.apify.com/v2/acts/{APIFY_NEWS_ACTOR}/run-sync-get-dataset-items",
            headers=headers,
            json={
                "queries": search_terms,
                "language": "en",
                "country": "US",
                "maxArticles": MAX_ARTICLES,
            },
            timeout=APIFY_TIMEOUT,
        )
        if res.status_code not in (200, 201):
            print(f"  Apify error {res.status_code}: {res.text[:300]}")
            return []

        items = res.json()
        print(f"  Apify returned {len(items)} articles")

        # Log first item's keys to debug available fields
        if items:
            print(f"  First article keys: {list(items[0].keys())}")

        for item in items:
            title = item.get("title") or item.get("headline") or ""
            source = item.get("source") or item.get("publisher") or item.get("sourceName") or ""

            # Try multiple field names for the real article URL
            url = (item.get("sourceUrl") or item.get("articleUrl") or
                   item.get("link") or item.get("url") or "")
            if not url or url in seen_urls:
                continue

            # Resolve Google News redirect URLs to actual article URLs
            url = resolve_google_news_url(url, title=title, source=source)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            all_articles.append({
                "title":   title,
                "url":     url,
                "source":  source,
                "date":    item.get("publishedAt") or item.get("date") or item.get("published") or item.get("publishDate") or "",
                "summary": item.get("description") or item.get("snippet") or item.get("text") or "",
            })

    except requests.exceptions.ReadTimeout:
        print(f"  Apify timeout after {APIFY_TIMEOUT}s")
    except Exception as e:
        print(f"  Apify error: {e}")

    # Deduplicate by title similarity (exact match)
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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    result = json.loads(clean)

    stories = result.get("stories", [])
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

        # Scrape Google News
        articles = fetch_news_apify(newsletter["search_terms"])

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
