"""
"In Today's Connect" teaser-section generator. Chained after the Welcome
Intro (and Subject Line) generation step in the weekly newsletter chain.

Pulls the FULL edition content from Notion — every section that could
contribute a teaser line — so Claude has the raw material to write a
curiosity-driven 5-8 line preview list. Calls Claude with
newsletter-in-todays-connect-skill.md as the system prompt and saves
the rendered markdown to the latest Intro DB row's "In Todays Connect"
rich_text field.

Sections fed into context:
  - intro greeting + blurb (so tone matches)
  - featured event (name, date, venue, blurb)
  - tier-1 restaurant (name, blurb, cuisine)
  - other restaurants (names + 1-line blurbs)
  - top 5 lowdown story headlines (+ short body snippets)
  - real estate listings (tier, headline, price, beds)
  - adoptable pet (name, species, shelter)
  - free event (title + when)
  - business brief (name, blurb)

Env vars (mirrors Subject_Line.py + adds RE and Business Brief):
  CLAUDE_API_KEY, NOTION_API_KEY
  NOTION_INTRO_DB_ID, NOTION_EVENTS_DB_ID, NOTION_RESTAURANTS_DB_ID,
  NOTION_LOWDOWN_DB_ID, NOTION_PETS_DB_ID, NOTION_FREE_EVENTS_DB_ID,
  NOTION_RE_DB_ID, NOTION_BUSINESS_BRIEF_DB_ID
  NEWSLETTER (East_Cobb_Connect | Perimeter_Post | Lewisville_Lake_Lookout | all)
"""
import json
import os
import re
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
    get_real_estate,
    get_latest_lowdown,
    get_approved_pet,
    get_latest_free_events,
    get_business_brief,
)
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_INTRO_DB_ID,
)

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
NEWSLETTER     = os.environ.get("NEWSLETTER", "East_Cobb_Connect")
from voice_helper import with_voice  # noqa: E402
SKILL_PATH     = (Path(__file__).parent.parent.parent
                  / "Skills"
                  / "newsletter-in-todays-connect-skill.md")


def load_skill_prompt() -> str:
    if SKILL_PATH.exists():
        return SKILL_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"In-Today's-Connect skill not found at {SKILL_PATH}")


def parse_lowdown_headlines(text: str, limit: int = 5) -> list[dict]:
    """Pull the first `limit` ### headlines + their first body line from
    the Local Lowdown markdown blob. The skill needs enough context to
    judge which story is most interesting."""
    if not text:
        return []
    out: list[dict] = []
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("### "):
            if section:
                out.append(section)
                if len(out) >= limit:
                    break
            section = {"headline": s.lstrip("# ").strip(), "snippet": ""}
        elif section is not None and not section["snippet"] and s:
            section["snippet"] = s[:200]
    if section and len(out) < limit:
        out.append(section)
    return out[:limit]


def parse_free_event(text: str) -> dict | None:
    """First free event block out of the markdown."""
    if not text:
        return None
    first = text.split("\n\n###")[0]
    lines = [ln for ln in first.splitlines() if ln.strip()]
    if not lines:
        return None
    return {
        "title": lines[0].lstrip("# ").strip(),
        "when":  lines[1].strip() if len(lines) > 1 else "",
    }


def build_context(newsletter_name: str) -> dict:
    """Assemble the FULL edition snapshot the skill needs to pick 5-8
    teaser hooks across all sections."""
    intro = get_latest_intro(newsletter_name) or {}
    featured = get_featured_event(newsletter_name) or {}
    restaurants = get_restaurants(newsletter_name) or []
    tier1 = next((r for r in restaurants if r.get("tier") == "Tier 1 Winner"), None)
    others = [r for r in restaurants if r.get("tier") != "Tier 1 Winner"]
    re_listings = get_real_estate(newsletter_name) or []
    pet = get_approved_pet(newsletter_name) or {}
    free_event = parse_free_event(get_latest_free_events(newsletter_name) or "")
    lowdown_stories = parse_lowdown_headlines(get_latest_lowdown(newsletter_name) or "")
    business = get_business_brief(newsletter_name) or {}

    return {
        "newsletter_name":  newsletter_name,
        "publication_date": datetime.today().strftime("%Y-%m-%d"),
        "intro": {
            "greeting": intro.get("greeting", ""),
            "blurb":    (intro.get("blurb") or "")[:400],
        } if intro else None,
        "featured_event": {
            "name":  featured.get("event_name", ""),
            "date":  featured.get("date", ""),
            "venue": featured.get("venue", ""),
            "blurb": (featured.get("blurb") or "")[:300],
        } if featured.get("event_name") else None,
        "tier1_restaurant": {
            "name":  tier1.get("name", ""),
            "blurb": (tier1.get("blurb") or "")[:200],
        } if tier1 else None,
        "other_restaurants": [
            {"name": r.get("name", ""), "blurb": (r.get("blurb") or "")[:120]}
            for r in others[:4]
        ],
        "top_news_stories": lowdown_stories,
        "real_estate": [
            {
                "tier":  l.get("tier", ""),
                "price": l.get("price", 0),
                "beds":  l.get("beds", 0),
                "headline": l.get("headline", ""),
                "address":  l.get("address", ""),
            }
            for l in re_listings
        ],
        "pet": {
            "name":    pet.get("name", ""),
            "species": pet.get("species", "") or pet.get("animal_type", ""),
            "shelter": pet.get("shelter", ""),
            "blurb":   (pet.get("blurb") or "")[:200],
        } if pet.get("name") else None,
        "free_event": free_event,
        "business_brief": {
            "name":  business.get("name", ""),
            "city":  business.get("city", ""),
            "blurb": (business.get("blurb") or "")[:200],
        } if business.get("name") else None,
    }


def call_claude(context: dict) -> str:
    """Single Claude call. Returns the full multi-line teaser block
    (the bold header + 5-8 emoji-led lines, blank-line-separated, as the
    skill specifies)."""
    skill = load_skill_prompt()
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    user_msg = (
        "Write the 'In Today's Connect' teaser section for this edition. "
        "Follow the skill rules exactly — emoji-led lines, no section "
        "labels, 5-8 lines, sorted by interest, light/pet closer.\n\n"
        + json.dumps(context, indent=2)
    )
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                system=with_voice(skill),
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
    # Strip code-fence wrapping if Claude added it despite instructions.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw.strip()


def latest_intro_page_id(newsletter_name: str) -> str | None:
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


def save_to_intro_row(page_id: str, teaser_md: str) -> bool:
    """Notion rich_text caps at 2000 chars per chunk; this teaser is tiny
    so a single chunk is fine."""
    return update_page(page_id, {
        "In Todays Connect": {
            "rich_text": [{"type": "text", "text": {"content": teaser_md[:1900]}}],
        },
    })


def run_one(newsletter_name: str) -> int:
    print(f"\n{'=' * 60}")
    print(f"  Generating 'In Today's Connect' for {newsletter_name}")
    print(f"{'=' * 60}")
    ctx = build_context(newsletter_name)
    populated = [k for k, v in ctx.items()
                 if v and k not in ("newsletter_name", "publication_date")]
    print(f"  Context available: {populated}")
    if not populated:
        print(f"  ⚠ No section data — skipping {newsletter_name}")
        return 0

    teaser = call_claude(ctx)
    if not teaser:
        print(f"  ✗ Claude returned empty teaser for {newsletter_name}")
        return 1
    print(f"  ↳ Generated ({len(teaser.splitlines())} lines):")
    for line in teaser.splitlines():
        print(f"    {line}")

    page_id = latest_intro_page_id(newsletter_name)
    if not page_id:
        print(f"  ⚠ No Intro DB row found for {newsletter_name} — teaser generated but not saved")
        return 0
    if save_to_intro_row(page_id, teaser):
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
