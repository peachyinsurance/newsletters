#!/usr/bin/env python3
"""
Newsletter Automation - Reader Poll
Generates a 4-option Beehiiv-ready reader poll per newsletter where each option
maps to a sponsorable local-business category. Target categories used in the
past 8 weeks are excluded so we build a heat map across many advertiser verticals.
"""
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import save_poll_to_notion, get_used_poll_categories


# ---------------------------------------------------------------------------
# 1. CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-poll-designer_auto.md"

LOOKBACK_WEEKS = 8

NEWSLETTERS = [
    {"name": "East_Cobb_Connect", "display_area": "East Cobb"},
    {"name": "Perimeter_Post",    "display_area": "Perimeter"},
]


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a newsletter poll designer. Generate a 4-option reader poll mapped to local-business advertiser categories."


# ---------------------------------------------------------------------------
# 3. CLAUDE GENERATE
# ---------------------------------------------------------------------------
def generate_poll(newsletter_name: str, display_area: str, excluded_categories: set,
                  pub_date: str, skill_prompt: str) -> dict:
    """Ask Claude to design a 4-option poll avoiding the given categories."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    user_payload = {
        "newsletter_name":     newsletter_name,
        "publication_date":    pub_date,
        "coverage_area":       display_area,
        "excluded_categories": sorted(excluded_categories),
    }

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""Design ONE reader poll for this week's {display_area} newsletter.

{json.dumps(user_payload, indent=2)}

Avoid every category in `excluded_categories` (these were used in the past 8 weeks).
If you cannot find 4 fully-fresh options, reuse the LEAST-recently-used category and
note it in `dropped_categories` with reason "recycled (oldest available)".

Return ONLY valid JSON, no preamble or markdown fences."""
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
        return {}

    return result


# ---------------------------------------------------------------------------
# 4. VALIDATE
# ---------------------------------------------------------------------------
def validate_poll(result: dict, excluded_categories: set) -> dict:
    """Sanity-check the poll structure and warn about excluded-category collisions.
    Returns the (possibly-annotated) result. Does NOT block on collision — pipeline
    accepts a recycled category but logs which ones overlapped."""
    options = result.get("options", []) or []
    if len(options) != 4:
        print(f"  ⚠ Expected 4 options, got {len(options)}")

    overlapping = []
    seen_cats = set()
    for opt in options:
        cats = [c.strip().lower() for c in (opt.get("categories") or []) if c]
        for c in cats:
            if c in excluded_categories:
                overlapping.append(c)
            seen_cats.add(c)

    if overlapping:
        print(f"  ⚠ Recycled categories from exclusion list: {sorted(set(overlapping))}")
    else:
        print(f"  ✓ All 4 options use fresh categories")

    # Also warn if duplicate categories across options
    flat = []
    for opt in options:
        for c in (opt.get("categories") or []):
            flat.append(c.strip().lower())
    duplicates = {c for c in flat if flat.count(c) > 1}
    if duplicates:
        print(f"  ⚠ Duplicate categories across options: {sorted(duplicates)}")

    return result


# ---------------------------------------------------------------------------
# 5. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Reader Poll automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()
    pub_date = datetime.today().strftime("%Y-%m-%d")

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        excluded = get_used_poll_categories(newsletter["name"], lookback_weeks=LOOKBACK_WEEKS)
        print(f"  Excluded categories ({len(excluded)} from past {LOOKBACK_WEEKS} weeks): {sorted(excluded)[:15]}{'…' if len(excluded) > 15 else ''}")

        print("\n  Generating poll with Claude...")
        result = generate_poll(
            newsletter_name=newsletter["name"],
            display_area=newsletter["display_area"],
            excluded_categories=excluded,
            pub_date=pub_date,
            skill_prompt=skill_prompt,
        )
        if not result or not result.get("options"):
            print(f"  No poll generated for {newsletter['name']}. Skipping.")
            continue

        result = validate_poll(result, excluded)

        # Print human-readable preview
        print(f"\n  📊 {result.get('question', '?')}")
        for opt in result.get("options", []):
            cats = ", ".join(opt.get("categories", []))
            print(f"    • {opt.get('text', '?')}  →  {cats}")

        # Save
        print()
        save_poll_to_notion(result, newsletter["name"])

        # Local backup
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        json_file = output_dir / f"poll_{newsletter['name']}_{datetime.today().strftime('%Y%m%d')}.json"
        json_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  ✓ Saved JSON to {json_file}")

    print(f"\nAll newsletters complete.")
