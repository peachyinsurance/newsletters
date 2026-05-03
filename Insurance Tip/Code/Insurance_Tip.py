#!/usr/bin/env python3
"""
Newsletter Automation - Insurance Tip Section
Searches trusted consumer-insurance sources, uses Claude to score and pick
the best educational tip for a shared audience, then saves the same picks
as Notion rows for both newsletters.
"""
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from brave_search import search_web
from claude_json import call_with_json_output, ClaudeJSONError
from notion_helper import save_tips_to_notion, get_existing_tip_urls, get_existing_tip_subjects
from url_validator import filter_valid_items

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY     = os.environ["CLAUDE_API_KEY"]
BRAVE_NEWS_API_KEY = os.environ["BRAVE_NEWS_API_KEY"]

SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-insurance-tip-skill_auto.md"

TOPICS_PER_RUN = 5
TARGET_TIPS    = 3

TRUSTED_DOMAINS = {
    "iii.org",
    "naic.org",
    "consumerreports.org",
    "nerdwallet.com",
    "forbes.com",
    "policygenius.com",
    "oid.ga.gov",
    "ready.gov",
    "fema.gov",
}

# Topic pool — (query_text, category, seasonal_months)
# seasonal_months = None means evergreen. Otherwise a set of months (1-12) when it's most relevant.
TOPIC_POOL = [
    # Auto
    ("auto insurance deductibles explained",                "auto",      None),
    ("gap insurance when you need it",                      "auto",      None),
    ("comprehensive vs collision coverage",                 "auto",      None),
    ("usage-based auto insurance discounts",                "auto",      None),
    ("teen driver auto insurance tips",                     "auto",      {6, 7, 8, 9}),
    ("roadside assistance coverage",                        "auto",      {6, 7, 8, 12}),
    # Home
    ("replacement cost vs actual cash value home insurance","home",      None),
    ("home insurance dwelling coverage limits",             "home",      None),
    ("home inventory for insurance claims",                 "home",      None),
    ("water damage home insurance coverage",                "home",      {3, 4, 5, 6, 7, 8, 9, 10}),
    ("wind and hail damage home insurance",                 "home",      {3, 4, 5, 6, 7, 8, 9}),
    ("home insurance coverage for home business",           "home",      None),
    # Flood
    ("flood insurance separate policy FEMA",                "flood",     {5, 6, 7, 8, 9, 10}),
    ("flood insurance Georgia NFIP",                        "flood",     {5, 6, 7, 8, 9, 10}),
    # Umbrella
    ("umbrella insurance policy when you need one",         "umbrella",  None),
    ("personal liability coverage limits",                  "umbrella",  None),
    # Life
    ("term life vs whole life insurance",                   "life",      None),
    ("how much life insurance coverage do you need",        "life",      None),
    ("life insurance beneficiary review",                   "life",      None),
    # Seasonal / specialty
    ("hurricane preparation insurance checklist",           "seasonal",  {5, 6, 7, 8, 9, 10, 11}),
    ("winter storm home insurance tips",                    "seasonal",  {11, 12, 1, 2}),
    ("holiday liability insurance hosting guests",          "seasonal",  {11, 12}),
    ("wildfire home insurance Georgia",                     "seasonal",  {5, 6, 7, 8, 9, 10}),
    # Life-event triggers
    ("new home insurance what to review",                   "life_event", None),
    ("marriage insurance policy review",                    "life_event", None),
    ("baby on the way insurance checklist",                 "life_event", None),
]

# Both newsletters get the same tip — define them here so we save one row per newsletter.
NEWSLETTERS = [
    {
        "name":         "East_Cobb_Connect",
        "display_area": "East Cobb",
        "demographics": {
            "median_income":    "$118,000",
            "median_age":       "42",
            "family_skew":      "Mix of established families and empty nesters. Many kids are teens or college-age.",
            "homeownership":    "78%",
            "education":        "65% bachelor's degree or higher",
        },
    },
    {
        "name":         "Perimeter_Post",
        "display_area": "Perimeter",
        "demographics": {
            "median_income":    "$105,000",
            "median_age":       "38",
            "family_skew":      "Mix of young professionals, young families, and empty nesters. More adult-skewing than East Cobb.",
            "homeownership":    "55%",
            "education":        "70% bachelor's degree or higher",
        },
    },
]


# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return (
        "You are a local newsletter writer picking educational insurance tips. "
        "Write short, warm, neighbor-style tips in Peachy Insurance's voice."
    )


# ---------------------------------------------------------------------------
# 3. PICK TOPICS FOR THIS RUN
# ---------------------------------------------------------------------------
def pick_topics_for_run(n: int = TOPICS_PER_RUN) -> list[tuple]:
    """
    Pick N topics for this week, shared across both newsletters.
    Seasonal topics are preferred when in season. Deterministic by ISO week
    so reruns within the same week stay consistent.
    """
    today = datetime.today()
    month = today.month
    iso_week = today.isocalendar().week

    in_season = [t for t in TOPIC_POOL if t[2] is not None and month in t[2]]
    evergreen = [t for t in TOPIC_POOL if t[2] is None]

    def seed_sort(topic):
        key = f"week-{iso_week}-{topic[0]}".encode()
        return hashlib.md5(key).hexdigest()

    in_season.sort(key=seed_sort)
    evergreen.sort(key=seed_sort)

    picks = in_season[:max(1, n // 2)] + evergreen
    picks = picks[:n]
    print(f"  Selected {len(picks)} topics (in-season first):")
    for topic, category, _ in picks:
        print(f"    [{category}] {topic}")
    return picks


# ---------------------------------------------------------------------------
# 4. CLAUDE: SCORE AND WRITE BLURBS (SHARED ACROSS BOTH NEWSLETTERS)
# ---------------------------------------------------------------------------
def build_claude_user_prompt(
    candidates: list[dict],
    newsletters: list[dict],
    previously_covered: list[dict] | None = None,
) -> str:
    """Build the Claude user-message content for the shared-tip prompt."""
    audience_sections = []
    for nl in newsletters:
        d = nl["demographics"]
        audience_sections.append(
            f"--- {nl['display_area']} ({nl['name'].replace('_', ' ')}) ---\n"
            f"Median household income: {d['median_income']}\n"
            f"Median age: {d['median_age']}\n"
            f"Family skew: {d['family_skew']}\n"
            f"Homeownership rate: {d['homeownership']}\n"
            f"Education level: {d['education']}"
        )
    demo_summary = "\n\n".join(audience_sections)

    today = datetime.today()
    pub_context = (
        f"Today is {today.strftime('%A, %B %d, %Y')}. "
        f"The newsletter publishes this week. "
        f"Current month is {today.strftime('%B')} — weight seasonal topics accordingly."
    )

    if previously_covered:
        prior_json = json.dumps(previously_covered, indent=2)
        prior_section = f"""
Previously covered in the last 6 months (across both newsletters). Each item has topic,
tip_title, summary, and date. Do NOT write another tip on the same subject UNLESS you can
approach it from a substantively different angle. Examples of "different angle":
 - Prior tip was about WHEN you need gap insurance → your tip is about what it DOESN'T cover.
 - Prior tip was HOW to build a home inventory → your tip is how to VALUE items in it.
 - Prior tip framed as a homeowner decision → your tip framed as a renter's parallel.
If no fresh angle is possible, skip the candidate. When you do pick a repeat subject with a
new angle, state the angle explicitly in `scoring_notes` (e.g., "Repeat subject of gap
insurance, new angle: coverage exclusions rather than when-you-need-it").

Previously covered:
{prior_json}
"""
    else:
        prior_section = ""

    candidates_json = json.dumps(candidates, indent=2)
    return f"""
{pub_context}

This tip will run in BOTH newsletters below. Pick tips that work for BOTH audiences.
If you have to trade off, favor the tip whose category aligns with where the audiences
overlap most (e.g., home-ownership tips still land because both newsletters have
meaningful homeowner populations).

Audiences:
{demo_summary}
{prior_section}
Below are insurance tip candidates pulled from trusted consumer-insurance sources.
Each candidate has a topic, category, source URL, source domain, title, and summary.

Your job:
1. Pick the top {TARGET_TIPS} tips that work for BOTH audiences, applying the guardrails
   and scoring rules in your instructions.
2. For each pick, write a polished blurb following the skill's format and voice rules
   exactly (short title, 3-5 sentence body, soft Peachy CTA, "Learn more" line).
3. Also write a 1-2 sentence `summary` capturing the SUBJECT and ANGLE of the tip (not the
   voice). This is used for future dedup — future runs will read it to judge whether a
   new candidate is a repeat subject. Keep it factual, specific, and under 300 characters.
4. Diversify categories — do not return two tips from the same category unless there
   are no good alternatives.
5. Score each 1-10 on relevance, actionability, timeliness.

Return ONLY a JSON array with no preamble, explanation, or markdown fences. Exact format:
[
  {{
    "topic": "Home Insurance - Coverage Limits",
    "category": "home",
    "tip_title": "Short headline",
    "blurb": "Full blurb text including CTA and learn-more line, following skill format",
    "summary": "1-2 sentence factual summary of the subject and angle for future dedup.",
    "source_url": "https://...",
    "source_name": "Insurance Information Institute",
    "relevance_score": 9,
    "actionability_score": 8,
    "timeliness_score": 6,
    "scoring_notes": "Why this tip fits these audiences right now..."
  }}
]

If fewer than {TARGET_TIPS} tips qualify, return fewer. If none qualify, return an empty array [].

Candidates:
{candidates_json}
"""


def score_and_sort(results: list[dict]) -> list[dict]:
    for r in results:
        r["total_score"] = (
            r.get("relevance_score", 0) +
            r.get("actionability_score", 0) +
            r.get("timeliness_score", 0)
        )
    results.sort(key=lambda x: x["total_score"], reverse=True)
    for r in results:
        print(f"  {r.get('tip_title', '')}: {r['total_score']}/30 "
              f"(rel:{r.get('relevance_score',0)} "
              f"act:{r.get('actionability_score',0)} "
              f"time:{r.get('timeliness_score',0)})")
    return results


# ---------------------------------------------------------------------------
# 5. FLAG DEFAULT WINNER
# ---------------------------------------------------------------------------
def flag_default_winner(results: list[dict]) -> list[dict]:
    for r in results:
        r["default_winner"] = ""
    if results:
        results[0]["default_winner"] = "yes"
        print(f"  Default winner: {results[0].get('tip_title')} ({results[0]['total_score']}/30)")
    return results


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Insurance Tip automation — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    topics = pick_topics_for_run()

    # Build query specs: each query carries its topic + category through to the result rows
    query_specs = [{"q": topic, "topic": topic, "category": category}
                   for topic, category, _ in topics]

    candidates = search_web(
        query_specs=query_specs,
        api_key=BRAVE_NEWS_API_KEY,
        trusted_domains=TRUSTED_DOMAINS,
    )
    print(f"  {len(candidates)} trusted candidates collected")

    if not candidates:
        print("No tip candidates found. Exiting.")
        sys.exit(0)

    # Union of existing URLs across all newsletters — no source reused anywhere
    existing_urls = set()
    for nl in NEWSLETTERS:
        existing_urls |= get_existing_tip_urls(nl["name"])
    if existing_urls:
        before = len(candidates)
        candidates = [c for c in candidates if c["url"] not in existing_urls]
        print(f"  Filtered {before - len(candidates)} previously-used URLs (union across both newsletters)")
    if not candidates:
        print("All candidates were previously used. Exiting.")
        sys.exit(0)

    # Previously-covered subjects (last 6 months, union across both newsletters).
    # Deduped by (topic, tip_title) so an identical tip saved twice (one row per
    # newsletter) shows up once in the prompt.
    seen_keys = set()
    previously_covered = []
    for nl in NEWSLETTERS:
        for s in get_existing_tip_subjects(nl["name"], months_back=6):
            key = (s.get("topic", ""), s.get("tip_title", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            previously_covered.append(s)
    print(f"  {len(previously_covered)} prior tip subjects loaded for Claude dedup context")

    print(f"\n  Validating {len(candidates)} candidate URLs...")
    candidates, rejected = filter_valid_items(
        candidates,
        critical_fields=["url"],
        optional_fields=[],
        label_field="title",
    )
    if rejected:
        print(f"  Dropped {len(rejected)} candidates with dead URLs")
    if not candidates:
        print("No candidates with valid URLs. Exiting.")
        sys.exit(0)

    print(f"\n  Sending {len(candidates)} candidates to Claude (one pick for both newsletters)...")
    user_prompt = build_claude_user_prompt(candidates, NEWSLETTERS, previously_covered)
    try:
        results = call_with_json_output(
            api_key=CLAUDE_API_KEY,
            system=skill_prompt,
            user_content=user_prompt,
        )
    except ClaudeJSONError as e:
        print(f"  ⚠ Claude returned unparseable output for Insurance Tip: {e}")
        print("  Skipping Insurance Tip generation this run.")
        sys.exit(0)

    if not results:
        print("Claude found no qualifying tips. Exiting.")
        sys.exit(0)

    results = score_and_sort(results)
    results = flag_default_winner(results)

    # Save the same picks once per newsletter (two identical-content rows).
    # Only the first newsletter's rows carry default_winner=yes — the tip is
    # shared, so flagging both would show up as "two winners" in the global
    # Notion view. Subsequent newsletters get the same tips with the flag cleared.
    for i, nl in enumerate(NEWSLETTERS):
        print(f"\n{'='*60}")
        print(f"Saving for: {nl['name']}")
        print(f"{'='*60}")
        if i == 0:
            save_tips_to_notion(results, nl["name"])
        else:
            unflagged = [{**r, "default_winner": ""} for r in results]
            save_tips_to_notion(unflagged, nl["name"])

    # Local JSON backup (one file — content is identical across newsletters)
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    json_file = output_dir / f"tips_{datetime.today().strftime('%Y%m%d')}.json"
    json_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"  Saved JSON to {json_file}")

    print(f"\nAll newsletters complete.")
