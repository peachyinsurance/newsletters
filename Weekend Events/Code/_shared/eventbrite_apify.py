#!/usr/bin/env python3
"""Eventbrite scraper — via Apify's aitorsm/eventbrite actor. LIBRARY.

Per-newsletter wrappers (East_Cobb_Connect/eventbrite.py,
Perimeter_Post/eventbrite.py, Lewisville_Lake_Lookout/eventbrite.py)
call `run_eventbrite(newsletter_tag, anchor_city, allowed_cities)`
with their config. The shared logic — Apify pagination, dedup, date /
price / city / content filtering, Notion upsert — lives here so adding
a new newsletter = one new wrapper file in that newsletter's folder.

Per-newsletter dedup is essential: a shared event scraped under both
East_Cobb_Connect and Perimeter_Post should land as two rows (one per
newsletter), not collide and corrupt the first newsletter's row.
existing_source_urls() is newsletter-scoped to enforce this.

Filters applied for every wrapper (Claude doesn't reject anything):
  1. Category allow-list — 7 chosen categories per Apify run.
  2. Date scrub — Eventbrite's date filter is loose; re-verify in window.
  3. Price ≤ $50 — best-effort parse; unknown price kept.
  4. City allow-list — only events whose venue city is in coverage.
  5. Cancelled / adult-NSFW / hookah via shared helpers.
  6. Cross-category dedup by (normalized_name, start_date) per wrapper.

Cost (per wrapper run): 7 categories × $0.02/event × maxPages=1 ≈
$2-3. Running ECC + PP + LLL = ~$6-9/scrape, weekly ≈ $25-35/month.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

# Sibling-folder import of shared helpers + Notion save.
# Same-directory imports (this file is in _shared/) plus NewsletterCreation/Code.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from html_utils  import _clean_html, _normalize_title  # noqa: E402
from notion_save import existing_source_urls, save_event  # noqa: E402
from event_date_filter import (upcoming_friday as _upcoming_friday,  # noqa: E402
                               effective_today as _effective_today)
from event_image_scraper import (is_cancelled_event,           # noqa: E402
                                 is_inappropriate_event,
                                 backfill_images)

APIFY_API_KEY        = os.environ.get("APIFY_API_KEY", "")
WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
DEBUG                = os.environ.get("EVENTBRITE_DEBUG", "") == "1"

ACTOR_ID  = "aitorsm~eventbrite"   # tilde-form for the API path
COUNTRY   = "united-states"
MAX_PAGES = 1                      # caps cost — Apify charges $0.02/event

# Eventbrite-only: target THIS coming weekend (Fri-Sun), not the broader
# 14-day window the other scrapers use.
WINDOW_END_OFFSET_DAYS = 2

# Category allow-list per Apify run. Excluded by absence: business /
# spirituality / school-activities / government / science-and-tech /
# film-and-media / fashion / home-and-lifestyle / health / auto-boat-and-air /
# community / family-and-education / travel-and-outdoor / other.
CATEGORIES = [
    "food-and-drink",
    "music",
    "charity-and-causes",
    "hobbies",
    "arts",                # performing & visual arts
    "sports-and-fitness",
    "holiday",
]

# Hard ceiling on ticket price (USD). Anything above is dropped at the
# scrape layer. Free / unknown is kept.
PRICE_CAP_USD = 50.0


# ---------------------------------------------------------------------------
# Apify call (one per category)
# ---------------------------------------------------------------------------
def fetch_category(anchor_city: str, category: str,
                   start: date, end: date,
                   max_pages: int = MAX_PAGES) -> list[dict]:
    """Trigger an Apify sync run for one (anchor_city, category)."""
    payload = {
        "country":   COUNTRY,
        "city":      anchor_city,
        "category":  category,
        "startDate": start.isoformat(),
        "endDate":   end.isoformat(),
        "maxPages":  max_pages,
    }
    print(f"  Apify run: city={anchor_city}, category={category!r}, "
          f"window={start}..{end}, maxPages={max_pages}")
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

    # aitorsm/eventbrite actor returns `start_date` / `end_date` as
    # YYYY-MM-DD strings with separate `start_time` / `end_time` as HH:MM.
    # camelCase variants kept as fallbacks for future actor versions.
    start_raw = (_first_str(item, "start_date", "startDate", "start",
                            "starts", "dateStart")
                 or (item.get("dates") or {}).get("start"))
    end_raw   = (_first_str(item, "end_date", "endDate", "end",
                            "ends", "dateEnd")
                 or (item.get("dates") or {}).get("end"))
    # If date is bare YYYY-MM-DD, compose with the separate time field
    # so downstream time-of-day extraction still works.
    if start_raw and "T" not in str(start_raw):
        st = _first_str(item, "start_time", "startTime")
        if st:
            start_raw = f"{start_raw}T{st}"
    if end_raw and "T" not in str(end_raw):
        et = _first_str(item, "end_time", "endTime")
        if et:
            end_raw = f"{end_raw}T{et}"
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
        # State / region. 2-letter abbreviations preferred (GA / TX) since
        # that's what newsletter wrappers pass as `required_state`. Strip
        # to first 2 chars uppercase for comparison (handles "Georgia" /
        # "GA" / "Texas" mixed shapes).
        region   = _first_str(venue, "region", "state", "stateName",
                              "regionCode", "stateCode")
        state_code = ""
        if region:
            r = region.strip()
            # If full state name, drop to first 2 chars (rough but works
            # for "Georgia" → "GE" — actually that's wrong, special-case).
            STATE_NAMES = {"georgia": "GA", "texas": "TX", "florida": "FL",
                           "california": "CA", "new york": "NY",
                           "illinois": "IL", "alabama": "AL",
                           "tennessee": "TN", "north carolina": "NC",
                           "south carolina": "SC"}
            state_code = STATE_NAMES.get(r.lower(), r[:2].upper())
        if city_str:
            addr_parts.append(city_str.title())
        if region:
            addr_parts.append(region)
        address = ", ".join(addr_parts)
    else:
        loc_name = str(venue or "")
        address  = ""
        city_str = ""
        state_code = ""

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
        "state":       state_code,
        "price_usd":   price,
    }


# ---------------------------------------------------------------------------
# Per-config scrape
# ---------------------------------------------------------------------------
def run_eventbrite(newsletter_tag: str,
                   anchor_city: str,
                   allowed_cities: set[str],
                   *,
                   required_state: str | None = None,
                   categories: list[str] | None = None,
                   max_pages: int = MAX_PAGES,
                   price_cap_usd: float = PRICE_CAP_USD,
                   window_end_offset_days: int = WINDOW_END_OFFSET_DAYS) -> int:
    """End-to-end Eventbrite scrape for ONE newsletter.

    Per-newsletter folders contain thin wrappers that call this with
    their config (tag, anchor city, allowed cities). The shared logic
    — Apify pagination, dedup, date/price/city/content filtering,
    Notion upsert — all lives here so adding a new newsletter is one
    new wrapper file in that newsletter's folder.

    `anchor_city` is what we pass to Apify's Eventbrite location search
    (e.g. 'marietta', 'sandy-springs', 'lewisville'). Eventbrite resolves
    loosely, so `allowed_cities` post-filters by actual venue city.

    `newsletter_tag` is the Notion DB tag for saved rows. Use ECC_PP
    for a row that should be visible in both East_Cobb_Connect and
    Perimeter_Post (the existing shared-tag pattern).

    Returns 0 on success, 1 on missing env / config error. Wrappers
    should `sys.exit(run_eventbrite(...))`."""
    if not APIFY_API_KEY:
        print("✗ APIFY_API_KEY is not set in env.")
        return 1
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    cats = categories if categories is not None else CATEGORIES

    # `effective_today()` honors the ISSUE_DATE env override (MM/DD/YYYY).
    # Lets the dedicated Eventbrite workflow's `issue_date` input target
    # a future weekend, matching the pattern used by the other workflows.
    today = _effective_today()
    start = _upcoming_friday(today)
    end   = start + timedelta(days=window_end_offset_days)

    print(f"\n{'='*70}")
    print(f"= Eventbrite scraper (via Apify)")
    print(f"= Newsletter:   {newsletter_tag}")
    print(f"= Anchor city:  {anchor_city}  (Apify search)")
    print(f"= Allow-list:   {sorted(allowed_cities)}")
    print(f"= Req. state:   {required_state or '(none — state filter off)'}")
    print(f"= Categories:   {len(cats)}  ({', '.join(cats)})")
    print(f"= Price cap:    ${price_cap_usd:.0f}")
    print(f"= Date window:  {start} → {end}  (target weekend Fri-Sun only)")
    print(f"= Actor:        {ACTOR_ID}, maxPages={max_pages}")
    print(f"{'='*70}")

    existing = existing_source_urls(WEEKEND_EVENTS_DB_ID, newsletter=newsletter_tag)
    print(f"  Dedup vs Notion ({newsletter_tag}): {len(existing)} URLs already saved\n")

    # Pull all categories, accumulate raw items.
    all_raw: list[dict] = []
    for cat in cats:
        all_raw.extend(fetch_category(anchor_city, cat, start, end, max_pages))
    print(f"\n  Total raw items across {len(cats)} categories: {len(all_raw)}")

    # Filter pipeline. Per-filter counters surface in the run log so it's
    # obvious which guardrail is dropping what.
    seen_urls:      set[str] = set()
    seen_name_keys: set[tuple[str, str]] = set()
    candidates: list[dict] = []

    skipped_no_data  = 0
    skipped_dup_url  = 0
    skipped_dup_name = 0
    skipped_date     = 0
    skipped_date_none   = 0   # diagnostic: how many had no parseable date
    skipped_date_before = 0   # how many were earlier than target Friday
    skipped_date_after  = 0   # how many were later than target Sunday
    date_drop_samples: list[tuple[str, str, str]] = []  # (event_name, raw_field, parsed)
    skipped_price    = 0
    skipped_city     = 0
    skipped_city_none = 0          # had no city extracted (field-name miss)
    city_drop_reasons: dict[str, int] = {}   # extracted city → count (cities NOT in allow-list)
    skipped_state    = 0
    state_drop_reasons: dict[str, int] = {}  # extracted state → count (states != required_state)

    for raw in all_raw:
        ev = normalize_event(raw)
        if not ev:
            skipped_no_data += 1
            continue

        if ev["source_url"] in seen_urls:
            skipped_dup_url += 1
            continue
        seen_urls.add(ev["source_url"])

        name_key = _normalize_title(ev["event_name"])
        date_key = ev["start_date"].isoformat() if ev["start_date"] else ""
        nd_key = (name_key, date_key)
        if name_key and nd_key in seen_name_keys:
            skipped_dup_name += 1
            continue
        if name_key:
            seen_name_keys.add(nd_key)

        sd = ev["start_date"]
        # Diagnostic: split date-drop into None vs out-of-window so the
        # log shows whether parsing failed or filter is over-aggressive.
        if not sd:
            skipped_date_none += 1
            if len(date_drop_samples) < 5:
                raw_start = (raw.get("start_date") or raw.get("startDate")
                             or raw.get("start") or raw.get("starts")
                             or raw.get("dateStart")
                             or (raw.get("dates") or {}).get("start"))
                date_drop_samples.append(
                    (ev.get("event_name", "?")[:50], f"{raw_start!r}", "<NONE>"))
        elif sd < start:
            skipped_date_before += 1
            if len(date_drop_samples) < 5:
                date_drop_samples.append(
                    (ev.get("event_name", "?")[:50], "", sd.isoformat()))
        elif sd > end:
            skipped_date_after += 1
            if len(date_drop_samples) < 5:
                date_drop_samples.append(
                    (ev.get("event_name", "?")[:50], "", sd.isoformat()))
        if not sd or sd < start or sd > end:
            skipped_date += 1
            continue

        if ev["price_usd"] is not None and ev["price_usd"] > price_cap_usd:
            skipped_price += 1
            continue

        # State filter — catches cross-state false positives from
        # Eventbrite's loose geo search (Lewisville TX vs. Lewisville NC,
        # Atlanta GA vs. Atlanta IL, etc.). Skip if required_state is set
        # AND the event has a state AND it doesn't match. Events with no
        # extracted state pass through — we don't want to over-drop when
        # the actor's schema is incomplete.
        if required_state:
            ev_state = (ev.get("state") or "").upper()
            if ev_state and ev_state != required_state.upper():
                skipped_state += 1
                state_drop_reasons[ev_state] = state_drop_reasons.get(ev_state, 0) + 1
                continue

        city = ev.get("city", "")
        if not city or city not in allowed_cities:
            skipped_city += 1
            if not city:
                skipped_city_none += 1
            else:
                city_drop_reasons[city] = city_drop_reasons.get(city, 0) + 1
            continue

        candidates.append(ev)

    print(f"\n  Filtered to {len(candidates)} keep for {newsletter_tag}:")
    print(f"    {skipped_dup_url:>3} dropped — duplicate URL across categories")
    print(f"    {skipped_dup_name:>3} dropped — duplicate (name, date) across categories")
    print(f"    {skipped_date:>3} dropped — date scrub  "
          f"(none={skipped_date_none}, before-window={skipped_date_before}, "
          f"after-window={skipped_date_after})")
    if date_drop_samples:
        print(f"        first {len(date_drop_samples)} date-drop sample(s):")
        for name, raw_field, parsed in date_drop_samples:
            if raw_field:
                print(f"          · {name}  raw startDate={raw_field}  parsed={parsed}")
            else:
                print(f"          · {name}  parsed={parsed}")
    print(f"    {skipped_price:>3} dropped — price > ${price_cap_usd:.0f}")
    print(f"    {skipped_state:>3} dropped — venue state != {required_state or '(off)'}")
    if state_drop_reasons:
        for st, n in sorted(state_drop_reasons.items(), key=lambda kv: -kv[1])[:5]:
            print(f"          · {st!r}: {n}")
    print(f"    {skipped_city:>3} dropped — venue city not in allow-list  "
          f"(no-city-extracted={skipped_city_none}, wrong-city={skipped_city - skipped_city_none})")
    if city_drop_reasons:
        top = sorted(city_drop_reasons.items(), key=lambda kv: -kv[1])[:8]
        print(f"        top extracted cities NOT in allow-list:")
        for city_name, n in top:
            print(f"          · {city_name!r}: {n}")
    print(f"    {skipped_no_data:>3} dropped — unparseable / cancelled / adult-NSFW")

    filled = backfill_images(candidates)
    if filled:
        print(f"\n  ↳ Backfilled {filled} image(s) from source pages")

    inserted = 0
    updated  = 0
    print(f"\n━━ Saving {len(candidates)} unique event(s) for {newsletter_tag} ━━")
    for ev in candidates:
        page_id = existing.get(ev["source_url"])
        if save_event(WEEKEND_EVENTS_DB_ID, ev, newsletter_tag, page_id=page_id):
            label = "↻" if page_id else "✓"
            if page_id:
                updated += 1
            else:
                inserted += 1
            price_disp = (f" ${ev['price_usd']:.0f}" if ev["price_usd"] else "")
            print(f"  {label} {ev['start_date']}  {ev['event_name'][:55]:55s}"
                  f"  ({ev.get('city','?')}){price_disp}")
    print(f"\n  ✓ {newsletter_tag}: inserted {inserted}, refreshed {updated}")
    return 0


if __name__ == "__main__":
    print("eventbrite_apify is a library — invoke run_eventbrite() from a "
          "per-newsletter wrapper in East_Cobb_Connect/, Perimeter_Post/, "
          "or Lewisville_Lake_Lookout/.")
    sys.exit(1)
