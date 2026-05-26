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
import re
import sys
import json
import requests
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(__file__))
from newsletters_config import newsletter_names

NOTION_API_KEY           = os.environ["NOTION_API_KEY"]
NOTION_PETS_DB_ID        = os.environ["NOTION_PETS_DB_ID"]
NOTION_RESTAURANTS_DB_ID = os.environ["NOTION_RESTAURANTS_DB_ID"]
NOTION_LOWDOWN_DB_ID     = os.environ.get("NOTION_LOWDOWN_DB_ID", "")
NOTION_RE_DB_ID          = os.environ.get("NOTION_RE_DB_ID", "")
NOTION_EVENTS_DB_ID      = os.environ.get("NOTION_EVENTS_DB_ID", "")
NOTION_INTRO_DB_ID       = os.environ.get("NOTION_INTRO_DB_ID", "")
NOTION_FREE_EVENTS_DB_ID = os.environ.get("NOTION_FREE_EVENTS_DB_ID", "")
NOTION_POLLS_DB_ID       = os.environ.get("NOTION_POLLS_DB_ID", "")
NOTION_WEEKEND_PLANNER_DB_ID = os.environ.get("NOTION_WEEKEND_PLANNER_DB_ID", "")
NOTION_BUSINESS_BRIEF_DB_ID = os.environ.get("NOTION_BUSINESS_BRIEF_DB_ID", "")
NOTION_TIPS_DB_ID        = os.environ.get("NOTION_TIPS_DB_ID", "")
NOTION_MEMES_DB_ID       = os.environ.get("NOTION_MEMES_DB_ID", "")
NOTION_IN_SEARCH_OF_DB_ID = os.environ.get("NOTION_IN_SEARCH_OF_DB_ID", "")
NOTION_SPONSOR_DB_ID     = os.environ.get("NOTION_SPONSOR_DB_ID", "")
NOTION_PARENT_PAGE_ID    = os.environ["NOTION_PARENT_PAGE_ID"]

HEADERS = {
    "Authorization":  f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    # charset=utf-8 to prevent emoji double-encoding on round-trip
    # (see same note in notion_helper.HEADERS)
    "Content-Type":   "application/json; charset=utf-8",
}

NEWSLETTERS = newsletter_names()

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
    """Delete all blocks from a page (to overwrite content).

    We used to fire 10 parallel deletes per batch with no rate limiting
    and no failure handling. That meant: (a) silent partial-clear bugs
    when Notion 429-throttled a few of the parallel requests, leaving
    stale blocks behind that the next append-phase would layer on top
    of; and (b) GitHub Actions started flagging full rebuild runs as
    "disruptive" because of the API burst pattern across 3 newsletters.

    Now: 3 workers (still parallel, much less burst-y), explicit 429
    retry with exponential backoff, raise_for_status() on the final
    attempt so unrecoverable deletes surface in the logs instead of
    silently leaving orphaned blocks."""
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    def delete_block(block_id):
        for attempt in range(4):
            r = requests.delete(
                f"https://api.notion.com/v1/blocks/{block_id}",
                headers=HEADERS, timeout=30,
            )
            if r.status_code == 200:
                return
            if r.status_code == 429 and attempt < 3:
                # Notion sends Retry-After (seconds). Honor it; otherwise back off.
                wait = float(r.headers.get("Retry-After", 0)) or (1.5 ** (attempt + 1))
                _time.sleep(wait)
                continue
            # Non-429 error or out of retries — surface it
            r.raise_for_status()

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
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(delete_block, block_ids))   # list() forces exceptions to surface
        print(f"    Cleared {len(block_ids)} blocks")
        # Brief breather between batches so the next paginated GET +
        # delete-wave doesn't pile on top of the previous wave's
        # in-flight requests at the Notion edge.
        _time.sleep(0.5)


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


def update_section(page_id: str, heading_text: str, new_blocks: list[dict], append: bool = False) -> bool:
    """Find a section by heading and write new_blocks into it.

    If append=False (default), the section's existing content is cleared first
    and replaced with new_blocks. If append=True, existing content is preserved
    and new_blocks are inserted at the END of the section (just before the next
    heading/divider). Append is useful for incrementally adding Weekend Planner
    events without wiping previously rendered content."""
    blocks = notion_get_blocks(page_id)
    section_ids, heading_id = find_section_blocks(blocks, heading_text)

    if not heading_id:
        print(f"  Could not find heading containing '{heading_text}'")
        return False

    if append:
        # Insert at the end of the existing section content (or right after the
        # heading if the section is currently empty).
        insert_after_id = section_ids[-1] if section_ids else heading_id
    else:
        # Clear the existing section content, insert directly after the heading.
        for block_id in section_ids:
            requests.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS, timeout=30)
        insert_after_id = heading_id

    if new_blocks:
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=HEADERS,
            json={"children": new_blocks, "after": insert_after_id},
            timeout=30,
        )
        if not r.ok:
            print(f"  Failed to insert blocks: {r.text[:300]}")
            return False

    action = "Appended to" if append else "Updated"
    print(f"  ✓ {action} '{heading_text}' section ({len(new_blocks)} blocks)")
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


def get_bot_user_id() -> str:
    """Get the integration's bot user ID. Cached on the function after first call."""
    if hasattr(get_bot_user_id, "_cached"):
        return get_bot_user_id._cached
    try:
        r = requests.get("https://api.notion.com/v1/users/me", headers=HEADERS, timeout=30)
        r.raise_for_status()
        uid = r.json().get("id", "")
    except Exception as e:
        print(f"  Warning: could not fetch bot user ID: {e}")
        uid = ""
    get_bot_user_id._cached = uid
    return uid


def extract_section_text(blocks: list[dict], heading_text: str) -> tuple[str, bool]:
    """Extract plain text content for a section between heading and next heading/divider.
    Returns (text, was_manually_edited). was_manually_edited is True if any block in the
    section has last_edited_by != bot user."""
    bot_id = get_bot_user_id()
    found = False
    lines = []
    human_edited = False

    for block in blocks:
        btype = block.get("type", "")
        if not found:
            if btype.startswith("heading_"):
                rt = block.get(btype, {}).get("rich_text", [])
                text = "".join(t.get("text", {}).get("content", "") for t in rt)
                if heading_text.lower() in text.lower():
                    found = True
            continue
        # In the section — stop at next heading or divider
        if btype.startswith("heading_") or btype == "divider":
            break
        # Check last_edited_by
        editor = block.get("last_edited_by", {}).get("id", "")
        if bot_id and editor and editor != bot_id:
            human_edited = True
        # Pull text from known block types
        if btype == "paragraph":
            rt = block.get("paragraph", {}).get("rich_text", [])
            line = "".join(t.get("text", {}).get("content", "") for t in rt)
            if line:
                lines.append(line)
        elif btype == "callout":
            rt = block.get("callout", {}).get("rich_text", [])
            line = "".join(t.get("text", {}).get("content", "") for t in rt)
            if line:
                lines.append(line)

    return "\n\n".join(lines), human_edited


def _strip_intro_addons(text: str) -> str:
    """Truncate any preview-line or In-Today's-Connect content that may
    have been swept into the blurb by a prior sync-back. These addons
    are rendered SEPARATELY from their own Intro DB fields
    (Preview Text, In Todays Connect), so if they're also in the blurb
    they render twice.

    Stops at the first paragraph that starts with `Preview:` (the
    italic line) or contains `In Today's Connect` (the teaser header).
    Everything before that point stays as the blurb."""
    if not text:
        return text
    paragraphs = text.split("\n\n")
    cutoff = len(paragraphs)
    for i, p in enumerate(paragraphs):
        s = p.strip()
        s_lower = s.lower()
        starts_with_preview = (
            s_lower.startswith("preview:") or
            s_lower.startswith("*preview:") or
            s_lower.startswith("_preview:")
        )
        if starts_with_preview or "in today's connect" in s_lower:
            cutoff = i
            break
    kept = [p for p in paragraphs[:cutoff] if p.strip()]
    return "\n\n".join(kept).strip()


def sync_edits_back(page_id: str, newsletter_name: str) -> None:
    """Before clearing the landing page, detect manual edits in Welcome Intro and
    Local Lowdown sections and sync them back to the corresponding database rows.
    Sets Manually Edited = True on any row whose content was synced."""
    bot_id = get_bot_user_id()
    if not bot_id:
        print("  Skipping sync-back (no bot user ID)")
        return

    blocks = notion_get_blocks(page_id)

    # --- Welcome Intro ---
    intro_text, intro_edited = extract_section_text(blocks, "Welcome Intro")
    if intro_edited and intro_text and NOTION_INTRO_DB_ID:
        # Greeting is the first paragraph if bold-only, otherwise treat entire text as blurb
        parts = intro_text.split("\n\n", 1)
        if len(parts) == 2 and len(parts[0]) < 100:
            greeting = parts[0]
            blurb = parts[1]
        else:
            greeting = ""
            blurb = intro_text
        # Strip preview-line + teaser content that the page renderer
        # adds underneath the blurb; otherwise the next rebuild renders
        # them twice (once as part of the blurb, once from their own
        # dedicated Preview Text / In Todays Connect fields).
        blurb = _strip_intro_addons(blurb)

        try:
            pages = query_database(NOTION_INTRO_DB_ID, filters={
                "property": "Newsletter",
                "select": {"equals": newsletter_name}
            })
        except Exception:
            pages = []

        if pages:
            pages.sort(
                key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
                reverse=True,
            )
            target_id = pages[0]["id"]

            CHUNK_SIZE = 1900
            chunks = [{"text": {"content": blurb[i:i + CHUNK_SIZE]}}
                      for i in range(0, len(blurb), CHUNK_SIZE)]
            if not chunks:
                chunks = [{"text": {"content": ""}}]

            props_update = {
                "Blurb":            {"rich_text": chunks},
                "Manually Edited":  {"checkbox": True},
            }
            if greeting:
                props_update["Greeting"] = {"rich_text": [{"text": {"content": greeting}}]}

            r = requests.patch(
                f"https://api.notion.com/v1/pages/{target_id}",
                headers=HEADERS,
                json={"properties": props_update},
                timeout=30,
            )
            if r.ok:
                print(f"  🔄 Synced Welcome Intro edits back to database (marked as manually edited)")
            else:
                print(f"  Warning: intro sync-back failed: {r.text[:200]}")

    # --- Local Lowdown ---
    lowdown_text, lowdown_edited = extract_section_text(blocks, "Local Lowdown")
    if lowdown_edited and lowdown_text and NOTION_LOWDOWN_DB_ID:
        try:
            pages = query_database(NOTION_LOWDOWN_DB_ID, filters={
                "property": "Newsletter",
                "select": {"equals": newsletter_name}
            })
        except Exception:
            pages = []

        if pages:
            pages.sort(
                key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
                reverse=True,
            )
            target_id = pages[0]["id"]

            CHUNK_SIZE = 1900
            chunks = [{"text": {"content": lowdown_text[i:i + CHUNK_SIZE]}}
                      for i in range(0, len(lowdown_text), CHUNK_SIZE)]
            if not chunks:
                chunks = [{"text": {"content": ""}}]

            r = requests.patch(
                f"https://api.notion.com/v1/pages/{target_id}",
                headers=HEADERS,
                json={"properties": {
                    "Full Section":    {"rich_text": chunks},
                    "Manually Edited": {"checkbox": True},
                }},
                timeout=30,
            )
            if r.ok:
                print(f"  🔄 Synced Local Lowdown edits back to database (marked as manually edited)")
            else:
                print(f"  Warning: lowdown sync-back failed: {r.text[:200]}")

    # --- Free Events ---
    free_events_text, free_events_edited = extract_section_text(blocks, "Free Event of the Week")
    if free_events_edited and free_events_text and NOTION_FREE_EVENTS_DB_ID:
        try:
            pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
                "property": "Newsletter",
                "select": {"equals": newsletter_name}
            })
        except Exception:
            pages = []

        if pages:
            pages.sort(
                key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
                reverse=True,
            )
            target_id = pages[0]["id"]

            CHUNK_SIZE = 1900
            chunks = [{"text": {"content": free_events_text[i:i + CHUNK_SIZE]}}
                      for i in range(0, len(free_events_text), CHUNK_SIZE)]
            if not chunks:
                chunks = [{"text": {"content": ""}}]

            r = requests.patch(
                f"https://api.notion.com/v1/pages/{target_id}",
                headers=HEADERS,
                json={"properties": {
                    "Full Section":    {"rich_text": chunks},
                    "Manually Edited": {"checkbox": True},
                }},
                timeout=30,
            )
            if r.ok:
                print(f"  🔄 Synced Free Events edits back to database (marked as manually edited)")
            else:
                print(f"  Warning: free events sync-back failed: {r.text[:200]}")

    # --- Reader Poll ---
    poll_text, poll_edited = extract_section_text(blocks, "Reader Poll")
    if poll_edited and poll_text and NOTION_POLLS_DB_ID:
        # Parse: first non-empty line = Question; lines starting with • or - are options.
        lines = [ln.strip() for ln in poll_text.split("\n") if ln.strip()]
        new_question = ""
        new_options = []
        for ln in lines:
            if ln.startswith("•") or ln.startswith("-") or ln.startswith("*"):
                new_options.append(ln.lstrip("•-* ").strip())
            elif not new_question:
                new_question = ln
        if new_question:
            try:
                pages = query_database(NOTION_POLLS_DB_ID, filters={
                    "property": "Newsletter",
                    "select": {"equals": newsletter_name}
                })
            except Exception:
                pages = []
            pages = [p for p in pages if
                     (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
            if pages:
                pages.sort(
                    key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
                    reverse=True,
                )
                target_id = pages[0]["id"]
                options_md = "\n".join(f"- {o}" for o in new_options)
                r = requests.patch(
                    f"https://api.notion.com/v1/pages/{target_id}",
                    headers=HEADERS,
                    json={"properties": {
                        "Question":        {"rich_text": [{"text": {"content": new_question[:2000]}}]},
                        "Options":         {"rich_text": [{"text": {"content": options_md[:2000]}}]},
                        "Manually Edited": {"checkbox": True},
                    }},
                    timeout=30,
                )
                if r.ok:
                    print(f"  🔄 Synced Reader Poll edits back to database (marked as manually edited)")
                else:
                    print(f"  Warning: poll sync-back failed: {r.text[:200]}")


# ---------------------------------------------------------------------------
# BLOCK BUILDERS
# ---------------------------------------------------------------------------

# Defense-in-depth safety net: strip em dashes from anything we render to Notion.
# House style bans em dashes (U+2014) — the skill banners enforce this in the
# LLM, but if one slips through we replace it with ", " here. En dashes (U+2013,
# used for ranges like "10am–4pm") are intentionally left alone.
_EM_DASH_RE = re.compile(r"\s*—\s*")

def _strip_em_dashes(text: str) -> str:
    if not text or "—" not in text:
        return text
    return _EM_DASH_RE.sub(", ", text)


def heading_block(text: str, level: int = 2) -> dict:
    text = _strip_em_dashes(text)
    key = f"heading_{level}"
    return {
        "object": "block",
        "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def paragraph_block(text: str, bold: bool = False, italic: bool = False) -> dict:
    text = _strip_em_dashes(text)
    annotations: dict = {}
    if bold:   annotations["bold"]   = True
    if italic: annotations["italic"] = True
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}, "annotations": annotations}]
        },
    }


# Inline Markdown patterns used by paragraph_block_with_markdown
_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def parse_inline_markdown(text: str) -> list[dict]:
    """Convert inline Markdown to a list of Notion rich_text spans.
    Supports `**bold**` and `[label](url)`. Plain text outside these is
    emitted as plain spans. Overlapping matches resolve to the earliest."""
    text = _strip_em_dashes(text)
    matches = []
    for m in _BOLD_PATTERN.finditer(text):
        matches.append(("bold", m.start(), m.end(), m.group(1), None))
    for m in _LINK_PATTERN.finditer(text):
        matches.append(("link", m.start(), m.end(), m.group(1), m.group(2)))
    matches.sort(key=lambda x: x[1])

    # Resolve overlaps — keep earliest, drop any that overlap a kept one
    accepted = []
    last_end = 0
    for m in matches:
        if m[1] >= last_end:
            accepted.append(m)
            last_end = m[2]

    spans: list[dict] = []
    pos = 0
    for kind, start, end, group1, group2 in accepted:
        if start > pos:
            spans.append({"type": "text", "text": {"content": text[pos:start]}, "annotations": {}})
        if kind == "bold":
            spans.append({"type": "text", "text": {"content": group1}, "annotations": {"bold": True}})
        elif kind == "link":
            spans.append({
                "type": "text",
                "text": {"content": group1, "link": {"url": group2}},
                "annotations": {"color": "blue"},
            })
        pos = end
    if pos < len(text):
        spans.append({"type": "text", "text": {"content": text[pos:]}, "annotations": {}})
    if not spans:
        spans.append({"type": "text", "text": {"content": text}, "annotations": {}})
    return spans


def paragraph_block_with_markdown(text: str) -> dict:
    """Paragraph block with inline `**bold**` and `[label](url)` rendered as
    proper Notion rich_text spans (vs. literal `**` characters)."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": parse_inline_markdown(text)},
    }


def link_block(label: str, url: str) -> dict:
    """A paragraph with clickable hyperlinked text."""
    label = _strip_em_dashes(label)
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
    text = _strip_em_dashes(text)
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
    """Query a Notion database with optional filter. On HTTP 400 from a
    filtered query (typical when a select option referenced in the filter
    hasn't yet been added to the schema — e.g. brand-new newsletter name),
    retry once unfiltered and let the caller filter in Python."""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"filter": filters} if filters else {}
    results = []
    has_more = True
    cursor = None
    while has_more:
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        if r.status_code == 400 and filters:
            print(f"  ⚠ Notion 400 on filtered query of {db_id[:8]}… — retrying unfiltered")
            return query_database(db_id, filters=None)
        r.raise_for_status()
        data = r.json()
        results += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return results


def _pet_row_to_dict(props: dict) -> dict:
    """Convert a Notion pet page's `properties` dict → newsletter pet dict."""
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


def get_approved_pet(newsletter_name: str) -> dict | None:
    """Get the approved pet for a newsletter. Falls back to the most recent
    'default winner' (auto-flagged by the pet pipeline) if no manual approval
    exists — so Send-to-Beehiiv always has a pet to feature."""
    try:
        pages = query_database(NOTION_PETS_DB_ID)
    except Exception as e:
        print(f"  Pet query FAILED: {e}")
        return None

    # Filter to this newsletter only
    nl_pages = []
    for p in pages:
        nl_prop = p["properties"].get("Newsletter", {})
        if (nl_prop.get("select") or {}).get("name") == newsletter_name:
            nl_pages.append(p)

    # Tier 1: explicitly approved
    approved = []
    for p in nl_pages:
        status_prop = p["properties"].get("Status", {})
        status_name = (status_prop.get("select") or status_prop.get("status") or {}).get("name", "")
        if status_name == "approved":
            approved.append(p)
    if approved:
        # Sort by Date Generated desc so the MOST RECENT approval wins
        # (legacy approved rows that weren't flipped to 'approved - old'
        # shouldn't beat this week's pick).
        approved.sort(
            key=lambda p: (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", ""),
            reverse=True,
        )
        print(f"  {len(approved)} approved pet(s) for {newsletter_name} — using most recent")
        return _pet_row_to_dict(approved[0]["properties"])

    # Tier 2 fallback: default winner (most recent batch). default_winner is a
    # checkbox set by the pet pipeline when scoring picks a fresh winner.
    # Skip rows already archived ('approved - old') or rejected — they're
    # explicitly out of rotation, so we shouldn't promote them as fallback.
    defaults = []
    for p in nl_pages:
        if not (p["properties"].get("Default Winner", {}).get("checkbox") or False):
            continue
        status_prop = p["properties"].get("Status", {})
        status_name = (status_prop.get("select") or status_prop.get("status") or {}).get("name", "")
        if status_name in ("approved - old", "rejected"):
            continue
        defaults.append(p)
    if defaults:
        # Sort by Date Generated desc — pick the most recent default winner
        defaults.sort(
            key=lambda p: (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", ""),
            reverse=True,
        )
        print(f"  No approved pet for {newsletter_name} — using default winner: "
              f"{defaults[0]['properties'].get('Name', {}).get('title', [{}])[0].get('text', {}).get('content', '')}")
        return _pet_row_to_dict(defaults[0]["properties"])

    print(f"  No approved pet AND no default winner found for {newsletter_name}")
    return None


def _restaurant_row_to_dict(props: dict, status: str) -> dict:
    def _rt(key):
        rt = props.get(key, {}).get("rich_text", [])
        return rt[0].get("text", {}).get("content", "") if rt else ""
    return {
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
    }


def get_restaurants(newsletter_name: str) -> list[dict]:
    """Get this week's restaurants for a newsletter.

    Priority order:
      1. Tier 1/Tier 2 Winners — explicitly approved by the user
      2. Default-winner fallback — when no winners exist, pick the row with
         Default Winner=True (auto-flagged by the pipeline) plus the next
         couple highest-scored rows from the most recent batch as Tier 2 stand-ins

    The fallback ensures Send-to-Beehiiv always has restaurants to feature
    even when no one approved manually this week.
    """
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID)
        pages = [p for p in pages if
                 (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
    except Exception:
        return []

    # Tier 1: explicitly tiered winners
    winners = []
    for page in pages:
        props = page["properties"]
        status = ((props.get("Status", {}).get("select") or props.get("Status", {}).get("status") or {})
                  .get("name", ""))
        if status in ("Tier 1 Winner", "Tier 2 Winner"):
            winners.append(_restaurant_row_to_dict(props, status))

    if winners:
        # Keep only the most recent batch
        dates = [r["date"] for r in winners if r.get("date")]
        if dates:
            latest_date = max(dates)
            winners = [r for r in winners if r.get("date") == latest_date]
        winners.sort(key=lambda x: (0 if x["tier"] == "Tier 1 Winner" else 1, -(x["score"] or 0)))
        return winners

    # Fallback: no Tier 1/2 Winners exist. Use Default Winner + top scorers
    # from the most recent batch.
    candidates = []
    for page in pages:
        props = page["properties"]
        status = ((props.get("Status", {}).get("select") or props.get("Status", {}).get("status") or {})
                  .get("name", ""))
        # Skip archived/historical rows
        if status == "approved - old":
            continue
        candidates.append((page, props, status))

    if not candidates:
        return []

    # Filter to most recent batch by date
    dated = [(p, pr, s, ((pr.get("Date Generated", {}).get("date") or {}).get("start", "")))
             for (p, pr, s) in candidates]
    dates = [d for *_, d in dated if d]
    if dates:
        latest = max(dates)
        dated = [t for t in dated if t[3] == latest]

    # Promote default winner to Tier 1 stand-in; next 2 by score → Tier 2 stand-ins
    default_winner = None
    others = []
    for page, props, status, _ in dated:
        is_default = props.get("Default Winner", {}).get("checkbox") or False
        if is_default and default_winner is None:
            default_winner = props
        else:
            others.append(props)

    fallback_results = []
    if default_winner:
        fb = _restaurant_row_to_dict(default_winner, "Tier 1 Winner")
        fb["_is_fallback"] = True
        fallback_results.append(fb)
        print(f"  ⓘ Restaurants for {newsletter_name}: no winners, using default-winner fallback: {fb['name']}")
    # Add up to 2 more high-scoring as Tier 2 stand-ins
    others.sort(key=lambda p: -(p.get("Total Score", {}).get("number") or 0))
    for o in others[:2]:
        fb = _restaurant_row_to_dict(o, "Tier 2 Winner")
        fb["_is_fallback"] = True
        fallback_results.append(fb)

    if fallback_results:
        print(f"  ⓘ Restaurants fallback total: {len(fallback_results)} (default + top-scored)")
    return fallback_results


def get_latest_lowdown(newsletter_name: str) -> str | None:
    """Get the most recent Local Lowdown section text. Skips rows in
    `approved - old` / `rejected` so archived weeks don't keep rendering."""
    if not NOTION_LOWDOWN_DB_ID:
        return None
    try:
        pages = query_database(NOTION_LOWDOWN_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return None
    pages = [p for p in pages
             if (p["properties"].get("Status", {}).get("select") or {}).get("name", "")
             not in ("approved - old", "rejected")]
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


def get_latest_free_events(newsletter_name: str) -> str | None:
    """Get the most recent Free Events section markdown from the database."""
    if not NOTION_FREE_EVENTS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return None
    # Only show current 'approved' rows (not 'approved - old' which are exclusion-only)
    pages = [p for p in pages if
             (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    if not pages:
        return None
    pages.sort(
        key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
        reverse=True
    )
    props = pages[0]["properties"]
    section_text = props.get("Full Section", {}).get("rich_text", [])
    if section_text:
        return "".join(chunk.get("text", {}).get("content", "") for chunk in section_text)
    return None


def get_latest_tip(newsletter_name: str) -> dict | None:
    """Get the tip row this newsletter should render.
    Drop rows in 'rejected' / 'approved - old' status. Prefer the most recent
    Default Winner; fall back to the most recent remaining row."""
    if not NOTION_TIPS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_TIPS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return None
    pages = [p for p in pages if
             ((p["properties"].get("Status", {}).get("select") or {}).get("name") or "pending")
             not in ("rejected", "approved - old")]
    if not pages:
        return None
    pages.sort(
        key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
        reverse=True,
    )
    winners = [p for p in pages if p["properties"].get("Default Winner", {}).get("checkbox", False)]
    chosen = winners[0] if winners else pages[0]
    props = chosen["properties"]
    def _rt(key: str) -> str:
        items = props.get(key, {}).get("rich_text", [])
        return "".join(c.get("text", {}).get("content", "") for c in items)
    # Static sponsor attribution — every tip row defaults to
    # "Brought to you by Peachy Insurance" with a link to the
    # corporate site. Read what's on the row (allows manual override
    # in Notion if someone edits the row to a different sponsor).
    sponsor_name = _rt("Sponsor Name") or "Peachy Insurance"
    sponsor_url  = (props.get("Sponsor URL", {}).get("url") or "https://peachyinsurance.com/").strip()
    return {
        "tip_title":     _rt("Tip Title"),
        "blurb":         _rt("Blurb"),
        "source_url":    props.get("Source URL", {}).get("url", "") or "",
        "source_name":   _rt("Source Name"),
        "sponsor_name":  sponsor_name,
        "sponsor_url":   sponsor_url,
    }


def get_latest_free_event_image(newsletter_name: str) -> str:
    """Return the Image URL for the latest approved Free Event row, or "" if none."""
    if not NOTION_FREE_EVENTS_DB_ID:
        return ""
    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return ""
    pages = [p for p in pages if
             (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    if not pages:
        return ""
    pages.sort(
        key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
        reverse=True,
    )
    return pages[0]["properties"].get("Image URL", {}).get("url", "") or ""


def get_latest_poll(newsletter_name: str) -> dict | None:
    """Get the most recent approved poll for this newsletter."""
    if not NOTION_POLLS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_POLLS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return None
    # Only show current 'approved' rows (not 'approved - old' which are exclusion-only)
    pages = [p for p in pages if
             (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    if not pages:
        return None
    pages.sort(
        key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
        reverse=True,
    )
    props = pages[0]["properties"]

    def _rt(key: str) -> str:
        rt = props.get(key, {}).get("rich_text", [])
        return "".join(chunk.get("text", {}).get("content", "") for chunk in rt) if rt else ""

    question = _rt("Question")
    options_md = _rt("Options")
    intel = _rt("Ad Intel Mapping")
    framing = _rt("Framing")
    if not question:
        return None

    options = []
    for line in (options_md or "").split("\n"):
        line = line.strip()
        if line.startswith("- "):
            options.append(line[2:].strip())
        elif line.startswith("* "):
            options.append(line[2:].strip())
        elif line:
            options.append(line)

    return {
        "question": question,
        "options":  options,
        "framing":  framing,
        "ad_intel": intel,
    }


def get_latest_intro(newsletter_name: str) -> dict | None:
    """Get the most recent Welcome Intro blurb. Skips rows in
    `approved - old` / `rejected` so archived weeks don't keep rendering."""
    if not NOTION_INTRO_DB_ID:
        return None
    try:
        pages = query_database(NOTION_INTRO_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return None
    pages = [p for p in pages
             if (p["properties"].get("Status", {}).get("select") or {}).get("name", "")
             not in ("approved - old", "rejected")]
    if not pages:
        return None
    # Sort by date descending to get the latest
    pages.sort(
        key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
        reverse=True
    )
    props = pages[0]["properties"]
    greeting_rt = props.get("Greeting", {}).get("rich_text", [])
    blurb_rt    = props.get("Blurb",    {}).get("rich_text", [])
    subject_rt  = props.get("Subject Line",      {}).get("rich_text", [])
    preview_rt  = props.get("Preview Text",      {}).get("rich_text", [])
    teaser_rt   = props.get("In Todays Connect", {}).get("rich_text", [])
    greeting = "".join(c.get("text", {}).get("content", "") for c in greeting_rt) if greeting_rt else ""
    blurb    = "".join(c.get("text", {}).get("content", "") for c in blurb_rt) if blurb_rt else ""
    subject  = "".join(c.get("text", {}).get("content", "") for c in subject_rt) if subject_rt else ""
    preview  = "".join(c.get("text", {}).get("content", "") for c in preview_rt) if preview_rt else ""
    teaser   = "".join(c.get("text", {}).get("content", "") for c in teaser_rt) if teaser_rt else ""
    if blurb:
        return {
            "greeting":          greeting,
            "blurb":             blurb,
            "subject_line":      subject,
            "preview_text":      preview,
            "in_todays_connect": teaser,
        }
    return None


def get_real_estate(newsletter_name: str) -> list[dict]:
    """Get real estate listings from the database for a newsletter."""
    if not NOTION_RE_DB_ID:
        return []
    try:
        pages = query_database(NOTION_RE_DB_ID)
        pages = [p for p in pages if
                 (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
        # Only show current/approved listings — 'approved - old' is exclusion-only
        pages = [p for p in pages if
                 (p["properties"].get("Status", {}).get("select") or {}).get("name") != "approved - old"]
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
            "trivia":   _rt("Trivia Options"),  # comma-separated prices for Showcase
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

    # Dedupe by tier — if multiple rows share the same tier + latest date
    # (manually edited row + auto-generated row saved same day), keep the first.
    seen_tiers = set()
    deduped = []
    for r in results:
        tier = r.get("tier", "")
        if tier in seen_tiers:
            continue
        seen_tiers.add(tier)
        deduped.append(r)
    results = deduped

    # Sort: Starter, Sweet Spot, Showcase
    tier_order = {"Starter": 0, "Sweet Spot": 1, "Showcase": 2}
    results.sort(key=lambda x: tier_order.get(x["tier"], 9))
    return results


def _event_row_to_dict(props: dict) -> dict:
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
        "image_url":        props.get("Image URL", {}).get("url", ""),
        "header_image_url": props.get("Header Image URL", {}).get("url", ""),
        "gif_url":          props.get("GIF URL", {}).get("url", ""),
        "score":       props.get("Total Score", {}).get("number", 0),
    }


def get_featured_event(newsletter_name: str) -> dict | None:
    """Get the featured event. Falls back to highest-scored pending row from
    the most recent batch when no manual approval exists."""
    if not NOTION_EVENTS_DB_ID:
        print(f"  ⚠ NOTION_EVENTS_DB_ID is empty — Featured Event section will not render")
        return None
    print(f"  Looking up Featured Event for {newsletter_name} (db {NOTION_EVENTS_DB_ID[:8]}…)")
    try:
        pages = query_database(NOTION_EVENTS_DB_ID)
    except Exception as e:
        print(f"  Featured event query failed: {e}")
        return None

    # Filter to this newsletter
    nl_pages = []
    for p in pages:
        if (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name:
            nl_pages.append(p)

    # Tier 1: explicitly approved
    approved = []
    for p in nl_pages:
        status = ((p["properties"].get("Status", {}).get("select")
                   or p["properties"].get("Status", {}).get("status") or {}).get("name", ""))
        if status == "approved":
            approved.append(p)
    if approved:
        approved.sort(
            key=lambda p: (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", ""),
            reverse=True,
        )
        return _event_row_to_dict(approved[0]["properties"])

    # Fallback: highest-scored row from the most recent batch (any status
    # except 'approved - old' / 'rejected') — the auto-default for this week.
    candidates = []
    for p in nl_pages:
        status = ((p["properties"].get("Status", {}).get("select")
                   or p["properties"].get("Status", {}).get("status") or {}).get("name", ""))
        if status in ("approved - old", "rejected"):
            continue
        candidates.append(p)

    if not candidates:
        print(f"  No approved featured event AND no fallback candidates for {newsletter_name}")
        return None

    # Pick most recent batch
    dates = [(p["properties"].get("Date Generated", {}).get("date") or {}).get("start", "") for p in candidates]
    dates = [d for d in dates if d]
    if dates:
        latest = max(dates)
        candidates = [p for p in candidates
                      if (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", "") == latest]

    # Highest score wins
    candidates.sort(
        key=lambda p: -(p["properties"].get("Total Score", {}).get("number") or 0),
    )
    pick = _event_row_to_dict(candidates[0]["properties"])
    print(f"  ⓘ No approved featured event for {newsletter_name} — using highest-scored fallback: {pick.get('event_name')}")
    return pick


def _weekend_event_row_to_dict(props: dict) -> dict:
    """Parse a Weekend Planner DB row."""
    def _rt(key):
        rt = props.get(key, {}).get("rich_text", [])
        return rt[0].get("text", {}).get("content", "") if rt else ""
    return {
        "audience":     (props.get("Audience", {}).get("select") or {}).get("name", ""),
        "day":          (props.get("Day", {}).get("select") or {}).get("name", ""),
        "date":         (props.get("Date", {}).get("date") or {}).get("start", ""),
        "emoji":        _rt("Emoji"),
        "event_name":   _rt("Event Name"),
        "venue":        _rt("Venue"),
        "address":      _rt("Address"),
        "time":         _rt("Time"),
        "price":        _rt("Price"),
        "source_url":   props.get("Source URL", {}).get("url", "") or "",
        "image_url":    props.get("Image URL", {}).get("url", "") or "",
        "description":  _rt("Description"),
        "status":       (props.get("Status", {}).get("select") or {}).get("name", ""),
    }


def get_weekend_events(newsletter_name: str) -> list[dict]:
    """Fetch Weekend Planner events for this newsletter.

    Filters: Newsletter == newsletter_name, Status not in (rejected,
    approved-old). The weekend-window filter is intentionally NOT applied
    here — Weekend_Planner.py already restricts saves to the target
    weekend at scrape time, and Status flipping handles last-week's rows.
    Re-filtering here just risked dropping the current batch whenever the
    assembler's local date math disagreed with the picker's."""
    if not NOTION_WEEKEND_PLANNER_DB_ID:
        print(f"  ⚠ NOTION_WEEKEND_PLANNER_DB_ID is empty — Weekend Planner section will not render")
        return []
    print(f"  Looking up Weekend Planner events for {newsletter_name} (db {NOTION_WEEKEND_PLANNER_DB_ID[:8]}…)")
    try:
        pages = query_database(NOTION_WEEKEND_PLANNER_DB_ID)
    except Exception as e:
        print(f"  Weekend Planner query failed: {e}")
        return []

    events = []
    for p in pages:
        props = p["properties"]
        if (props.get("Newsletter", {}).get("select") or {}).get("name") != newsletter_name:
            continue
        status = (props.get("Status", {}).get("select") or {}).get("name", "")
        if status in ("rejected", "approved - old"):
            continue
        events.append(_weekend_event_row_to_dict(props))
    print(f"  Found {len(events)} Weekend Planner events for {newsletter_name}")
    return events


def _business_brief_row_to_dict(props: dict) -> dict:
    """Parse a Business Brief DB row."""
    def _rt(key):
        rt = props.get(key, {}).get("rich_text", [])
        return rt[0].get("text", {}).get("content", "") if rt else ""
    return {
        "name":             _rt("Business Name"),
        "city":             _rt("City"),
        "outside_coverage": props.get("Outside Coverage", {}).get("checkbox", False),
        "blurb":            _rt("Blurb"),
        "price_level":      (props.get("Price Level", {}).get("select") or {}).get("name", ""),
        "hours":            _rt("Hours"),
        "address":          _rt("Address"),
        "source_url":       props.get("Source URL", {}).get("url", "") or "",
        "source_domain":    _rt("Source Domain"),
        "photo_url":        props.get("Photo URL", {}).get("url", "") or "",
        "status":           (props.get("Status", {}).get("select") or {}).get("name", ""),
        "default_winner":   props.get("Default Winner", {}).get("checkbox", False),
        "manually_edited":  props.get("Manually Edited", {}).get("checkbox", False),
        "relevance_score":  props.get("Relevance Score", {}).get("number", 0) or 0,
        "date_generated":   (props.get("Date Generated", {}).get("date") or {}).get("start", ""),
    }


def get_business_brief(newsletter_name: str) -> dict | None:
    """Fetch the Business Brief pick for this newsletter. Falls back to the
    highest-relevance pending row from the most recent batch when no manual
    approval exists (mirrors get_featured_event behavior)."""
    if not NOTION_BUSINESS_BRIEF_DB_ID:
        print(f"  ⚠ NOTION_BUSINESS_BRIEF_DB_ID is empty — Business Brief section will not render")
        return None
    print(f"  Looking up Business Brief for {newsletter_name} (db {NOTION_BUSINESS_BRIEF_DB_ID[:8]}…)")
    try:
        pages = query_database(NOTION_BUSINESS_BRIEF_DB_ID)
    except Exception as e:
        print(f"  Business Brief query failed: {e}")
        return None

    nl_pages = [p for p in pages
                if (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]

    # Tier 1: explicitly approved
    approved = [p for p in nl_pages
                if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    if approved:
        approved.sort(
            key=lambda p: (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", ""),
            reverse=True,
        )
        return _business_brief_row_to_dict(approved[0]["properties"])

    # Fallback: highest-relevance pending row from the most recent batch
    candidates = [p for p in nl_pages
                  if (p["properties"].get("Status", {}).get("select") or {}).get("name", "") not in ("approved - old", "rejected")]
    if not candidates:
        print(f"  No approved business brief AND no fallback candidates for {newsletter_name}")
        return None

    dates = [(p["properties"].get("Date Generated", {}).get("date") or {}).get("start", "") for p in candidates]
    dates = [d for d in dates if d]
    if dates:
        latest = max(dates)
        candidates = [p for p in candidates
                      if (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", "") == latest]

    candidates.sort(
        key=lambda p: -(p["properties"].get("Relevance Score", {}).get("number") or 0),
    )
    pick = _business_brief_row_to_dict(candidates[0]["properties"])
    print(f"  ⓘ No approved business brief for {newsletter_name} — using highest-scored fallback: {pick.get('name')}")
    return pick


# ---------------------------------------------------------------------------
# WEEKEND PLANNER FORMATTERS
# ---------------------------------------------------------------------------

def display_domain(url: str) -> str:
    """Strip protocol, strip path/params/fragment, KEEP `www.` if present.
    Used for the visible anchor text in Weekend Planner event links."""
    if not url:
        return ""
    no_proto = url.split("://", 1)[-1]
    host = no_proto.split("/", 1)[0]
    return host.lower()


def weekend_event_paragraph(event: dict) -> dict:
    """One paragraph block in the inline pipe-separated format:
       emoji **Event Name** - Venue | Address | Time | Price | More: [www.domain.com](url)
    Bold annotation on event name only; link span uses display_domain as visible
    text and the full source_url as href."""
    emoji   = event.get("emoji", "") or ""
    name    = event.get("event_name", "") or ""
    venue   = event.get("venue", "") or ""
    address = event.get("address", "") or ""
    time    = event.get("time", "") or ""
    price   = event.get("price", "") or ""
    url     = event.get("source_url", "") or ""

    spans = []

    if emoji:
        spans.append({
            "type": "text",
            "text": {"content": f"{emoji} "},
            "annotations": {},
        })

    if name:
        spans.append({
            "type": "text",
            "text": {"content": name},
            "annotations": {"bold": True},
        })

    metadata_parts = [p for p in (venue, address, time, price) if p]
    metadata = " | ".join(metadata_parts)

    plain_chunk = ""
    if metadata:
        plain_chunk += f" - {metadata}"
    if url:
        plain_chunk += " | More: "
    if plain_chunk:
        spans.append({
            "type": "text",
            "text": {"content": plain_chunk},
            "annotations": {},
        })

    if url:
        spans.append({
            "type": "text",
            "text": {"content": display_domain(url), "link": {"url": url}},
            "annotations": {"color": "blue"},
        })

    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": spans},
    }


# ---------------------------------------------------------------------------
# PAGE ASSEMBLER
# ---------------------------------------------------------------------------

def _placeholder(text: str) -> dict:
    return callout_block(text, emoji="✏️")


# ---------------------------------------------------------------------------
# PER-SECTION BUILDERS
# Each returns a list of CONTENT blocks (no heading, no trailing divider)
# so it can be plugged into either the full rebuild or update_section() for
# partial updates.
# ---------------------------------------------------------------------------

def _build_intro(newsletter_name: str) -> list[dict]:
    intro = get_latest_intro(newsletter_name)
    if not intro:
        return [_placeholder("Not yet automated.")]
    # Trace what the Intro DB row actually returned so we can see at a
    # glance whether preview_text and in_todays_connect are populated.
    print(f"  Intro fields loaded: "
          f"greeting={'✓' if intro.get('greeting') else '✗'}, "
          f"blurb={'✓' if intro.get('blurb') else '✗'}, "
          f"subject_line={'✓' if intro.get('subject_line') else '✗'}, "
          f"preview_text={'✓' if intro.get('preview_text') else '✗'} "
          f"({len(intro.get('preview_text') or '')} chars), "
          f"in_todays_connect={'✓' if intro.get('in_todays_connect') else '✗'} "
          f"({len((intro.get('in_todays_connect') or '').splitlines())} lines)")
    out = []
    if intro.get("greeting"):
        out.append(paragraph_block(intro["greeting"], bold=True))
    # Defense-in-depth: if the blurb has accumulated preview/teaser
    # content from a previous sync-back, strip it before rendering
    # so the dedicated Preview Text + In Todays Connect fields don't
    # render the same thing twice.
    out.append(paragraph_block(_strip_intro_addons(intro["blurb"])))
    # Preview Text — the one-line summary generated by the
    # subject_line pipeline (subject-preview-text skill). Renders in
    # italics under the blurb so reviewers can sanity-check what will
    # land in the email body as {summary_text}.
    preview = (intro.get("preview_text") or "").strip()
    if preview:
        out.append(paragraph_block(""))  # spacer
        out.append(paragraph_block(f"Preview: {preview}", italic=True))
    # "In Today's Connect" teaser block lives right under the editor's
    # note — it's generated by a separate pipeline (chained after the
    # intro) and stored in the same Intro DB row.
    teaser = (intro.get("in_todays_connect") or "").strip()
    if teaser:
        out.append(paragraph_block(""))  # spacer
        for line in teaser.splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(paragraph_block_with_markdown(line))
    return out


def _build_poll(newsletter_name: str) -> list[dict]:
    poll = get_latest_poll(newsletter_name)
    if not poll:
        return [_placeholder("Not yet automated.")]
    out = [paragraph_block(poll["question"], bold=True)]
    for opt in poll.get("options", []):
        out.append(paragraph_block(f"• {opt}"))
    if poll.get("ad_intel"):
        out.append(paragraph_block(""))
        out.append(callout_block(
            "Ad intel mapping (internal — do not paste into Beehiiv):\n" + poll["ad_intel"],
            emoji="🧭",
        ))
    return out


def _build_featured_event(newsletter_name: str) -> list[dict]:
    event = get_featured_event(newsletter_name)
    if not (event and event.get("blurb")):
        return [callout_block("No featured event selected yet. Run the Featured Event pipeline and approve an event.", emoji="⏳")]
    out = []
    # Rotating GIF of 1-3 alternate candidate images (mirrors Restaurant Radar pattern).
    # Falls back to the static featured image if no GIF was built.
    if event.get("gif_url"):
        out.append(image_block(event["gif_url"]))
    elif event.get("image_url"):
        out.append(image_block(event["image_url"]))
    out.append(paragraph_block(f"⭐ Featured Event: {event['event_name']}", bold=True))
    detail_parts = [p for p in (event.get("date"), event.get("time"), event.get("venue")) if p]
    if detail_parts:
        out.append(paragraph_block("📅 " + " | ".join(detail_parts)))
    if event.get("price"):
        out.append(paragraph_block(f"🎟️ {event['price']}"))
    out.append(paragraph_block(event["blurb"]))
    if event.get("ticket_url"):
        out.append(link_block("Get Tickets", event["ticket_url"]))
    elif event.get("source_url"):
        out.append(link_block("Learn More", event["source_url"]))
    return out


def _build_restaurants(newsletter_name: str) -> list[dict]:
    restaurants = get_restaurants(newsletter_name)
    if not restaurants:
        return [callout_block("No restaurants selected yet. Run the pipeline and approve in the review app.", emoji="⏳")]
    out = []
    for r in restaurants:
        tier_label = "⭐ TIER 1 — FEATURED" if r["tier"] == "Tier 1 Winner" else "TIER 2"
        out.append(paragraph_block(f"[{tier_label}] {r['name']} (Score: {r['score']}/40)", bold=True))
        if r.get("gif"):
            out.append(image_block(r["gif"]))
        elif r.get("photo"):
            out.append(image_block(r["photo"]))
        if r.get("blurb"):
            out.append(paragraph_block(r["blurb"]))
        details_parts = []
        if r.get("cuisine"):
            details_parts.append(r["cuisine"])
        if r.get("rating"):
            details_parts.append(f"{r['rating']}★")
        if details_parts:
            out.append(paragraph_block(" | ".join(details_parts)))
        if r.get("address"):
            out.append(paragraph_block(r["address"]))
        if r.get("phone"):
            out.append(paragraph_block(r["phone"]))
        if r.get("hours"):
            out.append(paragraph_block(r["hours"]))
        if r.get("website"):
            out.append(link_block("Website", r["website"]))
        if r.get("maps_url"):
            out.append(link_block("Google Maps", r["maps_url"]))
        if r.get("photo"):
            out.append(link_block("Download Photo", r["photo"]))
        out.append(paragraph_block(""))
    return out


def _build_real_estate(newsletter_name: str) -> list[dict]:
    re_listings = get_real_estate(newsletter_name)
    if not re_listings:
        return [callout_block("No real estate listings yet. Run the Real Estate Corner pipeline.", emoji="⏳")]
    out = []
    for listing in re_listings:
        tier_emoji = {"Starter": "🏠", "Sweet Spot": "🏡", "Showcase": "🏰"}.get(listing["tier"], "🏠")
        out.append(paragraph_block(f"{tier_emoji} {listing['tier']}: {listing.get('headline', '')}", bold=True))
        if listing.get("template"):
            out.append(image_block(listing["template"]))
        elif listing.get("photo"):
            out.append(image_block(listing["photo"]))
        if listing.get("blurb"):
            out.append(paragraph_block(listing["blurb"]))
        # Showcase tier: price-guess trivia immediately under the image/blurb.
        # The Trivia Options field is a comma-separated list of 4 prices
        # (one of which is the actual). Render as a-b-c-d choices, then
        # reveal the answer on a separate line.
        if listing["tier"] == "Showcase" and listing.get("trivia"):
            try:
                options = [int(p) for p in listing["trivia"].split(",")
                           if p.strip().isdigit()]
            except Exception:
                options = []
            actual = int(listing.get("price") or 0)
            if options and actual in options:
                labels = "ABCD"
                choice_strs = [
                    f"{labels[i]}) ${p:,}" for i, p in enumerate(sorted(options))
                ]
                out.append(paragraph_block(
                    "🎲 Guess the price! " + "   ".join(choice_strs),
                    bold=True,
                ))
                # Reveal answer on a separate line so it can be hidden in
                # email layouts that style it (small caps / italic).
                answer_letter = labels[sorted(options).index(actual)]
                out.append(paragraph_block(
                    f"Answer: {answer_letter}) ${actual:,}",
                ))
        if listing.get("url"):
            out.append(link_block("View Listing", listing['url']))
        if listing.get("template"):
            out.append(link_block("Download Image", listing['template']))
        out.append(paragraph_block(""))
    return out


def _build_lowdown(newsletter_name: str) -> list[dict]:
    lowdown_text = get_latest_lowdown(newsletter_name)
    if not lowdown_text:
        return [callout_block("No Local Lowdown generated yet. Run the Local Lowdown pipeline.", emoji="⏳")]
    out = []
    for para in lowdown_text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("### "):
            out.append(paragraph_block(para.replace("### ", ""), bold=True))
        else:
            out.append(paragraph_block(para))
    return out


def _build_pets(newsletter_name: str) -> list[dict]:
    pet = get_approved_pet(newsletter_name)
    if not (pet and pet.get("blurb")):
        return [callout_block("No approved pet yet. Run the pipeline and approve a pet in the review app.", emoji="⏳")]
    out = [paragraph_block(pet["name"], bold=True)]
    if pet.get("gif"):
        out.append(image_block(pet["gif"]))
    elif pet.get("photo"):
        out.append(image_block(pet["photo"]))
    out.append(paragraph_block(pet["blurb"]))
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
            out.append(paragraph_block("\n".join(shelter_lines)))
    if pet.get("url"):
        out.append(link_block("View Pet Listing", pet['url']))
    if pet.get("gif"):
        out.append(link_block("Download GIF", pet['gif']))
    elif pet.get("photo"):
        out.append(link_block("Download Photo", pet['photo']))
    return out


def _build_weekend_planner(newsletter_name: str) -> list[dict]:
    """Render the Weekend Planner section: Family Events + Adult Events,
    each with Friday/Saturday/Sunday subsections, each event as one inline
    paragraph + one description paragraph.

    Honors WEEKEND_AUDIENCE and WEEKEND_DAY env vars for partial rendering:
      WEEKEND_AUDIENCE: Family | Adult | both (default: both)
      WEEKEND_DAY: Friday | Saturday | Sunday | all (default: all)"""
    events = get_weekend_events(newsletter_name)
    if not events:
        return [_placeholder("No Weekend Planner events generated yet. Run the Weekend Planner pipeline.")]

    # Apply audience filter
    audience_arg = (os.environ.get("WEEKEND_AUDIENCE") or "both").strip()
    if audience_arg.lower() in ("both", "all"):
        audiences = ["Family", "Adult"]
    elif audience_arg in ("Family", "Adult"):
        audiences = [audience_arg]
    else:
        print(f"  ⚠ Unknown WEEKEND_AUDIENCE '{audience_arg}', falling back to both")
        audiences = ["Family", "Adult"]

    # Apply day filter
    day_arg = (os.environ.get("WEEKEND_DAY") or "all").strip()
    if day_arg.lower() == "all":
        days = ["Friday", "Saturday", "Sunday"]
    elif day_arg in ("Friday", "Saturday", "Sunday"):
        days = [day_arg]
    else:
        print(f"  ⚠ Unknown WEEKEND_DAY '{day_arg}', falling back to all")
        days = ["Friday", "Saturday", "Sunday"]

    if audiences != ["Family", "Adult"] or days != ["Friday", "Saturday", "Sunday"]:
        print(f"  Weekend Planner filtered: audience={audiences}, day={days}")

    # Group: audience -> day -> [events]
    grouped: dict = {a: {d: [] for d in days} for a in audiences}
    for ev in events:
        a = ev.get("audience", "")
        d = ev.get("day", "")
        if a in grouped and d in grouped[a]:
            grouped[a][d].append(ev)

    # Sort each (audience, day) bucket alphabetically by event_name.
    # Case-insensitive, strips leading "The "/"A "/"An " articles so
    # 'The Marietta Greek Festival' sorts under M, not T.
    import re as _re_sort
    def _sort_key(ev: dict) -> str:
        name = (ev.get("event_name") or "").strip()
        # Strip a single leading article + ordinal prefixes ("36th Annual ...")
        # so headline qualifiers don't dictate the order
        name = _re_sort.sub(r"^(the|a|an)\s+", "", name, flags=_re_sort.IGNORECASE)
        name = _re_sort.sub(r"^\d+(st|nd|rd|th)?\s+(annual\s+)?", "",
                            name, flags=_re_sort.IGNORECASE)
        return name.lower()
    for a in grouped:
        for d in grouped[a]:
            grouped[a][d].sort(key=_sort_key)

    # Pick a date label per day from any event in that bucket (they should all share)
    def _day_header(day: str, bucket_events: list[dict]) -> str:
        if not bucket_events:
            return day
        iso = bucket_events[0].get("date", "")
        if not iso:
            return day
        try:
            dt = datetime.fromisoformat(iso)
            return f"{day}, {dt.strftime('%B')} {dt.day}"
        except Exception:
            return day

    blocks: list[dict] = []
    # (Canva-style banner removed at user request — Weekend Planner
    # renders as a plain section without the templated header image.)

    for audience_key in audiences:
        # Skip the entire pane if it has no events at all
        pane_total = sum(len(grouped[audience_key][d]) for d in days)
        if pane_total == 0:
            continue
        blocks.append(heading_block(f"{audience_key} Events", level=3))
        for day in days:
            day_events = grouped[audience_key][day]
            if not day_events:
                continue
            blocks.append(paragraph_block(_day_header(day, day_events), bold=True))
            for ev in day_events:
                # Per-event thumbnail (small image_url) above the event title
                if ev.get("image_url"):
                    blocks.append(image_block(ev["image_url"]))
                blocks.append(weekend_event_paragraph(ev))
                desc = ev.get("description", "")
                if desc:
                    blocks.append(paragraph_block(desc))
    return blocks


def _build_free_events(newsletter_name: str) -> list[dict]:
    """Render the Free Activity of the Week section.

    The pipeline saves a Markdown blob to Notion (composed by save_free_events_to_notion).
    Render rules:
      `### text`      -> bold paragraph (the activity title row)
      everything else -> paragraph with inline `**bold**` and `[label](url)`
                         parsed into rich_text spans (so the published Notion
                         page actually shows bold sub-section labels and clickable
                         links rather than raw `**` characters)."""
    free_events_text = get_latest_free_events(newsletter_name)
    if not free_events_text:
        return [callout_block("No Free Events generated yet. Run the Free Events pipeline.", emoji="⏳")]
    out = []
    for para in free_events_text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("### "):
            out.append(paragraph_block(para.replace("### ", ""), bold=True))
        else:
            out.append(paragraph_block_with_markdown(para))
    return out


def _build_business_brief(newsletter_name: str) -> list[dict]:
    """Render the Business Brief section: bold business name + blurb paragraphs
    + a Price/Hours/Website metadata block. Mirrors the Free Events renderer's
    inline-Markdown approach so any `**bold**` or `[label](url)` Claude emits
    in the blurb renders as Notion rich_text spans."""
    business = get_business_brief(newsletter_name)
    if not (business and business.get("blurb")):
        return [callout_block("No business brief yet. Run the Business Brief pipeline and approve a pick.", emoji="⏳")]

    out: list[dict] = []
    # Photo from Google Places (when set on the row) renders above the name.
    if business.get("photo_url"):
        out.append(image_block(business["photo_url"]))
    # Bold business name as the title row
    out.append(paragraph_block(business["name"], bold=True))

    # Blurb paragraphs (split on blank lines so each becomes its own paragraph block)
    for para in business["blurb"].split("\n\n"):
        para = para.strip()
        if not para:
            continue
        out.append(paragraph_block_with_markdown(para))

    # Metadata block (Price / Hours / Address / Website) as plain lines
    if business.get("price_level"):
        out.append(paragraph_block_with_markdown(f"**Price:** {business['price_level']}"))
    if business.get("hours"):
        out.append(paragraph_block_with_markdown(f"**Hours:** {business['hours']}"))
    if business.get("address"):
        out.append(paragraph_block_with_markdown(f"**Address:** {business['address']}"))
    if business.get("source_url"):
        domain = display_domain(business["source_url"])
        out.append(paragraph_block_with_markdown(f"**Website:** [{domain}]({business['source_url']})"))
    return out


def _build_tip(newsletter_name: str) -> list[dict]:
    """Render the Insurance Tip section: bold tip title + the blurb body
    (parsed for inline `**bold**` and `[label](url)` so links render). The
    skill stores the tip body in the Blurb column and the title in Tip Title.
    The 'Learn more from <Source Name>' line is part of the blurb."""
    tip = get_latest_tip(newsletter_name)
    if not (tip and tip.get("blurb")):
        return [callout_block("No Insurance Tip yet. Run the Insurance Tip pipeline.", emoji="⏳")]
    out: list[dict] = []
    if tip.get("tip_title"):
        out.append(paragraph_block(tip["tip_title"], bold=True))
    for para in tip["blurb"].split("\n\n"):
        para = para.strip()
        if not para:
            continue
        out.append(paragraph_block_with_markdown(para))
    # Sponsor attribution: "Brought to you by Peachy Insurance" with the
    # name linked to the sponsor URL. Defaults are static (Peachy +
    # peachyinsurance.com) so the line always appears.
    sponsor_name = (tip.get("sponsor_name") or "").strip()
    sponsor_url  = (tip.get("sponsor_url") or "").strip()
    if sponsor_name:
        if sponsor_url:
            out.append(paragraph_block_with_markdown(
                f"Brought to you by [{sponsor_name}]({sponsor_url})"
            ))
        else:
            out.append(paragraph_block(f"Brought to you by {sponsor_name}"))
    return out


def _build_static_placeholder(_newsletter_name: str) -> list[dict]:
    """Standard 'Not yet automated.' placeholder for un-automated sections."""
    return [_placeholder("Not yet automated.")]


def get_memes(newsletter_name: str) -> list[dict]:
    """Fetch approved (or pending fallback) memes for this newsletter.
    Returns up to 3, sorted by Reddit score desc."""
    if not NOTION_MEMES_DB_ID:
        return []
    try:
        pages = query_database(NOTION_MEMES_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name},
        })
    except Exception as e:
        print(f"  Meme query failed: {e}")
        return []
    # Tier 1: rows marked 'approved'. Fall back to top-scored 'pending'
    # so a missed approval still ships something rather than nothing.
    approved = [p for p in pages
                if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    pool = approved or [p for p in pages
                        if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "pending"]
    out: list[dict] = []
    for p in pool:
        props = p["properties"]
        out.append({
            "caption":   "".join(c.get("text", {}).get("content", "")
                                 for c in (props.get("Caption", {}).get("rich_text") or [])),
            "image_url": props.get("Image URL", {}).get("url", "") or "",
            "permalink": props.get("Reddit Permalink", {}).get("url", "") or "",
            "score":     props.get("Score", {}).get("number", 0) or 0,
            "subreddit": (props.get("Subreddit", {}).get("select") or {}).get("name", ""),
        })
    out.sort(key=lambda r: -(r.get("score") or 0))
    return out[:3]


def _extract_file_or_url(prop: dict) -> str:
    """Extract a usable URL from a Notion property that might be either
    a `url` type or a `files` type (Notion-uploaded files have a
    .file.url with a temporary signed URL; external files have a
    .external.url that's stable). Returns "" if nothing usable."""
    if not prop:
        return ""
    if prop.get("type") == "url" or prop.get("url"):
        return prop.get("url") or ""
    files = prop.get("files") or []
    for f in files:
        ext = (f.get("external") or {}).get("url")
        if ext:
            return ext
        inner = (f.get("file") or {}).get("url")
        if inner:
            return inner
    return ""


def get_sponsor(newsletter_name: str) -> dict | None:
    """Fetch this week's approved sponsor for the given newsletter.
    Sponsor DB schema (per the user's existing layout):
      Name            (title)
      Logo            (files — Notion-uploaded)
      Multi-select    (select — value 'Approved' = active)
      Newsletter      (multi_select — comma-separated list)
      blurb           (rich_text)
      hours           (rich_text)
      images          (files — optional secondary)
      website         (url)
    Picks the first 'Approved' row tagged for this newsletter."""
    if not NOTION_SPONSOR_DB_ID:
        return None
    try:
        pages = query_database(NOTION_SPONSOR_DB_ID)
    except Exception as e:
        print(f"  Sponsor query failed: {e}")
        return None

    def _status_of(p):
        sel = p["properties"].get("Multi-select", {}).get("select")
        if sel:
            return (sel.get("name") or "").strip()
        # Some Notion templates store status as multi_select instead.
        for opt in p["properties"].get("Multi-select", {}).get("multi_select") or []:
            if (opt.get("name") or "").strip().lower() == "approved":
                return "Approved"
        return ""

    def _newsletters_of(p):
        out = []
        for opt in p["properties"].get("Newsletter", {}).get("multi_select") or []:
            out.append((opt.get("name") or "").strip())
        # Fall back to single select shape just in case
        sel = p["properties"].get("Newsletter", {}).get("select") or {}
        if sel.get("name"):
            out.append(sel["name"])
        return out

    matched = [
        p for p in pages
        if _status_of(p).lower() == "approved"
        and newsletter_name in _newsletters_of(p)
    ]
    if not matched:
        return None

    def _rt(key):
        rt = matched[0]["properties"].get(key, {}).get("rich_text", [])
        return "".join(c.get("text", {}).get("content", "") for c in rt) if rt else ""

    def _title(key):
        t = matched[0]["properties"].get(key, {}).get("title", [])
        return "".join(c.get("text", {}).get("content", "") for c in t) if t else ""

    props = matched[0]["properties"]
    return {
        "name":      _title("Name"),
        "blurb":     _rt("blurb"),
        "hours":     _rt("hours"),
        "website":   (props.get("website") or {}).get("url", "") or "",
        "logo_url":  _extract_file_or_url(props.get("Logo") or {}),
        "image_url": _extract_file_or_url(props.get("images") or {}),
    }


def _build_sponsor(newsletter_name: str) -> list[dict]:
    """Render the Sponsor Corner on the assembled landing page: logo
    image + bold sponsor name + blurb + (optional hours) + website link.
    Falls back to the static placeholder when nothing's approved."""
    sponsor = get_sponsor(newsletter_name)
    if not sponsor or not sponsor.get("name"):
        return [_placeholder("No active sponsor configured. Approve a row in the Sponsor Corner DB.")]
    out: list[dict] = []
    if sponsor.get("logo_url"):
        out.append(image_block(sponsor["logo_url"]))
    if sponsor.get("image_url"):
        out.append(image_block(sponsor["image_url"]))
    out.append(paragraph_block(sponsor["name"], bold=True))
    for para in (sponsor.get("blurb") or "").split("\n\n"):
        para = para.strip()
        if para:
            out.append(paragraph_block_with_markdown(para))
    if sponsor.get("hours"):
        out.append(paragraph_block_with_markdown(f"**Hours:** {sponsor['hours']}"))
    if sponsor.get("website"):
        domain = display_domain(sponsor["website"])
        out.append(paragraph_block_with_markdown(f"**Visit:** [{domain}]({sponsor['website']})"))
    return out


def get_in_search_of(newsletter_name: str) -> list[dict]:
    """Fetch In Search Of rows for this newsletter.

    Approved-first: returns approved rows when any exist. Otherwise
    falls back to pending so a missed approval still ships something
    rather than nothing. Bonus rows are kept at the end of the result
    list so the assembler renders them after employer rows."""
    if not NOTION_IN_SEARCH_OF_DB_ID:
        return []
    try:
        pages = query_database(NOTION_IN_SEARCH_OF_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name},
        })
    except Exception as e:
        print(f"  In Search Of query failed: {e}")
        return []
    approved = [p for p in pages
                if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    pool = approved or [p for p in pages
                        if (p["properties"].get("Status", {}).get("select") or {}).get("name") == "pending"]
    out: list[dict] = []
    for p in pool:
        props = p["properties"]
        out.append({
            "employer":         "".join(c.get("text", {}).get("content", "")
                                       for c in (props.get("Employer", {}).get("rich_text") or [])),
            "description":      "".join(c.get("text", {}).get("content", "")
                                        for c in (props.get("Description", {}).get("rich_text") or [])),
            "job_listings_url": props.get("Job Listings URL", {}).get("url", "") or "",
            "image_url":        props.get("Image URL", {}).get("url", "") or "",
            "bonus":             bool((props.get("Bonus", {}) or {}).get("checkbox")),
        })
    # Regular rows first, bonus rows last (preserving relative order
    # within each group).
    out.sort(key=lambda r: (1 if r["bonus"] else 0))
    return out


def _build_in_search_of(newsletter_name: str) -> list[dict]:
    """Render the In Search Of section: per-employer blurb (with bolded
    name from inline markdown) followed by a clickable CTA. Bonus rows
    render with a 'Visit [employer]' CTA; regular rows with 'Browse
    openings'. Falls back to the static placeholder if nothing's been
    written yet."""
    rows = get_in_search_of(newsletter_name)
    if not rows:
        return [_placeholder("No In Search Of rows yet. Scrape job sources and run the In Search Of pipeline.")]
    out: list[dict] = []
    for r in rows:
        blurb = (r.get("description") or "").strip()
        if not blurb:
            # Don't render a row with no AI-written blurb; the pipeline
            # hasn't processed it yet.
            continue
        out.append(paragraph_block_with_markdown(blurb))
        url = r.get("job_listings_url") or ""
        if url:
            if r.get("bonus"):
                label = f"👉 Visit {r.get('employer', 'site')}"
            else:
                label = "👉 Browse openings"
            out.append(link_block(label, url))
        out.append(paragraph_block(""))  # spacer between rows
    # Strip trailing spacer
    if out and out[-1]["paragraph"]["rich_text"] == []:
        out.pop()
    return out


def _build_meme_corner(newsletter_name: str) -> list[dict]:
    """Render up to 3 approved memes: image + 'r/<sub> • caption' caption
    line under each. Falls back to the static placeholder if nothing's
    approved yet."""
    memes = get_memes(newsletter_name)
    if not memes:
        return [_placeholder("No memes selected yet. Approve a row in the Meme Corner DB.")]
    out: list[dict] = []
    for m in memes:
        if m.get("image_url"):
            out.append(image_block(m["image_url"]))
        sub = m.get("subreddit") or ""
        cap = m.get("caption") or ""
        line = f"r/{sub} • {cap}" if sub else cap
        if line:
            out.append(paragraph_block(line))
    return out


# ---------------------------------------------------------------------------
# SECTION REGISTRY — order matters for the full rebuild path
# ---------------------------------------------------------------------------
SECTIONS = {
    "intro":          {"heading": "👋 Welcome Intro",          "builder": _build_intro},
    "summary":        {"heading": "📑 Summary",                "builder": _build_static_placeholder},
    "poll":           {"heading": "📊 Reader Poll",            "builder": _build_poll},
    "sponsor":        {"heading": "💼 Sponsor Corner",         "builder": _build_sponsor},
    "featured_event": {"heading": "🎪 Event of the Week",      "builder": _build_featured_event},
    "restaurants":    {"heading": "🍽️ Restaurant Radar",       "builder": _build_restaurants},
    "business_brief": {"heading": "🏢 Business Brief",         "builder": _build_business_brief},
    "real_estate":    {"heading": "🏠 Real Estate Corner",     "builder": _build_real_estate},
    "lowdown":        {"heading": "🗞️ Local Lowdown",          "builder": _build_lowdown},
    "pets":           {"heading": "🐾 Furry Friends",          "builder": _build_pets},
    "weekend_planner": {"heading": "📅 Weekend Planner",        "builder": _build_weekend_planner},
    "free_events":    {"heading": "🆓 Free Event of the Week", "builder": _build_free_events},
    "tip":            {"heading": "🛡️ Insurance Tip",          "builder": _build_tip},
    "in_search_of":   {"heading": "🔍 In Search Of",           "builder": _build_in_search_of},
    "meme":           {"heading": "😂 Meme Corner",            "builder": _build_meme_corner},
}

SECTION_ORDER = [
    "intro", "summary", "poll", "sponsor", "featured_event", "restaurants",
    "business_brief", "real_estate", "lowdown", "pets", "weekend_planner",
    "free_events", "tip", "in_search_of", "meme",
]


def build_newsletter_blocks(newsletter_name: str) -> list[dict]:
    """Build all Notion blocks for a newsletter landing page."""
    today = datetime.today().strftime("%B %d, %Y")
    blocks = []

    # Hero: featured event Canva-style header composite at the very top
    _ev = get_featured_event(newsletter_name)
    if _ev and _ev.get("header_image_url"):
        blocks.append(image_block(_ev["header_image_url"]))

    blocks.extend([
        callout_block(
            f"Last updated: {today}\nCopy each section below into the newsletter template.",
            emoji="📋",
        ),
        divider_block(),
    ])
    for key in SECTION_ORDER:
        cfg = SECTIONS[key]
        blocks.append(heading_block(cfg["heading"]))
        blocks.extend(cfg["builder"](newsletter_name))
        blocks.append(divider_block())
    # The last section's trailing divider is harmless; keeps consistency
    return blocks


def update_one_section(page_id: str, newsletter_name: str, section_key: str) -> bool:
    """Update one section's content on the landing page.

    Default behavior: clear the section's current content and replace it with
    a fresh build from the DB. With APPEND=true env var, instead append the
    new build to the end of the existing section (useful for incrementally
    adding Weekend Planner events without wiping previous renders)."""
    cfg = SECTIONS.get(section_key)
    if not cfg:
        print(f"  ✗ Unknown section key: '{section_key}'. Known: {list(SECTIONS)}")
        return False

    append_mode = (os.environ.get("APPEND") or "false").strip().lower() == "true"
    mode_label = "appending to" if append_mode else "updating"
    print(f"  {mode_label.capitalize()} section '{section_key}' ({cfg['heading']})…")

    # Sync any landing-page edits back to the DB before we overwrite from DB.
    # Skip the sync in append mode — the existing content is being preserved,
    # not replaced, so there's nothing to sync back.
    if not append_mode:
        sync_edits_back(page_id, newsletter_name)
    new_blocks = cfg["builder"](newsletter_name)
    return update_section(page_id, cfg["heading"], new_blocks, append=append_mode)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    section_arg = (os.environ.get("SECTION") or "all").strip().lower()
    is_partial = section_arg and section_arg != "all"
    if is_partial and section_arg not in SECTIONS:
        print(f"⚠ Unknown SECTION '{section_arg}'. Falling back to full rebuild. Known: {list(SECTIONS)}")
        is_partial = False

    mode = f"partial ({section_arg})" if is_partial else "full rebuild"

    # Optional NEWSLETTER filter — process just one newsletter or "all"
    newsletter_arg = (os.environ.get("NEWSLETTER") or "all").strip()
    if newsletter_arg.lower() == "all":
        to_process = NEWSLETTERS
    elif newsletter_arg in NEWSLETTERS:
        to_process = [newsletter_arg]
    else:
        print(f"⚠ Unknown NEWSLETTER '{newsletter_arg}'. Falling back to all. Known: {NEWSLETTERS}")
        to_process = NEWSLETTERS

    scope = "all newsletters" if to_process is NEWSLETTERS else newsletter_arg
    print(f"Assembling newsletter landing pages — {datetime.today().strftime('%Y-%m-%d')} — mode: {mode} — scope: {scope}")

    for newsletter_name in to_process:
        display_name = newsletter_name.replace("_", " ")
        page_title = f"{display_name} — Current Edition"

        print(f"\n{'='*60}")
        print(f"  {page_title}")
        print(f"{'='*60}")

        # Find or create the page
        page_id = notion_search_page(page_title)
        if not page_id:
            print(f"  Creating new page...")
            page_id = notion_create_page(page_title, NOTION_PARENT_PAGE_ID)
            print(f"  Created page: {page_id}")
            # New page — must do a full rebuild regardless of partial mode
            blocks = build_newsletter_blocks(newsletter_name)
            print(f"  Writing {len(blocks)} blocks...")
            notion_append_blocks(page_id, blocks)
            print(f"  ✓ Done")
            continue

        print(f"  Found existing page: {page_id}")

        if is_partial:
            # Update only the specified section — leaves other sections (and any in-progress edits) alone
            ok = update_one_section(page_id, newsletter_name, section_arg)
            print(f"  ✓ Done" if ok else "  ✗ Section update failed")
        else:
            # Full rebuild — sync edits back, clear, rebuild
            print(f"  Checking for manual edits to sync back...")
            sync_edits_back(page_id, newsletter_name)
            print(f"  Clearing old content...")
            notion_clear_page(page_id)
            blocks = build_newsletter_blocks(newsletter_name)
            print(f"  Writing {len(blocks)} blocks...")
            notion_append_blocks(page_id, blocks)
            print(f"  ✓ Done")

    print(f"\nAll landing pages updated.")
