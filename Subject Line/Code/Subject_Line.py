"""
Subject Line generator. Runs after the Welcome Intro step in the weekly
newsletter chain. Pulls all the section data from Notion (intro,
featured event, tier-1 restaurant, top lowdown headline, pet, free
event), assembles the same context shape the subject-line skill expects,
calls Claude with `newsletter-subject-line_auto.md`, and writes the
result back to the latest Intro DB row's "Subject Line" field.

Send_To_Beehiiv still generates its own subject at send time per the
configured override rule — this step's job is to produce the subject
EARLY so it's visible in Notion alongside the intro for review/editing.

Env vars consumed (mirrors what Send_To_Beehiiv reads):
  CLAUDE_API_KEY
  NOTION_API_KEY
  NOTION_INTRO_DB_ID
  NOTION_EVENTS_DB_ID
  NOTION_RESTAURANTS_DB_ID
  NOTION_LOWDOWN_DB_ID
  NOTION_PETS_DB_ID
  NOTION_FREE_EVENTS_DB_ID
  NEWSLETTER             (e.g. East_Cobb_Connect, or 'all')
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

sys.path.append(str(Path(__file__).parent.parent.parent
                    / "NewsletterCreation" / "Code"))
from assemble_newsletter_page import (  # noqa: E402
    get_latest_intro,
    get_featured_event,
    get_restaurants,
    get_latest_lowdown,
    get_approved_pet,
    get_latest_free_events,
)
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_INTRO_DB_ID,
)

CLAUDE_API_KEY    = os.environ.get("CLAUDE_API_KEY", "")
NEWSLETTER        = os.environ.get("NEWSLETTER", "East_Cobb_Connect")
SUBJECT_SKILL_PATH = (Path(__file__).parent.parent.parent
                       / "Skills"
                       / "newsletter-subject-line_auto.md")


def load_skill_prompt() -> str:
    if SUBJECT_SKILL_PATH.exists():
        return SUBJECT_SKILL_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Subject-line skill not found at {SUBJECT_SKILL_PATH}")


def first_lowdown_headline(newsletter_name: str) -> str:
    """Pull the first headline out of the Local Lowdown markdown blob.
    Same parser shape Send_To_Beehiiv uses."""
    text = get_latest_lowdown(newsletter_name) or ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("###"):
            return s.lstrip("# ").strip()
    return ""


def build_context(newsletter_name: str) -> dict:
    """Assemble the context dict the subject-line skill expects.
    Any missing section comes back as None (or empty string) so Claude
    can pick from whichever hooks are available."""
    intro = get_latest_intro(newsletter_name)
    featured = get_featured_event(newsletter_name)
    restaurants = get_restaurants(newsletter_name) or []
    tier1 = next((r for r in restaurants if r.get("tier") == "Tier 1 Winner"), None)
    pet = get_approved_pet(newsletter_name)
    free_text = get_latest_free_events(newsletter_name) or ""
    free_title = ""
    for line in free_text.splitlines():
        if line.startswith("###"):
            free_title = line.lstrip("# ").strip()
            break
    top_news = first_lowdown_headline(newsletter_name)

    ctx = {
        "newsletter_name":  newsletter_name,
        "publication_date": datetime.today().strftime("%Y-%m-%d"),
        "intro": {
            "greeting": (intro or {}).get("greeting", ""),
            "blurb":    (intro or {}).get("blurb", ""),
        } if intro else None,
        "featured_event": {
            "name":  featured.get("event_name", ""),
            "date":  featured.get("date", ""),
            "venue": featured.get("venue", ""),
        } if featured and featured.get("event_name") else None,
        "tier1_restaurant": {
            "name":  tier1.get("name", ""),
            "blurb": (tier1.get("blurb") or "")[:200],
        } if tier1 else None,
        "top_news_headline": top_news,
        "pet": {
            "name":        (pet or {}).get("name", ""),
            "animal_type": (pet or {}).get("species", "") or (pet or {}).get("animal_type", ""),
        } if pet and pet.get("name") else None,
        "free_event": {
            "name": free_title,
        } if free_title else None,
    }
    return ctx


def call_claude(context: dict) -> str:
    """Single Claude call with the subject-line skill as system prompt.
    Strips quotes / leading whitespace; caps to one line."""
    skill = load_skill_prompt()
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    user_msg = (
        "Write the subject line for this issue. Output ONLY the subject "
        "string, no quotes, no preamble.\n\n"
        + json.dumps(context, indent=2)
    )
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=200,
                system=skill,
                messages=[{"role": "user", "content": user_msg}],
            )
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Claude error (attempt {attempt + 1}): {e}")
                time.sleep(8)
            else:
                raise
    raw = next((b.text for b in response.content if b.type == "text"), "").strip()
    raw = raw.strip('"\'')
    raw = raw.split("\n")[0].strip()
    return raw


def latest_intro_page_id(newsletter_name: str) -> str | None:
    """Find the most recent Intro DB row for this newsletter. We patch
    its Subject Line field. If there isn't one yet, return None — the
    caller can decide whether to fall back (e.g. log and exit cleanly)."""
    if not NOTION_INTRO_DB_ID:
        return None
    pages = query_database(NOTION_INTRO_DB_ID, filters={
        "property": "Newsletter",
        "select":   {"equals": newsletter_name},
    })
    if not pages:
        return None
    pages.sort(
        key=lambda p: (p["properties"].get("Date Generated", {}).get("date") or {}).get("start", ""),
        reverse=True,
    )
    return pages[0].get("id")


def save_subject_to_intro_row(page_id: str, subject: str) -> bool:
    """PATCH the Intro DB row's Subject Line rich_text field. Returns
    True on success. Auto-heal in update_page silently drops the field
    if the schema doesn't have a 'Subject Line' column yet, so caller
    should run the Setup Notion Databases workflow once to add it."""
    return update_page(page_id, {
        "Subject Line": {
            "rich_text": [{"type": "text", "text": {"content": subject}}],
        },
    })


def run_one(newsletter_name: str) -> int:
    print(f"\n{'=' * 60}")
    print(f"  Generating subject line for {newsletter_name}")
    print(f"{'=' * 60}")
    ctx = build_context(newsletter_name)
    available = [k for k, v in ctx.items() if v and k not in ("newsletter_name", "publication_date")]
    print(f"  Context available: {available}")
    if not available:
        print(f"  ⚠ No section data available — skipping subject for {newsletter_name}")
        return 0

    subject = call_claude(ctx)
    if not subject:
        print(f"  ✗ Claude returned empty subject for {newsletter_name}")
        return 1
    print(f"  📧 Subject: {subject}")

    page_id = latest_intro_page_id(newsletter_name)
    if not page_id:
        print(f"  ⚠ No Intro DB row found for {newsletter_name} — subject generated but not saved")
        return 0

    if save_subject_to_intro_row(page_id, subject):
        print(f"  ✓ Saved to Intro row {page_id[:8]}…")
        return 0
    print(f"  ✗ Failed to save to Intro row {page_id[:8]}…")
    return 1


def main() -> int:
    if NEWSLETTER.lower() == "all":
        targets = ["East_Cobb_Connect", "Perimeter_Post", "Lewisville_Lake_Lookout"]
    else:
        targets = [NEWSLETTER]
    rc = 0
    for nl in targets:
        rc = run_one(nl) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
