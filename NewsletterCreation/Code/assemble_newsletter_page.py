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
import base64
import hashlib
import requests
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(__file__))
from newsletters_config import newsletter_names, get_newsletter

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

def _notion_req(method: str, url: str, *, json_body: dict | None = None,
                timeout: int = 30, max_attempts: int = 5,
                ok_statuses: tuple = ()):
    """Resilient Notion request. Retries transient network errors (read
    timeouts, connection resets) and 429/5xx with exponential backoff
    (honoring Retry-After), then raise_for_status() on the final result.

    A single read-timeout used to crash a full rebuild mid-clear; this wraps
    every raw call so one blip retries instead of aborting. Returns the
    Response, or None when the status is in `ok_statuses` (e.g. a 404 on a
    delete = block already gone)."""
    import time as _t
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.request(method, url, headers=HEADERS,
                                 json=json_body, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            wait = min(2 ** attempt, 30)
            print(f"  ⚠ Notion network error (attempt {attempt}/{max_attempts}): {e} — sleeping {wait}s")
            _t.sleep(wait)
            continue
        if r.status_code in ok_statuses:
            return None
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            try:
                wait = float(r.headers.get("Retry-After", "")) or min(2 ** attempt, 30)
            except (ValueError, TypeError):
                wait = min(2 ** attempt, 30)
            print(f"  ⚠ Notion {r.status_code} (attempt {attempt}/{max_attempts}) — sleeping {wait}s")
            _t.sleep(wait)
            continue
        r.raise_for_status()
        return r
    if last_exc:
        raise last_exc
    raise RuntimeError("Notion request failed after retries")


def notion_search_page(title: str) -> str | None:
    """Search for an existing page by title. Returns page_id or None."""
    r = _notion_req(
        "POST", "https://api.notion.com/v1/search",
        json_body={"query": title, "filter": {"value": "page", "property": "object"}},
    )
    for result in r.json().get("results", []):
        page_title = result.get("properties", {}).get("title", {}).get("title", [])
        if page_title and page_title[0].get("text", {}).get("content", "") == title:
            if not result.get("archived", False):
                return result["id"]
    return None


def notion_create_page(title: str, parent_id: str) -> str:
    """Create a new page under a parent page. Returns page_id."""
    r = _notion_req(
        "POST", "https://api.notion.com/v1/pages",
        json_body={
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {"title": [{"text": {"content": title}}]}
            },
        },
    )
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
        # _notion_req retries transient network errors / 429 / 5xx; a 404
        # means the block is already gone, which we treat as success.
        _notion_req("DELETE", f"https://api.notion.com/v1/blocks/{block_id}",
                    ok_statuses=(404,))

    while True:
        r = _notion_req(
            "GET",
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
        )
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
        r = _notion_req("GET", url)
        data = r.json()
        blocks += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return blocks


def find_section_blocks(blocks: list[dict], heading_text: str) -> tuple[list[str], str | None]:
    """Find block IDs between a section heading and the next SECTION boundary.
    Returns (block_ids_to_delete, heading_block_id).

    The section ends at the next divider or the next heading whose level is the
    SAME OR HIGHER than the section heading (a sibling/parent section). Deeper
    sub-headings are part of this section and must be collected for clearing —
    e.g. the Weekend Planner emits 'Family Events'/'Adult Events' as heading_3
    blocks; if we stopped at the first one, the clear would bail immediately and
    each re-assemble would stack new content on top of the old (duplication)."""
    found_heading = False
    heading_id = None
    heading_level = 2
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
                    try:
                        heading_level = int(block_type.split("_")[1])
                    except (IndexError, ValueError):
                        heading_level = 2
                    continue
        else:
            if block_type == "divider":
                break
            if block_type.startswith("heading_"):
                try:
                    lvl = int(block_type.split("_")[1])
                except (IndexError, ValueError):
                    lvl = 1
                if lvl <= heading_level:
                    break  # next section at same/higher level — stop here
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
            _notion_req("DELETE", f"https://api.notion.com/v1/blocks/{block_id}",
                        ok_statuses=(404,))
        insert_after_id = heading_id

    if new_blocks:
        # Notion caps children at 100 per request, so insert in batches.
        # Thread the `after` cursor: each batch is inserted after the last
        # block created by the previous batch (the PATCH response returns the
        # created blocks in order), so the section stays correctly ordered.
        for i in range(0, len(new_blocks), 100):
            chunk = new_blocks[i:i + 100]
            try:
                r = _notion_req(
                    "PATCH",
                    f"https://api.notion.com/v1/blocks/{page_id}/children",
                    json_body={"children": chunk, "after": insert_after_id},
                )
            except Exception as e:
                print(f"  Failed to insert blocks: {e}")
                return False
            created = (r.json() or {}).get("results", [])
            if created:
                insert_after_id = created[-1]["id"]

    action = "Appended to" if append else "Updated"
    print(f"  ✓ {action} '{heading_text}' section ({len(new_blocks)} blocks)")
    return True


def notion_append_blocks(page_id: str, blocks: list[dict]) -> None:
    """Append blocks to a page. Notion limits to 100 blocks per call."""
    total = len(blocks)
    n_batches = (total + 99) // 100
    for bi, i in enumerate(range(0, total, 100), 1):
        chunk = blocks[i:i + 100]
        # DEBUG: show batch index, block-range, and the block-type histogram
        # so a rejected batch can be traced to the section that produced it.
        from collections import Counter as _Counter
        types = _Counter(b.get("type", "?") for b in chunk)
        type_summary = ", ".join(f"{t}×{c}" for t, c in types.most_common())
        print(f"    → append batch {bi}/{n_batches}: blocks {i}–{i + len(chunk) - 1} "
              f"({len(chunk)} blocks) [{type_summary}]")
        try:
            _notion_req(
                "PATCH",
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                json_body={"children": chunk},
            )
        except requests.HTTPError as e:
            # DEBUG: dump the failing batch's block types AND Notion's full
            # rejection reason so we can pinpoint the offending block.
            resp = e.response
            print(f"  ✗ Block append FAILED on batch {bi}/{n_batches} "
                  f"(blocks {i}–{i + len(chunk) - 1})")
            if resp is not None:
                print(f"    HTTP {resp.status_code}: {resp.text[:600]}")
            print(f"    batch block types: {dict(types)}")
            raise


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
        try:
            r = _notion_req("POST", url, json_body=payload)
        except requests.HTTPError as e:
            # A filtered query can 400 if a property/select changed — retry
            # once unfiltered. (_notion_req already retried timeouts/429/5xx.)
            if (e.response is not None and e.response.status_code == 400
                    and filters):
                print(f"  ⚠ Notion 400 on filtered query of {db_id[:8]}… — retrying unfiltered")
                return query_database(db_id, filters=None)
            raise
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


# How many restaurants the published newsletter features (single pick). The
# review app surfaces several candidates in Notion; only this many — the
# approved pick (or the default-winner fallback) — are rendered into the
# Notion page, the Beehiiv email, the subject-line context, and the "In
# Today's Connect" teaser.
FEATURED_RESTAURANT_COUNT = 1


def get_restaurants(newsletter_name: str) -> list[dict]:
    """Get this week's single featured restaurant for a newsletter.

    Returns at most FEATURED_RESTAURANT_COUNT rows (1).

    Priority order:
      1. The 'approved' pick — explicitly approved in the review app (legacy
         'Tier 1 Winner' rows are also honored during the transition).
      2. Default-winner fallback — when nothing is approved, the row flagged
         Default Winner=True (auto-picked by the pipeline) from the most
         recent batch.

    The fallback ensures Send-to-Beehiiv always has a restaurant to feature
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
        # "approved" is the single-pick status; "Tier 1 Winner" is accepted
        # for backward compatibility so the current week's legacy pick still
        # renders during the transition. (Tier 2 Winner is intentionally NOT
        # included — the section features one restaurant now.)
        if status in ("approved", "Tier 1 Winner"):
            winners.append(_restaurant_row_to_dict(props, status))

    if winners:
        # Keep only the most recent batch
        dates = [r["date"] for r in winners if r.get("date")]
        if dates:
            latest_date = max(dates)
            winners = [r for r in winners if r.get("date") == latest_date]
        winners.sort(key=lambda x: (0 if x["tier"] == "Tier 1 Winner" else 1, -(x["score"] or 0)))
        return winners[:FEATURED_RESTAURANT_COUNT]

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

    # Single featured restaurant: fall back to the default-winner row
    # (auto-flagged by the pipeline) from the most recent batch.
    default_winner = next(
        (props for _p, props, _s, _d in dated
         if props.get("Default Winner", {}).get("checkbox")), None)
    if not default_winner:
        return []
    fb = _restaurant_row_to_dict(default_winner, "approved")
    fb["_is_fallback"] = True
    print(f"  ⓘ Restaurants for {newsletter_name}: no approved pick — "
          f"using default-winner fallback: {fb['name']}")
    return [fb][:FEATURED_RESTAURANT_COUNT]


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
    raw_n = len(pages or [])
    # Filter by Newsletter in Python — NOT just on the query. query_database
    # falls back to an UNFILTERED fetch when Notion 400s the filtered query
    # ("retrying unfiltered"), which would otherwise leak other newsletters'
    # rows here and let a different (e.g. Lewisville) approved row win the sort.
    pages = [p for p in pages if
             (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
    nl_n = len(pages)
    # Only show current 'approved' rows (not 'approved - old' which are exclusion-only)
    statuses = [(p["properties"].get("Status", {}).get("select") or {}).get("name") for p in pages]
    pages = [p for p in pages if
             (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    if not pages:
        print(f"  [free-events] no approved {newsletter_name} row "
              f"(DB returned {raw_n} row(s); {nl_n} for this newsletter; "
              f"statuses={statuses})")
        return None
    # Sort by Date Generated, then Notion's page-level created_time as the
    # tiebreaker. Date Generated is date-only, so two same-day re-runs tie and
    # the stale row could win; created_time is a full timestamp that always
    # picks the newest row.
    pages.sort(
        key=lambda p: (p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
                       p.get("created_time", "")),
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


def get_latest_free_event_images(newsletter_name: str) -> list[str]:
    """Return up to 3 image URLs for the latest approved Free Event row.

    Reads the `Image URLs` rich_text column (" | "-separated, written by
    save_free_events_to_notion). Falls back to the single `Image URL` field
    for legacy rows that predate the gallery column. Returns [] if none."""
    if not NOTION_FREE_EVENTS_DB_ID:
        return []
    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return []
    # Filter by Newsletter in Python too — query_database may return UNFILTERED
    # rows when Notion 400s the filtered query (see get_latest_free_events).
    pages = [p for p in pages if
             (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
    pages = [p for p in pages if
             (p["properties"].get("Status", {}).get("select") or {}).get("name") == "approved"]
    if not pages:
        return []
    # Date Generated (date-only) ties on same-day re-runs; created_time (full
    # timestamp) breaks the tie so the GIF is built from the NEWEST row's
    # images, not a stale same-day row.
    pages.sort(
        key=lambda p: (p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
                       p.get("created_time", "")),
        reverse=True,
    )
    props = pages[0]["properties"]
    raw = "".join(t.get("plain_text", "") for t in
                  props.get("Image URLs", {}).get("rich_text", []))
    urls = [u.strip() for u in raw.split("|") if u.strip()]
    if not urls:
        single = props.get("Image URL", {}).get("url", "") or ""
        if single:
            urls = [single]
    return urls[:3]


def _sniff_image_ext(content: bytes, content_type: str) -> str:
    """Pick a file extension from magic bytes first (content-type is often a
    generic 'binary/octet-stream' for CDN-served originals), falling back to
    the Content-Type header, then .jpg."""
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    ct = (content_type or "").lower()
    return (".png" if "png" in ct else ".gif" if "gif" in ct
            else ".webp" if "webp" in ct else ".jpg")


def _rehost_free_event_image(url: str, slug: str) -> str:
    """Re-host a single external free-event image on gh-pages so it renders
    reliably in Beehiiv / email — a stable URL with a correct image/* content-
    type. Needed because some sources are hotlink-hostile or serve a generic
    content-type (e.g. the raw cdn.evbuc.com Eventbrite original →
    binary/octet-stream). Content-addressed + HEAD-cached so it's fetched once.
    Already-gh-pages URLs pass through. Returns the original on any failure."""
    if not url or f"{_GH_OWNER}.github.io" in url:
        return url
    key = hashlib.md5(url.encode()).hexdigest()[:10]
    base = f"https://{_GH_OWNER}.github.io/{_GH_REPO}/free_events/{slug}_img_{key}"
    # Reuse a prior re-host (any of the common extensions) without re-fetching.
    try:
        for ext in (".jpg", ".png", ".webp", ".gif"):
            if requests.head(f"{base}{ext}", timeout=8).status_code == 200:
                return f"{base}{ext}?v={key}"
    except Exception:
        pass
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124 Safari/537.36"})
        if r.status_code == 200 and r.content:
            ext = _sniff_image_ext(r.content, r.headers.get("Content-Type", ""))
            hosted = _publish_image_to_gh_pages(
                r.content, f"free_events/{slug}_img_{key}{ext}")
            if hosted:
                print(f"  ↳ [free-events] re-hosted image on gh-pages: "
                      f"{hosted.rsplit('/', 1)[-1]}")
                return f"{hosted}?v={key}"
        else:
            print(f"  ⚠ [free-events] image fetch for re-host returned "
                  f"HTTP {r.status_code} — using original URL")
    except Exception as e:
        print(f"  ⚠ [free-events] image re-host failed ({e}) — using original URL")
    return url


def free_event_render_images(newsletter_name: str) -> list[str]:
    """The image URL(s) to render for the Free Event of the Week.

    When the featured event has 2+ photos, combine them into a single
    animated GIF (cycling through the photos) hosted on gh-pages and return
    [gif_url] — so the section shows ONE cycling image instead of stacked
    photos. With 0-1 photos, returns the raw list unchanged.

    The gh-pages path is content-addressed (hash of the source URLs), so the
    GIF is built once and reused: a HEAD check returns the existing URL
    without rebuilding (and without importing Pillow), which lets the
    Beehiiv send reuse what the assembler already produced. Any failure
    (no token, Pillow missing, download error) falls back to the photos."""
    images = get_latest_free_event_images(newsletter_name)
    print(f"  [free-events] {len(images)} source image(s) from latest approved row:")
    for u in images:
        print(f"      • {u}")
    slug = re.sub(r"[^a-z0-9]+", "-", newsletter_name.lower()).strip("-")
    if len(images) < 2:
        # Single image: re-host external/hotlink-protected sources (e.g. the
        # raw cdn.evbuc.com Eventbrite original, which serves as
        # binary/octet-stream) on gh-pages so it renders reliably in Beehiiv /
        # email with a correct image/* content-type and a stable URL.
        return [_rehost_free_event_image(u, slug) for u in images]
    key  = hashlib.md5("|".join(images).encode()).hexdigest()[:10]
    path = f"free_events/{slug}_{key}.gif"
    public = f"https://{_GH_OWNER}.github.io/{_GH_REPO}/{path}"

    # Already built for this exact photo set? Reuse it without rebuilding.
    # (Content-addressed by the SOURCE URLs, so a different photo set always
    # yields a different path and forces a fresh build — see the key above.)
    try:
        if requests.head(public, timeout=10).status_code == 200:
            print(f"  [free-events] reusing existing GIF {path} (same photo set)")
            return [f"{public}?v={key}"]
    except Exception:
        pass

    try:
        from gif_maker import create_gif_from_urls
        gif_bytes = create_gif_from_urls(images, duration_ms=2000)
        if gif_bytes:
            hosted = _publish_image_to_gh_pages(gif_bytes, path)
            if hosted:
                print(f"  ✓ Free Events: combined {len(images)} photos into a GIF")
                return [f"{hosted}?v={key}"]
    except Exception as e:
        print(f"  ⚠ free-event GIF build failed, using individual photos: {e}")
    return images


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


def _strip_leading_greeting(blurb: str, greeting: str) -> str:
    """Drop a leading copy of the greeting from the blurb so the intro doesn't
    show e.g. 'What's up neighbors' twice — once as the bold greeting line and
    again as the first words of the editor's note (Claude sometimes repeats it
    in the blurb body). Matches on words, punctuation-insensitive, so the
    greeting 'What's up neighbors' also strips a blurb opening 'What's up,
    neighbors!'. Only strips a multi-word greeting, and re-capitalizes the
    remainder."""
    if not blurb or not greeting:
        return blurb
    g_words = re.findall(r"[a-z0-9']+", greeting.lower())
    if len(g_words) < 2:
        return blurb
    tokens = list(re.finditer(r"[a-z0-9']+", blurb.lower()))
    if [m.group(0) for m in tokens[:len(g_words)]] != g_words:
        return blurb
    rest = re.sub(r"^[\s,!.;:—–\-]+", "", blurb[tokens[len(g_words) - 1].end():])
    return (rest[0].upper() + rest[1:]) if rest else blurb


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
    # Filter by Newsletter in Python — query_database falls back to an
    # UNFILTERED fetch when Notion 400s the filtered query, so without this a
    # 400 would let another newsletter's intro win the date sort (e.g. LLL
    # rendering East Cobb's welcome). Same fix as the Free Events getters.
    pages = [p for p in pages
             if (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
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
            # Strip a duplicate greeting from the blurb body so it doesn't
            # render twice (bold greeting line + blurb opening).
            "blurb":             _strip_leading_greeting(blurb, greeting),
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


def _parse_business_candidates(rt_text: str) -> list[str]:
    """The Business Brief 'Image Candidates' column stores a JSON list of
    Google Places photo URLs. Return it as a list[str] (empty on any error)."""
    try:
        v = json.loads(rt_text) if rt_text else []
        return [u for u in v if isinstance(u, str) and u.strip()] if isinstance(v, list) else []
    except Exception:
        return []


def _url_is_live(url: str) -> bool:
    """True only when the URL currently returns 200 (follows redirects)."""
    if not url:
        return False
    try:
        r = requests.head(url, timeout=8, allow_redirects=True)
        if r.status_code in (403, 405):
            r = requests.get(url, timeout=10, allow_redirects=True, stream=True)
        return r.status_code == 200
    except Exception:
        return False


def _resolve_business_photo(photo_url: str, candidates: list[str],
                            newsletter: str, name: str) -> str:
    """Return a WORKING photo URL for a business brief.

    The stored Photo URL is sometimes a gh-pages GIF that was never published
    (the build/publish step is flaky) and now 404s — that's why a business
    can show no photo even though Google Places returned images. When the
    Photo URL is dead or empty, fall back to the first live Image Candidate
    and re-host it on gh-pages for a stable URL (or use it directly when
    hosting isn't available)."""
    if _url_is_live(photo_url):
        return photo_url
    for cand in candidates:
        if not _url_is_live(cand):
            continue
        try:
            r = requests.get(cand, timeout=15)
            if r.status_code == 200 and r.content:
                slug = re.sub(r"[^a-z0-9]+", "-", f"{newsletter}-{name}".lower()).strip("-")[:60] or "business"
                key = hashlib.md5(r.content).hexdigest()[:8]
                hosted = _publish_image_to_gh_pages(r.content, f"business_brief/{slug}.jpg")
                if hosted:
                    print(f"  ↻ business photo re-hosted from a live candidate ({name})")
                    return f"{hosted}?v={key}"
        except Exception:
            pass
        print(f"  ↻ business photo using live candidate directly ({name})")
        return cand
    if photo_url:
        print(f"  ⚠ business photo URL is dead and no live candidate found ({name})")
    return photo_url


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
        "image_candidates": _parse_business_candidates(_rt("Image Candidates")),
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
        pick = _business_brief_row_to_dict(approved[0]["properties"])
        pick["photo_url"] = _resolve_business_photo(
            pick.get("photo_url", ""), pick.get("image_candidates") or [],
            newsletter_name, pick.get("name", ""))
        return pick

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
    pick["photo_url"] = _resolve_business_photo(
        pick.get("photo_url", ""), pick.get("image_candidates") or [],
        newsletter_name, pick.get("name", ""))
    print(f"  ⓘ No approved business brief for {newsletter_name} — using highest-scored fallback: {pick.get('name')}")
    return pick


# ---------------------------------------------------------------------------
# WEEKEND PLANNER FORMATTERS
# ---------------------------------------------------------------------------

def display_domain(url: str) -> str:
    """Strip protocol, strip path/params/fragment, and strip a leading
    `www.` so links read as the bare root domain (e.g. `cityofmarietta.com`).
    Used for the visible anchor text in Weekend Planner event links."""
    if not url:
        return ""
    no_proto = url.split("://", 1)[-1]
    host = no_proto.split("/", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


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
        out.append(paragraph_block(r["name"], bold=True))
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

    # Import the shared og:image scraper so each story can carry a lead image
    # pulled from its first source article. Use event_image_scraper (a sibling
    # module, deps: requests only) rather than Free_Events, which transitively
    # imports anthropic — a package the assembler workflow doesn't install, so
    # that import silently failed and the section rendered text-only.
    try:
        from event_image_scraper import fetch_event_image as _fetch_img  # noqa: E402
    except Exception as e:
        print(f"  ⚠ [lowdown] image scraper unavailable ({e}) — rendering text-only")
        _fetch_img = None

    # The Full Section blob is markdown: each story is a '### {emoji} {headline}'
    # heading, a body, then an optional 'More: [label](url) | …' sources line.
    # Group lines into per-story blocks so we can insert the image right under
    # each headline (before its body).
    lines = lowdown_text.split("\n")
    stories: list[list[str]] = []
    preamble: list[str] = []
    cur: list[str] = []
    for line in lines:
        if line.strip().startswith("### "):
            if cur:
                stories.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
        else:
            preamble.append(line)
    if cur:
        stories.append(cur)

    out: list[dict] = []
    for para in preamble:
        para = para.strip()
        if para:
            out.append(paragraph_block(para))

    seen_imgs: set[str] = set()
    img_count = 0
    for story in stories:
        headline = story[0].strip().replace("### ", "")
        out.append(paragraph_block(headline, bold=True))

        # Extract source URLs from the markdown links in this story's body
        # (the 'More: [label](url)' line, mainly).
        source_urls: list[str] = []
        for ln in story[1:]:
            for m in re.finditer(r"\]\((https?://[^)\s]+)\)", ln):
                source_urls.append(m.group(1))

        # Scrape a lead image from the first source that yields one. Dedup on
        # the normalized URL so two stories never show the same publisher hero.
        if _fetch_img:
            for url in source_urls:
                try:
                    # Article hero only: validate it's a real image, never fall
                    # back to the publisher homepage logo, and skip the raw <img>
                    # scan (which grabs popups/ads). Correct photo or none.
                    img = _fetch_img(url, validate=True,
                                     allow_root_fallback=False, meta_only=True)
                except Exception:
                    img = ""
                if not img or not img.lower().startswith(("http://", "https://")):
                    continue
                norm = img.split("?")[0].rstrip("/").lower()
                if norm in seen_imgs:
                    continue
                seen_imgs.add(norm)
                out.append(image_block(img))
                img_count += 1
                print(f"  [lowdown] image for '{headline[:50]}': {img[:80]}")
                break

        for ln in story[1:]:
            ln = ln.strip()
            if not ln:
                continue
            if ln.startswith("### "):
                out.append(paragraph_block(ln.replace("### ", ""), bold=True))
            else:
                out.append(paragraph_block(ln))

    print(f"  [lowdown] {len(stories)} story(ies), {img_count} image(s) attached")
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
            # Ordinal suffix: 1st/2nd/3rd/…/11th–13th are all "th".
            n = dt.day
            suffix = "th" if 11 <= (n % 100) <= 13 else \
                {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
            return f"{day}, {dt.strftime('%B')} {n}{suffix} {dt.year}"
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
    # Lead with the event photo(s) — combined into a single cycling GIF when
    # there are 2+ pictures (see free_event_render_images).
    for img_url in free_event_render_images(newsletter_name):
        out.append(image_block(img_url))
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


_GH_OWNER = "peachyinsurance"
_GH_REPO  = "newsletters"


def _is_notion_temp_url(url: str) -> bool:
    """True for Notion-hosted (uploaded) file URLs, whose signed S3 links
    expire ~1 hour after they're generated — so embedding them directly in a
    Notion page leaves a broken image once they lapse."""
    u = (url or "").lower()
    return ("amazonaws.com" in u and
            ("x-amz-" in u or "notion-static" in u or "prod-files-secure" in u))


def _publish_image_to_gh_pages(image_bytes: bytes, path: str) -> str:
    """Commit `image_bytes` to gh-pages at `path` via the GitHub Contents API.
    Idempotent: skips the commit when the existing blob is byte-identical.
    gh-pages deploys use keep_files:true, so the file persists. Returns the
    permanent gh-pages URL, or "" on any failure (caller falls back)."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not image_bytes:
        return ""
    api    = f"https://api.github.com/repos/{_GH_OWNER}/{_GH_REPO}/contents/{path}"
    public = f"https://{_GH_OWNER}.github.io/{_GH_REPO}/{path}"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    # git blob sha = sha1("blob <len>\0<content>") — matches what the API returns.
    blob_sha = hashlib.sha1(b"blob %d\0" % len(image_bytes) + image_bytes).hexdigest()
    sha = None
    try:
        g = requests.get(api + "?ref=gh-pages", headers=headers, timeout=15)
        if g.status_code == 200:
            existing = g.json()
            if existing.get("sha") == blob_sha:
                return public  # already up to date — no commit
            sha = existing.get("sha")
    except Exception:
        pass
    body = {
        "message": f"sponsor logo: {path} [skip ci]",
        "content": base64.b64encode(image_bytes).decode(),
        "branch":  "gh-pages",
    }
    if sha:
        body["sha"] = sha
    try:
        p = requests.put(api, headers=headers, json=body, timeout=20)
        if p.status_code in (200, 201):
            return public
        print(f"  ⚠ sponsor logo publish failed: {p.status_code} {p.text[:160]}")
    except Exception as e:
        print(f"  ⚠ sponsor logo publish error: {e}")
    return ""


def _stabilize_image_url(url: str, newsletter: str, kind: str) -> str:
    """If `url` is a Notion-uploaded (expiring) file URL, download it and
    re-host it on gh-pages so the embedded image never expires. Permanent /
    external URLs pass through unchanged. Falls back to the original URL on
    any problem (e.g. no GITHUB_TOKEN)."""
    if not url or not _is_notion_temp_url(url):
        return url
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200 or not r.content:
            return url
        ct  = (r.headers.get("Content-Type") or "").lower()
        ext = (".png" if "png" in ct else
               ".jpg" if ("jpeg" in ct or "jpg" in ct) else
               ".gif" if "gif" in ct else
               ".svg" if "svg" in ct else
               ".webp" if "webp" in ct else ".png")
        slug = re.sub(r"[^a-z0-9]+", "-", newsletter.lower()).strip("-")
        path = f"sponsor_logos/{slug}_{kind}{ext}"
        permanent = _publish_image_to_gh_pages(r.content, path)
        if permanent:
            # cache-bust so a changed sponsor logo refreshes in-page
            return f"{permanent}?v={hashlib.md5(r.content).hexdigest()[:8]}"
    except Exception as e:
        print(f"  ⚠ sponsor logo re-host failed: {e}")
    return url


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
        "logo_url":  _stabilize_image_url(_extract_file_or_url(props.get("Logo") or {}), newsletter_name, "logo"),
        "image_url": _stabilize_image_url(_extract_file_or_url(props.get("images") or {}), newsletter_name, "image"),
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
            # Show the bare root domain as the link text — same format as the
            # other sections (Business Brief, Weekend Planner, Sponsor, …) —
            # instead of a generic 'Browse openings' CTA.
            domain = display_domain(url)
            prefix = "Visit" if r.get("bonus") else "Apply"
            out.append(paragraph_block_with_markdown(f"**{prefix}:** [{domain}]({url})"))
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
    try:
        _ev = get_featured_event(newsletter_name)
        if _ev and _ev.get("header_image_url"):
            blocks.append(image_block(_ev["header_image_url"]))
            print(f"  [build] hero header image: 1 block")
    except Exception as e:
        import traceback
        print(f"  ⚠ [build] hero header image failed: {e}")
        traceback.print_exc()

    blocks.extend([
        callout_block(
            f"Last updated: {today}\nCopy each section below into the newsletter template.",
            emoji="📋",
        ),
        divider_block(),
    ])
    # DEBUG: build each section under its own guard so ONE failing builder
    # surfaces its traceback and is skipped, instead of aborting the whole
    # rebuild after the page has already been cleared (which would leave the
    # page partially/empty). Per-section block counts make it obvious which
    # sections rendered content vs came back empty.
    section_counts: list[tuple[str, int]] = []
    for key in SECTION_ORDER:
        cfg = SECTIONS[key]
        blocks.append(heading_block(cfg["heading"]))
        try:
            body = cfg["builder"](newsletter_name)
        except Exception as e:
            import traceback
            print(f"  ⚠ [build] section '{key}' ({cfg['heading']}) FAILED: {e}")
            traceback.print_exc()
            body = []
        blocks.extend(body)
        blocks.append(divider_block())
        section_counts.append((key, len(body)))
        flag = "" if body else "  ← EMPTY"
        print(f"  [build] {key:<16} {cfg['heading']:<28} {len(body):>4} body block(s){flag}")
    # The last section's trailing divider is harmless; keeps consistency
    print(f"  [build] TOTAL {len(blocks)} blocks across {len(SECTION_ORDER)} sections "
          f"({sum(1 for _, n in section_counts if n == 0)} empty)")
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

        # Prefer the pinned canonical page ID from config — Notion's global
        # /v1/search is eventually-consistent and was failing to return
        # existing pages, so each run created a duplicate. A pinned ID makes
        # the assembler update the same page in place every time. Fall back to
        # search only for newsletters that don't have a landing_page_id yet.
        cfg = get_newsletter(newsletter_name) or {}
        page_id = cfg.get("landing_page_id") or notion_search_page(page_title)
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
