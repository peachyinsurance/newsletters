#!/usr/bin/env python3
"""
Newsletter Automation - Welcome Intro Section
Generates the opening blurb / editor's note for each newsletter.
Uses a two-pass Claude pipeline:
  Pass 1: Generate the blurb from newsletter context
  Pass 2: Self-review against voice rules, revise if needed
Saves results to Notion.
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
from notion_helper import (
    HEADERS as NOTION_HEADERS,
    query_database,
    save_intro_to_notion,
)

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]

# Database IDs for reading context from other sections
NOTION_RESTAURANTS_DB_ID  = os.environ.get("NOTION_RESTAURANTS_DB_ID", "")
NOTION_PETS_DB_ID         = os.environ.get("NOTION_PETS_DB_ID", "")
NOTION_LOWDOWN_DB_ID      = os.environ.get("NOTION_LOWDOWN_DB_ID", "")
NOTION_RE_DB_ID           = os.environ.get("NOTION_RE_DB_ID", "")
NOTION_EVENTS_DB_ID       = os.environ.get("NOTION_EVENTS_DB_ID", "")
NOTION_FREE_EVENTS_DB_ID  = os.environ.get("NOTION_FREE_EVENTS_DB_ID", "")

SKILL_PROMPT_PATH  = Path(__file__).parent.parent.parent / "Skills" / "newsletter-welcome-intro-skill_auto.md"
REVIEW_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-welcome-intro-review-skill.md"

NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "display_area": "East Cobb",
    },
    {
        "name":         "Perimeter_Post",
        "display_area": "Perimeter",
    },
]


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPTS
# ---------------------------------------------------------------------------
def load_prompt(path: Path, fallback: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    print(f"  Warning: skill prompt not found at {path}, using fallback")
    return fallback


# ---------------------------------------------------------------------------
# 3. GATHER NEWSLETTER CONTEXT FROM NOTION
# ---------------------------------------------------------------------------
def _rt(props: dict, key: str) -> str:
    rt = props.get(key, {}).get("rich_text", [])
    return "".join(chunk.get("text", {}).get("content", "") for chunk in rt) if rt else ""


def get_featured_event(newsletter_name: str) -> dict | None:
    """Get the approved featured event for this newsletter."""
    if not NOTION_EVENTS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select": {"equals": newsletter_name}
        })
        approved = [p for p in pages if
                    (p["properties"].get("Status", {}).get("select") or {}).get("name", "") == "approved"]
        if not approved:
            return None
        approved.sort(
            key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
            reverse=True,
        )
        props = approved[0]["properties"]
        return {
            "name":  _rt(props, "Event Name"),
            "date":  _rt(props, "Date"),
            "time":  _rt(props, "Time"),
            "venue": _rt(props, "Venue"),
            "price": _rt(props, "Price"),
            "blurb": _rt(props, "Blurb")[:400],
        }
    except Exception:
        return None


def get_tier1_restaurant(newsletter_name: str) -> dict | None:
    """Get the Tier 1 Winner restaurant for this newsletter."""
    if not NOTION_RESTAURANTS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
            "property": "Newsletter",
            "select": {"equals": newsletter_name}
        })
        tier1 = [p for p in pages if
                 (p["properties"].get("Status", {}).get("select") or {}).get("name", "") == "Tier 1 Winner"]
        if not tier1:
            return None
        props = tier1[0]["properties"]
        name = (props.get("Name", {}).get("title") or [{}])[0].get("text", {}).get("content", "")
        # Title is stored as "Newsletter - Restaurant Name" — take the tail
        if " - " in name:
            name = name.split(" - ", 1)[-1]
        return {
            "name":    name,
            "cuisine": (props.get("Cuisine", {}).get("select") or {}).get("name", ""),
            "blurb":   _rt(props, "Blurb")[:400],
        }
    except Exception:
        return None


def get_approved_pet(newsletter_name: str) -> dict | None:
    """Get the approved adoptable pet for this newsletter."""
    if not NOTION_PETS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_PETS_DB_ID, filters={
            "property": "Newsletter",
            "select": {"equals": newsletter_name}
        })
        approved = [p for p in pages if
                    (p["properties"].get("Status", {}).get("select") or {}).get("name", "") == "approved"]
        if not approved:
            return None
        props = approved[0]["properties"]
        name = (props.get("Name", {}).get("title") or [{}])[0].get("text", {}).get("content", "")
        if " - " in name:
            name = name.split(" - ", 1)[-1]
        return {
            "name":        name,
            "animal_type": (props.get("Animal Type", {}).get("select") or {}).get("name", ""),
            "shelter":     _rt(props, "Shelter"),
            "blurb":       _rt(props, "Blurb")[:400],
        }
    except Exception:
        return None


def get_top_free_event(newsletter_name: str) -> dict | None:
    """Get the top free event (first one in the Full Section markdown)."""
    if not NOTION_FREE_EVENTS_DB_ID:
        return None
    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select": {"equals": newsletter_name}
        })
        if not pages:
            return None
        pages.sort(
            key=lambda p: p["properties"].get("Date Generated", {}).get("date", {}).get("start", ""),
            reverse=True,
        )
        props = pages[0]["properties"]
        section = _rt(props, "Full Section")
        if not section:
            return None
        # Parse first ### heading + following paragraph for a single event teaser
        lines = [ln.strip() for ln in section.split("\n") if ln.strip()]
        title = ""
        body  = ""
        for line in lines:
            if line.startswith("### ") and not title:
                title = line.removeprefix("### ").strip()
            elif title and not line.startswith("###") and not line.startswith("More:"):
                body = line[:300]
                break
        if not title:
            return None
        return {"name": title, "details": body}
    except Exception:
        return None


def gather_context(newsletter_name: str) -> dict:
    """Pull focused context for the Welcome Intro.
    Priority: featured event, Tier 1 restaurant, adoptable pet.
    Fill (optional): top free event if space allows."""
    context = {
        "newsletter_name": newsletter_name,
        "publication_date": datetime.today().strftime("%Y-%m-%d"),
        "sections_summary": {},
    }

    event = get_featured_event(newsletter_name)
    if event:
        context["sections_summary"]["featured_event"] = event

    restaurant = get_tier1_restaurant(newsletter_name)
    if restaurant:
        context["sections_summary"]["tier1_restaurant"] = restaurant

    pet = get_approved_pet(newsletter_name)
    if pet:
        context["sections_summary"]["adoptable_pet"] = pet

    free = get_top_free_event(newsletter_name)
    if free:
        context["sections_summary"]["top_free_event"] = free

    return context


# ---------------------------------------------------------------------------
# 4. CLAUDE PASS 1: GENERATE BLURB
# ---------------------------------------------------------------------------
def generate_blurb(context: dict, skill_prompt: str) -> dict:
    """Use Claude to generate the welcome intro blurb."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    context_json = json.dumps(context, indent=2)

    display_area = context["newsletter_name"].replace("_", " ")

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=skill_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""Write the welcome intro blurb for this week's {display_area} newsletter.

The blurb MUST mention, in this priority order:
1. The featured event (sections_summary.featured_event) — biggest item, always mention
2. The Tier 1 restaurant (sections_summary.tier1_restaurant) — always mention
3. The adoptable pet (sections_summary.adoptable_pet) — always mention
4. The top free event (sections_summary.top_free_event) — ONLY if the blurb still has room
   within the 150-250 word count. Skip it rather than pad.

If any of #1–#3 is missing from the context, just skip it gracefully (don't invent).

Here is the newsletter context:

{context_json}

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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    result = json.loads(clean)

    print(f"  Pass 1 — Generated blurb: {result.get('word_count', '?')} words")
    return result


# ---------------------------------------------------------------------------
# 5. CLAUDE PASS 2: SELF-REVIEW
# ---------------------------------------------------------------------------
def review_blurb(blurb_result: dict, review_prompt: str) -> dict:
    """Use Claude to review and optionally revise the generated blurb."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    blurb_json = json.dumps(blurb_result, indent=2)

    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=3000,
                system=review_prompt,
                messages=[{
                    "role": "user",
                    "content": f"""Review this welcome intro blurb against the voice and quality rules.

{blurb_json}

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

    raw = next(block.text for block in response.content if block.type == "text")
    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    review = json.loads(clean)

    passed = review.get("pass", False)
    score = review.get("score", 0)
    violations = review.get("violations", [])

    print(f"  Pass 2 — Review: {'PASS' if passed else 'FAIL'} (score: {score}/10)")
    if violations:
        for v in review.get("violation_details", violations):
            print(f"    ✗ {v}")

    return review


# ---------------------------------------------------------------------------
# 6. MERGE RESULTS
# ---------------------------------------------------------------------------
def merge_results(blurb_result: dict, review: dict) -> dict:
    """Merge generation and review results. Use revised version if review failed."""
    passed = review.get("pass", False)
    score = review.get("score", 0)
    violations = review.get("violations", [])

    if not passed and review.get("revised_blurb"):
        # Use the revised version
        greeting = review.get("revised_greeting") or blurb_result.get("greeting", "")
        blurb = review["revised_blurb"]
        word_count = len(blurb.split())
        print(f"  Using revised blurb ({word_count} words)")
    else:
        # Use the original
        greeting = blurb_result.get("greeting", "")
        blurb = blurb_result.get("blurb", "")
        word_count = blurb_result.get("word_count", len(blurb.split()))

    return {
        "newsletter_name": blurb_result.get("newsletter_name", ""),
        "publication_date": blurb_result.get("publication_date", ""),
        "greeting": greeting,
        "blurb": blurb,
        "word_count": word_count,
        "review_score": score,
        "review_violations": ", ".join(violations) if violations else "",
        "review_passed": passed,
    }


# ---------------------------------------------------------------------------
# 7. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Welcome Intro automation — {datetime.today().strftime('%Y-%m-%d')}")

    skill_prompt = load_prompt(
        SKILL_PROMPT_PATH,
        "You are a casual, neighbor-style newsletter writer. Write a 150-250 word opening blurb."
    )
    review_prompt = load_prompt(
        REVIEW_PROMPT_PATH,
        "You are an editor reviewing a newsletter blurb for voice and quality."
    )

    for newsletter in NEWSLETTERS:
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Gather context from other newsletter sections
        print("  Gathering newsletter context from Notion...")
        context = gather_context(newsletter["name"])

        sections = context.get("sections_summary", {})
        if sections:
            for key, val in sections.items():
                # val is now a dict (structured object) — print its 'name' field for preview
                if isinstance(val, dict):
                    preview = val.get("name", "") or str(val)[:80]
                else:
                    preview = str(val)[:80]
                print(f"    {key}: {preview}")
        else:
            print("    No section context available (other pipelines may not have run yet)")

        # Pass 1: Generate
        print("\n  Pass 1 — Generating blurb...")
        blurb_result = generate_blurb(context, skill_prompt)

        # Pass 2: Review
        print("\n  Pass 2 — Reviewing blurb...")
        review = review_blurb(blurb_result, review_prompt)

        # Merge and pick final version
        final = merge_results(blurb_result, review)

        # Save to Notion
        print(f"\n  Saving to Notion...")
        save_intro_to_notion(final, newsletter["name"])

        print(f"\n  Done with {newsletter['name']}.")
        print(f"  Final: {final['word_count']} words, score {final['review_score']}/10"
              f"{' (revised)' if not final['review_passed'] else ''}")

    print(f"\nAll newsletters complete.")
