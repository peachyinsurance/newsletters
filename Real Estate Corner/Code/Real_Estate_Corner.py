#!/usr/bin/env python3
"""
Newsletter Automation - Real Estate Corner
Features a single Showcase home from Realtor.com via RapidAPI. Listings are
sorted by price (highest first), then up to 3 qualifying candidates are gathered
via a distance-escalation ladder (see collect_showcase_candidates):
  Phase 1 — within RE_RADIUS_MILES (5), top 15 homes by price.
  Phase 2 — if none, re-evaluate those top 15 within RE_FALLBACK_RADIUS_MILES (10).
  Phase 3 — if still none, resample the next 10 homes at the fallback radius,
            repeating down the price-sorted list.
Each candidate must also not have been previously featured. Claude then picks
the most compelling one (it never sees or generates the listing URL — that's
preserved from source). A blurb is generated and the pick is saved to Notion.

Previously-featured homes are excluded FOREVER (by listing URL and address):
cleanup_real_estate.py flips 'approved' → 'approved - old' but never archives
those rows, so the exclusion history is permanent.
"""
import os
import sys
import json
import time
import re
import re as _re   # alias used in the Showcase blurb price-stripper
import math
import random
from datetime import datetime
from pathlib import Path

import requests
import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import create_page, query_database, safe_str, HEADERS as NOTION_HEADERS
from url_validator import validate_url, filter_valid_items
from newsletters_config import NEWSLETTERS, filter_by_env

# ---------------------------------------------------------------------------
# 1. ENVIRONMENT & CONFIG
# ---------------------------------------------------------------------------
CLAUDE_API_KEY    = os.environ["CLAUDE_API_KEY"]
REALTOR_API_KEY   = os.environ["REALTOR_API_KEY"]
NOTION_API_KEY    = os.environ["NOTION_API_KEY"]

from voice_helper import with_voice  # noqa: E402
SKILL_PROMPT_PATH = Path(__file__).parent.parent.parent / "Skills" / "newsletter-real-estate-skill_auto.md"

REALTOR_HOST = "realtor-search.p.rapidapi.com"

# Showcase home distance ladder (miles from the newsletter's central zip, whose
# lat/lng live in newsletters_config). Per-newsletter overrides: "re_radius_miles"
# and "re_fallback_radius_miles".
#   Phase 1: PRIMARY radius, top (PHASE1_BATCH × PHASE1_BATCHES) homes by price.
#   Phase 2: if NOTHING within PRIMARY, re-evaluate those same top homes at FALLBACK.
#   Phase 3: if still nothing, resample the next PHASE3_BATCH homes at FALLBACK,
#            repeating down the price-sorted list until candidates are found.
RE_RADIUS_MILES = 5.0           # primary
RE_FALLBACK_RADIUS_MILES = 10.0  # widened fallback
CANDIDATES_WANTED = 3            # how many qualified homes to hand Claude
PHASE1_BATCH = 5                 # "samplings" of 5…
PHASE1_BATCHES = 3               # …×3 = top 15 homes scanned at the primary radius
PHASE3_BATCH = 10               # resample window once we've widened to fallback


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points, in miles."""
    R = 3958.7613  # Earth radius in miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def listing_distance_miles(raw: dict, center_lat: float, center_lng: float) -> float | None:
    """Distance in miles from (center_lat, center_lng) to a raw API listing's
    coordinate (location.address.coordinate.{lat,lon}). Returns None if the
    listing has no usable coordinate so callers can decide how to treat it."""
    if center_lat is None or center_lng is None:
        return None
    coord = (((raw.get("location") or {}).get("address") or {}).get("coordinate")) or {}
    lat = coord.get("lat")
    lng = coord.get("lon", coord.get("lng"))
    if lat is None or lng is None:
        return None
    try:
        return _haversine_miles(center_lat, center_lng, float(lat), float(lng))
    except (TypeError, ValueError):
        return None


def normalize_address(addr: str) -> str:
    """Lowercase, collapse whitespace — a stable key for de-duping listings by
    street address when the listing URL differs run-to-run."""
    return re.sub(r"\s+", " ", (addr or "").strip().lower())

# ---------------------------------------------------------------------------
# 2. LOAD SKILL PROMPT
# ---------------------------------------------------------------------------
def load_skill_prompt() -> str:
    if SKILL_PROMPT_PATH.exists():
        return SKILL_PROMPT_PATH.read_text(encoding="utf-8")
    return "You are a local newsletter writer. Write short, neighbor-style real estate blurbs."

# ---------------------------------------------------------------------------
# 3. FETCH LISTINGS FROM REALTOR.COM API
# ---------------------------------------------------------------------------
def fetch_listings(location: str, limit: int = 100) -> list[dict]:
    """Fetch listings from Realtor.com API, paginating to collect up to `limit` rows.

    The /properties/search-buy endpoint caps at 20 per request, so we issue
    multiple calls with increasing `offset`. Stops early when:
      - we've collected `limit` rows
      - the API returns fewer than 20 (last page)
      - we hit MAX_PAGES safety cap to avoid runaway cost
    """
    headers = {
        "Content-Type": "application/json",
        "x-rapidapi-host": REALTOR_HOST,
        "x-rapidapi-key": REALTOR_API_KEY,
    }

    # API caps at 20 rows per request regardless of requested `limit`.
    # We paginate via `page` until we have enough rows or run out of pages.
    # Last-page detection uses meta.totalPage — NOT a "got fewer rows than
    # requested" heuristic, since the API always returns 20 and ignores higher.
    REQUESTED_LIMIT = 50  # ask for more; API will silently cap at 20
    MAX_PAGES = 8         # safety: 8 calls × 20 = up to 160 listings
    collected: list[dict] = []
    seen_ids: set = set()
    api_page_size = None
    total_pages = None

    last_page = 0
    for page in range(1, MAX_PAGES + 1):
        params = {
            "location": location,
            "limit":    str(REQUESTED_LIMIT),
            "page":     str(page),
            # Most-expensive first so the highest-priced in-range home surfaces
            # on the early pages (we only feature one Showcase home now).
            "sortBy":   "highest_price",
        }
        try:
            res = requests.get(
                f"https://{REALTOR_HOST}/properties/search-buy",
                headers=headers,
                params=params,
                timeout=30,
            )
        except Exception as e:
            print(f"    API error on page {page}: {e}")
            break

        if res.status_code != 200:
            print(f"    API error {res.status_code} on page {page}: {res.text[:200]}")
            break

        body = res.json()
        data = body.get("data", {})
        meta = body.get("meta", {})
        results = data.get("results", []) or []

        if page == 1:
            api_page_size = meta.get("limit") or len(results) or 20
            total_pages   = meta.get("totalPage")
            print(f"  Realtor URL (page 1): {res.url}")
            print(f"  API meta: limit={api_page_size} totalRecords={meta.get('totalRecords')} totalPage={total_pages}")

        new_rows = 0
        for r in results:
            pid = r.get("property_id")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            collected.append(r)
            new_rows += 1

        print(f"    page {page}: returned {len(results)} rows, +{new_rows} new (total: {len(collected)})")
        last_page = page

        # Stop conditions
        if len(collected) >= limit:
            break
        if total_pages and page >= total_pages:
            break
        # Got an empty page → no more data
        if not results:
            break
        # Got fewer than the API's actual page size → last page
        if api_page_size and len(results) < api_page_size:
            break

    print(f"  Got {len(collected)} listings from API across {last_page} page(s)")
    return collected[:limit]


def has_valid_photo(listing: dict) -> bool:
    """Check if a listing has a real photo (not a placeholder)."""
    photo = listing.get("primary_photo", {}).get("href", "")
    if not photo:
        return False
    # 'l-f' in URL = placeholder/coming soon, 'l-m' = real photo
    if "l-f" in photo:
        return False
    return True


def is_available_for_sale(listing: dict) -> bool:
    """Drop pending, under contract, or coming-soon listings — we only feature
    listings a reader can actually shop today."""
    status = (listing.get("status") or "").lower()
    flags = listing.get("flags") or {}
    if status in ("pending", "contingent", "coming_soon", "under_contract", "sold"):
        return False
    if flags.get("is_pending"):
        return False
    if flags.get("is_contingent"):
        return False
    if flags.get("is_coming_soon"):
        return False
    return True


def filter_by_tier(listings: list[dict], min_price: int, max_price: int | None,
                   min_beds: int = 0, min_baths: int = 0,
                   type_filter: str | None = None) -> list[dict]:
    """Filter raw listings by price range, bed/bath minimums, property type, and valid photo."""
    filtered = []
    for r in listings:
        price = r.get("list_price", 0) or 0
        beds = r.get("description", {}).get("beds", 0) or 0
        baths = r.get("description", {}).get("baths", 0) or 0
        prop_type = r.get("description", {}).get("type", "") or ""

        if price < min_price:
            continue
        if max_price and price > max_price:
            continue
        if beds < min_beds or baths < min_baths:
            continue
        if type_filter and prop_type != type_filter:
            continue
        if not has_valid_photo(r):
            continue
        filtered.append(r)
    return filtered


def build_listing_url(raw: dict) -> str:
    """Get listing URL from API href. Returns empty string if missing."""
    href = raw.get("href", "")
    if href.startswith("https://www.realtor.com"):
        return href
    if href.startswith("/"):
        return f"https://www.realtor.com{href}"
    return ""


def parse_listing(raw: dict) -> dict:
    """Parse a raw API listing into a clean dict."""
    loc = raw.get("location", {}).get("address", {})
    desc = raw.get("description", {})
    listing_url = build_listing_url(raw)

    # Get full-size photos (Realtor's API often returns thumbnails).
    # Realtor CDN URLs end with a single-letter size code: s/m/n/l/p/x.jpg → tiny→large.
    # `od.jpg` (or `od-w####_h####_x2.webp`) is the original/full-resolution variant.
    # We rewrite any single-letter size suffix to `od.jpg` to force a hi-res fetch.
    import re as _re

    def _upgrade_photo(url):
        if not url:
            return ""
        url = url.replace("http://", "https://")
        # Realtor's CDN encodes size as a single letter immediately before `.jpg`,
        # following the asset hash digits. Examples we see in the wild:
        #     .../<hash>s.jpg   →  small  (~120x80 thumbnail)
        #     .../<hash>m.jpg   →  medium
        #     .../<hash>l.jpg   →  large
        #     .../<hash>od.jpg  →  original / full-resolution
        # We rewrite any single-letter size code to `od` for full quality.
        # Anchored with a preceding digit so we don't accidentally chop the last
        # letter of a non-Realtor URL or a hash that happens to end in s/m/l.
        url = _re.sub(r"(\d)[smnlpx]\.jpg(\?[^/]*)?$", r"\1od.jpg\2", url)
        # Also handle the parameterized form /od-w480_... → bump width ≥ 1280
        def _bump(m: _re.Match) -> str:
            cur = int(m.group(1))
            return f"w{max(cur, 1280)}"
        url = _re.sub(r"w(\d{2,4})(?=[_./])", _bump, url)
        return url

    primary_photo = _upgrade_photo(raw.get("primary_photo", {}).get("href", ""))

    # Get up to 3 valid photo URLs (scan up to 6 in case some are missing/empty).
    all_photos = []
    seen = set()
    for p in raw.get("photos", [])[:6]:
        url = _upgrade_photo(p.get("href", ""))
        if url and url not in seen:
            all_photos.append(url)
            seen.add(url)
        if len(all_photos) >= 3:
            break
    if not all_photos and primary_photo:
        all_photos = [primary_photo]

    return {
        "price":       raw.get("list_price", 0),
        "address":     f"{loc.get('line', '')} {loc.get('city', '')} {loc.get('state_code', '')} {loc.get('postal_code', '')}".strip(),
        "city":        loc.get("city", ""),
        "zip":         loc.get("postal_code", ""),
        "beds":        desc.get("beds", 0),
        "baths":       desc.get("baths", 0),
        "sqft":        desc.get("sqft") or 0,
        "type":        (desc.get("type") or "").replace("_", " ").title(),
        "lot_sqft":    desc.get("lot_sqft") or 0,
        "year_built":  desc.get("year_built") or "",
        "photo_url":   primary_photo,
        "photos":      all_photos,
        "listing_url": listing_url,
        "list_date":   raw.get("list_date", ""),
        "property_id": raw.get("property_id", ""),
    }


def collect_showcase_candidates(pool: list[dict], center_lat, center_lng,
                                excluded_addrs: set,
                                primary: float = RE_RADIUS_MILES,
                                fallback: float = RE_FALLBACK_RADIUS_MILES,
                                want: int = CANDIDATES_WANTED) -> tuple[list[dict], float]:
    """Walk a price-sorted pool of raw listings and gather up to `want` qualified
    Showcase candidates, escalating distance as needed. Returns
    (list[parsed_dict], radius_used_miles).

    Ladder:
      Phase 1 — within `primary` miles, top (PHASE1_BATCH × PHASE1_BATCHES) homes
                by price ("3 samplings of 5"). If any qualify, use them.
      Phase 2 — if NOTHING within `primary`, re-evaluate those same top homes at
                the wider `fallback` radius.
      Phase 3 — if still nothing, resample the next PHASE3_BATCH homes at
                `fallback`, repeating down the list, accumulating up to `want`.

    "Qualified" = within the active radius AND not previously featured (address
    de-dupe; URL de-dupe happened upstream)."""

    def scan(rows: list[dict], radius: float, acc: list[dict]) -> None:
        for raw in rows:
            if len(acc) >= want:
                return
            d = listing_distance_miles(raw, center_lat, center_lng)
            if d is None or d > radius:
                continue
            parsed = parse_listing(raw)
            if normalize_address(parsed.get("address", "")) in excluded_addrs:
                print(f"    ↷ skip (already featured): ${parsed['price']:,} | {parsed['address']}")
                continue
            parsed["distance_mi"] = round(d, 2)
            acc.append(parsed)
            print(f"    + candidate {len(acc)} @ ≤{radius:g}mi: ${parsed['price']:,} | "
                  f"{parsed['distance_mi']} mi | {parsed['address']}")

    top_n = PHASE1_BATCH * PHASE1_BATCHES  # e.g. 15

    # Phase 1 — primary radius, top homes.
    print(f"  Phase 1: top {top_n} by price within {primary:g} mi")
    acc: list[dict] = []
    scan(pool[:top_n], primary, acc)
    if acc:
        return acc, primary

    # Phase 2 — widen the SAME top homes to the fallback radius.
    print(f"  Phase 2: nothing within {primary:g} mi — re-evaluating top {top_n} within {fallback:g} mi")
    acc = []
    scan(pool[:top_n], fallback, acc)
    if acc:
        return acc, fallback

    # Phase 3 — resample the next PHASE3_BATCH homes at the fallback radius,
    # walking down the price-sorted list until we have `want` or run out.
    acc = []
    start = top_n
    while start < len(pool) and len(acc) < want:
        end = start + PHASE3_BATCH
        print(f"  Phase 3: resampling rows {start + 1}-{end} within {fallback:g} mi")
        scan(pool[start:end], fallback, acc)
        start = end
    return acc, fallback


# ---------------------------------------------------------------------------
# 4. GENERATE BLURBS VIA CLAUDE
# ---------------------------------------------------------------------------
def select_best_showcase(candidates: list[dict], newsletter_display: str) -> int:
    """Ask Claude to choose the single most compelling Showcase home from the
    qualified candidates. Returns the 0-based index into `candidates`.

    Claude is given ONLY descriptive facts (price, size, beds/baths, lot, year,
    address) — NOT the listing URL, which is preserved from our source data and
    must never be model-generated. With 0/1 candidate there's nothing to choose,
    and any API/parse failure falls back to index 0 (the highest-priced home)."""
    if len(candidates) <= 1:
        return 0
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. ${c.get('price', 0):,} | {c.get('beds', 0)}bd/{c.get('baths', 0)}ba | "
            f"{c.get('sqft', 0):,} sqft | {c.get('type', '')} | built {c.get('year_built') or '?'} | "
            f"lot {c.get('lot_sqft', 0):,} sqft | {c.get('address', '')}"
        )
    prompt = (
        f"You are choosing the single most compelling Showcase home for the "
        f"{newsletter_display} area newsletter — the one readers will most want "
        f"to gawk at. Weigh size, beds/baths, lot, year built, and address "
        f"appeal (price matters less; they're all top-of-market).\n\n"
        f"Candidates:\n" + "\n".join(lines) +
        f"\n\nReply with ONLY the number (1-{len(candidates)}) of the best home. "
        f"No other text."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = "".join(b.text for b in (resp.content or []) if getattr(b, "type", "") == "text")
        m = re.search(r"\d+", txt)
        if m:
            idx = int(m.group(0)) - 1
            if 0 <= idx < len(candidates):
                return idx
        print(f"    ⚠ Claude selection unparseable ('{txt.strip()}'); using highest-priced")
    except Exception as e:
        print(f"    ⚠ Claude selection failed ({e}); using highest-priced")
    return 0


def generate_blurbs(listings: list[dict], skill_prompt: str, newsletter_display: str) -> list[dict]:
    """Generate blurbs for the three tier listings."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    listings_text = ""
    for listing in listings:
        # For Showcase, hide the actual price in the Claude prompt so it
        # can't accidentally reference the dollar amount in the blurb.
        # The price-guess trivia rendered underneath the image makes the
        # price the reveal — the blurb shouldn't spoil it.
        is_showcase = listing["tier"] == "Showcase"
        price_line = ("Price: ???? (HIDDEN — readers play a price-guess trivia "
                      "after the image. Do NOT reference the price, range, or "
                      "any dollar amount in the blurb. Focus on the property's "
                      "features, location, and lifestyle fit.)"
                      if is_showcase
                      else f"Price: ${listing['price']:,}")
        listings_text += f"""
--- {listing['tier']} ---
{price_line}
Address: {listing['address']}
Beds: {listing['beds']} | Baths: {listing['baths']} | Sqft: {listing['sqft']:,} | Type: {listing['type']}
Year Built: {listing['year_built']}
Listing URL: {listing['listing_url']}
Photo: {listing['photo_url']}

"""

    listing_count = len(listings)
    response = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=3000,
                system=with_voice(skill_prompt),
                messages=[{
                    "role": "user",
                    "content": f"""
Write a Real Estate Corner section for the {newsletter_display} area newsletter.
There are {listing_count} listing(s) below. Write a short, neighbor-style blurb for each.

IMPORTANT for the SHOWCASE tier: do NOT mention price, price range, or any
dollar amount in the blurb. The price is intentionally hidden — readers play
a guess-the-price trivia rendered underneath the image. Write the Showcase
blurb about the home's features, location, lifestyle, and what makes it
special, but say nothing about cost. Words like "expensive", "luxury price",
"affordable for this size", "on the high end", "deal at this size", etc. are
also off-limits — anything that hints at price. (Other tiers can mention
price freely; this restriction is Showcase-only.)

Return ONLY a JSON array with exactly {listing_count} object(s), no preamble or markdown.
Exact format:
[
  {{
    "tier": "Starter",
    "headline": "Short catchy headline",
    "blurb": "2-3 sentence blurb about the listing",
    "price": 350000,
    "address": "123 Main St Marietta GA 30062",
    "beds": 3,
    "baths": 2,
    "sqft": 1500,
    "photo_url": "https://...",
    "listing_url": "https://..."
  }}
]

Listings:
{listings_text}
"""
                }]
            )
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Claude API error (attempt {attempt + 1}): {e}")
                time.sleep(10 * (attempt + 1))
            else:
                raise

    # Extract Claude's text. Be defensive: response may have no text blocks at all.
    raw = ""
    if response and response.content:
        for block in response.content:
            if getattr(block, "type", "") == "text":
                raw = block.text
                break

    if not raw.strip():
        print("  ⚠ Claude returned no text; skipping blurb generation for this batch")
        print(f"    Response stop_reason: {getattr(response, 'stop_reason', 'unknown')}")
        return []

    clean = raw.strip().removeprefix("```json").removesuffix("```").strip()
    # Tolerant JSON extraction: pull the first JSON array out of the response,
    # whether or not Claude wrapped it in prose or fences.
    try:
        results = json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            print("  ⚠ Could not parse Claude's response as JSON. Raw output (first 500 chars):")
            print(f"    {raw[:500]}")
            return []
        try:
            results = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            print(f"  ⚠ JSON salvage failed: {e}. Raw (first 500 chars):")
            print(f"    {raw[:500]}")
            return []

    if not isinstance(results, list):
        print(f"  ⚠ Claude returned non-array result: {type(results).__name__}. Skipping.")
        return []

    print(f"  Generated {len(results)} real estate blurbs (expected {listing_count})")
    return results


# ---------------------------------------------------------------------------
# 5. SAVE TO NOTION
# ---------------------------------------------------------------------------
NOTION_RE_DB_ID = os.environ.get("NOTION_RE_DB_ID", "")


def get_used_listing_keys(newsletter_name: str) -> dict:
    """Get the listing URLs AND normalized addresses already used for this
    newsletter, so a previously-featured high-value home doesn't repeat.

    Matching on both keys is belt-and-suspenders: Realtor listing URLs usually
    carry the property id, but re-listings (new MLS number, same house) reuse
    the address — so the address key catches repeats the URL key would miss.

    The rolling exclusion window is enforced upstream by cleanup_real_estate.py,
    which archives rows out of the DB after 8 weeks; whatever is still queryable
    here is therefore the live exclusion set.

    Returns {"urls": set[str], "addresses": set[str]}."""
    keys = {"urls": set(), "addresses": set()}
    if not NOTION_RE_DB_ID:
        return keys
    try:
        pages = query_database(NOTION_RE_DB_ID)
        for page in pages:
            props = page["properties"]
            nl = (props.get("Newsletter", {}).get("select") or {}).get("name", "")
            if nl != newsletter_name:
                continue
            url = props.get("Listing URL", {}).get("url", "")
            if url:
                keys["urls"].add(url)
            addr_rt = props.get("Address", {}).get("rich_text", [])
            addr = addr_rt[0].get("text", {}).get("content", "") if addr_rt else ""
            norm = normalize_address(addr)
            if norm:
                keys["addresses"].add(norm)
        print(f"  Loaded {len(keys['urls'])} used URL(s) + "
              f"{len(keys['addresses'])} used address(es) to exclude")
        return keys
    except Exception:
        return keys


def cleanup_old_re_listings() -> None:
    """Archive stale CANDIDATE rows (pending / rejected / blank) older than 8
    weeks. Featured homes ('approved' and 'approved - old') are kept FOREVER so
    a high-value home is never re-featured — they form the permanent exclusion
    history read by get_used_listing_keys()."""
    if not NOTION_RE_DB_ID:
        return
    from notion_helper import archive_page
    from datetime import timedelta
    cutoff = (datetime.today() - timedelta(weeks=8)).strftime("%Y-%m-%d")
    try:
        pages = query_database(NOTION_RE_DB_ID, filters={
            "property": "Date Generated",
            "date": {"before": cutoff}
        })
    except Exception:
        pages = []
    count = 0
    for page in pages:
        props = page["properties"]
        status = (props.get("Status", {}).get("select") or {}).get("name", "")
        # Permanent retention for anything ever featured.
        if status in ("approved", "approved - old"):
            continue
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        archive_page(page["id"])
        print(f"  Archived stale candidate: {name} (status: '{status}')")
        count += 1
    if count:
        print(f"  Archived {count} stale candidate rows older than 8 weeks "
              f"(featured homes kept forever)")


def _round_to_listing_increment(price: int) -> int:
    """Round a price to a natural listing increment so the trivia
    distractors look like real MLS listings, not random numbers:
      under $500k → nearest $5k
      $500k–$1M  → nearest $10k
      over $1M   → nearest $25k
    """
    if price < 500_000:
        step = 5_000
    elif price < 1_000_000:
        step = 10_000
    else:
        step = 25_000
    return int(round(price / step) * step)


def _generate_price_trivia(actual_price: int, seed: int | None = None) -> list[int]:
    """Generate 4 candidate prices for the Showcase price-guess trivia.

    The actual price is randomly placed at position A, B, C, or D (each
    ~25% likely). Distractors are sampled from ±5–15% offsets and rounded
    to natural housing increments so they look like real listings.

    Returns the 4 prices sorted ascending (the caller turns them into
    A/B/C/D in that order). All pairs are ≥ 5% of actual apart so no
    near-duplicate prices appear next to each other in the choices."""
    rng = random.Random(seed)

    # Decide where the actual price lands when prices are sorted ascending.
    # Position 1 = A (lowest), 4 = D (highest). Uniform → ~25% each.
    actual_position = rng.choice([1, 2, 3, 4])
    n_below = actual_position - 1   # how many distractors should be BELOW actual
    n_above = 4 - actual_position   # how many should be ABOVE actual

    # Sample distractor offsets (in % points). Below = negative, above = positive.
    # Pool spans -15..-5 and 5..15 — guarantees ≥ 5% gap from the actual.
    below_pool = list(range(-15, -4))  # -15, -14, ..., -5
    above_pool = list(range(5, 16))    # 5, 6, ..., 15

    def _sample_with_gap(pool: list[int], n: int) -> list[int]:
        """Pick n offsets from pool with pairwise gap ≥ 5%."""
        if n == 0:
            return []
        for _ in range(50):
            trial = sorted(rng.sample(pool, n))
            gaps = [trial[i + 1] - trial[i] for i in range(n - 1)]
            if all(g >= 5 for g in gaps):
                return trial
        # Fallback: deterministic spread
        return sorted(pool[::max(1, len(pool) // n)][:n])

    below_offsets = _sample_with_gap(below_pool, n_below)
    above_offsets = _sample_with_gap(above_pool, n_above)

    distractors = [
        _round_to_listing_increment(int(actual_price * (1 + off / 100)))
        for off in below_offsets + above_offsets
    ]

    # If rounding happened to collide with actual or another distractor,
    # nudge by one increment until unique.
    final = [actual_price]
    for d in distractors:
        if d in final:
            step = 5_000 if d < 500_000 else (10_000 if d < 1_000_000 else 25_000)
            # Move further away from actual, not toward it.
            direction = -1 if d < actual_price else 1
            while d in final:
                d += direction * step
        final.append(d)

    return sorted(final)


def save_real_estate_to_notion(results: list[dict], newsletter_name: str) -> None:
    """Save real estate listings to Notion database. Replaces existing entries for this newsletter.
    Manually edited rows (Manually Edited = True) are preserved and skipped."""
    if not NOTION_RE_DB_ID:
        print("  No NOTION_RE_DB_ID set, skipping Notion save")
        return

    # Find existing entries. Flip previous auto-generated "approved" rows to "approved - old"
    # so they stay in the database for exclusion (no repeat listings) but clear the slot for new picks.
    # Manually edited rows are preserved with their current status.
    protected_tiers = set()
    try:
        existing = query_database(NOTION_RE_DB_ID)
        existing = [p for p in existing if
                    (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
        flipped = 0
        for page in existing:
            props = page["properties"]
            is_edited = props.get("Manually Edited", {}).get("checkbox", False)
            tier = (props.get("Tier", {}).get("select") or {}).get("name", "")
            status = (props.get("Status", {}).get("select") or {}).get("name", "")
            # Only respect Manually Edited on CURRENT rows. Once a row's
            # been archived ('approved - old' / 'rejected'), historical
            # manual edits shouldn't permanently block this tier.
            if is_edited and status not in ("approved - old", "rejected"):
                protected_tiers.add(tier)
                print(f"  🔒 Preserving manually edited {tier} listing")
            elif status == "approved":
                # Flip to "approved - old" so it stays for exclusion but is no longer current
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page['id']}",
                    headers=NOTION_HEADERS,
                    json={"properties": {"Status": {"select": {"name": "approved - old"}}}},
                    timeout=30,
                )
                flipped += 1
        if flipped:
            print(f"  Flipped {flipped} previous RE entries to 'approved - old' for {newsletter_name}")
    except Exception as e:
        print(f"  Warning: could not process existing RE entries: {e}")

    for listing in results:
        tier = listing.get("tier", "")
        if tier in protected_tiers:
            print(f"  ⏭ Skipping {tier} (manually edited row kept)")
            continue
        properties = {
            "Name":           {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {tier} - {listing.get('address', '')}"}}]},
            "Tier":           {"select": {"name": tier}},
            "Price":          {"number": listing.get("price", 0)},
            "Address":        {"rich_text": [{"text": {"content": safe_str(listing.get("address", ""))}}]},
            "Beds":           {"number": listing.get("beds", 0)},
            "Baths":          {"number": listing.get("baths", 0)},
            "Sqft":           {"number": listing.get("sqft", 0)},
            "Headline":       {"rich_text": [{"text": {"content": safe_str(listing.get("headline", ""))}}]},
            "Blurb":          {"rich_text": [{"text": {"content": safe_str(listing.get("blurb", ""))[:2000]}}]},
            "Photo URL":      {"url": listing.get("photo_url") or None},
            "GIF URL":        {"url": listing.get("gif_url") or None},
            "Template Image": {"url": listing.get("template_image_url") or None},
            "Listing URL":    {"url": listing.get("listing_url") or None},
            "Trivia Options": {"rich_text": [{"text": {"content": safe_str(listing.get("trivia_options", ""))[:200]}}]},
            "Newsletter":     {"select": {"name": newsletter_name}},
            "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":         {"select": {"name": "approved"}},
            "Manually Edited": {"checkbox": False},
        }
        create_page(NOTION_RE_DB_ID, properties)
        print(f"  ✓ Saved: {tier} - {listing.get('address')}")


# ---------------------------------------------------------------------------
# 6. MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Starting Real Estate Corner — {datetime.today().strftime('%Y-%m-%d')}")
    skill_prompt = load_skill_prompt()

    # Cleanup old listings first
    print("\nCleaning up old listings...")
    cleanup_old_re_listings()

    for newsletter in filter_by_env():
        print(f"\n{'='*60}")
        print(f"Processing: {newsletter['name']} ({newsletter['display_area']})")
        print(f"{'='*60}")

        # Load previously featured homes to exclude (URLs + addresses).
        excluded = get_used_listing_keys(newsletter["name"])
        excluded_urls = excluded["urls"]
        excluded_addrs = excluded["addresses"]

        # Fetch listings (highest-priced first via sortBy).
        all_listings = fetch_listings(location=newsletter["realtor_location"], limit=160)
        if not all_listings:
            print(f"  No listings found for {newsletter['name']}. Skipping.")
            continue

        # Remove previously featured listings (URL match; address match happens
        # at pick time once we've parsed the candidate).
        before_count = len(all_listings)
        all_listings = [r for r in all_listings if
                        r.get("href", "") not in excluded_urls and
                        f"https://www.realtor.com{r.get('href', '')}" not in excluded_urls]
        if len(all_listings) < before_count:
            print(f"  Excluded {before_count - len(all_listings)} previously featured listings")

        # Exclude listings that aren't actually for sale today (pending, contingent, coming soon, etc.)
        before_count = len(all_listings)
        all_listings = [r for r in all_listings if is_available_for_sale(r)]
        if len(all_listings) < before_count:
            print(f"  Excluded {before_count - len(all_listings)} pending / coming-soon listings")

        # Validate listing URLs before tier selection (saves Claude costs on dead listings)
        print(f"\n  Validating {len(all_listings)} listing URLs...")
        valid_listings = []
        for r in all_listings:
            url = build_listing_url(r)
            if not url or validate_url(url):
                valid_listings.append(r)
            else:
                loc = r.get("location", {}).get("address", {})
                addr = f"{loc.get('line', '')} {loc.get('city', '')}".strip()
                print(f"    ✗ Dead listing URL: {addr} ({url[:80]})")
        if len(valid_listings) < len(all_listings):
            print(f"  Dropped {len(all_listings) - len(valid_listings)} listings with dead URLs")
        all_listings = valid_listings
        if not all_listings:
            print(f"  No listings with valid URLs for {newsletter['name']}. Skipping.")
            continue

        # Showcase config drives the property-type / bed / bath filters. We no
        # longer run Starter / Sweet Spot tiers — the section features a single
        # Showcase home: the highest-priced qualifying listing within range.
        tiers = newsletter["real_estate_tiers"]
        showcase_cfg = next((t for t in tiers if t.get("name") == "Showcase"), tiers[-1])
        tier_listings = []

        center_lat = newsletter.get("lat")
        center_lng = newsletter.get("lng")
        primary  = float(newsletter.get("re_radius_miles", RE_RADIUS_MILES))
        fallback = float(newsletter.get("re_fallback_radius_miles", RE_FALLBACK_RADIUS_MILES))

        # Price-sorted pool of qualifying single-family homes, highest first.
        ptype = showcase_cfg.get("type_filter") or "all types"
        pool = filter_by_tier(
            all_listings, 0, None,
            min_beds=showcase_cfg.get("min_beds", 0),
            min_baths=showcase_cfg.get("min_baths", 0),
            type_filter=showcase_cfg.get("type_filter"),
        )
        pool.sort(key=lambda r: (r.get("list_price") or 0), reverse=True)
        print(f"\n  🏰 Showcase — {primary:g} mi → {fallback:g} mi ladder around {newsletter.get('zip', '?')}")
        print(f"    Filter: {ptype} | {showcase_cfg.get('min_beds', 0)}+bd/"
              f"{showcase_cfg.get('min_baths', 0)}+ba | {len(pool)} qualifying listing(s)")

        qualified, radius_used = collect_showcase_candidates(
            pool, center_lat, center_lng, excluded_addrs,
            primary=primary, fallback=fallback,
        )

        showcase_result = None
        if qualified:
            # Claude picks the most compelling of the qualified candidates. It
            # never sees or generates the listing URL — that's preserved from
            # our source data through the merge step below.
            chosen = select_best_showcase(qualified, newsletter["display_area"])
            showcase_result = qualified[chosen]
            print(f"    ★ Claude selected #{chosen + 1} of {len(qualified)} "
                  f"(within {radius_used:g} mi): ${showcase_result['price']:,} | "
                  f"{showcase_result['address']}")
            showcase_result["tier"] = "Showcase"
            showcase_result["tier_label"] = "🏰 Showcase"
            tier_listings.append(showcase_result)
        else:
            print(f"    ✗ Showcase — no qualifying homes within {fallback:g} mi")

        if not tier_listings:
            print(f"  No listings found for {newsletter['name']}. Skipping.")
            continue

        # Generate blurbs
        print(f"\n  Generating blurbs for {len(tier_listings)} listings...")
        results = generate_blurbs(tier_listings, skill_prompt, newsletter["display_area"])

        # Merge original listing data into Claude's results (source of truth for URLs/photos).
        # Claude only provides text: headline, blurb. Everything else is overwritten from source.
        tier_data_map = {l["tier"]: l for l in tier_listings}
        validated_results = []
        for r in results:
            tier = r.get("tier", "")
            original = tier_data_map.get(tier)
            if not original:
                print(f"  ✗ Rejecting result with unknown tier: '{tier}'")
                continue
            print(f"  Merge {tier}: Claude addr='{r.get('address', '')[:40]}' | Original addr='{original.get('address', '')[:40]}'")
            r["photo_url"]   = original.get("photo_url", "")
            r["listing_url"] = original.get("listing_url", "")
            r["address"]     = original.get("address", "")
            r["price"]       = original.get("price", 0)
            r["beds"]        = original.get("beds", 0)
            r["baths"]       = original.get("baths", 0)
            r["sqft"]        = original.get("sqft", 0)
            validated_results.append(r)
        results = validated_results

        # Generate template images (animated GIF with border overlay)
        sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)
        print(f"\n  Creating listing images...")
        try:
            from re_image_maker import generate_re_images
            image_results = generate_re_images(tier_listings, newsletter["name"], str(output_dir))

            # Build image URL map and merge into results
            for img_result in image_results:
                tier = img_result["tier"]
                img_filename = img_result["image_filename"]
                cache_bust = int(datetime.today().timestamp())
                img_url = f"https://peachyinsurance.github.io/newsletters/gifs/{img_filename}?v={cache_bust}"
                for r in results:
                    if r.get("tier") == tier:
                        r["template_image_url"] = img_url
                        break
                # Also store on tier_listings for gh-pages publish
                for listing in tier_listings:
                    if listing.get("tier") == tier:
                        listing["image_filename"] = img_filename
                        break
        except Exception as e:
            print(f"  ✗ Image generation failed: {e}")

        # Showcase price-guess trivia. Generate 4 candidate prices for the
        # Showcase listing — one is the actual, three are distractors within
        # ±15% of the real price, all pairs ≥5% apart (of actual), each
        # rounded to nearest $1,000. Saved as comma-separated string to the
        # Notion "Trivia Options" field; the assemble script renders the
        # row underneath the Showcase tier.
        # Also strip $-amounts from the Showcase blurb so the reveal doesn't
        # leak in the body text.
        for r in results:
            if r.get("tier") != "Showcase":
                continue
            actual = int(r.get("price") or 0)
            if actual <= 0:
                continue
            options = _generate_price_trivia(actual)
            r["trivia_options"] = ",".join(str(p) for p in options)
            print(f"  ↳ Showcase trivia options: {options} (actual={actual})")
            blurb = r.get("blurb") or ""
            # Belt-and-suspenders: if Claude regressed and slipped a dollar
            # amount or our own "[price hidden]" placeholder into the blurb,
            # strip it out cleanly. Drop the surrounding sentence rather
            # than leaving "[price hidden]k is on the accessible end…" gunk.
            cleaned = blurb
            # 1. Drop sentences that contain "$amount" or "[price hidden]"
            sentences = _re.split(r'(?<=[.!?])\s+', cleaned)
            kept_sentences = []
            for s in sentences:
                if _re.search(r"\$\s*\d|\[price[\s_]*hidden\]", s, _re.IGNORECASE):
                    continue   # drop the sentence entirely
                kept_sentences.append(s)
            cleaned = " ".join(kept_sentences).strip()
            if cleaned != blurb:
                print(f"  ↳ Cleaned price reference(s) out of Showcase blurb")
                r["blurb"] = cleaned

        # Save to Notion
        save_real_estate_to_notion(results, newsletter["name"])

    print(f"\nAll newsletters complete.")
