"""
Subject Line + Preview Text generator. Runs after the Welcome Intro
step in the weekly newsletter chain.

Pulls all the section data from Notion (intro, featured event, tier-1
restaurant, top lowdown headline, pet, free event), assembles the
context the subject-preview-text skill expects, calls Claude with
`newsletter-subject-preview-text_auto.md`, and writes the parsed
results back to the latest Intro DB row:

  - "Subject Line"  ← subject from the first option Claude returns
  - "Preview Text"  ← preview text from the same option

Send_To_Beehiiv reads the Subject Line for the email subject and
exposes Preview Text via {summary_text} for the in-body summary line.

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
from voice_helper import with_voice  # noqa: E402
SUBJECT_SKILL_PATH = (Path(__file__).parent.parent.parent
                       / "Skills"
                       / "newsletter-subject-preview-text_auto.md")


def load_skill_prompt() -> str:
    if SUBJECT_SKILL_PATH.exists():
        return SUBJECT_SKILL_PATH.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Subject-preview-text skill not found at {SUBJECT_SKILL_PATH}")


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


def call_claude(context: dict) -> tuple[str, str]:
    """Single Claude call with the subject+preview skill. Returns the
    (subject, preview) pair parsed from the FIRST option Claude emits.

    The skill outputs in this shape:
        ## Subject Line Options

        **Option 1 — Style B**
        Subject: Some Subject Line (38 chars)
        Preview: Preview text goes here

        **Option 2 — Style D**
        Subject: ...
        Preview: ...

    We pick option 1 by default (the first/recommended one). If the
    user wants A/B testing later, the saved subject can still be edited
    manually in Notion."""
    skill = load_skill_prompt()
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    user_msg = (
        "Generate subject line options for this newsletter edition, "
        "following the skill's output format exactly. We will use the "
        "first option's Subject + Preview as the published headline.\n\n"
        + json.dumps(context, indent=2)
    )
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1200,
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
    return parse_first_option(raw)


def parse_first_option(raw: str) -> tuple[str, str]:
    """Extract (subject, preview) from the first option in the skill's
    output. Tolerates minor formatting drift — looks for the first
    `Subject:` and the first `Preview:` line in the text."""
    if not raw:
        return "", ""
    subject = ""
    preview = ""
    m = re.search(r"^\s*Subject:\s*(.+?)\s*$", raw, re.IGNORECASE | re.MULTILINE)
    if m:
        subject = m.group(1).strip()
        # Strip a trailing "(NN chars)" / "(NN characters)" hint if Claude added one.
        subject = re.sub(r"\s*\(\d+\s*(?:chars?|characters?)\)\s*$", "", subject, flags=re.IGNORECASE)
        subject = subject.strip('"\'').strip()
    m = re.search(r"^\s*Preview:\s*(.+?)\s*$", raw, re.IGNORECASE | re.MULTILINE)
    if m:
        preview = m.group(1).strip().strip('"\'').strip()
    return subject, preview


def latest_intro_page_id(newsletter_name: str) -> str | None:
    """Find the most recent Intro DB row for this newsletter. We patch
    its Subject Line + Preview Text fields. If there isn't one yet,
    return None — the caller can decide whether to fall back (e.g. log
    and exit cleanly)."""
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


def save_to_intro_row(page_id: str, subject: str, preview: str) -> bool:
    """PATCH both Subject Line + Preview Text rich_text fields. Auto-heal
    in update_page silently drops fields whose columns don't exist in the
    Notion DB, so run Setup Notion Databases once before relying on Preview Text."""
    props: dict = {}
    if subject:
        props["Subject Line"] = {
            "rich_text": [{"type": "text", "text": {"content": subject[:1900]}}],
        }
    if preview:
        props["Preview Text"] = {
            "rich_text": [{"type": "text", "text": {"content": preview[:1900]}}],
        }
    if not props:
        return False
    return update_page(page_id, props)


def run_one(newsletter_name: str) -> int:
    print(f"\n{'=' * 60}")
    print(f"  Generating subject + preview for {newsletter_name}")
    print(f"{'=' * 60}")
    ctx = build_context(newsletter_name)
    available = [k for k, v in ctx.items() if v and k not in ("newsletter_name", "publication_date")]
    print(f"  Context available: {available}")
    if not available:
        print(f"  ⚠ No section data available — skipping for {newsletter_name}")
        return 0

    subject, preview = call_claude(ctx)
    if not subject and not preview:
        print(f"  ✗ Claude returned no usable output for {newsletter_name}")
        return 1
    print(f"  📧 Subject: {subject!r}  ({len(subject)} chars)")
    print(f"  🔍 Preview: {preview!r}  ({len(preview)} chars)")

    page_id = latest_intro_page_id(newsletter_name)
    if not page_id:
        print(f"  ⚠ No Intro DB row found for {newsletter_name} — generated but not saved")
        return 0

    if save_to_intro_row(page_id, subject, preview):
        print(f"  ✓ Saved subject + preview to Intro row {page_id[:8]}…")
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
