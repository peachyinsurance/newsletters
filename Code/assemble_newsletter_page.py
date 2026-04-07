#!/usr/bin/env python3
"""
Assemble Newsletter Landing Pages in Notion.

Creates/updates one Notion page per newsletter with all sections laid out
in publication order. Automated sections (pets, restaurants) pull approved
content from their databases. Other sections show placeholders.

Env vars:
  NOTION_API_KEY
  NOTION_PETS_DB_ID
  NOTION_RESTAURANTS_DB_ID
  NOTION_PARENT_PAGE_ID   – parent page where landing pages are created
"""
import os
import sys
import json
import requests
from datetime import datetime

sys.path.append(os.path.dirname(__file__))

NOTION_API_KEY           = os.environ["NOTION_API_KEY"]
NOTION_PETS_DB_ID        = os.environ["NOTION_PETS_DB_ID"]
NOTION_RESTAURANTS_DB_ID = os.environ["NOTION_RESTAURANTS_DB_ID"]
NOTION_LOWDOWN_DB_ID     = os.environ.get("NOTION_LOWDOWN_DB_ID", "")
NOTION_PARENT_PAGE_ID    = os.environ["NOTION_PARENT_PAGE_ID"]

HEADERS = {
    "Authorization":  f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type":   "application/json",
}

NEWSLETTERS = ["East_Cobb_Connect", "Perimeter_Post"]

# ---------------------------------------------------------------------------
# NOTION API HELPERS
# ---------------------------------------------------------------------------

def notion_search_page(title: str) -> str | None:
    """Search for an existing page by title. Returns page_id or None."""
    r = requests.post(
        "https://api.notion.com/v1/search",
        headers=HEADERS,
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


def notion_create_page(title: str, parent_id: str) -> str:
    """Create a new page under a parent page. Returns page_id."""
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=HEADERS,
        json={
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {"title": [{"text": {"content": title}}]}
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def notion_clear_page(page_id: str) -> None:
    """Delete all blocks from a page (to overwrite content)."""
    r = requests.get(
        f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    for block in r.json().get("results", []):
        requests.delete(
            f"https://api.notion.com/v1/blocks/{block['id']}",
            headers=HEADERS,
            timeout=30,
        )


def notion_get_blocks(page_id: str) -> list[dict]:
    """Get all child blocks of a page."""
    blocks = []
    has_more = True
    cursor = None
    while has_more:
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        blocks += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return blocks


def find_section_blocks(blocks: list[dict], heading_text: str) -> tuple[list[str], str | None]:
    """Find block IDs between a heading and the next heading/divider.
    Returns (block_ids_to_delete, heading_block_id)."""
    found_heading = False
    heading_id = None
    section_block_ids = []

    for block in blocks:
        block_type = block.get("type", "")

        # Check if this is the target heading
        if not found_heading:
            if block_type.startswith("heading_"):
                rich_text = block.get(block_type, {}).get("rich_text", [])
                text = "".join(t.get("text", {}).get("content", "") for t in rich_text)
                if heading_text.lower() in text.lower():
                    found_heading = True
                    heading_id = block["id"]
                    continue
        else:
            # Stop at the next heading or divider
            if block_type.startswith("heading_") or block_type == "divider":
                break
            section_block_ids.append(block["id"])

    return section_block_ids, heading_id


def update_section(page_id: str, heading_text: str, new_blocks: list[dict]) -> bool:
    """Find a section by heading, clear its content, and insert new blocks."""
    blocks = notion_get_blocks(page_id)
    section_ids, heading_id = find_section_blocks(blocks, heading_text)

    if not heading_id:
        print(f"  Could not find heading containing '{heading_text}'")
        return False

    # Delete existing content in the section
    for block_id in section_ids:
        requests.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS, timeout=30)

    # Insert new content after the heading
    if new_blocks:
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{heading_id}/children" if False else
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=HEADERS,
            json={"children": new_blocks, "after": heading_id},
            timeout=30,
        )
        if not r.ok:
            # Try appending after heading using the after parameter
            r = requests.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=HEADERS,
                json={"children": new_blocks, "after": heading_id},
                timeout=30,
            )
        if not r.ok:
            print(f"  Failed to insert blocks: {r.text[:300]}")
            return False

    print(f"  ✓ Updated '{heading_text}' section ({len(new_blocks)} blocks)")
    return True


def notion_append_blocks(page_id: str, blocks: list[dict]) -> None:
    """Append blocks to a page. Notion limits to 100 blocks per call."""
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=HEADERS,
            json={"children": chunk},
            timeout=30,
        )
        if not r.ok:
            print(f"  Block append error: {r.text[:300]}")
        r.raise_for_status()


# ---------------------------------------------------------------------------
# BLOCK BUILDERS
# ---------------------------------------------------------------------------

def heading_block(text: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {
        "object": "block",
        "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def paragraph_block(text: str, bold: bool = False) -> dict:
    annotations = {"bold": bold} if bold else {}
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}, "annotations": annotations}]
        },
    }


def divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def callout_block(text: str, emoji: str = "📝") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


# ---------------------------------------------------------------------------
# DATA FETCHERS
# ---------------------------------------------------------------------------

def query_database(db_id: str, filters: dict = None) -> list:
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"filter": filters} if filters else {}
    results = []
    has_more = True
    cursor = None
    while has_more:
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        results += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return results


def get_approved_pet(newsletter_name: str) -> dict | None:
    """Get the approved pet for a newsletter."""
    try:
        pages = query_database(NOTION_PETS_DB_ID, filters={
            "and": [
                {"property": "Status", "status": {"equals": "approved"}},
                {"property": "Newsletter", "select": {"equals": newsletter_name}},
            ]
        })
        print(f"  Pet query returned {len(pages)} results for {newsletter_name}")
    except Exception as e:
        print(f"  Pet query FAILED: {e}")
        return None
    if not pages:
        print(f"  No approved pet found for {newsletter_name}")
        return None
    props = pages[0]["properties"]
    return {
        "name":     props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", ""),
        "blurb":    props.get("Blurb", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "") if props.get("Blurb", {}).get("rich_text") else "",
        "shelter":  props.get("Shelter", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "") if props.get("Shelter", {}).get("rich_text") else "",
        "photo":    props.get("Photo URL", {}).get("url", ""),
        "url":      props.get("Source URL", {}).get("url", "") or props.get("Listing URL", {}).get("url", ""),
    }


def get_restaurants(newsletter_name: str) -> list[dict]:
    """Get Tier 1 and Tier 2 restaurants for a newsletter."""
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
            "and": [
                {"property": "Newsletter", "select": {"equals": newsletter_name}},
                {"property": "Status", "status": {"does_not_equal": "pending"}},
            ]
        })
    except Exception:
        return []
    results = []
    for page in pages:
        props = page["properties"]
        status = props.get("Status", {}).get("select", {}).get("name", "")
        results.append({
            "name":   props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", ""),
            "blurb":  props.get("Blurb", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "") if props.get("Blurb", {}).get("rich_text") else "",
            "tier":   status,
            "score":  props.get("Total Score", {}).get("number", 0),
            "photo":  props.get("Photo URL", {}).get("url", ""),
        })
    # Sort: Tier 1 first, then by score
    results.sort(key=lambda x: (0 if x["tier"] == "Tier 1 Winner" else 1, -(x["score"] or 0)))
    return results


def get_latest_lowdown(newsletter_name: str) -> str | None:
    """Get the most recent Local Lowdown section text from the database."""
    if not NOTION_LOWDOWN_DB_ID:
        return None
    try:
        pages = query_database(NOTION_LOWDOWN_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return None
    if not pages:
        return None
    # Sort by date descending to get the latest
    pages.sort(
        key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
        reverse=True
    )
    props = pages[0]["properties"]
    section_text = props.get("Full Section", {}).get("rich_text", [])
    if section_text:
        return section_text[0].get("text", {}).get("content", "")
    return None


# ---------------------------------------------------------------------------
# PAGE ASSEMBLER
# ---------------------------------------------------------------------------

def build_newsletter_blocks(newsletter_name: str) -> list[dict]:
    """Build all Notion blocks for a newsletter landing page."""
    display_name = newsletter_name.replace("_", " ")
    today = datetime.today().strftime("%B %d, %Y")
    blocks = []

    # Header
    blocks.append(callout_block(
        f"Last updated: {today}\nCopy each section below into the newsletter template.",
        emoji="📋"
    ))
    blocks.append(divider_block())

    # 1. Subject Line & Preview Text
    blocks.append(heading_block("📧 Subject Line & Preview Text"))
    blocks.append(callout_block("Not yet automated. Write or paste your subject line and preview text here.", emoji="✏️"))
    blocks.append(divider_block())

    # 2. Editor's Blurb
    blocks.append(heading_block("📝 Editor's Blurb"))
    blocks.append(callout_block("Not yet automated. Write or paste the opening editor's note here.", emoji="✏️"))
    blocks.append(divider_block())

    # 3. In Today's Connect
    blocks.append(heading_block("📑 In Today's Connect"))
    blocks.append(callout_block("Not yet automated. Write or paste the teaser list here.", emoji="✏️"))
    blocks.append(divider_block())

    # 4. Featured Event
    blocks.append(heading_block("🎪 Featured Event"))
    blocks.append(callout_block("Not yet automated. Write or paste the featured event here.", emoji="✏️"))
    blocks.append(divider_block())

    # 5. Local Lowdown (automated)
    blocks.append(heading_block("🗞️ Local Lowdown"))
    lowdown_text = get_latest_lowdown(newsletter_name)
    if lowdown_text:
        # Split into paragraphs and add as blocks
        for para in lowdown_text.split("\n"):
            para = para.strip()
            if not para:
                continue
            if para.startswith("### "):
                blocks.append(paragraph_block(para.replace("### ", ""), bold=True))
            elif para.startswith("More: "):
                blocks.append(paragraph_block(para))
            else:
                blocks.append(paragraph_block(para))
    else:
        blocks.append(callout_block("No Local Lowdown generated yet. Run the Local Lowdown pipeline.", emoji="⏳"))
    blocks.append(divider_block())

    # 6. Pet Adoption (automated)
    blocks.append(heading_block("🐾 Pet Adoption"))
    pet = get_approved_pet(newsletter_name)
    if pet and pet.get("blurb"):
        blocks.append(paragraph_block(pet["name"], bold=True))
        blocks.append(paragraph_block(pet["blurb"]))
        if pet.get("shelter"):
            blocks.append(paragraph_block(f"Shelter: {pet['shelter']}"))
        if pet.get("url"):
            blocks.append(paragraph_block(f"Link: {pet['url']}"))
        if pet.get("photo"):
            blocks.append(paragraph_block(f"Photo: {pet['photo']}"))
    else:
        blocks.append(callout_block("No approved pet yet. Run the pipeline and approve a pet in the review app.", emoji="⏳"))
    blocks.append(divider_block())

    # 7. Restaurant (automated)
    blocks.append(heading_block("🍽️ Restaurant"))
    restaurants = get_restaurants(newsletter_name)
    if restaurants:
        for r in restaurants:
            tier_label = "⭐ TIER 1 — FEATURED" if r["tier"] == "Tier 1 Winner" else "TIER 2"
            blocks.append(paragraph_block(f"[{tier_label}] {r['name']} (Score: {r['score']}/40)", bold=True))
            if r.get("blurb"):
                blocks.append(paragraph_block(r["blurb"]))
            if r.get("photo"):
                blocks.append(paragraph_block(f"Photo: {r['photo']}"))
            blocks.append(paragraph_block(""))  # spacer
    else:
        blocks.append(callout_block("No restaurants selected yet. Run the pipeline and approve in the review app.", emoji="⏳"))
    blocks.append(divider_block())

    # 8. Reader Poll
    blocks.append(heading_block("📊 Reader Poll"))
    blocks.append(callout_block("Not yet automated. Write or paste the reader poll here.", emoji="✏️"))

    return blocks


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Assembling newsletter landing pages — {datetime.today().strftime('%Y-%m-%d')}")

    for newsletter_name in NEWSLETTERS:
        display_name = newsletter_name.replace("_", " ")
        page_title = f"{display_name} — Current Edition"

        print(f"\n{'='*60}")
        print(f"  {page_title}")
        print(f"{'='*60}")

        # Find or create the page
        page_id = notion_search_page(page_title)
        if page_id:
            print(f"  Found existing page: {page_id}")
            print(f"  Clearing old content...")
            notion_clear_page(page_id)
        else:
            print(f"  Creating new page...")
            page_id = notion_create_page(page_title, NOTION_PARENT_PAGE_ID)
            print(f"  Created page: {page_id}")

        # Build and write content
        blocks = build_newsletter_blocks(newsletter_name)
        print(f"  Writing {len(blocks)} blocks...")
        notion_append_blocks(page_id, blocks)
        print(f"  ✓ Done")

    print(f"\nAll landing pages updated.")
