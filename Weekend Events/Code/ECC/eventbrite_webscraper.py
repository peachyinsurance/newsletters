#!/usr/bin/env python3
"""Eventbrite scraper — via Apify's aitorsm/eventbrite actor.

Replaces the prior direct HTML scrape which started returning HTTP 405
from Eventbrite's bot detection on GitHub Actions data-center IPs.
Apify runs the scraper on residential proxies, so the IP block goes
away. Output is upserted into the Weekend Events Notion DB via the
shared save_event helper.

Filtering done at this layer (Claude no longer rejects anything, so
data has to arrive clean):
  1. Category allow-list — 7 chosen categories, skip business / spirituality
     / school-activities / etc. where MLM and recruitment pitches live.
  2. Date scrub — Eventbrite's date filter is loose (returns past-dated
     events occasionally); we re-verify start_date is in the target
     weekend window after Apify returns.
  3. Price ≤ $50 — best-effort parse of the price field; drop pricier
     events. Free / unknown-price events are kept.
  4. City allow-list — only events whose venue city is in our coverage.
  5. Cancelled / adult-NSFW / hookah filters via the shared
     is_cancelled_event / is_inappropriate_event helpers.
  6. Cross-category dedup by (normalized_name, start_date) — Eventbrite
     tags the same event in multiple categories; we'd otherwise pay 7×
     and save 7× rows for one event.

Cost: aitorsm/eventbrite charges $0.02 per event scraped. With 7
categories × maxPages=1 × ~20 events/cat ≈ 140 events ≈ $2.80 per run.
Weekly = ~$11/month. Tune MAX_PAGES to trade coverage vs cost.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

# Sibling-folder import of shared helpers + Notion save.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from ecc_event_webscraper import (  # noqa: E402
    _clean_html,
    _normalize_title,
    existing_source_urls,
    save_event,
)
from event_date_filter import upcoming_friday as _upcoming_friday  # noqa: E402
from event_image_scraper import (is_cancelled_event,           # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

APIFY_API_KEY        = os.environ.get("APIFY_API_KEY", "")
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NEWSLETTER           = os.environ.get("NEWSLETTER", "East_Cobb_Connect")
DEBUG                = os.environ.get("EVENTBRITE_DEBUG", "") == "1"

ACTOR_ID  = "aitorsm~eventbrite"   # tilde-form for the API path
COUNTRY   = "united-states"
CITY      = "marietta"             # passed to Apify; Eventbrite resolves loosely
MAX_PAGES = 1                      # caps cost — Apify charges $0.02/event

# Eventbrite-only: target THIS coming weekend (Fri-Sun), not the broader
# 14-day window the other scrapers use.
WINDOW_END_OFFSET_DAYS = 2

# Allow-list of Eventbrite categories. Single value per Apify run, so
# this becomes a 7-call loop. Excluded by absence: business / spirituality /
# school-activities / government / science-and-tech / film-and-media /
# fashion / home-and-lifestyle / health / auto-boat-and-air / community /
# family-and-education / travel-and-outdoor / other — most MLM, recruitment,
# and noise lives in those categories.
CATEGORIES = [
    "food-and-drink",
    "music",
    "charity-and-causes",
    "hobbies",
    "arts",                # performing & visual arts
    "sports-and-fitness",
    "holiday",
]

# Strict city allow-list for the newsletter's coverage area.
ALLOWED_CITIES = {"marietta", "east cobb", "sandy springs"}

# Hard ceiling on ticket price (USD). Anything above is dropped at the
# scrape layer. Free / unknown is kept.
PRICE_CAP_USD = 50.0


# ---------------------------------------------------------------------------
# Apify call (one per category)
# ---------------------------------------------------------------------------
def fetch_category(category: str, start: date, end: date) -> list[dict]:
    """Trigger an Apify sync run for one category. Returns raw items."""
    payload = {
        "country":   COUNTRY,
        "city":      CITY,
        "category":  category,
        "startDate": start.isoformat(),
        "endDate":   end.isoformat(),
        "maxPages":  MAX_PAGES,
    }
    print(f"  Apify run: city={CITY}, category={category!r}, "
          f"window={start}..{end}, maxPages={MAX_PAGES}")
    try:
        r = requests.post(
            f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items",
            headers={"Authorization": f"Bearer {APIFY_API_KEY}",
                     "Content-Type":  "application/json"},
            json=payload, timeout=600,
        )
    except Exception as e:
        print(f"    ✗ Apify request error: {e}")
        return []
    if r.status_code not in (200, 201):
        print(f"    ✗ Apify HTTP {r.status_code}: {r.text[:300]}")
        return []
    items = r.json() or []
    print(f"    → {len(items)} item(s)")
    if DEBUG and items:
        print(f"    [DEBUG] first item keys: {sorted(items[0].keys())}")
        print(f"    [DEBUG] first item:\n{json.dumps(items[0], indent=2, default=str)[:2000]}")
    return items


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------
def _parse_date(s) -> date | None:
    if not s:
        return None
    s = str(s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _first_str(d, *keys) -> str:
    """Try each key on `d`; return the first non-empty string value.
    Tolerates `d` being None or not a dict."""
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for inner in ("url", "src", "name", "value"):
                iv = v.get(inner)
                if isinstance(iv, str) and iv.strip():
                    return iv.strip()
    return ""


def _max_price_usd(item: dict) -> float | None:
    """Best-effort max-ticket-price extraction. Returns None when we
    can't determine — caller treats None as 'keep' so we don't
    false-positive drop legitimate events with weird price fields."""
    raw = (item.get("price") or item.get("ticketPrice")
           or item.get("priceRange") or item.get("priceMax")
           or item.get("ticket_classes") or item.get("tickets"))
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        for k in ("max", "maximum", "highest", "high", "max_value", "amount"):
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        for k in ("min", "minimum", "lowest", "low", "min_value"):
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return float(v)
    if isinstance(raw, list):
        prices: list[float] = []
        for tc in raw:
            if isinstance(tc, (int, float)):
                prices.append(float(tc))
            elif isinstance(tc, dict):
                for k in ("price", "cost", "amount", "value", "max", "high"):
                    v = tc.get(k)
                    if isinstance(v, (int, float)):
                        prices.append(float(v))
                    elif isinstance(v, dict):
                        for kk in ("value", "amount", "major_value"):
                            vv = v.get(kk)
                            if isinstance(vv, (int, float)):
                                prices.append(float(vv))
        return max(prices) if prices else None
    if isinstance(raw, str):
        s = raw.lower().strip()
        if any(t in s for t in ("free", "no charge", "complimentary", "$0")):
            return 0.0
        import re as _re
        amounts = _re.findall(r"\$?\s*(\d+(?:\.\d+)?)", s)
        if amounts:
            return max(float(a) for a in amounts)
    return None


# ---------------------------------------------------------------------------
# Normalize one Apify event → standard event dict
# ---------------------------------------------------------------------------
def normalize_event(item: dict) -> dict | None:
    """Returns None if the event is missing essentials, cancelled, or
    matches the adult/NSFW filter."""
    name = _first_str(item, "name", "title", "eventName")
    url  = _first_str(item, "url", "eventUrl", "link", "permalink")
    if not name or not url:
        return None

    description = _clean_html(_first_str(item, "description", "summary",
                                         "fullDescription", "shortDescription"))[:2000]
    if is_cancelled_event(name, description):
        return None
    venue_str = _first_str(item.get("venue"), "name") if isinstance(item.get("venue"), dict) else ""
    if is_inappropriate_event(name, description, venue_str):
        return None

    start_raw = (_first_str(item, "startDate", "start", "starts", "dateStart")
                 or (item.get("dates") or {}).get("start"))
    end_raw   = (_first_str(item, "endDate", "end", "ends", "dateEnd")
                 or (item.get("dates") or {}).get("end"))
    start = _parse_date(start_raw)
    end   = _parse_date(end_raw) or start

    time_str = ""
    if start_raw and "T" in str(start_raw):
        try:
            sdt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            if sdt.hour or sdt.minute:
                time_str = sdt.strftime("%-I:%M %p")
                if end_raw and "T" in str(end_raw):
                    try:
                        edt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                        if edt.hour or edt.minute:
                            time_str += " – " + edt.strftime("%-I:%M %p")
                    except Exception:
                        pass
        except Exception:
            pass

    venue = item.get("venue") or item.get("location") or {}
    if isinstance(venue, dict):
        loc_name = _first_str(venue, "name", "title")
        addr_parts = []
        for k in ("address", "streetAddress", "address1", "addressLine1"):
            v = venue.get(k)
            if isinstance(v, dict):
                v = _first_str(v, "localizedAddressDisplay", "address1",
                                  "streetAddress", "line1")
            if isinstance(v, str) and v.strip() and v.strip() not in addr_parts:
                addr_parts.append(v.strip())
        city_str = _first_str(venue, "city", "localityName", "town").lower()
        region   = _first_str(venue, "region", "state", "stateName")
        if city_str:
            addr_parts.append(city_str.title())
        if region:
            addr_parts.append(region)
        address = ", ".join(addr_parts)
    else:
        loc_name = str(venue or "")
        address  = ""
        city_str = ""

    image = _first_str(item, "image", "imageUrl", "logo", "thumbnail", "imageURL")
    price = _max_price_usd(item)

    return {
        "event_name":  name,
        "description": description,
        "source_url":  url,
        "image_url":   image,
        "start_date":  start,
        "end_date":    end,
        "time":        time_str,
        "location":    loc_name,
        "address":     address,
        "city":        city_str,
        "price_usd":   price,   # None when unknown
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if not APIFY_API_KEY:
        print("✗ APIFY_API_KEY is not set in env.")
        return 1
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    today = date.today()
    start = _upcoming_friday(today)
    end   = start + timedelta(days=WINDOW_END_OFFSET_DAYS)

    print("Eventbrite scraper (via Apify)")
    print(f"  → Actor:        {ACTOR_ID}")
    print(f"  → City search:  {CITY}  (then filtered to {sorted(ALLOWED_CITIES)})")
    print(f"  → Categories:   {len(CATEGORIES)}  ({', '.join(CATEGORIES)})")
    print(f"  → Price cap:    ${PRICE_CAP_USD:.0f}")
    print(f"  → Notion DB:    {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  → Newsletter:   {NEWSLETTER}")
    print(f"  → Date window:  {start} → {end}  (target weekend Fri-Sun only)")
    print()

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID)
    print(f"Dedup vs Notion: {len(existing)} URLs already in DB\n")

    # Pull all categories, accumulate raw items.
    all_raw: list[dict] = []
    for cat in CATEGORIES:
        all_raw.extend(fetch_category(cat, start, end))
    print(f"\n  Total raw items across {len(CATEGORIES)} categories: {len(all_raw)}")

    # Filter pipeline. Counters surface in the run log so it's obvious
    # which filter is dropping events.
    seen_urls:      set[str] = set()
    seen_name_keys: set[tuple[str, str]] = set()   # (normalized_name, iso_date)
    candidates: list[dict] = []

    skipped_no_data  = 0
    skipped_dup_url  = 0
    skipped_dup_name = 0
    skipped_date     = 0
    skipped_price    = 0
    skipped_city     = 0

    for raw in all_raw:
        ev = normalize_event(raw)
        if not ev:
            skipped_no_data += 1
            continue

        # Cross-category URL dedup
        if ev["source_url"] in seen_urls:
            skipped_dup_url += 1
            continue
        seen_urls.add(ev["source_url"])

        # Cross-category name+date dedup. Same event in two categories with
        # slightly different URLs gets collapsed; same event on a different
        # day still gets its own row (multi-day events keep both occurrences).
        name_key = _normalize_title(ev["event_name"])
        date_key = ev["start_date"].isoformat() if ev["start_date"] else ""
        nd_key = (name_key, date_key)
        if name_key and nd_key in seen_name_keys:
            skipped_dup_name += 1
            continue
        if name_key:
            seen_name_keys.add(nd_key)

        # Date scrub — Eventbrite's date filter is loose; re-verify
        # actual start_date is inside the target weekend window.
        sd = ev["start_date"]
        if not sd or sd < start or sd > end:
            skipped_date += 1
            continue

        # Price scrub — drop above cap. Unknown price (None) is kept.
        if ev["price_usd"] is not None and ev["price_usd"] > PRICE_CAP_USD:
            skipped_price += 1
            continue

        # City filter — strict allow-list. Online-only / empty city dropped.
        city = ev.get("city", "")
        if not city or city not in ALLOWED_CITIES:
            skipped_city += 1
            continue

        candidates.append(ev)

    print(f"\n  Filtered to {len(candidates)} keep:")
    print(f"    {skipped_dup_url:>3} dropped — duplicate URL across categories")
    print(f"    {skipped_dup_name:>3} dropped — duplicate (name, date) across categories")
    print(f"    {skipped_date:>3} dropped — out-of-window date (Eventbrite filter slop)")
    print(f"    {skipped_price:>3} dropped — price > ${PRICE_CAP_USD:.0f}")
    print(f"    {skipped_city:>3} dropped — venue city not in allow-list")
    print(f"    {skipped_no_data:>3} dropped — unparseable / cancelled / adult-NSFW")

    # Backfill images for events Apify didn't return one for.
    filled = backfill_images(candidates)
    if filled:
        print(f"\n  ↳ Backfilled {filled} image(s) from source pages")

    inserted = 0
    updated  = 0
    print(f"\n━━ Saving {len(candidates)} unique event(s) ━━")
    for ev in candidates:
        page_id = existing.get(ev["source_url"])
        if save_event(WEEKEND_EVENTS_DB_ID, ev, NEWSLETTER, page_id=page_id):
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            price_disp = (f" ${ev['price_usd']:.0f}" if ev["price_usd"] else "")
            print(f"  {label} {ev['start_date']}  {ev['event_name'][:55]:55s}"
                  f"  ({ev.get('city','?')}){price_disp}")
    print()
    print(f"✓ Done. Inserted {inserted}, refreshed {updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
