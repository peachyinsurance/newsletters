#!/usr/bin/env python3
"""In Search Of pipeline — Claude blurb writer for local job listings.

Reads the In Search Of Notion DB for the target newsletter, sends each
row to Claude to rewrite the scraped snippet into a neighborly hiring
blurb following the skill's voice + format rules, and PATCHes the row's
Description field with the rewritten copy.

Approved-first / non-archived fallback (same pattern as Weekend Planner):
  1. Query rows where Status='approved'. Use these.
  2. If no approved rows, fall back to Status='pending'.
  3. Skip 'rejected' and 'archived' entirely.

The assembler reads the same DB at render time and pulls
approved/pending rows with the AI-written Description field.

Env vars:
  CLAUDE_API_KEY              required
  NOTION_API_KEY              required
  NOTION_IN_SEARCH_OF_DB_ID   required
  NEWSLETTER                  optional (default 'all')
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import (  # noqa: E402
    HEADERS as NOTION_HEADERS,
    query_database,
    update_page,
    NOTION_IN_SEARCH_OF_DB_ID,
)
from newsletters_config import filter_by_env  # noqa: E402
from claude_json import call_with_json_output  # noqa: E402


CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
from voice_helper import with_voice  # noqa: E402
SKILL_PROMPT_PATH = (Path(__file__).parent.parent.parent
                     / "Skills" / "newsletter-in-search-of-skill_auto.md")


def load_skill_prompt() -> str:
    if not SKILL_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Skill not found at {SKILL_PROMPT_PATH}")
    return SKILL_PROMPT_PATH.read_text(encoding="utf-8")


def _rich_text_value(prop) -> str:
    if not isinstance(prop, dict):
        return ""
    chunks = prop.get("rich_text") or prop.get("title") or []
    return "".join(c.get("plain_text", "") for c in chunks).strip()


def fetch_jobs_pool(newsletter_name: str) -> list[dict]:
    """Pull all rows for this newsletter (any non-archived/rejected
    status). The pipeline prefers `approved` rows but falls back to
    `pending` when no approvals exist yet — mirrors the Weekend Planner
    approved-first pattern."""
    if not NOTION_IN_SEARCH_OF_DB_ID:
        print("  ⚠ NOTION_IN_SEARCH_OF_DB_ID not set; nothing to do")
        return []
    filters = {
        "and": [
            {"property": "Newsletter", "select": {"equals": newsletter_name}},
            {"property": "Status", "select": {"does_not_equal": "archived"}},
            {"property": "Status", "select": {"does_not_equal": "rejected"}},
            {"property": "Status", "select": {"does_not_equal": "approved - old"}},
        ]
    }
    pages = query_database(NOTION_IN_SEARCH_OF_DB_ID, filters=filters) or []
    out: list[dict] = []
    for p in pages:
        props = p.get("properties", {})
        out.append({
            "notion_page_id":   p.get("id"),
            "status":           (props.get("Status", {}).get("select") or {}).get("name", ""),
            "employer":         _rich_text_value(props.get("Employer")),
            "scraped_snippet":  _rich_text_value(props.get("Scraped Snippet")),
            "job_listings_url": (props.get("Job Listings URL", {}).get("url") or "").strip(),
            "image_url":        (props.get("Image URL", {}).get("url") or "").strip(),
            "city":             _rich_text_value(props.get("City")),
            "bonus":            bool((props.get("Bonus", {}) or {}).get("checkbox")),
            "current_description": _rich_text_value(props.get("Description")),
        })
    return out


def select_pool(rows: list[dict]) -> list[dict]:
    """Approved-first: if any approved rows exist, use only those.
    Otherwise fall back to pending. Caller hands us all non-archived
    non-rejected rows already; this just stratifies."""
    approved = [r for r in rows if r["status"] == "approved"]
    if approved:
        print(f"    Using {len(approved)} approved row(s)")
        return approved
    pending = [r for r in rows if r["status"] == "pending"]
    if pending:
        print(f"    No approved rows; falling back to {len(pending)} pending row(s)")
        return pending
    return []


def call_claude(pool: list[dict], newsletter_name: str, skill_prompt: str) -> list[dict]:
    """Send the pool to Claude. Returns list of {candidate_index, blurb,
    roles, bonus, drop, drop_reason}."""
    if not pool:
        return []
    indexed = [
        {
            "candidate_index": i + 1,
            "employer":         r["employer"],
            "scraped_snippet":  r["scraped_snippet"],
            "city":             r["city"],
            "newsletter":       newsletter_name,
            "is_resource_hint": r["bonus"],
        }
        for i, r in enumerate(pool)
    ]
    user_prompt = f"""
Newsletter: {newsletter_name.replace('_', ' ')}
Rows to write: {len(indexed)}

Write each row per the skill's voice + format rules. Default to including;
only drop on 404 / off-topic / inappropriate content. NEVER fabricate
salary / bonus / role specifics that aren't in the scraped_snippet.

Rows:
{json.dumps(indexed, indent=2, ensure_ascii=False)}
"""
    try:
        results = call_with_json_output(
            api_key=CLAUDE_API_KEY,
            system=with_voice(skill_prompt),
            user_content=user_prompt,
        )
    except Exception as e:
        print(f"  ✗ Claude error: {e}")
        return []
    return results or []


def apply_results(pool: list[dict], results: list[dict]) -> tuple[int, int]:
    """PATCH each pool row's Description with the Claude blurb. Returns
    (updated, dropped) counts."""
    by_index = {i + 1: r for i, r in enumerate(pool)}
    updated, dropped = 0, 0
    for res in results:
        idx = res.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        row = by_index.get(idx) if idx is not None else None
        if not row:
            print(f"    ✗ invalid candidate_index {idx}; skipping")
            continue
        if res.get("drop"):
            print(f"    ✗ dropping '{row['employer']}': {res.get('drop_reason', 'no reason')}")
            dropped += 1
            # Mark Status=rejected so the next run doesn't re-process it
            try:
                update_page(row["notion_page_id"], properties={
                    "Status": {"select": {"name": "rejected"}},
                })
            except Exception as e:
                print(f"      (couldn't update Status to rejected: {e})")
            continue
        blurb = (res.get("blurb") or "").strip()
        roles = (res.get("roles") or "").strip()
        bonus = bool(res.get("bonus"))
        if not blurb:
            print(f"    ⚠ empty blurb for '{row['employer']}'; leaving row alone")
            continue
        # PATCH Description (Claude blurb) + Roles + Bonus checkbox
        try:
            update_page(row["notion_page_id"], properties={
                "Description": {"rich_text": [{"text": {"content": blurb[:2000]}}]},
                "Roles":       {"rich_text": [{"text": {"content": roles[:300]}}]},
                "Bonus":       {"checkbox": bonus},
            })
            updated += 1
            print(f"    ✓ wrote blurb for '{row['employer']}' ({len(blurb)} chars)")
        except Exception as e:
            print(f"    ✗ PATCH failed for '{row['employer']}': {e}")
    return updated, dropped


def main() -> int:
    skill_prompt = load_skill_prompt()
    print("In Search Of pipeline — Claude blurb pass")

    for newsletter in filter_by_env():
        nl_name = newsletter["name"]
        print(f"\n{'=' * 60}")
        print(f"Processing: {nl_name} ({newsletter['display_area']})")
        print(f"{'=' * 60}")

        rows = fetch_jobs_pool(nl_name)
        if not rows:
            print(f"  No In Search Of rows for {nl_name}; skipping")
            continue
        pool = select_pool(rows)
        if not pool:
            print(f"  No approved or pending rows for {nl_name}; skipping")
            continue
        results = call_claude(pool, nl_name, skill_prompt)
        if not results:
            print(f"  Claude returned nothing for {nl_name}")
            continue
        updated, dropped = apply_results(pool, results)
        print(f"  ✓ {updated} updated, {dropped} dropped for {nl_name}")
        time.sleep(0.5)

    print("\nAll newsletters complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
