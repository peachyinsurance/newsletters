#!/usr/bin/env python3
"""
Newsletter Automation - Send issue to Beehiiv.

Pulls all section data from Notion (using the same get_* helpers the assembler
uses, so manual edits flow through), uploads images to Beehiiv's media library,
fills the template-post placeholders, creates a draft post, and (optionally)
attaches a native Beehiiv poll.

Env vars (required):
  CLAUDE_API_KEY                — for subject-line generation
  NOTION_API_KEY                — Notion API
  NOTION_*_DB_ID                — all Notion DBs the assembler reads
  BEEHIIV_API_KEY
  BEEHIIV_ECC_PUBLICATION_ID
  BEEHIIV_ECC_TEMPLATE_POST_ID

Env vars (optional):
  NEWSLETTER  — "East_Cobb_Connect" (default; only ECC supported in v1)
  STATUS      — "draft" (default), "scheduled", or "confirmed"
"""
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Reuse Notion data fetchers from the assembler
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from assemble_newsletter_page import (
    get_latest_intro,
    get_featured_event,
    get_restaurants,
    get_real_estate,
    get_latest_lowdown,
    get_approved_pet,
    get_latest_free_events,
    get_latest_free_event_image,
    get_latest_free_event_images,
    free_event_render_images,
    get_latest_poll,
    get_weekend_events,
    get_business_brief,
    get_latest_tip,
    get_memes,
    get_sponsor,
    display_domain,
    sync_edits_back,
    notion_search_page,
)

import anthropic
from beehiiv_client import BeehiivClient, BeehiivError


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]

NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")
STATUS     = os.environ.get("STATUS", "draft")


# Per-newsletter Beehiiv config — built dynamically from the central
# newsletters_config so adding a new newsletter only requires editing
# that one dict. Each newsletter's beehiiv_env_tag drives which env vars
# hold its publication + template post IDs.
def _build_newsletter_config() -> dict:
    from newsletters_config import NEWSLETTERS_DICT, beehiiv_credentials
    out: dict[str, dict] = {}
    for name, nl in NEWSLETTERS_DICT.items():
        creds = beehiiv_credentials(name)
        out[name] = {
            "publication_id":   creds["publication_id"],
            "template_post_id": creds["template_post_id"],
            "display_area":     nl.get("display_area", ""),
            "poll_vote_base":   nl.get("poll_vote_base", ""),
        }
    return out

NEWSLETTER_CONFIG = _build_newsletter_config()

from voice_helper import with_voice  # noqa: E402
SUBJECT_SKILL_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-subject-line_auto.md"


# ---------------------------------------------------------------------------
# 1. SUBJECT LINE
# ---------------------------------------------------------------------------
def load_skill_prompt(path: Path, fallback: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    print(f"  ⚠ skill prompt not found at {path}, using fallback")
    return fallback


def generate_subject_line(context: dict) -> str:
    """Single Claude call to produce a punchy 6-12 word subject line."""
    skill = load_skill_prompt(
        SUBJECT_SKILL_PATH,
        "You write punchy 6-12 word email subject lines. Output only the subject string.",
    )
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    user = json.dumps(context, indent=2)
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=200,
                system=with_voice(skill),
                messages=[{
                    "role":    "user",
                    "content": f"Write the subject line for this issue. Output ONLY the subject string, no quotes, no preamble.\n\n{user}",
                }],
            )
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Subject-line Claude error (attempt {attempt + 1}): {e}")
                time.sleep(8)
            else:
                raise
    raw = next((b.text for b in response.content if b.type == "text"), "").strip()
    # Strip surrounding quotes if Claude added them despite instructions
    raw = raw.strip('"\'')
    # Cap to one line
    raw = raw.split("\n")[0].strip()
    return raw


# ---------------------------------------------------------------------------
# 2. IMAGE UPLOAD
# ---------------------------------------------------------------------------
def _guess_content_type(url: str) -> str:
    ct, _ = mimetypes.guess_type(url)
    return ct or "image/png"


# Module-level cache: once Beehiiv tells us media upload isn't available on this
# plan, stop trying and just pass through the original URL. Beehiiv's email engine
# fetches external image URLs at send time, so this works fine.
_BEEHIIV_UPLOAD_DISABLED = False


def upload_remote_image(client: BeehiivClient, publication_id: str, url: str) -> str:
    """Try to upload an image to Beehiiv's media library, fall back to passing through
    the original URL if Beehiiv's media endpoint isn't available on this plan."""
    global _BEEHIIV_UPLOAD_DISABLED
    if not url:
        return ""
    if _BEEHIIV_UPLOAD_DISABLED:
        return url  # already determined Beehiiv upload isn't available — just pass through
    try:
        r = requests.get(url, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 (newsletter-automation)"})
        if r.status_code != 200 or not r.content:
            print(f"    ✗ Image fetch failed ({r.status_code}): {url[:60]}")
            return url
        filename = url.split("/")[-1].split("?")[0] or "image.png"
        ct = _guess_content_type(filename)
        hosted = client.upload_image(publication_id, r.content, filename, ct)
        print(f"    ✓ Uploaded {filename} → {hosted[:80]}")
        return hosted
    except BeehiivError as e:
        # If the FIRST upload returns 404, the media API isn't enabled on this plan.
        # Disable for the rest of the run and just return original URLs.
        if "404" in str(e):
            _BEEHIIV_UPLOAD_DISABLED = True
            print(f"    ⓘ Beehiiv media upload not available (404) — using original URLs")
        else:
            print(f"    ⚠ Beehiiv upload error for {url[:50]}: {e}")
        return url
    except Exception as e:
        print(f"    ⚠ Image upload error for {url[:50]}: {e}")
        return url


# ---------------------------------------------------------------------------
# 3. PLACEHOLDER REPLACEMENT
# ---------------------------------------------------------------------------
def _placeholder_variants(key: str) -> list[str]:
    """Beehiiv's editor stores curly braces as HTML entities, so `{key}` in the
    UI may be `&#123;key&#125;`, `&lbrace;key&rbrace;`, or even split across
    span tags. Return every form we should try to match.
    """
    inner = key
    return [
        "{" + inner + "}",
        "&#123;" + inner + "&#125;",
        "&#x7b;" + inner + "&#x7d;",
        "&lbrace;" + inner + "&rbrace;",
        "&lcub;" + inner + "&rcub;",
    ]


# Keys whose values are full URLs. When Beehiiv stores these in a link/button
# URL field, it auto-prepends `http://` (e.g. typed `{key}` → saved as
# `http://{key}`). We need to clean those scheme-prepended forms in addition
# to the bare placeholder.
URL_TYPED_KEYS = {
    # canonical keys
    "event_of_the_week_link",
    "free_event_link_1",
    "PET_SOURCE_URL",
    "restaurant_radar_url",
    "restaurant_radar_2_url",
    "restaurant_radar_3_url",
    "real_estate_starter_link",
    "real_estate_sweetspot_link",
    "real_estate_showcase_link",
    "local_lowdown_link",
    "business_brief_url",
    "business_brief_link",
    "insurance_tip_url",
    "insurance_tip_link",
    "insurance_tip_sponsor_url",
    "insurance_tip_sponsor_link",
    "sponsor_url",
    "sponsor_link",
    # (Poll URLs intentionally NOT here — we use `{poll_option_N_slug}`
    # embedded in the template URL field, e.g.
    # `https://www.eastcobbconnect.com/?vote={poll_option_1_slug}`. Beehiiv
    # treats the full URL as valid and doesn't auto-prepend a scheme.)
    # aliases (shorter forms users typed in URL fields)
    "event_of_the_week",
    "free_event_link",
}


# Slots that should auto-prune when their primary content key is empty.
# Each entry is (primary_key, anchor_token). The anchor_token is the
# placeholder string we search for in the HTML to locate the slot's container.
# Walking up to the nearest <tr> from the anchor identifies the slot's row/card.
PRUNEABLE_SLOTS = [
    # Restaurants — primary key is the name; if empty, drop the whole card.
    # Multiple anchors per card so we mop up every cell of a deep
    # Beehiiv table layout (cells for name, message, url, address each
    # sit under a different innermost container).
    ("restaurant_radar_name",      "restaurant_radar_name"),
    ("restaurant_radar_name",      "restaurant_radar_message"),
    ("restaurant_radar_name",      "restaurant_radar_url"),
    ("restaurant_radar_2_name",    "restaurant_radar_2_name"),
    ("restaurant_radar_2_name",    "restaurant_radar_2_message"),
    ("restaurant_radar_2_name",    "restaurant_radar_2_url"),
    ("restaurant_radar_3_name",    "restaurant_radar_3_name"),
    ("restaurant_radar_3_name",    "restaurant_radar_3_message"),
    ("restaurant_radar_3_name",    "restaurant_radar_3_url"),
    ("restaurant_radar_4_name",    "restaurant_radar_4_name"),
    ("restaurant_radar_4_name",    "restaurant_radar_4_message"),
    ("restaurant_radar_4_name",    "restaurant_radar_4_url"),
    ("restaurant_radar_5_name",    "restaurant_radar_5_name"),
    ("restaurant_radar_5_name",    "restaurant_radar_5_message"),
    ("restaurant_radar_5_name",    "restaurant_radar_5_url"),
    # Local Lowdown — slots are now expanded dynamically by
    # expand_lowdown_slots (clones one template card per parsed story),
    # so the prune-on-empty machinery is no longer needed here.
    # Local Events
    ("local_event_date_1",         "local_event_date_1"),
    ("local_event_date_2",         "local_event_date_2"),
    ("local_event_date_3",         "local_event_date_3"),
    ("local_event_date_4",         "local_event_date_4"),
    ("local_event_date_5",         "local_event_date_5"),
    # Free Event (only 1 today, future-proof more)
    ("free_event_title_1",         "free_event_title_1"),
    # Featured event — if no event approved, drop the whole featured-event card
    ("event_of_the_week_headline", "event_of_the_week_headline"),
    # Pet
    ("PET_NAME",                   "PET_NAME"),
    # Business Brief — if no business approved, drop the whole card.
    ("business_brief_name",        "business_brief_name"),
    ("business_brief_name",        "business_brief_blurb"),
    ("business_brief_name",        "business_brief_url"),
    # Insurance Tip — if no tip picked, drop the whole card.
    ("insurance_tip_title",        "insurance_tip_title"),
    ("insurance_tip_title",        "insurance_tip_blurb"),
    ("insurance_tip_title",        "insurance_tip_url"),
    # Sponsor Corner — if no approved sponsor, drop the whole card.
    ("sponsor_name",               "sponsor_name"),
    ("sponsor_name",               "sponsor_blurb"),
    ("sponsor_name",               "sponsor_url"),
    # Real Estate — three tiers (Starter / Sweet Spot / Showcase). Each tier's
    # data is the listing URL; if a tier has no listing, the URL is empty and
    # the whole tier card gets pruned.
    ("real_estate_starter_link",    "real_estate_starter_link"),
    ("real_estate_sweetspot_link",  "real_estate_sweetspot_link"),
    ("real_estate_showcase_link",   "real_estate_showcase_link"),
    # Poll — anchor on `poll_question`; if no poll for this issue, drop the
    # whole poll block. Then anchor on each option label individually so unused
    # option rows (e.g. poll has 3 options, slots 4-5 in template) disappear.
    ("poll_question",              "poll_question"),
    ("poll_option_1_label",        "poll_option_1_label"),
    ("poll_option_2_label",        "poll_option_2_label"),
    ("poll_option_3_label",        "poll_option_3_label"),
    ("poll_option_4_label",        "poll_option_4_label"),
    ("poll_option_5_label",        "poll_option_5_label"),
]


def prune_empty_slots(html: str, replacements: dict[str, str]) -> str:
    """Remove repeating-slot containers whose primary content wasn't filled.

    Strategy: for each slot whose primary_key has no value in replacements,
    locate the anchor placeholder text (e.g., `{restaurant_radar_4_name}`) in
    the HTML and remove the smallest enclosing structural element (preferring
    <tr>, falling back to <table> or <div>). Beehiiv emails use deeply nested
    table layouts, so a single <tr> typically wraps one card/row.

    Falls back gracefully (no-op) if BeautifulSoup isn't installed.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠ beautifulsoup4 not installed; skipping slot pruning")
        return html

    soup = BeautifulSoup(html, "html.parser")
    pruned = 0
    for primary_key, anchor in PRUNEABLE_SLOTS:
        if (replacements.get(primary_key) or "").strip():
            continue  # slot has data — keep
        # Try every encoded form Beehiiv might use
        variants = _placeholder_variants(anchor)
        for variant in variants:
            # find_all with `string=` callable searches text node content
            text_nodes = soup.find_all(string=lambda s, v=variant: bool(s) and v in str(s))
            if not text_nodes:
                continue
            target = text_nodes[0]
            # Walk up the DOM looking for the right container.
            # Prefer <tr>; if not present, take the nearest <table>; else <div>.
            container = (
                target.find_parent("tr")
                or target.find_parent("table")
                or target.find_parent("div")
            )
            # Guard: only decompose if we actually got a named structural
            # element (not the root document or a degenerate tag). This
            # prevents accidentally nuking the whole soup if BeautifulSoup
            # returns something unexpected from find_parent().
            if container and container.name in ("tr", "table", "div"):
                container.decompose()
                pruned += 1
                print(f"    ✓ pruned slot: {primary_key} (anchor='{anchor}', container=<{container.name}>)")
                break
            elif container:
                print(f"    ⚠ skipped prune for {primary_key}: unexpected container <{container.name!r}>")
        else:
            # no variant found in HTML — slot wasn't in template, nothing to prune
            pass
    print(f"  Empty-slot pruning: removed {pruned} unused slots")
    return str(soup)


def replace_placeholders(html: str, replacements: dict[str, str]) -> str:
    """Replace `{placeholder}` tokens (in any HTML-encoded form) with values.
    For URL-typed keys, also replace `http://{key}` / `https://{key}` patterns
    that Beehiiv produces when you paste a placeholder into a URL field.
    Unset placeholders are left alone (visible in the draft so editor can spot them)."""
    out = html
    hits = 0
    for key, value in replacements.items():
        replacement = value or ""
        # First handle Beehiiv's auto-prepended scheme on URL fields,
        # so `http://{key}` → real URL (not `http://real-url`).
        if key in URL_TYPED_KEYS:
            for variant in _placeholder_variants(key):
                for scheme in ("http://", "https://"):
                    prefixed = scheme + variant
                    if prefixed in out:
                        out = out.replace(prefixed, replacement)
                        hits += 1
        # Then ordinary token replacement (covers text uses).
        for token in _placeholder_variants(key):
            if token in out:
                out = out.replace(token, replacement)
                hits += 1
    print(f"  Placeholder replacements applied: {hits} matches")
    return out


def _autolink_bare_urls(html: str) -> str:
    """Turn bare http(s) URLs into clickable <a> tags, but leave URLs that
    are already inside an <a>…</a> (e.g. just produced from [text](url)
    markdown, or hand-authored anchors) untouched.

    Split on existing anchors so only the text BETWEEN them is scanned; the
    captured <a> spans land on odd indices and are passed through verbatim.
    Trailing sentence punctuation is kept OUTSIDE the link so 'see x.com.'
    doesn't put the period in the href."""
    parts = re.split(r'(<a\b[^>]*>.*?</a>)', html, flags=re.IGNORECASE | re.DOTALL)
    url_re = re.compile(r'(https?://[^\s<>()]+)')

    def _repl(m: re.Match) -> str:
        u = m.group(1)
        trail = ""
        while u and u[-1] in ".,;:!?)":
            trail = u[-1] + trail
            u = u[:-1]
        if not u:
            return m.group(0)
        return (f'<a href="{u}" target="_blank" rel="noopener noreferrer">'
                f'{u}</a>{trail}')

    for i in range(0, len(parts), 2):  # even indices = non-anchor text
        parts[i] = url_re.sub(_repl, parts[i])
    return "".join(parts)


def md_inline_to_html(text: str) -> str:
    """Inline-only markdown → HTML conversion (bold + links). No paragraph
    handling — that's done by expand_paragraph_field via real DOM ops.

    Markdown links convert first; then any remaining BARE URLs (common when
    a Notion-authored blurb just pastes the website) are auto-linked so they
    render clickable instead of as plain URL text."""
    if not text:
        return ""
    out = text
    out = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        out,
    )
    out = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", out)
    out = _autolink_bare_urls(out)
    out = out.replace("\n", " ")
    return out


def md_to_html(text: str) -> str:
    """Backwards-compatible alias for short single-paragraph fields where
    paragraph splitting isn't needed (e.g. the free-event metadata line).
    For multi-paragraph prose, use expand_paragraph_field instead — it
    creates real sibling <p> blocks that Beehiiv won't collapse."""
    return md_inline_to_html(text)


def expand_paragraph_field(html: str, placeholder_key: str, text: str) -> str:
    """Replace the placeholder's wrapping <p> with one real <p> block per
    blank-line-separated paragraph in `text`. Each paragraph gets inline
    markdown (bold + links) converted to HTML.

    Why this exists: Beehiiv strips injected raw tags like `</p><p>` and
    `<br><br>` when they appear inside an existing <p> via string
    substitution — the resulting email renders as one wall of text.
    Properly-structured sibling <p> elements created via the DOM tree
    can't be ignored, so they render with real paragraph spacing.

    If the placeholder isn't in the HTML or isn't inside a <p>, falls
    back to inline-only substitution so the field still renders (just
    without paragraph breaks)."""
    if not text:
        return html
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html

    token = "{" + placeholder_key + "}"
    if token not in html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(string=lambda t: t and token in str(t))
    if not node:
        return html

    # Walk up to the wrapping <p>. If the placeholder sits inside an
    # inline element (span, strong, em, a), keep climbing.
    block = node.parent
    while block is not None and block.name != "p":
        block = block.parent
    if block is None:
        # No <p> wrapper — fall back to inline substitution. Caller's
        # replace_placeholders pass will substitute the raw token.
        return html

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paragraphs:
        block.decompose()
        return str(soup)

    # Preserve the wrapping <p>'s attributes (style, class, id) so the
    # new paragraphs inherit the template's typography.
    attrs_html = "".join(f' {k}="{v}"' for k, v in (block.attrs or {}).items()
                         if isinstance(v, str))
    if "class" in (block.attrs or {}):
        cls = " ".join(block["class"]) if isinstance(block["class"], list) else block["class"]
        attrs_html = re.sub(r' class="[^"]*"', "", attrs_html)
        attrs_html += f' class="{cls}"'

    new_html = "".join(
        f"<p{attrs_html}>{md_inline_to_html(p)}</p>" for p in paragraphs
    )
    new_fragment = BeautifulSoup(new_html, "html.parser")
    for child in list(new_fragment.children):
        block.insert_before(child)
    block.decompose()
    return str(soup)


def hide_unused_lowdown_slots(html: str, used_count: int) -> str:
    """For Local Lowdown placeholders we don't fill (e.g., we have 3 stories, slots
    4-5 are unused), wipe the remaining placeholders so they don't render literally."""
    for n in range(used_count + 1, 6):
        for key in (f"local_lowdown{n}_title", f"local_lowdown{n}_message"):
            for token in _placeholder_variants(key):
                html = html.replace(token, "")
    return html


# ---------------------------------------------------------------------------
# Weekend Planner slot definitions
# ---------------------------------------------------------------------------
# Day-first naming so the template can render Friday/Saturday/Sunday as
# top-level sections with Family and Adult side-by-side under each.
WEEKEND_SLOT_KEYS: list[tuple[str, str, str]] = [
    ("friday_family",   "Friday",   "Family"),
    ("friday_adult",    "Friday",   "Adult"),
    ("saturday_family", "Saturday", "Family"),
    ("saturday_adult",  "Saturday", "Adult"),
    ("sunday_family",   "Sunday",   "Family"),
    ("sunday_adult",    "Sunday",   "Adult"),
]


def _weekend_event_to_card(ev: dict, slot_key: str) -> dict[str, str]:
    """Render one event into the local-lowdown-style title/message/link
    triple. Title bolds the event name (with leading emoji); message
    chains venue / address / time / price with bullets and appends the
    one-sentence description on its own line; link is the source URL.

    The practical info — venue, address, time — is wrapped in <strong> so it
    stands out in the Beehiiv render (the message placeholder renders HTML,
    same as the Local Lowdown cards). Price is left plain."""
    emoji = (ev.get("emoji") or "").strip()
    name  = (ev.get("event_name") or "").strip()
    title = f"{emoji} {name}".strip() if emoji else name

    bold_parts = [f"<strong>{v}</strong>" for v in
                  ((ev.get(k) or "").strip() for k in ("venue", "address", "time"))
                  if v]
    price = (ev.get("price") or "").strip()
    metadata = " • ".join(bold_parts + ([price] if price else []))
    desc = (ev.get("description") or "").strip()
    message = metadata + (f"\n\n{desc}" if desc else "")

    return {
        f"{slot_key}_title":   title,
        f"{slot_key}_message": message,
        f"{slot_key}_link":    ev.get("source_url") or "",
    }


_CARD_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "li"}


def _expand_one_slot(soup, slot_key: str, items: list[dict],
                     item_to_fields,
                     item_dom_mutator=None) -> tuple[int, int]:
    """Generic slot expansion. Finds the {<slot>_title} placeholder in
    `soup`, walks up to its enclosing block element, then collects
    consecutive sibling block elements that contain THIS slot's
    `_message` or `_link` placeholders. That tuple is the "card" — we
    insert N substituted copies in place of the originals.

    `items` is the list of data records for this slot.
    `item_to_fields` takes `(item, slot_key)` and returns a
    `{key: value}` dict for the placeholder substitutions.

    Returns the number of cards rendered (0 if the slot is empty AND
    the placeholder cards were removed; -1 if the title placeholder
    wasn't found in the template; otherwise len(items))."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return -1

    title_token = f"{{{slot_key}_title}}"
    msg_token   = f"{{{slot_key}_message}}"
    link_token  = f"{{{slot_key}_link}}"

    title_text = soup.find(string=lambda t: t and title_token in str(t))
    if not title_text:
        return -1, 0
    msg_text  = soup.find(string=lambda t: t and msg_token  in str(t))
    link_text = soup.find(string=lambda t: t and link_token in str(t))

    # Walk up to the nearest block-level container for the title.
    title_node = title_text.parent
    while title_node is not None and title_node.name not in _CARD_BLOCK_TAGS:
        title_node = title_node.parent
    if title_node is None:
        return -1, 0

    # Find the smallest ancestor of the title that ALSO contains the
    # message (if it exists) and the link (if it exists). Beehiiv often
    # wraps each placeholder paragraph in its own <table><tr><td>
    # structure for email-safe rendering, so the title and message
    # aren't direct siblings — they're cousins under a common column
    # ancestor. Walking up until all three placeholders are in the
    # subtree finds that ancestor.
    def _subtree_has(node, token):
        return token in str(node) if node is not None else True
    need_msg  = msg_text  is not None
    need_link = link_text is not None
    ancestor = title_node
    while ancestor is not None:
        s = str(ancestor)
        has_msg  = (not need_msg)  or (msg_token  in s)
        has_link = (not need_link) or (link_token in s)
        if has_msg and has_link:
            break
        ancestor = ancestor.parent
    if ancestor is None:
        # Couldn't find a common ancestor — fall back to title only.
        ancestor = title_node.parent or title_node

    # Within `ancestor`, find the contiguous range of direct children
    # that touch any of the three placeholders. The "card" is that
    # range — clone IT, not the entire ancestor subtree, so adjacent
    # static content (h2 heading, image block, etc.) stays put.
    element_children = [c for c in ancestor.children
                        if getattr(c, "name", None) is not None]
    indices: list[int] = []
    for i, c in enumerate(element_children):
        cs = str(c)
        if title_token in cs or msg_token in cs or link_token in cs:
            indices.append(i)
    if indices:
        start_idx = min(indices)
        end_idx = max(indices)
        # Extend forward to include trailing decorative blocks (dividers,
        # spacers, blank rows) so a visual separator placed between cards
        # in the template clones with each card instead of appearing
        # only at the bottom of the section. Stop the moment we hit a
        # block with real content — especially an <img> (the next
        # section's hero image), so we don't accidentally pull in
        # something that belongs to a different section.
        for i in range(end_idx + 1, len(element_children)):
            ch = element_children[i]
            if ch.find("img") is not None:
                break
            text = (ch.get_text() or "").replace("\xa0", " ").strip()
            has_hr = ch.find("hr") is not None
            if not text or has_hr:
                end_idx = i
            else:
                break
        card_nodes = element_children[start_idx:end_idx + 1]
    else:
        # Placeholders are directly inside `ancestor`, not inside its
        # children — treat the ancestor itself as the card.
        card_nodes = [ancestor]

    if not items:
        for n in card_nodes:
            n.decompose()
        return 0, len(card_nodes)

    template_html = "".join(str(n) for n in card_nodes)
    new_chunks: list[str] = []
    for item in items:
        copy = template_html
        for k, v in item_to_fields(item, slot_key).items():
            token = "{" + k + "}"
            # Handle Beehiiv's auto-prepended scheme on URL fields FIRST.
            # When the template's URL field had a bare placeholder like
            # `{friday_family_link}`, Beehiiv stores it as
            # `http://{friday_family_link}` to satisfy its URL validator.
            # If we replaced the bare token first, that turns the saved
            # form into `http://https://www.example.com/…`, which
            # Beehiiv then "normalizes" by stripping the second colon
            # → `http://https//www.example.com/…` (the broken URL the
            # user saw). Replace the prefixed forms first so the
            # `http://` prefix gets consumed cleanly.
            if k.endswith("_link"):
                for scheme in ("http://", "https://"):
                    copy = copy.replace(scheme + token, v)
            copy = copy.replace(token, v)
        if item_dom_mutator is not None:
            # Parse this clone, hand it to the mutator (e.g. to swap the
            # <img src> for per-card images), then serialize back.
            clone_soup = BeautifulSoup(copy, "html.parser")
            try:
                item_dom_mutator(clone_soup, item)
            except Exception as e:
                print(f"    ⚠ slot '{slot_key}' DOM mutator error: {e}")
            copy = str(clone_soup)
        new_chunks.append(copy)

    new_fragment = BeautifulSoup("".join(new_chunks), "html.parser")
    for node in list(new_fragment.children):
        card_nodes[0].insert_before(node)
    for n in card_nodes:
        n.decompose()
    return len(items), len(card_nodes)


# Weekend Planner per-card photo sizing. Responsive ("dynamic"): the photo
# fluidly fills its container up to WP_IMAGE_MAX_WIDTH_PX, then stops — so it
# scales down on narrow/mobile widths but never blows up to full email width
# (the "too big" complaint). Bump this one number to resize every weekend photo.
WP_IMAGE_MAX_WIDTH_PX = 300


def _rewrite_weekend_link_text(clone_soup, ev: dict) -> None:
    """Swap the card's source-link visible text from the Beehiiv template's
    hardcoded 'More Info' to the bare root URL (e.g. `www.example.com`), so
    the Beehiiv render matches the Notion page's `display_domain` link text.
    The href is left untouched — only the anchor's text changes.

    Matches the source anchor by href (already substituted to the real URL by
    `_expand_one_slot`) or, as a fallback, by the literal 'More Info'/'More'
    label. Weekend event messages aren't markdown-converted, so the source
    link is the only <a> in the card — no risk of clobbering inline links."""
    url = (ev.get("source_url") or "").strip()
    if not url:
        return
    label = display_domain(url)
    if not label:
        return
    for a in clone_soup.find_all("a"):
        href = (a.get("href") or "").strip()
        txt = (a.get_text() or "").strip().lower()
        if href == url or url in href or txt in ("more info", "more"):
            a.clear()
            a.append(label)


def _weekend_image_mutator(clone_soup, ev: dict) -> None:
    """Per-card hook for Weekend Planner: render THIS event's photo inside its
    own card so multiple photos mix in among the events (the pipeline now keeps
    up to 3 image-bearing events per slot). If the cloned card already has an
    <img>, retarget its src; otherwise insert a responsive <img> at the top of
    the card. Events with no image have any stray placeholder <img> removed so
    no broken template token URL shows.

    Also rewrites the source link's visible text to the root URL (see
    `_rewrite_weekend_link_text`).

    The photo is sized responsively — width:100% capped at
    WP_IMAGE_MAX_WIDTH_PX — so it scales with the layout but never renders
    full-bleed."""
    _rewrite_weekend_link_text(clone_soup, ev)
    img_url = (ev.get("image_url") or "").strip()
    existing = clone_soup.find("img")
    style = (f"display:block;width:100%;max-width:{WP_IMAGE_MAX_WIDTH_PX}px;"
             "height:auto;margin:0 auto 12px;border-radius:6px;")
    if not img_url:
        if existing is not None:
            existing.decompose()
        return
    alt = (ev.get("event_name") or "event photo")[:120]
    if existing is not None:
        existing["src"] = img_url
        existing["alt"] = alt
        existing["style"] = style
        existing["width"] = str(WP_IMAGE_MAX_WIDTH_PX)
        return
    new_img = clone_soup.new_tag("img")
    new_img["src"] = img_url
    new_img["alt"] = alt
    # width attr is a fallback for clients that ignore max-width CSS; the
    # max-width in style keeps it responsive on smaller screens.
    new_img["width"] = str(WP_IMAGE_MAX_WIDTH_PX)
    new_img["style"] = style
    # Anchor the photo INSIDE this event's own text block (as the first
    # child of the first text-bearing block element — normally the title),
    # not as a bare sibling before the whole card. Beehiiv wraps cards in
    # nested <table><tr><td> structures; a bare <img> placed before that
    # table can drift to the top of the section in some email clients,
    # which is what made "all the photos pile up on top" instead of sitting
    # with each event. Inserting it within the title's block keeps the
    # photo glued directly above that event's text.
    _BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "td", "li"}
    anchor = next((el for el in clone_soup.find_all(True)
                   if el.name in _BLOCK and (el.get_text() or "").strip()), None)
    if anchor is not None:
        anchor.insert(0, new_img)
    else:
        first = clone_soup.find(True)
        if first is not None:
            first.insert_before(new_img)
        else:
            clone_soup.append(new_img)


def expand_weekend_slots(html: str, events: list[dict]) -> str:
    """For each (day, audience) Weekend Planner slot, duplicate the
    template card once per event. See `_expand_one_slot` for the
    template structure. Empty slots have their card removed."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠ beautifulsoup4 not installed; skipping Weekend Planner expansion")
        return html

    soup = BeautifulSoup(html, "html.parser")
    total_expanded = 0
    for slot_key, day, audience in WEEKEND_SLOT_KEYS:
        slot_events = [e for e in events
                       if e.get("day") == day and e.get("audience") == audience]
        # Each event card now carries its OWN image (see
        # _weekend_image_mutator), so we no longer float the single
        # image-owner to the top. Order alphabetically — matching the Notion
        # page's sort — so the photos interleave naturally among the cards
        # instead of clustering. Strip a leading article / ordinal so headline
        # qualifiers don't dictate the order.
        def _wp_sort_key(e: dict) -> str:
            name = (e.get("event_name") or "").strip()
            name = re.sub(r"^(the|a|an)\s+", "", name, flags=re.IGNORECASE)
            name = re.sub(r"^\d+(st|nd|rd|th)?\s+(annual\s+)?", "",
                          name, flags=re.IGNORECASE)
            return name.lower()
        slot_events.sort(key=_wp_sort_key)
        n, width = _expand_one_slot(soup, slot_key, slot_events, _weekend_event_to_card,
                                    item_dom_mutator=_weekend_image_mutator)
        if n == -1:
            print(f"    · weekend slot '{slot_key}' — no {{{slot_key}_title}} in template")
        elif n == 0:
            print(f"    · weekend slot '{slot_key}' — 0 events, card removed")
        else:
            total_expanded += n
            warn = "  ⚠ card width=1 (only title) — message/link not in same parent" if width == 1 else ""
            print(f"    ✓ weekend slot '{slot_key}' — expanded into {n} card(s) "
                  f"[card width: {width} block(s) each]{warn}")
    print(f"  Weekend Planner expansion: {total_expanded} event card(s) total")
    return str(soup)


def _lowdown_story_to_card(story: dict, slot_key: str) -> dict[str, str]:
    """Map a parsed lowdown story to the title/message/link placeholders.
    Message gets markdown→HTML conversion so `**bold**` and `[label](url)`
    in the body render correctly inside Beehiiv."""
    return {
        f"{slot_key}_title":   story.get("heading", ""),
        f"{slot_key}_message": md_to_html(story.get("body", "")),
        f"{slot_key}_link":    story.get("url", ""),
    }


def _lowdown_image_mutator(clone_soup, story: dict) -> None:
    """Per-card hook: point the cloned Local Lowdown card's image at this
    story's scraped photo. If the template card already has an <img>
    placeholder (like the Meme Corner card), swap its src. Otherwise insert
    a responsive <img> at the top of the card so images still render even
    when the Beehiiv lowdown card has no image placeholder yet."""
    img_url = (story.get("image_url") or "").strip()
    if not img_url:
        return
    alt = (story.get("heading") or "news photo")[:120]
    img = clone_soup.find("img")
    if img is not None:
        img["src"] = img_url
        img["alt"] = alt
        return
    # No placeholder in the template card — insert one above the headline.
    new_img = clone_soup.new_tag("img", src=img_url, alt=alt)
    new_img["style"] = ("max-width:100%;height:auto;display:block;"
                        "margin:0 auto 12px;border-radius:6px;")
    first = clone_soup.find(True)
    if first is not None:
        first.insert_before(new_img)
    else:
        clone_soup.append(new_img)


def _meme_to_card(meme: dict, slot_key: str) -> dict[str, str]:
    """Map a meme dict to title/message/link placeholders. The "title"
    is just the caption (the meme's Reddit post title); message is the
    subreddit tag; link is the Reddit permalink (so the More Info button
    sends curious readers back to the original post)."""
    cap = (meme.get("caption") or "").strip()
    sub = (meme.get("subreddit") or "").strip()
    return {
        f"{slot_key}_title":   cap,
        f"{slot_key}_message": f"r/{sub}" if sub else "",
        f"{slot_key}_link":    meme.get("permalink") or "",
    }


def _meme_image_mutator(clone_soup, meme: dict) -> None:
    """Per-card hook: swap the FIRST <img src> in the cloned card to
    this meme's Reddit-hosted image URL. The template card contains one
    placeholder image (any filename works — only the first <img> tag is
    targeted), and each clone gets pointed at its specific meme."""
    img_url = meme.get("image_url")
    if not img_url:
        return
    img = clone_soup.find("img")
    if img is not None:
        img["src"] = img_url
        img["alt"] = (meme.get("caption") or "meme")[:120]


def expand_meme_slots(html: str, memes: list[dict]) -> str:
    """Duplicate the Meme Corner template card once per approved meme.
    Slot key: `meme`. Template card uses {meme_title} / {meme_message}
    / {meme_link} placeholders (mirrors Local Lowdown). One placeholder
    image inside the card gets per-clone src swapped to the matching
    meme's actual image URL via _meme_image_mutator."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠ beautifulsoup4 not installed; skipping Meme Corner expansion")
        return html

    soup = BeautifulSoup(html, "html.parser")
    n, width = _expand_one_slot(
        soup, "meme", memes, _meme_to_card,
        item_dom_mutator=_meme_image_mutator,
    )
    if n == -1:
        print(f"    · meme — no {{meme_title}} in template, skipping "
              f"({len(memes)} memes will not render)")
    elif n == 0:
        print(f"    · meme — 0 approved memes, placeholder card removed")
    else:
        warn = "  ⚠ card width=1 (only title)" if width == 1 else ""
        print(f"    ✓ meme — expanded into {n} card(s) [card width: {width} block(s) each]{warn}")
    return str(soup)


def expand_free_event_images(html: str, image_urls: list[str]) -> str:
    """Dynamically render 1-3 free-event pictures by cloning the template's
    single free-event image block once per image — the same duplicate-a-unit
    approach the Weekend Planner / Meme slots use. This means the Beehiiv
    template only needs ONE free-event <img> placeholder; we repeat it as
    many times as there are images instead of relying on static free-pic-2/3
    placeholders.

    The template placeholder is the <img> whose src contains the 'free-event'
    token. With 0 images the block is removed; with 1 its src is retargeted;
    with 2-3 the enclosing image-only block is cloned per URL."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠ beautifulsoup4 not installed; skipping Free Event image expansion")
        return html

    soup = BeautifulSoup(html, "html.parser")

    # Locate the template free-event <img> by its 'free-event' filename token.
    target = None
    for img in soup.find_all("img"):
        if "free-event" in (img.get("src") or ""):
            target = img
            break
    if target is None:
        print("    · free-event images — no placeholder <img> in template")
        return str(soup)

    # Walk up from the <img> to the largest block-level ancestor that still
    # wraps ONLY this image (no text, no sibling images). That image-only cell
    # is the repeatable unit — cloning it (rather than the bare <img>) keeps
    # the email-safe table/spacing markup so the pictures stack cleanly.
    BLOCK_TAGS = {"tr", "table", "div", "td", "p", "figure", "center"}
    unit = target
    node = target.parent
    while node is not None and getattr(node, "name", None):
        text = (node.get_text() or "").replace("\xa0", " ").strip()
        imgs = node.find_all("img")
        if text or len(imgs) > 1:
            break
        if node.name in BLOCK_TAGS:
            unit = node
        node = node.parent

    if not image_urls:
        unit.decompose()
        print("    · free-event images — 0 images, placeholder removed")
        return str(soup)

    template_html = str(unit)
    chunks: list[str] = []
    for url in image_urls[:3]:
        clone = BeautifulSoup(template_html, "html.parser")
        cimg = clone.find("img")
        if cimg is not None:
            cimg["src"] = url
            cimg["alt"] = "Free event photo"
        chunks.append(str(clone))

    new_fragment = BeautifulSoup("".join(chunks), "html.parser")
    for n in list(new_fragment.children):
        unit.insert_before(n)
    unit.decompose()
    print(f"    ✓ free-event images — rendered {len(image_urls[:3])} picture(s)")
    return str(soup)


def expand_lowdown_slots(html: str, stories: list[dict]) -> str:
    """Duplicate the Local Lowdown template card once per parsed story.
    Slot key: `local_lowdown`. The template should contain ONE card with
    {local_lowdown_title}, {local_lowdown_message}, {local_lowdown_link}
    placeholders in consecutive block elements."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠ beautifulsoup4 not installed; skipping Local Lowdown expansion")
        return html

    soup = BeautifulSoup(html, "html.parser")
    n, width = _expand_one_slot(soup, "local_lowdown", stories, _lowdown_story_to_card,
                                item_dom_mutator=_lowdown_image_mutator)
    if n == -1:
        print(f"    · local_lowdown — no {{local_lowdown_title}} in template, "
              f"skipping ({len(stories)} parsed stories will not render)")
    elif n == 0:
        print(f"    · local_lowdown — 0 stories, placeholder card removed")
    else:
        warn = "  ⚠ card width=1 (only title) — message/link not in same parent" if width == 1 else ""
        print(f"    ✓ local_lowdown — expanded into {n} card(s) [card width: {width} block(s) each]{warn}")
    return str(soup)


# ---------------------------------------------------------------------------
# 4. SECTION DATA → REPLACEMENT MAP
# ---------------------------------------------------------------------------
def build_replacements(client: BeehiivClient, publication_id: str,
                      newsletter_name: str) -> tuple[dict, dict, dict, int, list, list, dict, list, list]:
    """Pull section data, upload images, return:
      (text_replacements, image_url_swaps, alt_image_swaps,
       lowdown_story_count, weekend_events, lowdown_stories,
       paragraph_prose, memes, free_event_images)

    text_replacements: {placeholder_key: string_value}
    image_url_swaps:   {original_url: beehiiv_hosted_url}  — used as fallback string-find
    alt_image_swaps:   {alt_text: image_url}  — used to swap <img alt="..." src="...">
    lowdown_story_count: number of stories actually used (presence flag for subject-line context)
    weekend_events:    raw list of Weekend Planner events for slot expansion
    lowdown_stories:   parsed Local Lowdown stories (heading/body/url) for slot expansion
    paragraph_prose:   {placeholder_key: raw_text} for fields whose wrapping
                       <p> needs DOM-level splitting into per-paragraph <p>s
                       (Beehiiv strips injected `<br><br>` / `</p><p>`)
    free_event_images: list of 1-3 hosted free-event picture URLs (in render
                       order) for dynamic image-block duplication
    """
    repl: dict[str, str] = {}
    image_swaps: dict[str, str] = {}
    alt_swaps: dict[str, str] = {}
    # Hosted URLs of the 1-3 free-event pictures, in render order. Consumed by
    # expand_free_event_images() to dynamically clone the template image block.
    free_event_images: list[str] = []
    # Raw multi-paragraph text for fields that need DOM-level paragraph
    # expansion (real sibling <p> blocks so Beehiiv doesn't collapse them
    # into one wall of text).
    paragraph_prose: dict[str, str] = {}

    # ---- Welcome Intro ----
    intro = get_latest_intro(newsletter_name)
    if intro:
        # {intro_message} = greeting only. The blurb has its own slot
        # via {intro_blurb} (or {summary_text} when wired separately).
        intro_msg = (intro.get("greeting") or "").strip()
        paragraph_prose["intro_message"] = intro_msg
        repl["intro_message"] = md_to_html(intro_msg)  # inline fallback

        # {intro_blurb} = the 2-paragraph editorial body, split into
        # per-paragraph <p> blocks by expand_paragraph_field. Keeps the
        # blurb in the email body in a slot the template author can
        # style independently of greeting / summary / outline.
        blurb_body = (intro.get("blurb") or "").strip()
        if blurb_body:
            paragraph_prose["intro_blurb"] = blurb_body
            repl["intro_blurb"] = md_to_html(blurb_body)

        # "In Today's Connect" teaser block. The skill emits a bold
        # header line + 5-8 emoji-led lines with blank-line separators,
        # so the same DOM-level paragraph expansion that handles the
        # free-event description turns each line into its own <p>.
        teaser = (intro.get("in_todays_connect") or "").strip()
        if teaser:
            paragraph_prose["in_todays_connect"] = teaser
            repl["in_todays_connect"] = md_to_html(teaser)

        # Top-of-issue summary placeholders. These mirror the three Intro
        # DB fields already populated by the chained welcome_intro →
        # subject_line → in_todays_connect pipeline, exposed under
        # editorial-friendly names so the template can render them
        # together as a "What's inside" preamble.
        headline = (intro.get("subject_line") or "").strip()
        if headline:
            repl["headline"] = headline
        # {summary_text} pulls the Preview Text generated by the
        # subject_line pipeline (subject-preview-text skill). Falls
        # back to the editorial blurb if no preview was produced yet,
        # so the section never ships empty mid-rollout.
        preview = (intro.get("preview_text") or "").strip()
        blurb   = (intro.get("blurb") or "").strip()
        summary_src = preview or blurb
        if summary_src:
            paragraph_prose["summary_text"] = summary_src
            repl["summary_text"] = md_to_html(summary_src)
        if teaser:
            # summary_outline = the emoji-led teaser lines only, WITHOUT
            # the bold "**In Today's Connect:**" header line that the
            # skill emits. The header is fine on the Notion landing page
            # and inside {in_todays_connect}, but the template uses
            # {summary_outline} in a context that already has its own
            # heading above it.
            outline_lines = [ln for ln in teaser.splitlines()
                             if not (ln.strip().startswith("**")
                                     and ln.strip().endswith("**"))]
            outline_only = "\n".join(outline_lines).strip()
            paragraph_prose["summary_outline"] = outline_only
            repl["summary_outline"] = md_to_html(outline_only)

    # ---- Featured Event ----
    event = get_featured_event(newsletter_name)
    if event and event.get("blurb"):
        repl["event_of_the_week_headline"]    = event.get("event_name", "")
        repl["event_of_the_week_description"] = event.get("blurb", "")
        ev_link = event.get("ticket_url") or event.get("source_url") or ""
        repl["event_of_the_week_link"]        = ev_link
        # Alias: template URL fields sometimes lose the `_link` suffix
        repl["event_of_the_week"]             = ev_link
        # Prefer the body composite GIF (built by build_event_body_gif —
        # has the ticket / date / address / venue text overlaid on rotating
        # candidate photos) over the raw event photo. Mirrors what the
        # assembled Notion landing page does: gif_url is the primary visual
        # for the featured-event card, with image_url as the static fallback.
        ev_img = (event.get("gif_url")
                  or event.get("image_url")
                  or event.get("photo")
                  or "")
        if ev_img:
            hosted = upload_remote_image(client, publication_id, ev_img)
            if hosted:
                image_swaps[ev_img] = hosted
            alt_swaps["event_of_the_week_image"] = hosted or ev_img

        # ---- Newsletter Header (Canva-style composite) ----
        # Prefer the URL already saved to Notion by the review-app image
        # picker (Header Image URL). Falls back to the predicted per-event
        # gh-pages URL (matching what the picker writes) so we don't point
        # at the shared `Newsletter_Header_image_{nl}.png` file — that
        # generic name gets overwritten by every event and surfaces the
        # wrong image when an older row is approved.
        import time as _t
        safe_title = "".join(
            c if c.isalnum() else "_" for c in (event.get("event_name") or "")
        )[:40] or "event"
        header_url = event.get("header_image_url") or (
            f"https://peachyinsurance.github.io/newsletters/gifs/"
            f"Newsletter_Header_image_{newsletter_name}_{safe_title}.png"
            f"?v={int(_t.time())}"
        )
        alt_swaps["newsletter_header_image"] = header_url

    # ---- Restaurant (single featured pick) ----
    # Since the single-pick migration, get_restaurants returns ONE row whose
    # tier is "approved" (or legacy "Tier 1 Winner"). The old code only matched
    # "Tier 1 Winner", so the new approved pick was dropped and the whole
    # Restaurant Radar card got pruned. Just take the first returned row.
    restaurants = get_restaurants(newsletter_name)
    featured = restaurants[0] if restaurants else None
    if featured:
        print(f"  Restaurant pick for {newsletter_name}: {featured.get('name','')} "
              f"(tier={featured.get('tier','')})")
        repl["restaurant_radar_name"]    = featured.get("name", "")
        repl["restaurant_radar_message"] = featured.get("blurb", "")
        repl["restaurant_radar_url"]     = featured.get("maps_url") or featured.get("website") or ""
        img_url = featured.get("gif") or featured.get("photo")
        if img_url:
            hosted = upload_remote_image(client, publication_id, img_url)
            if hosted:
                image_swaps[img_url] = hosted
            alt_swaps["restaurant_radar_image"] = hosted or img_url
    else:
        print(f"  ⚠ No restaurant found for {newsletter_name} — Restaurant Radar card will prune")

    # ---- Real Estate ----
    # Tier names from RE corner: "Starter Home", "Sweet Spot", "Showcase"
    re_listings = get_real_estate(newsletter_name)
    # Notion stores Tier as "Starter" / "Sweet Spot" / "Showcase"
    # (not "Starter Home"). Match the actual value in the DB.
    re_tier_to_alt = {
        "Starter":      "real_estate_image_starter",
        "Sweet Spot":   "real_estate_image_sweetspot",
        "Showcase":     "real_estate_image_showcase",
    }
    re_tier_to_link_key = {
        "Starter":      "real_estate_starter_link",
        "Sweet Spot":   "real_estate_sweetspot_link",
        "Showcase":     "real_estate_showcase_link",
    }
    for listing in re_listings:
        img_url = listing.get("template") or listing.get("photo")
        if img_url:
            hosted = upload_remote_image(client, publication_id, img_url)
            if hosted:
                image_swaps[img_url] = hosted
            alt_key = re_tier_to_alt.get(listing.get("tier", ""))
            if alt_key:
                alt_swaps[alt_key] = hosted or img_url
        link_key = re_tier_to_link_key.get(listing.get("tier", ""))
        if link_key:
            repl[link_key] = listing.get("url", "")

        # Showcase price-guess trivia: fill {showcase_answer_1..4} with the
        # four candidate prices (sorted ascending to match the A/B/C/D order
        # used in the assembled newsletter page). Each answer is wired in the
        # Beehiiv template as a hyperlink pointing to {real_estate_showcase_link}
        # (set above), so clicking ANY answer takes the reader to the listing
        # without revealing which one was correct.
        if listing.get("tier") == "Showcase" and listing.get("trivia"):
            try:
                options = sorted(
                    int(p) for p in listing["trivia"].split(",")
                    if p.strip().lstrip("$").replace(",", "").isdigit()
                )
            except Exception:
                options = []
            for i in range(1, 5):
                if i <= len(options):
                    repl[f"showcase_answer_{i}"] = f"${options[i-1]:,}"
                else:
                    repl[f"showcase_answer_{i}"] = ""

    # ---- Local Lowdown (parsed into a story list; rendered via
    #      expand_lowdown_slots which clones ONE template card per story).
    lowdown_stories: list[dict] = []
    lowdown_text = get_latest_lowdown(newsletter_name)
    if lowdown_text:
        # Parse the markdown: ### {emoji} {headline}\n\n{body}\n\nMore: [label](url)
        sections = re.split(r"\n(?=### )", lowdown_text.strip())
        for section in sections:
            lines = section.splitlines()
            heading = lines[0].lstrip("# ").strip() if lines else ""
            body_lines = [ln for ln in lines[1:] if ln.strip()]
            # Pull the trailing markdown link `More: [label](url)` if present.
            # The URL becomes {local_lowdown_link} (used by the button/hyperlink),
            # so strip the line from the body to avoid the same link appearing twice.
            story_url = ""
            cleaned_body_lines = []
            for ln in body_lines:
                m = re.search(r"\((https?://[^\)]+)\)", ln)
                # Treat the line as the "More: ..." trailer if it has a markdown link
                # AND begins with More/Source/Read/Link (case-insensitive) OR is just `[label](url)`
                trailer = (
                    m and (
                        re.match(r"^\s*(more|source|read more|link|via|see more)\b", ln, re.IGNORECASE)
                        or re.match(r"^\s*\[[^\]]+\]\(https?://[^\)]+\)\s*$", ln)
                    )
                )
                if trailer:
                    if not story_url:
                        story_url = m.group(1)
                    continue  # drop the line from body
                cleaned_body_lines.append(ln)
            body = "\n".join(cleaned_body_lines).strip()
            if heading or body:
                lowdown_stories.append({
                    "heading": heading,
                    "body":    body,
                    "url":     story_url,
                })
    # Scrape a lead image per story (og:image from its source URL), upload to
    # Beehiiv's media library, and stash the hosted URL on the story so
    # expand_lowdown_slots can swap it into each cloned card. Mirrors how the
    # assembler attaches Local Lowdown images to the Notion landing page.
    # Best-effort: any failure leaves that story text-only.
    if lowdown_stories:
        try:
            import sys as _sys, os as _os
            _sys.path.append(_os.path.join(_os.path.dirname(__file__), '..', '..',
                                           'Free Events', 'Code'))
            from Free_Events import fetch_event_image as _fetch_img  # noqa: E402
        except Exception as e:
            print(f"  ⚠ [lowdown] image scraper unavailable ({e})")
            _fetch_img = None
        if _fetch_img:
            seen_imgs: set[str] = set()
            for story in lowdown_stories:
                src = (story.get("url") or "").strip()
                if not src:
                    continue
                try:
                    img = _fetch_img(src)
                except Exception:
                    img = ""
                if not img or not img.lower().startswith(("http://", "https://")):
                    continue
                norm = img.split("?")[0].rstrip("/").lower()
                if norm in seen_imgs:
                    continue
                seen_imgs.add(norm)
                hosted = upload_remote_image(client, publication_id, img)
                story["image_url"] = hosted or img
                print(f"  [lowdown] image for '{story.get('heading','')[:50]}': "
                      f"{(hosted or img)[:80]}")
            n_imgs = sum(1 for s in lowdown_stories if s.get("image_url"))
            print(f"  [lowdown] {n_imgs}/{len(lowdown_stories)} story(ies) have an image")

    # Keep `story_count` for the existing return-tuple contract; callers
    # (currently just subject-line generation) use it as a presence flag.
    story_count = len(lowdown_stories)
    if lowdown_stories:
        # Expose the first story's heading via the legacy placeholder name
        # so the subject-line context that reads `local_lowdown1_title`
        # keeps working without a downstream change.
        repl["local_lowdown1_title"] = lowdown_stories[0].get("heading", "")

    # ---- Business Brief ----
    business = get_business_brief(newsletter_name)
    if business and business.get("blurb"):
        repl["business_brief_name"]    = business.get("name", "")
        bb_url = business.get("source_url", "") or ""
        # Embed a clickable website link directly in the blurb prose so the
        # email always has a working link, independent of whether the Beehiiv
        # template wires the {business_brief_url}/{business_brief_link} token
        # into an <a>. md_inline_to_html (used by both expand_paragraph_field
        # and md_to_html) converts the [text](url) markdown into a real
        # anchor. Appended as its own paragraph, mirroring the Notion page's
        # "Website:" line. Skip if the URL is already in the blurb so an
        # author/Claude-supplied link isn't double-rendered.
        bb_blurb = business.get("blurb", "")
        if bb_url and bb_url not in bb_blurb:
            bb_blurb = bb_blurb.rstrip() + f"\n\n**Website:** [{display_domain(bb_url)}]({bb_url})"
        paragraph_prose["business_brief_blurb"] = bb_blurb
        repl["business_brief_blurb"]   = md_to_html(bb_blurb)
        repl["business_brief_city"]    = business.get("city", "")
        repl["business_brief_price"]   = business.get("price_level", "")
        repl["business_brief_hours"]   = business.get("hours", "")
        repl["business_brief_address"] = business.get("address", "")
        # `_url` is the token the template prints below the address — show the
        # clean domain (palmettobath.com), not the raw https://… URL. `_link`
        # keeps the full URL for any button/anchor href (when it fills a Beehiiv
        # URL field, the auto-prepended http:// still resolves the bare domain).
        repl["business_brief_url"]     = display_domain(bb_url) if bb_url else ""
        repl["business_brief_link"]    = bb_url  # full URL for href-wired slots
        repl["business_brief_domain"]  = display_domain(bb_url) if bb_url else ""
        # Google Places photo (when populated on the Notion row). Uploaded
        # to Beehiiv up front so the long Places media URL doesn't ever
        # need to re-resolve at email-render time, then swapped into the
        # template via the standard alt-text/filename-token mechanism.
        bb_photo = (business.get("photo_url") or "").strip()
        if bb_photo:
            hosted = upload_remote_image(client, publication_id, bb_photo)
            if hosted:
                image_swaps[bb_photo] = hosted
            alt_swaps["business_brief_image"] = hosted or bb_photo

    # ---- Sponsor Corner ----
    # User-curated: one approved sponsor per newsletter, populated by
    # hand in the Sponsor Corner Notion DB. Logo is usually a
    # Notion-uploaded file (the .file.url is a signed URL that expires
    # in ~1 hour), so we upload it to Beehiiv's image hosting up front
    # to get a stable URL the email can keep loading from.
    sponsor = get_sponsor(newsletter_name)
    if sponsor and sponsor.get("name"):
        repl["sponsor_name"]    = sponsor.get("name", "")
        paragraph_prose["sponsor_blurb"] = sponsor.get("blurb", "")
        repl["sponsor_blurb"]   = md_to_html(sponsor.get("blurb", ""))
        repl["sponsor_hours"]   = sponsor.get("hours", "")
        sp_url = (sponsor.get("website") or "").strip()
        repl["sponsor_url"]     = sp_url
        repl["sponsor_link"]    = sp_url  # alias
        repl["sponsor_domain"]  = display_domain(sp_url) if sp_url else ""
        logo = sponsor.get("logo_url") or ""
        if logo:
            hosted = upload_remote_image(client, publication_id, logo)
            if hosted:
                image_swaps[logo] = hosted
            alt_swaps["sponsor_logo"] = hosted or logo
        sponsor_img = sponsor.get("image_url") or ""
        if sponsor_img:
            hosted = upload_remote_image(client, publication_id, sponsor_img)
            if hosted:
                image_swaps[sponsor_img] = hosted
            alt_swaps["sponsor_image"] = hosted or sponsor_img

    # ---- Meme Corner ----
    # Expanded later by expand_meme_slots once we have the live HTML;
    # here we just fetch + return the list. Image URLs are Reddit-hosted
    # so no upload to Beehiiv is needed (Reddit's CDN allows hotlinking).
    memes = get_memes(newsletter_name)

    # ---- Insurance Tip ----
    tip = get_latest_tip(newsletter_name)
    if tip and tip.get("blurb"):
        title = (tip.get("tip_title") or "").strip()
        blurb = (tip.get("blurb") or "").strip()
        # The skill embeds the title at the top of the blurb (usually
        # as "💡 Insurance Tip: <title>" or just "<title>" bold). We
        # render the title separately via {insurance_tip_title}, so
        # strip that leading line from the blurb to avoid showing it
        # twice in the email.
        if title:
            paragraphs = blurb.split("\n\n")
            if paragraphs:
                first = paragraphs[0].strip()
                first_clean = first.lstrip("*_# ").rstrip("*_").strip()
                title_low = title.lower()
                # Drop the first paragraph if it's clearly the title
                # echo: contains the title text and is short enough
                # to plausibly be just a header line (not real prose).
                if (title_low in first_clean.lower()
                        and len(first_clean) <= len(title) + 60):
                    blurb = "\n\n".join(paragraphs[1:]).strip()

        # Strip trailing "📖 Learn more from <Source>" / markdown-link
        # attribution paragraphs from the blurb. The template has a
        # dedicated {insurance_tip_url} button (CTA) that points at the
        # same source, so keeping the inline link in the body shows
        # the same URL twice. The Notion landing page keeps the line
        # because it has no button equivalent there.
        paragraphs = blurb.split("\n\n")
        while paragraphs:
            last = paragraphs[-1].strip()
            last_low = last.lower()
            # Match short attribution-style closers:
            #   "📖 Learn more from FEMA"
            #   "Learn more at https://…"
            #   "[FEMA](https://…)"   (markdown-link only line)
            looks_attribution = (
                "learn more from" in last_low or
                "learn more at"   in last_low or
                last.startswith("📖") or
                last.startswith("🔗") or
                last.startswith("📰") or
                # Bare markdown link line (entire paragraph is one link)
                (re.match(r"^\s*\[[^\]]+\]\(https?://[^)]+\)\s*$", last) is not None)
            )
            # Only strip if the paragraph is short enough to be an
            # attribution (≤ 200 chars) — don't accidentally clip a
            # long body paragraph that happens to mention the source.
            if looks_attribution and len(last) <= 200:
                paragraphs.pop()
            else:
                break
        blurb = "\n\n".join(paragraphs).strip()

        repl["insurance_tip_title"]  = title
        paragraph_prose["insurance_tip_blurb"] = blurb
        repl["insurance_tip_blurb"]  = md_to_html(blurb)
        tip_url = tip.get("source_url", "") or ""
        repl["insurance_tip_url"]    = tip_url
        repl["insurance_tip_link"]   = tip_url  # alias
        repl["insurance_tip_source"] = tip.get("source_name", "")
        repl["insurance_tip_domain"] = display_domain(tip_url) if tip_url else ""
        # Static sponsor attribution — every tip row defaults to
        # "Peachy Insurance" / "https://peachyinsurance.com/" via
        # save_tips_to_notion. Surface as two placeholders so the
        # template can render "Brought to you by [sponsor]" however
        # the editor wants (linked text, button, full line).
        repl["insurance_tip_sponsor_name"] = (tip.get("sponsor_name") or "").strip()
        repl["insurance_tip_sponsor_url"]  = (tip.get("sponsor_url") or "").strip()
        repl["insurance_tip_sponsor_link"] = repl["insurance_tip_sponsor_url"]  # alias

    # ---- Pet ----
    pet = get_approved_pet(newsletter_name)
    if pet and pet.get("blurb"):
        repl["furry_friends_name"]   = pet.get("name", "")
        repl["PET_NAME"]             = pet.get("name", "")
        repl["PET_BLURB"]            = pet.get("blurb", "")
        repl["PET_SHELTER_NAME"]     = pet.get("shelter", "")
        repl["PET_SHELTER_ADDRESS"]  = pet.get("shelter_address", "")
        repl["PET_SHELTER_PHONE"]    = pet.get("shelter_phone", "")
        repl["PET_SHELTER_EMAIL"]    = pet.get("shelter_email", "")
        repl["PET_SHELTER_HOURS"]    = pet.get("shelter_hours", "")
        repl["PET_SOURCE_URL"]       = pet.get("url", "")
        # Prefer the animated gh-pages GIF, but a pet's GIF URL is written to
        # Notion at generation time and may be SENT a cycle later. If the
        # gh-pages file was wiped (e.g. an older newsletter's run) or never
        # published, the URL 404s — Beehiiv hotlinks live, so it renders broken
        # (Notion masks this via its server-side image cache). So check the GIF
        # first and, if it's unreachable, FALL BACK to the pet's live source
        # photo so the section still shows the pet instead of a broken image.
        gif_url   = (pet.get("gif") or "").strip()
        photo_url = (pet.get("photo") or "").strip()

        def _pet_img_reachable(u: str) -> bool:
            if not u:
                return False
            try:
                return requests.head(u, timeout=10,
                                     allow_redirects=True).status_code == 200
            except Exception as _e:
                print(f"  ⚠ [pet] reachability check failed for {u[:60]} ({_e})")
                return False

        if _pet_img_reachable(gif_url):
            img_url, _src = gif_url, "gif"
        elif _pet_img_reachable(photo_url):
            img_url, _src = photo_url, "photo"
            if gif_url:
                print("  ↳ [pet] GIF unreachable (404) — falling back to live source photo")
        else:
            # Nothing reachable: keep the best available URL so the section
            # still renders and the warning is actionable.
            img_url = gif_url or photo_url
            _src = "gif" if gif_url else ("photo" if photo_url else "none")
            if img_url:
                print(f"  ⚠ [pet] no reachable image URL (HTTP 404) — it will render "
                      f"broken in Beehiiv. Re-run the pet pipeline so the gh-pages GIF "
                      f"is (re)published, or re-approve a pet with a live image.")
        print(f"  [pet] image source={_src}: {img_url[:90] or '(empty)'}")
        if img_url:
            hosted = upload_remote_image(client, publication_id, img_url)
            if hosted:
                image_swaps[img_url] = hosted
            # Template uses both PET_IMAGE and PET_PHOTO as alt-text aliases
            alt_swaps["PET_IMAGE"] = hosted or img_url
            alt_swaps["PET_PHOTO"] = hosted or img_url

    # ---- Free Event ----
    free_text = get_latest_free_events(newsletter_name)
    if free_text:
        # Parse first event from the markdown section
        first_block = free_text.split("\n\n###")[0]
        lines = [ln for ln in first_block.splitlines() if ln.strip()]
        if lines:
            title = lines[0].lstrip("# ").strip()
            # Address line is typically the second line ("**Saturday, 10am-2pm** • Venue • audience")
            details = lines[1].strip() if len(lines) > 1 else ""
            # Join description chunks with a BLANK line between them so
            # expand_paragraph_field can split each markdown paragraph
            # (e.g. **What it is:** …, **Plan it:** …) into its own <p>.
            # Otherwise the whole thing renders as one wall of text in
            # Beehiiv even after markdown bold gets converted.
            description = "\n\n".join(lines[2:-1]).strip() if len(lines) > 2 else ""
            # The "More: [label](url)" is the last line — parse out URL if present
            link = ""
            for ln in reversed(lines):
                m = re.search(r"\((https?://[^\)]+)\)", ln)
                if m:
                    link = m.group(1)
                    break
            repl["free_event_title_1"]       = title
            repl["free_event_address_1"]     = md_to_html(details)
            paragraph_prose["free_event_description_1"] = description
            repl["free_event_description_1"] = md_to_html(description)
            repl["free_event_link_1"]        = link
            # Alias: template URL fields sometimes drop the trailing `_1`
            repl["free_event_link"]          = link

            # Event photo(s): 2+ pictures are combined into a single cycling
            # GIF (free_event_render_images → [gif_url]); 0-1 pass through as
            # individual images. Upload each to Beehiiv and collect the hosted
            # URLs; expand_free_event_images() later clones the template's
            # single free-event <img> block once per URL.
            for free_img in free_event_render_images(newsletter_name)[:3]:
                hosted = upload_remote_image(client, publication_id, free_img)
                if hosted:
                    image_swaps[free_img] = hosted
                free_event_images.append(hosted or free_img)

    # ---- Poll (inline HTML, click-tracked via Beehiiv's link analytics) ----
    # Beehiiv polls API is plan-locked — POST /polls returns 404 on this account.
    # Workaround: each option becomes a regular link with `?vote=<slug>`.
    # Beehiiv tracks clicks per URL → the click counts in Beehiiv's per-issue
    # dashboard give us the vote tally for free.
    #
    # IMPORTANT: only fill the SLUG, never a full URL.
    # Beehiiv's URL-field validator mangles full-URL placeholders (we ended up
    # with `http://http/https://...`). To work around it, the template URL field
    # holds the entire URL EXCEPT the slug — like:
    #     https://www.eastcobbconnect.com/?vote={poll_option_1_slug}
    # Beehiiv sees a valid URL and doesn't touch it. We just substitute the slug.
    poll = get_latest_poll(newsletter_name)
    if poll and poll.get("question"):
        repl["poll_question"] = poll["question"]
        for i, opt in enumerate(poll.get("options", [])[:5], start=1):
            opt_slug = re.sub(r"[^a-z0-9]+", "-", opt.lower().strip()).strip("-")
            repl[f"poll_option_{i}_label"] = opt
            repl[f"poll_option_{i}_slug"]  = opt_slug
        print(f"  ✓ Poll filled: '{poll['question'][:60]}…' "
              f"({len(poll.get('options') or [])} options)")

    # ---- Weekend Planner ----
    # Card text is substituted by expand_weekend_slots() AFTER this returns
    # (it duplicates the template card once per event). Here we only handle
    # the per-(day, audience) image (uploaded to Beehiiv and wired via the
    # alt-text/filename-token swap mechanism) plus the day-level date
    # strings for the section headers.
    weekend_events = get_weekend_events(newsletter_name)
    weekend_dates_seen: dict[str, str] = {}
    for slot_key, day, audience in WEEKEND_SLOT_KEYS:
        slot_events = [e for e in weekend_events
                       if e.get("day") == day and e.get("audience") == audience]
        if not slot_events:
            continue
        # Remember the date for this day so the section header can show e.g.
        # "Friday, May 22". Any event in this day-bucket has the same date.
        if day not in weekend_dates_seen:
            iso = slot_events[0].get("date") or ""
            if iso:
                try:
                    dt = datetime.fromisoformat(iso)
                    weekend_dates_seen[day] = f"{dt.strftime('%B')} {dt.day}"
                except Exception:
                    pass
        # Per-event images: the Weekend Planner pipeline now keeps up to 3
        # image-bearing events per slot. Host each (the WP images are already
        # gh-pages URLs, so upload_remote_image passes through) and write the
        # hosted URL back on the event dict — expand_weekend_slots() then
        # renders a photo INSIDE each event's card via _weekend_image_mutator.
        # We deliberately DON'T register a {slot_key}_image alt-swap anymore:
        # leaving the weekend slots out of alt_swaps lets prune_unused_image_
        # slots() remove the old single static slot placeholder, so per-card
        # images fully take over.
        for ev in slot_events:
            src = (ev.get("image_url") or "").strip()
            if not src:
                continue
            hosted = upload_remote_image(client, publication_id, src)
            if hosted and hosted != src:
                image_swaps[src] = hosted
                ev["image_url"] = hosted

    # Day-level date placeholders (used in the section headers).
    for day, friendly in weekend_dates_seen.items():
        repl[f"{day.lower()}_date"] = friendly

    return repl, image_swaps, alt_swaps, story_count, weekend_events, lowdown_stories, paragraph_prose, memes, free_event_images


def swap_images_by_alt(html: str, alt_swaps: dict[str, str]) -> tuple[str, int]:
    """Swap <img src> by detecting a filename token in the URL.

    Beehiiv strips alt text on user-uploaded images but PRESERVES the
    original filename in the hosted URL (e.g., `.../restaurant-radar-1.png`).
    So author renames each placeholder image to a known filename before
    uploading; we find <img> tags whose src URL contains that filename
    and replace the src with the real content URL.

    The map `alt_swaps` keys are logical slot names (e.g., 'restaurant_radar_image');
    we translate each to a filename token via SLOT_TO_FILENAME and match URL substrings.
    """
    if not alt_swaps:
        return html, 0

    # Map: logical slot key → filename token to look for in src URLs.
    # IMPORTANT: tokens must NOT collide with section-banner filenames
    # already in the template (restaurant-radar.png, furry-friends.png,
    # real-estate-corner.png are headers).
    # `restaurant-radar-1` etc. don't collide with `restaurant-radar` (substring).
    # `furry-friends` would collide → use `pet-photo` instead.
    SLOT_TO_FILENAME = {
        "newsletter_header_image":     "output-onlinepngtools",
        "event_of_the_week_image":     "event-of-the-week",
        "restaurant_radar_image":      "restaurant-radar-1",
        "restaurant_radar_2_image":    "restaurant-radar-2",
        "restaurant_radar_3_image":    "restaurant-radar-3",
        "restaurant_radar_4_image":    "restaurant-radar-4",
        "restaurant_radar_5_image":    "restaurant-radar-5",
        "real_estate_image_starter":   "real-estate-starter",
        "real_estate_image_sweetspot": "real-estate-sweetspot",
        "real_estate_image_showcase":  "real-estate-showcase",
        "PET_IMAGE":                   "pet-photo",
        "PET_PHOTO":                   "pet-photo",
        "free_event_image_1":          "free-event",
        "business_brief_image":        "business-brief.png",
        "sponsor_logo":                "sponsor_logo",
        "sponsor_image":               "sponsor_image",
        "friday_family_image":         "family_event_friday",
        "friday_adult_image":          "adult_event_friday",
        "saturday_family_image":       "family_event_saturday",
        "saturday_adult_image":        "adult_event_saturday",
        "sunday_family_image":         "family_event_sunday",
        "sunday_adult_image":          "adult_event_sunday",
    }

    out = html
    total_swaps = 0
    for slot_key, new_src in alt_swaps.items():
        token = SLOT_TO_FILENAME.get(slot_key)
        if not token:
            print(f"    · slot='{slot_key}' — no filename token configured")
            continue

        # Match <img ...src="...{token}...">  (token must appear in the src URL)
        pat = re.compile(
            r'(<img\b[^>]*\bsrc\s*=\s*["\'])([^"\']*' + re.escape(token) + r'[^"\']*)(["\'][^>]*>)',
            re.IGNORECASE,
        )

        def _replace_src(m: re.Match) -> str:
            return f"{m.group(1)}{new_src}{m.group(3)}"

        out, n = pat.subn(_replace_src, out)
        if n:
            total_swaps += n
            print(f"    ✓ slot='{slot_key}' (token='{token}') → swapped {n} <img>")
        else:
            print(f"    · slot='{slot_key}' (token='{token}') — no <img> with that filename")
    return out, total_swaps


def wrap_images_with_links(html: str, links: dict[str, str]) -> tuple[str, int]:
    """For each (filename_token, click_url) pair, find <img> tags whose
    src contains the token and wrap them in <a href="click_url">…</a>.
    Skips images that are already inside an <a>. Used to auto-link
    sponsor logos/images to {sponsor_url} so the user doesn't have to
    manually set the click-through in the Beehiiv editor.

    `links` shape: {filename_token: click_through_url}. Empty values
    skip that slot.
    """
    if not links:
        return html, 0

    out = html
    total = 0
    for token, click_url in links.items():
        if not token or not click_url:
            continue
        # Match <img …src="…token…"…>, but only when NOT already preceded
        # by an open <a tag with no </a> in between. Cheap heuristic
        # via negative-lookbehind on `</a>?` then `<a [^>]*>` proximity
        # is hard in Python re, so do a simple two-pass:
        # 1. Find all matching <img> with their positions.
        # 2. For each, check the substring just before — if a recent
        #    <a … is open (no intervening </a>), skip; else wrap.
        img_pat = re.compile(
            r'<img\b[^>]*\bsrc\s*=\s*["\'][^"\']*' + re.escape(token) + r'[^"\']*["\'][^>]*>',
            re.IGNORECASE,
        )
        replacements = []
        for m in img_pat.finditer(out):
            before = out[:m.start()]
            # Look for the last <a … in `before`; if there's no </a> after it, the <img> is wrapped.
            last_a_open = before.rfind("<a ")
            if last_a_open != -1:
                last_a_close = before.find("</a>", last_a_open)
                if last_a_close == -1:
                    continue  # already inside an <a>
            wrapped = f'<a href="{click_url}" target="_blank" rel="noopener">{m.group(0)}</a>'
            replacements.append((m.start(), m.end(), wrapped))
        if not replacements:
            continue
        # Apply replacements right-to-left so earlier offsets stay valid.
        for start, end, wrapped in reversed(replacements):
            out = out[:start] + wrapped + out[end:]
        total += len(replacements)
        print(f"    ✓ wrapped {len(replacements)} <img> (token='{token}') in <a href={click_url[:40]}…>")
    return out, total


def prune_unused_image_slots(html: str, alt_swaps: dict[str, str]) -> tuple[str, int]:
    """For image slots that are present in the template but have no real
    image URL in `alt_swaps`, remove the placeholder <img> tag entirely so
    the email doesn't show a generic placeholder.

    Uses the same SLOT_TO_FILENAME mapping as swap_images_by_alt — if a
    template <img>'s src contains a known filename token AND we don't have
    a swap target for that slot, the <img> is removed.
    """
    SLOT_TO_FILENAME = {
        "newsletter_header_image":     "output-onlinepngtools",
        "event_of_the_week_image":     "event-of-the-week",
        "restaurant_radar_image":      "restaurant-radar-1",
        "restaurant_radar_2_image":    "restaurant-radar-2",
        "restaurant_radar_3_image":    "restaurant-radar-3",
        "restaurant_radar_4_image":    "restaurant-radar-4",
        "restaurant_radar_5_image":    "restaurant-radar-5",
        "real_estate_image_starter":   "real-estate-starter",
        "real_estate_image_sweetspot": "real-estate-sweetspot",
        "real_estate_image_showcase":  "real-estate-showcase",
        "PET_IMAGE":                   "pet-photo",
        "PET_PHOTO":                   "pet-photo",
        "free_event_image_1":          "free-event",
        "business_brief_image":        "business-brief.png",
        "sponsor_logo":                "sponsor_logo",
        "sponsor_image":               "sponsor_image",
        "friday_family_image":         "family_event_friday",
        "friday_adult_image":          "adult_event_friday",
        "saturday_family_image":       "family_event_saturday",
        "saturday_adult_image":        "adult_event_saturday",
        "sunday_family_image":         "family_event_sunday",
        "sunday_adult_image":          "adult_event_sunday",
    }

    out = html
    pruned = 0
    for slot_key, token in SLOT_TO_FILENAME.items():
        if slot_key in alt_swaps:
            continue  # we have an image for this slot — keep the <img>
        # No image for this slot. Remove any <img> whose src contains the token.
        pat = re.compile(
            r'<img\b[^>]*\bsrc\s*=\s*["\'][^"\']*' + re.escape(token) + r'[^"\']*["\'][^>]*>',
            re.IGNORECASE,
        )
        out, n = pat.subn("", out)
        if n:
            pruned += n
            print(f"    ✗ pruned placeholder <img> for missing slot='{slot_key}' (token='{token}')")
    return out, pruned


# ---------------------------------------------------------------------------
# 5. POLL
# ---------------------------------------------------------------------------
def attach_poll_to_post(client: BeehiivClient, publication_id: str,
                       newsletter_name: str) -> dict | None:
    """If a poll exists for this newsletter, create it in Beehiiv and return the poll record.
    Caller can embed the poll's id/url into the post body if Beehiiv requires it."""
    poll = get_latest_poll(newsletter_name)
    if not poll or not poll.get("question"):
        return None
    options = poll.get("options") or []
    if not options:
        return None
    try:
        result = client.create_poll(
            publication_id,
            title=poll["question"],
            options=options[:4],
        )
        print(f"    ✓ Created Beehiiv poll: {result.get('id', '?')}")
        return result
    except BeehiivError as e:
        print(f"    ⚠ Could not create Beehiiv poll (will skip): {e}")
        return None


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
def main():
    cfg = NEWSLETTER_CONFIG.get(NEWSLETTER)
    if not cfg:
        sys.exit(f"Unknown NEWSLETTER '{NEWSLETTER}'. Known: {list(NEWSLETTER_CONFIG)}")
    if not cfg["publication_id"]:
        sys.exit(f"BEEHIIV_*_PUBLICATION_ID not set for {NEWSLETTER}")
    if not cfg["template_post_id"]:
        sys.exit(f"BEEHIIV_*_TEMPLATE_POST_ID not set for {NEWSLETTER}")

    print(f"Sending {NEWSLETTER} to Beehiiv (status: {STATUS})…")
    print(f"  Publication: {cfg['publication_id']}  (len={len(cfg['publication_id'])})")
    print(f"  Template:    {cfg['template_post_id']}  (len={len(cfg['template_post_id'])}, prefix_ok={cfg['template_post_id'].startswith('post_')})")

    # Sync any manual edits made on the Notion landing page back to the
    # underlying section DBs, so get_* below reads the freshest content.
    display_name = NEWSLETTER.replace("_", " ")
    landing_page_title = f"{display_name} — Current Edition"
    print(f"\n  Syncing landing page edits back to DBs ({landing_page_title})…")
    landing_page_id = notion_search_page(landing_page_title)
    if landing_page_id:
        try:
            sync_edits_back(landing_page_id, NEWSLETTER)
            print(f"  ✓ Sync complete")
        except Exception as e:
            print(f"  ⚠ sync_edits_back failed: {e} (continuing with DB content)")
    else:
        print(f"  ⚠ Landing page not found — using DB content as-is")

    client = BeehiivClient()

    # Fetch the template post body
    print("\n  Fetching template post body…")
    template_post = client.get_post(
        cfg["publication_id"],
        cfg["template_post_id"],
        expand=["free_email_content", "free_web_content", "premium_email_content"],
    )
    # Beehiiv stores body under content.free.email (string of HTML).
    # Fall back to other shapes in case a higher tier exposes things differently.
    content_node = template_post.get("content") or {}
    body_html = ""
    if isinstance(content_node, dict):
        free = content_node.get("free") or {}
        # `email` and `web` are HTML strings under content.free
        body_html = free.get("email") or free.get("web") or ""
        if not body_html:
            premium = content_node.get("premium") or {}
            body_html = premium.get("email") or premium.get("web") or ""
    if not body_html:
        body_html = (
            template_post.get("free_email_content")
            or template_post.get("free_web_content")
            or template_post.get("body_html")
            or template_post.get("content_html")
            or ""
        )

    if not body_html:
        # Last-ditch: dump the keys so we can adjust
        print("  ⚠ Could not locate body HTML in template response. Top-level keys:")
        print(f"    {list(template_post.keys())}")
        sys.exit("No body found in template post — check the API response shape.")

    print(f"  Template body: {len(body_html):,} characters")

    # Gather all section data + upload images
    print("\n  Gathering section data + uploading images…")
    repl, image_swaps, alt_swaps, story_count, weekend_events, lowdown_stories, paragraph_prose_fields, memes, free_event_images = build_replacements(
        client, cfg["publication_id"], NEWSLETTER,
    )

    # Dynamic-slot expansions must run BEFORE prune_empty_slots so the
    # pruner doesn't yank the original (unfilled) template card thinking
    # it's an empty slot, and BEFORE replace_placeholders because the
    # substitutions for the duplicated cards happen inline during
    # expansion.
    print("\n  Expanding Weekend Planner slots…")
    body_html = expand_weekend_slots(body_html, weekend_events)
    print("\n  Expanding Local Lowdown slots…")
    body_html = expand_lowdown_slots(body_html, lowdown_stories)
    print("\n  Expanding Meme Corner slots…")
    body_html = expand_meme_slots(body_html, memes)
    print("\n  Expanding Free Event images…")
    body_html = expand_free_event_images(body_html, free_event_images)

    # Long-form prose fields: split each into real sibling <p> blocks via
    # DOM ops so Beehiiv renders paragraph spacing. Done here (vs. inside
    # build_replacements) because we need the live HTML to find the
    # placeholder's wrapping <p>.
    print("\n  Expanding paragraph prose fields…")
    for key, raw_text in (paragraph_prose_fields or {}).items():
        if not raw_text:
            print(f"    · {key}: skipped (empty value)")
            continue
        token = "{" + key + "}"
        was_in_template = token in body_html
        body_html = expand_paragraph_field(body_html, key, raw_text)
        still_present = token in body_html
        if was_in_template and not still_present:
            print(f"    ✓ {key}: DOM-expanded ({raw_text.count(chr(10) + chr(10)) + 1} para(s))")
            # Placeholder is gone now — drop from repl so the later
            # string-replace doesn't overwrite anywhere else.
            repl.pop(key, None)
        elif was_in_template:
            print(f"    · {key}: token present but DOM expansion couldn't find wrapping <p> "
                  f"— will fall through to plain string substitution")
        else:
            print(f"    · {key}: {{{key}}} not in template, skipping")

    # Apply text placeholder replacements
    # Prune unused repeating slots (restaurants 4-5, lowdown 4-5, etc.)
    # BEFORE replacing placeholders so we can anchor on the literal {token}.
    print("\n  Pruning empty slots…")
    pruned_body = prune_empty_slots(body_html, repl)

    new_body = replace_placeholders(pruned_body, repl)

    # Apply image URL string-swaps (covers cases where the template already
    # references the original gh-pages URL directly)
    for orig, hosted in image_swaps.items():
        new_body = new_body.replace(orig, hosted)

    # Apply alt-text → src swaps for <img> tags. This is how the template's
    # placeholder images get replaced with the actual content image. Author
    # sets alt='restaurant_radar_image' etc. on each placeholder image in
    # Beehiiv editor; we find by alt and replace its src.
    # DEBUG: dump every <img> tag so we can see how Beehiiv stores alt text
    img_tags = re.findall(r"<img\b[^>]*>", new_body, re.IGNORECASE)
    print(f"\n  [debug] Found {len(img_tags)} <img> tags in body. Listing src-only filenames:")
    for i, t in enumerate(img_tags):
        m = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', t, re.IGNORECASE)
        src = m.group(1) if m else "(no src)"
        # Just the filename portion at the tail of the URL (last `/` segment, stripping query)
        tail = src.split("?")[0].rstrip("/").split("/")[-1] if "/" in src else src
        print(f"    [{i:2d}] …/{tail}   (full src len={len(src)})")
    print("\n  Swapping image src by filename token (Beehiiv strips alt; filenames persist)…")
    new_body, alt_swap_count = swap_images_by_alt(new_body, alt_swaps)
    print(f"  Image alt-swaps applied: {alt_swap_count}")

    # Auto-wrap sponsor logo + sponsor image in <a href="{sponsor_url}">
    # so both are clickable without manual editor work. Keyed by the
    # FILENAME TOKEN the image carries (after swap, the src still
    # contains that token in its filename portion). Only wraps when the
    # image isn't already inside an <a>.
    sponsor_click = (repl.get("sponsor_url") or "").strip()
    if sponsor_click:
        print("\n  Auto-wrapping sponsor images in clickable <a>…")
        new_body, link_wraps = wrap_images_with_links(new_body, {
            "sponsor_logo":  sponsor_click,
            "sponsor_image": sponsor_click,
        })
        print(f"  Sponsor image wraps: {link_wraps}")

    # Remove placeholder <img> tags for sections we have no image for
    # (e.g., free event has no og:image — strip the placeholder so the email
    # doesn't show a generic stock image).
    print("\n  Pruning unused image placeholders…")
    new_body, pruned_imgs = prune_unused_image_slots(new_body, alt_swaps)
    print(f"  Image placeholders removed: {pruned_imgs}")

    # Generate subject line
    print("\n  Generating subject line…")
    subject_ctx = {
        "newsletter_name":   NEWSLETTER,
        "publication_date":  datetime.today().strftime("%Y-%m-%d"),
        "featured_event":    {k: repl.get(f"event_of_the_week_{k}", "") for k in ("headline", "description")} if repl.get("event_of_the_week_headline") else None,
        "tier1_restaurant":  {"name": repl.get("restaurant_radar_name", "")} if repl.get("restaurant_radar_name") else None,
        "top_news_headline": repl.get("local_lowdown1_title", ""),
        "pet":               {"name": repl.get("PET_NAME", "")} if repl.get("PET_NAME") else None,
        "free_event":        {"name": repl.get("free_event_title_1", "")} if repl.get("free_event_title_1") else None,
    }
    subject = generate_subject_line(subject_ctx)
    print(f"  📧 Subject: {subject}")

    # Title — same as subject for v1 (Beehiiv distinguishes title vs subject_line)
    title = f"{NEWSLETTER.replace('_', ' ')} — {datetime.today().strftime('%B %d, %Y')}"

    # The header image is generated + published to gh-pages by the
    # Featured Event picker (or the review-app image picker) using a
    # PER-EVENT filename `Newsletter_Header_image_<NL>_<EventName>.png`.
    # Read the currently-approved event's Header Image URL from Notion
    # so the thumbnail matches the featured event being shipped. Falls
    # back to the legacy generic filename only if the per-event URL
    # isn't set (e.g. legacy rows from before the per-event rename).
    _thumb_event = get_featured_event(NEWSLETTER) or {}
    thumbnail_url = (_thumb_event.get("header_image_url") or "").strip() or (
        f"https://peachyinsurance.github.io/newsletters/gifs/"
        f"Newsletter_Header_image_{NEWSLETTER}.png"
    )

    # Create the post
    print(f"\n  Creating Beehiiv post (status: {STATUS})…")
    print(f"  Thumbnail: {thumbnail_url}")
    new_post = client.create_post(
        cfg["publication_id"],
        title=title,
        subject_line=subject,
        content_html=new_body,
        status=STATUS,
    )
    new_post_id = new_post.get("id", "")
    print(f"  ✓ Post created: {new_post_id}")

    # Beehiiv's per-post thumbnail API is plan-locked: POST/PATCH `thumbnail_url`
    # are silently dropped and the post falls back to the publication default
    # logo. Set the thumbnail manually in the editor instead — the gh-pages
    # composite URL below is the same image we feed into the email body.
    print()
    print("  📌 MANUAL STEP: upload this thumbnail in the Beehiiv editor:")
    print(f"     {thumbnail_url}")
    print(f"     (Open the post → top of page → Add/Change thumbnail → paste or upload)")

    # Native Beehiiv polls API is plan-locked (POST /polls returns 404).
    # We use inline HTML poll instead — the {poll_question} + {poll_option_N_*}
    # placeholders in the template are filled by build_replacements above.
    # Beehiiv's link-click analytics dashboard captures votes per option URL.

    # Print URL hint
    print()
    print("=" * 60)
    print(f"✓ Sent to Beehiiv: {new_post_id}")
    # Beehiiv UI URLs use the bare UUID, not the API's `post_` prefix
    ui_post_id = new_post_id[5:] if new_post_id.startswith("post_") else new_post_id
    print(f"  Edit in Beehiiv: https://app.beehiiv.com/posts/{ui_post_id}/edit")
    print("=" * 60)


if __name__ == "__main__":
    main()
