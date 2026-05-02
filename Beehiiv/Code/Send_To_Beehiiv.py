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
    get_latest_poll,
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


def _normalize_post_id(raw: str) -> str:
    """Beehiiv's API requires post IDs prefixed with 'post_', but the dashboard
    URL only shows the bare UUID. Auto-prefix so a secret containing either form
    works."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("post_"):
        return raw
    return f"post_{raw}"


# Per-newsletter Beehiiv config — extend when PP is added
NEWSLETTER_CONFIG = {
    "East_Cobb_Connect": {
        "publication_id":   os.environ.get("BEEHIIV_ECC_PUBLICATION_ID", "").strip(),
        "template_post_id": _normalize_post_id(os.environ.get("BEEHIIV_ECC_TEMPLATE_POST_ID", "")),
        "display_area":     "East Cobb",
    },
    # "Perimeter_Post": {
    #     "publication_id":   os.environ.get("BEEHIIV_PP_PUBLICATION_ID", ""),
    #     "template_post_id": os.environ.get("BEEHIIV_PP_TEMPLATE_POST_ID", ""),
    #     "display_area":     "Perimeter",
    # },
}

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
                system=skill,
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


def replace_placeholders(html: str, replacements: dict[str, str]) -> str:
    """Replace `{placeholder}` tokens (in any HTML-encoded form) with values.
    Unset placeholders are left alone (visible in the draft so editor can spot them)."""
    out = html
    hits = 0
    for key, value in replacements.items():
        replacement = value or ""
        for token in _placeholder_variants(key):
            if token in out:
                out = out.replace(token, replacement)
                hits += 1
    print(f"  Placeholder replacements applied: {hits} matches")
    return out


def hide_unused_lowdown_slots(html: str, used_count: int) -> str:
    """For Local Lowdown placeholders we don't fill (e.g., we have 3 stories, slots
    4-5 are unused), wipe the remaining placeholders so they don't render literally."""
    for n in range(used_count + 1, 6):
        for key in (f"local_lowdown{n}_title", f"local_lowdown{n}_message"):
            for token in _placeholder_variants(key):
                html = html.replace(token, "")
    return html


# ---------------------------------------------------------------------------
# 4. SECTION DATA → REPLACEMENT MAP
# ---------------------------------------------------------------------------
def build_replacements(client: BeehiivClient, publication_id: str,
                      newsletter_name: str) -> tuple[dict, dict, int]:
    """Pull section data, upload images, return:
      (text_replacements, image_url_swaps, alt_image_swaps, lowdown_story_count)

    text_replacements: {placeholder_key: string_value}
    image_url_swaps:   {original_url: beehiiv_hosted_url}  — used as fallback string-find
    alt_image_swaps:   {alt_text: image_url}  — used to swap <img alt="..." src="...">
    lowdown_story_count: number of stories actually used (0-5)
    """
    repl: dict[str, str] = {}
    image_swaps: dict[str, str] = {}
    alt_swaps: dict[str, str] = {}

    # ---- Welcome Intro ----
    intro = get_latest_intro(newsletter_name)
    if intro:
        intro_msg = ((intro.get("greeting") or "") + "\n\n" + (intro.get("blurb") or "")).strip()
        repl["intro_message"] = intro_msg

    # ---- Featured Event ----
    event = get_featured_event(newsletter_name)
    if event and event.get("blurb"):
        repl["event_of_the_week_headline"]    = event.get("event_name", "")
        repl["event_of_the_week_description"] = event.get("blurb", "")
        repl["event_of_the_week_link"]        = event.get("ticket_url") or event.get("source_url") or ""
        ev_img = event.get("image_url") or event.get("photo") or ""
        if ev_img:
            hosted = upload_remote_image(client, publication_id, ev_img)
            if hosted:
                image_swaps[ev_img] = hosted
            alt_swaps["event_of_the_week_image"] = hosted or ev_img

    # ---- Restaurants (Tier 1 + others) ----
    restaurants = get_restaurants(newsletter_name)
    # restaurants is sorted Tier 1 first, then Tier 2 by score
    tier1 = next((r for r in restaurants if r["tier"] == "Tier 1 Winner"), None)
    others = [r for r in restaurants if r["tier"] != "Tier 1 Winner"]
    if tier1:
        repl["restaurant_radar_name"]    = tier1.get("name", "")
        repl["restaurant_radar_message"] = tier1.get("blurb", "")
        # upload tier 1 image
        img_url = tier1.get("gif") or tier1.get("photo")
        if img_url:
            hosted = upload_remote_image(client, publication_id, img_url)
            if hosted:
                image_swaps[img_url] = hosted
            alt_swaps["restaurant_radar_image"] = hosted or img_url
    for i, r in enumerate(others[:2], start=2):
        repl[f"restaurant_radar_{i}_name"]    = r.get("name", "")
        repl[f"restaurant_radar_{i}_message"] = r.get("blurb", "")
        img_url = r.get("gif") or r.get("photo")
        if img_url:
            hosted = upload_remote_image(client, publication_id, img_url)
            if hosted:
                image_swaps[img_url] = hosted
            alt_swaps[f"restaurant_radar_{i}_image"] = hosted or img_url

    # ---- Real Estate ----
    # Tier names from RE corner: "Starter Home", "Sweet Spot", "Showcase"
    re_listings = get_real_estate(newsletter_name)
    re_tier_to_alt = {
        "Starter Home": "real_estate_image_starter",
        "Sweet Spot":   "real_estate_image_sweetspot",
        "Showcase":     "real_estate_image_showcase",
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

    # ---- Local Lowdown (1–5 stories) ----
    lowdown_text = get_latest_lowdown(newsletter_name)
    story_count = 0
    if lowdown_text:
        # Parse the markdown: ### {emoji} {headline}\n\n{body}\n\nMore: ...
        sections = re.split(r"\n(?=### )", lowdown_text.strip())
        for i, section in enumerate(sections[:5], start=1):
            lines = section.splitlines()
            heading = lines[0].lstrip("# ").strip() if lines else ""
            body_lines = [ln for ln in lines[1:] if ln.strip()]
            body = "\n".join(body_lines).strip()
            repl[f"local_lowdown{i}_title"]   = heading
            repl[f"local_lowdown{i}_message"] = body
            story_count += 1

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
        img_url = pet.get("gif") or pet.get("photo")
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
            description = "\n".join(lines[2:-1]).strip() if len(lines) > 2 else ""
            # The "More: [label](url)" is the last line — parse out URL if present
            link = ""
            for ln in reversed(lines):
                m = re.search(r"\((https?://[^\)]+)\)", ln)
                if m:
                    link = m.group(1)
                    break
            repl["free_event_title_1"]       = title
            repl["free_event_address_1"]     = details
            repl["free_event_description_1"] = description
            repl["free_event_link_1"]        = link

    return repl, image_swaps, alt_swaps, story_count


def swap_images_by_alt(html: str, alt_swaps: dict[str, str]) -> tuple[str, int]:
    """For each <img> tag whose alt attribute matches a key in alt_swaps,
    replace its src with the mapped URL. Returns (new_html, swap_count).

    Why alt-based and not src-based: the template's placeholder images have
    Beehiiv-hosted URLs we don't know in advance. Alt text is editor-controlled
    and stable. Author tags each placeholder image with alt='restaurant_radar_image'
    etc., and we find/replace by alt.
    """
    if not alt_swaps:
        return html, 0

    # Beehiiv may serialize alt text with HTML entities — accept both literal and encoded forms
    import html as _htmllib

    def _build_pattern(alt_value: str) -> re.Pattern:
        # Match an <img ...> tag where alt="<alt_value>" appears (any attribute order)
        # Also handle alt='...' single quotes
        encoded_alt = _htmllib.escape(alt_value, quote=True)
        alt_alts = {alt_value, encoded_alt}
        # Build the alt match group: alt="X" or alt='X' for any of the alts
        alt_group = "|".join(re.escape(a) for a in alt_alts)
        # Tolerant <img> matcher — we capture the whole tag, then replace its src=
        return re.compile(
            r'(<img\b[^>]*\balt\s*=\s*["\'](?:' + alt_group + r')["\'][^>]*>)',
            re.IGNORECASE,
        )

    out = html
    total_swaps = 0
    for alt_value, new_src in alt_swaps.items():
        pat = _build_pattern(alt_value)

        def _replace_src_in_tag(m: re.Match) -> str:
            tag = m.group(1)
            # Replace src="..." or src='...' with the new URL
            new_tag, n = re.subn(
                r'(\bsrc\s*=\s*)(["\'])[^"\']*\2',
                lambda mm: f'{mm.group(1)}"{new_src}"',
                tag,
                count=1,
                flags=re.IGNORECASE,
            )
            return new_tag if n else tag

        out, n = pat.subn(_replace_src_in_tag, out)
        if n:
            total_swaps += n
            print(f"    ✓ alt='{alt_value}' → swapped {n} <img> src")
        else:
            print(f"    · alt='{alt_value}' — no matching <img> tag found")
    return out, total_swaps


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
    repl, image_swaps, alt_swaps, story_count = build_replacements(client,
                                                                  cfg["publication_id"],
                                                                  NEWSLETTER)

    # Apply text placeholder replacements
    new_body = replace_placeholders(body_html, repl)
    new_body = hide_unused_lowdown_slots(new_body, story_count)

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
    print(f"\n  [debug] Found {len(img_tags)} <img> tags in body. Showing up to 8:")
    for t in img_tags[:8]:
        print(f"    {t[:300]}")
    print("\n  Swapping image src by alt-text…")
    new_body, alt_swap_count = swap_images_by_alt(new_body, alt_swaps)
    print(f"  Image alt-swaps applied: {alt_swap_count}")

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

    # Create the post
    print(f"\n  Creating Beehiiv post (status: {STATUS})…")
    new_post = client.create_post(
        cfg["publication_id"],
        title=title,
        subject_line=subject,
        content_html=new_body,
        status=STATUS,
    )
    new_post_id = new_post.get("id", "")
    print(f"  ✓ Post created: {new_post_id}")

    # Attempt to attach a native poll
    print("\n  Attempting to attach native poll…")
    attach_poll_to_post(client, cfg["publication_id"], NEWSLETTER)

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
