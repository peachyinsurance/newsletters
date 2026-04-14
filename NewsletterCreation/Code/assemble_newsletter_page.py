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
NOTION_RE_DB_ID          = os.environ.get("NOTION_RE_DB_ID", "")
NOTION_EVENTS_DB_ID      = os.environ.get("NOTION_EVENTS_DB_ID", "")
NOTION_INTRO_DB_ID       = os.environ.get("NOTION_INTRO_DB_ID", "")
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
    from concurrent.futures import ThreadPoolExecutor

    def delete_block(block_id):
        requests.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS, timeout=30)

    while True:
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        blocks = r.json().get("results", [])
        if not blocks:
            break
        block_ids = [b["id"] for b in blocks]
        with ThreadPoolExecutor(max_workers=10) as pool:
            pool.map(delete_block, block_ids)
        print(f"    Cleared {len(block_ids)} blocks")


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


def link_block(label: str, url: str) -> dict:
    """A paragraph with clickable hyperlinked text."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": label, "link": {"url": url}},
                "annotations": {"color": "blue"},
            }]
        },
    }


def image_block(url: str) -> dict:
    """An embedded image block with no caption."""
    return {
        "object": "block",
        "type": "image",
        "image": {
            "type": "external",
            "external": {"url": url},
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
        # Try "status" type filter first, fall back to "select" type
        pages = None
        for filter_type in ["status", "select"]:
            try:
                pages = query_database(NOTION_PETS_DB_ID, filters={
                    "property": "Status",
                    filter_type: {"equals": "approved"}
                })
                print(f"  Pet query worked with '{filter_type}' filter, returned {len(pages)} results")
                break
            except Exception:
                print(f"  Pet '{filter_type}' filter failed, trying next...")
                continue
        if pages is None:
            # Last resort: no filter, check status in Python
            print(f"  Trying unfiltered query...")
            pages = query_database(NOTION_PETS_DB_ID)
            print(f"  Unfiltered query returned {len(pages)} total pets")

        # Filter to approved pets for this newsletter in Python
        filtered = []
        for p in pages:
            props = p["properties"]
            status_prop = props.get("Status", {})
            status_name = (status_prop.get("select") or status_prop.get("status") or {}).get("name", "")
            nl_prop = props.get("Newsletter", {})
            nl_name = (nl_prop.get("select") or {}).get("name", "")
            if status_name == "approved" and nl_name == newsletter_name:
                filtered.append(p)
        pages = filtered
        print(f"  {len(pages)} approved pets for {newsletter_name}")
    except Exception as e:
        print(f"  Pet query FAILED: {e}")
        return None
    if not pages:
        print(f"  No approved pet found for {newsletter_name}")
        return None
    props = pages[0]["properties"]
    def _rt(key):
        rt = props.get(key, {}).get("rich_text", [])
        return rt[0].get("text", {}).get("content", "") if rt else ""
    return {
        "name":            props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", ""),
        "blurb":           _rt("Blurb"),
        "shelter":         _rt("Shelter"),
        "shelter_address": _rt("Shelter Address"),
        "shelter_phone":   _rt("Shelter Phone"),
        "shelter_email":   _rt("Shelter Email"),
        "shelter_hours":   _rt("Shelter Hours"),
        "photo":           props.get("Photo URL", {}).get("url", ""),
        "gif":             props.get("GIF URL", {}).get("url", ""),
        "url":             props.get("Source URL", {}).get("url", "") or props.get("Listing URL", {}).get("url", ""),
    }


def get_restaurants(newsletter_name: str) -> list[dict]:
    """Get Tier 1 and Tier 2 restaurants for a newsletter."""
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID)
        # Filter in Python to avoid compound filter issues
        pages = [p for p in pages if
                 (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
    except Exception:
        return []
    results = []
    for page in pages:
        props = page["properties"]
        status_prop = props.get("Status", {})
        status = (status_prop.get("select") or status_prop.get("status") or {}).get("name", "")
        if status == "pending" or not status:
            continue
        def _rt(key):
            rt = props.get(key, {}).get("rich_text", [])
            return rt[0].get("text", {}).get("content", "") if rt else ""
        results.append({
            "name":     props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", ""),
            "blurb":    _rt("Blurb"),
            "address":  _rt("Address"),
            "phone":    _rt("Phone"),
            "hours":    _rt("Hours"),
            "cuisine":  (props.get("Cuisine", {}).get("select") or {}).get("name", ""),
            "tier":     status,
            "score":    props.get("Total Score", {}).get("number", 0),
            "rating":   props.get("Rating", {}).get("number", 0),
            "photo":    props.get("Photo URL", {}).get("url", ""),
            "gif":      props.get("GIF URL", {}).get("url", ""),
            "website":  props.get("Website", {}).get("url", ""),
            "maps_url": props.get("Google Maps URL", {}).get("url", ""),
            "date":     (props.get("Date Generated", {}).get("date") or {}).get("start", ""),
        })

    # Only keep the most recent batch (by date) to prevent duplicates
    if results:
        dates = [r["date"] for r in results if r.get("date")]
        if dates:
            latest_date = max(dates)
            results = [r for r in results if r.get("date") == latest_date]

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
        return "".join(chunk.get("text", {}).get("content", "") for chunk in section_text)
    return None


def get_latest_intro(newsletter_name: str) -> dict | None:
    """Get the most recent Welcome Intro blurb from the database."""
    if not NOTION_INTRO_DB_ID:
        return None
    try:
        pages = query_database(NOTION_INTRO_DB_ID, filters={
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
    greeting_rt = props.get("Greeting", {}).get("rich_text", [])
    blurb_rt = props.get("Blurb", {}).get("rich_text", [])
    greeting = "".join(chunk.get("text", {}).get("content", "") for chunk in greeting_rt) if greeting_rt else ""
    blurb = "".join(chunk.get("text", {}).get("content", "") for chunk in blurb_rt) if blurb_rt else ""
    if blurb:
        return {"greeting": greeting, "blurb": blurb}
    return None


def get_real_estate(newsletter_name: str) -> list[dict]:
    """Get real estate listings from the database for a newsletter."""
    if not NOTION_RE_DB_ID:
        return []
    try:
        pages = query_database(NOTION_RE_DB_ID)
        pages = [p for p in pages if
                 (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
    except Exception:
        return []
    results = []
    for page in pages:
        props = page["properties"]
        def _rt(key):
            rt = props.get(key, {}).get("rich_text", [])
            return rt[0].get("text", {}).get("content", "") if rt else ""
        results.append({
            "tier":     (props.get("Tier", {}).get("select") or {}).get("name", ""),
            "headline": _rt("Headline"),
            "blurb":    _rt("Blurb"),
            "price":    props.get("Price", {}).get("number", 0),
            "address":  _rt("Address"),
            "beds":     props.get("Beds", {}).get("number", 0),
            "baths":    props.get("Baths", {}).get("number", 0),
            "sqft":     props.get("Sqft", {}).get("number", 0),
            "photo":    props.get("Photo URL", {}).get("url", ""),
            "gif":      props.get("GIF URL", {}).get("url", ""),
            "template": props.get("Template Image", {}).get("url", ""),
            "url":      props.get("Listing URL", {}).get("url", ""),
            "date":     (props.get("Date Generated", {}).get("date") or {}).get("start", ""),
        })
    # Only keep the most recent batch (by date)
    if results:
        dates = set()
        for r in results:
            d = r.get("date", "")
            if d:
                dates.add(d)
        if dates:
            latest_date = max(dates)
            results = [r for r in results if r.get("date", "") == latest_date]

    # Sort: Starter, Sweet Spot, Showcase
    tier_order = {"Starter": 0, "Sweet Spot": 1, "Showcase": 2}
    results.sort(key=lambda x: tier_order.get(x["tier"], 9))
    return results


def get_featured_event(newsletter_name: str) -> dict | None:
    """Get the approved featured event for a newsletter."""
    if not NOTION_EVENTS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_EVENTS_DB_ID)
        # Filter to approved events for this newsletter
        filtered = []
        for p in pages:
            props = p["properties"]
            status_prop = props.get("Status", {})
            status_name = (status_prop.get("select") or status_prop.get("status") or {}).get("name", "")
            nl_prop = props.get("Newsletter", {})
            nl_name = (nl_prop.get("select") or {}).get("name", "")
            if status_name == "approved" and nl_name == newsletter_name:
                filtered.append(p)
        if not filtered:
            print(f"  No approved featured event for {newsletter_name}")
            return None
        # Sort by date generated descending, pick latest
        filtered.sort(
            key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
            reverse=True
        )
        props = filtered[0]["properties"]
        def _rt(key):
            rt = props.get(key, {}).get("rich_text", [])
            return rt[0].get("text", {}).get("content", "") if rt else ""
        return {
            "event_name":  _rt("Event Name"),
            "date":        _rt("Date"),
            "time":        _rt("Time"),
            "venue":       _rt("Venue"),
            "price":       _rt("Price"),
            "blurb":       _rt("Blurb"),
            "source_url":  props.get("Source URL", {}).get("url", ""),
            "ticket_url":  props.get("Ticket URL", {}).get("url", ""),
            "score":       props.get("Total Score", {}).get("number", 0),
        }
    except Exception as e:
        print(f"  Featured event query failed: {e}")
        return None


# ---------------------------------------------------------------------------
# PAGE ASSEMBLER
# ---------------------------------------------------------------------------

def _placeholder(text: str) -> dict:
    return callout_block(text, emoji="✏️")


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

    # 1. Welcome Intro
    blocks.append(heading_block("👋 Welcome Intro"))
    intro = get_latest_intro(newsletter_name)
    if intro:
        if intro.get("greeting"):
            blocks.append(paragraph_block(intro["greeting"], bold=True))
        blocks.append(paragraph_block(intro["blurb"]))
    else:
        blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 2. Summary
    blocks.append(heading_block("📑 Summary"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 3. Poll
    blocks.append(heading_block("📊 Poll"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 4. Sponsor Corner
    blocks.append(heading_block("💼 Sponsor Corner"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 5. Event of the Week (automated)
    blocks.append(heading_block("🎪 Event of the Week"))
    event = get_featured_event(newsletter_name)
    if event and event.get("blurb"):
        # Header line: event name
        header = f"⭐ Featured Event: {event['event_name']}"
        blocks.append(paragraph_block(header, bold=True))
        # Details line: date | time | venue
        detail_parts = []
        if event.get("date"):
            detail_parts.append(event["date"])
        if event.get("time"):
            detail_parts.append(event["time"])
        if event.get("venue"):
            detail_parts.append(event["venue"])
        if detail_parts:
            blocks.append(paragraph_block("📅 " + " | ".join(detail_parts)))
        # Price + ticket link
        price_line = ""
        if event.get("price"):
            price_line = f"🎟️ {event['price']}"
        if price_line:
            blocks.append(paragraph_block(price_line))
        # Blurb body
        blocks.append(paragraph_block(event["blurb"]))
        # Links
        if event.get("ticket_url"):
            blocks.append(link_block("Get Tickets", event["ticket_url"]))
        elif event.get("source_url"):
            blocks.append(link_block("Learn More", event["source_url"]))
    else:
        blocks.append(callout_block("No featured event selected yet. Run the Featured Event pipeline and approve an event.", emoji="⏳"))
    blocks.append(divider_block())

    # 6. Restaurant Radar (automated)
    blocks.append(heading_block("🍽️ Restaurant Radar"))
    restaurants = get_restaurants(newsletter_name)
    if restaurants:
        for r in restaurants:
            tier_label = "⭐ TIER 1 — FEATURED" if r["tier"] == "Tier 1 Winner" else "TIER 2"
            blocks.append(paragraph_block(f"[{tier_label}] {r['name']} (Score: {r['score']}/40)", bold=True))
            if r.get("gif"):
                blocks.append(image_block(r["gif"]))
            elif r.get("photo"):
                blocks.append(image_block(r["photo"]))
            if r.get("blurb"):
                blocks.append(paragraph_block(r["blurb"]))
            # Details line
            details_parts = []
            if r.get("cuisine"):
                details_parts.append(r["cuisine"])
            if r.get("rating"):
                details_parts.append(f"{r['rating']}★")
            if details_parts:
                blocks.append(paragraph_block(" | ".join(details_parts)))
            if r.get("address"):
                blocks.append(paragraph_block(r["address"]))
            if r.get("phone"):
                blocks.append(paragraph_block(r["phone"]))
            if r.get("hours"):
                blocks.append(paragraph_block(r["hours"]))
            if r.get("website"):
                blocks.append(link_block("Website", r["website"]))
            if r.get("maps_url"):
                blocks.append(link_block("Google Maps", r["maps_url"]))
            if r.get("photo"):
                blocks.append(link_block("Download Photo", r["photo"]))
            blocks.append(paragraph_block(""))
    else:
        blocks.append(callout_block("No restaurants selected yet. Run the pipeline and approve in the review app.", emoji="⏳"))
    blocks.append(divider_block())

    # 7. Business Brief
    blocks.append(heading_block("🏢 Business Brief"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 8. Real Estate Corner (automated)
    blocks.append(heading_block("🏠 Real Estate Corner"))
    re_listings = get_real_estate(newsletter_name)
    if re_listings:
        for listing in re_listings:
            tier_emoji = {"Starter": "🏠", "Sweet Spot": "🏡", "Showcase": "🏰"}.get(listing["tier"], "🏠")
            price_str = f"${listing['price']:,}" if listing.get("price") else ""
            blocks.append(paragraph_block(f"{tier_emoji} {listing['tier']}: {listing.get('headline', '')}", bold=True))
            # Show template image (has border + details baked in)
            if listing.get("template"):
                blocks.append(image_block(listing["template"]))
            elif listing.get("photo"):
                blocks.append(image_block(listing["photo"]))
            if listing.get("blurb"):
                blocks.append(paragraph_block(listing["blurb"]))
            if listing.get("url"):
                blocks.append(link_block("View Listing", listing['url']))
            if listing.get("template"):
                blocks.append(link_block("Download Image", listing['template']))
            blocks.append(paragraph_block(""))
    else:
        blocks.append(callout_block("No real estate listings yet. Run the Real Estate Corner pipeline.", emoji="⏳"))
    blocks.append(divider_block())

    # 9. Local Lowdown (automated)
    blocks.append(heading_block("🗞️ Local Lowdown"))
    lowdown_text = get_latest_lowdown(newsletter_name)
    if lowdown_text:
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

    # 10. Furry Friends (automated)
    blocks.append(heading_block("🐾 Furry Friends"))
    pet = get_approved_pet(newsletter_name)
    if pet and pet.get("blurb"):
        blocks.append(paragraph_block(pet["name"], bold=True))
        # Show GIF if available, otherwise static photo
        if pet.get("gif"):
            blocks.append(image_block(pet["gif"]))
        elif pet.get("photo"):
            blocks.append(image_block(pet["photo"]))
        blocks.append(paragraph_block(pet["blurb"]))
        # Only add shelter info if it's NOT already in the blurb
        blurb_lower = pet["blurb"].lower()
        shelter_name = (pet.get("shelter") or "").lower()
        if shelter_name and shelter_name not in blurb_lower:
            shelter_lines = []
            if pet.get("shelter"):
                shelter_lines.append(pet["shelter"])
            if pet.get("shelter_address"):
                shelter_lines.append(pet["shelter_address"])
            if pet.get("shelter_phone") or pet.get("shelter_email"):
                contact = " | ".join(filter(None, [pet.get("shelter_phone"), pet.get("shelter_email")]))
                shelter_lines.append(contact)
            if pet.get("shelter_hours"):
                shelter_lines.append(pet["shelter_hours"])
            if shelter_lines:
                blocks.append(paragraph_block("\n".join(shelter_lines)))
        if pet.get("url"):
            blocks.append(link_block("View Pet Listing", pet['url']))
        if pet.get("gif"):
            blocks.append(link_block("Download GIF", pet['gif']))
        elif pet.get("photo"):
            blocks.append(link_block("Download Photo", pet['photo']))
    else:
        blocks.append(callout_block("No approved pet yet. Run the pipeline and approve a pet in the review app.", emoji="⏳"))
    blocks.append(divider_block())

    # 11. Local Events
    blocks.append(heading_block("📅 Local Events"))
    blocks.append(paragraph_block("Family Fun", bold=True))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(paragraph_block("Adults Only", bold=True))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 12. Free Activity Ideas
    blocks.append(heading_block("🆓 Free Activity Ideas"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 13. Insurance Tip
    blocks.append(heading_block("🛡️ Insurance Tip"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 14. In Search Of
    blocks.append(heading_block("🔍 In Search Of"))
    blocks.append(_placeholder("Not yet automated."))
    blocks.append(divider_block())

    # 15. Meme Corner
    blocks.append(heading_block("😂 Meme Corner"))
    blocks.append(_placeholder("Not yet automated."))

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
