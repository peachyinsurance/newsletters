#!/usr/bin/env python3
"""Distance-based newsletter tagging for the Weekend Events DB.

Replaces the hardcoded city-name sweep (normalize_city_tags) with a real
PER-EVENT straight-line distance test against each newsletter's anchor.

For every (upcoming) event row:
  1. Resolve the event's actual coordinates:
       a. cached `Geo` field on the row (lat,lng) if present, else
       b. geocode the full street Address via Nominatim/OSM (free, no key),
          and cache the result back onto the row's `Geo` field, else
       c. fall back to the ZIP centroid (offline pgeocode) when the full
          geocode fails.
  2. Haversine distance from those coordinates to each newsletter's anchor
     (lat/lng in newsletters_config).
  3. Tag by which anchors are within RADIUS_MILES:
       0 anchors -> 'untagged_group'  (used by NO newsletter)
       1 anchor  -> that newsletter (e.g. East_Cobb_Connect)
       2 anchors -> joint tag (East_Cobb_Connect + Perimeter_Post -> ECC_PP)

Per-event matters: ZIP centroids collapse a whole ZIP to one point (all of
Roswell read 12 mi from East Cobb), but the actual addresses range 9-11 mi —
so the southern edge of Roswell is correctly ECC_PP while the rest is PP-only.

Events we can't place at all (no Address, no ZIP, no coords) are LEFT ON
THEIR CURRENT TAG (logged) rather than dropped.

Run:
    NOTION_WEEKEND_EVENTS_DB_ID=... python "Weekend Events/Code/utilities/geo_tagger.py"
"""
import os
import re
import sys
import time
from datetime import date
from math import asin, cos, radians, sin, sqrt

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import query_database, update_page, HEADERS  # noqa: E402
from newsletters_config import NEWSLETTERS  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
RADIUS_MILES = 10.0
UNTAGGED_TAG = "untagged_group"

# Joint tag when more than one newsletter's anchor claims the same event.
# (LLL is in Texas, so the only realistic overlap is ECC + PP.)
JOINT_TAGS = {
    frozenset({"East_Cobb_Connect", "Perimeter_Post"}): "ECC_PP",
}

# (name, lat, lng) for every newsletter that has an anchor coordinate.
ANCHORS = [(nl["name"], nl["lat"], nl["lng"]) for nl in NEWSLETTERS
           if nl.get("lat") is not None and nl.get("lng") is not None]

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_GEO_HEADERS = {"User-Agent": "east-cobb-connect-newsletter-geotagger/1.0 "
                              "(contact: peachyinsurance)"}
_GEO_CACHE: dict[str, tuple] = {}   # per-run cache: address → (lat, lng) | None

try:
    import pgeocode
    _NOMI = pgeocode.Nominatim("us")
except Exception as e:  # pragma: no cover
    _NOMI = None
    print(f"⚠ pgeocode unavailable ({e}) — ZIP fallback disabled")


def _haversine_miles(lat1, lng1, lat2, lng2):
    R = 3958.7613  # mean earth radius, miles
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _valid(lat, lng) -> bool:
    return lat == lat and lng == lng and -90 <= lat <= 90 and -180 <= lng <= 180


def _zip_centroid(address: str):
    if not address or _NOMI is None:
        return None
    zips = _ZIP_RE.findall(address)
    if not zips:
        return None
    rec = _NOMI.query_postal_code(zips[-1])
    try:
        lat, lng = float(rec.latitude), float(rec.longitude)
    except (TypeError, ValueError):
        return None
    return (lat, lng) if _valid(lat, lng) else None


def _geocode_full(address: str):
    """Precise (lat, lng) for a full street address via Nominatim. Rate-limited
    to 1 req/sec per the OSM usage policy. None on miss/error."""
    try:
        time.sleep(1.1)
        r = requests.get(_NOMINATIM,
                         params={"q": address, "format": "json",
                                 "limit": 1, "countrycodes": "us"},
                         headers=_GEO_HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json() or []
            if data:
                lat, lng = float(data[0]["lat"]), float(data[0]["lon"])
                if _valid(lat, lng):
                    return (lat, lng)
    except Exception as e:
        print(f"    ⚠ geocode error for {address[:50]!r}: {e}")
    return None


def event_coords(address: str, cached: tuple | None):
    """Best (lat, lng) for an event: cached row coords → full-address geocode
    → ZIP centroid. Returns (coords, newly_geocoded_bool)."""
    if cached:
        return cached, False
    if not address:
        return None, False
    key = address.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key], False
    coords = _geocode_full(address) or _zip_centroid(address)
    _GEO_CACHE[key] = coords
    return coords, bool(coords)


def tag_for_coords(coords):
    if not coords:
        return None
    lat, lng = coords
    near = sorted(name for name, alat, alng in ANCHORS
                  if _haversine_miles(lat, lng, alat, alng) <= RADIUS_MILES)
    if not near:
        return UNTAGGED_TAG
    if len(near) == 1:
        return near[0]
    return JOINT_TAGS.get(frozenset(near)) or near[0]


def _rt(props: dict, key: str) -> str:
    rt = (props.get(key) or {}).get("rich_text") or []
    return (rt[0].get("text", {}).get("content", "") if rt else "").strip()


def _parse_geo(s: str):
    try:
        lat, lng = (float(x) for x in s.split(",", 1))
        return (lat, lng) if _valid(lat, lng) else None
    except Exception:
        return None


def _ensure_geo_field(db_id: str) -> None:
    """Idempotently add a `Geo` rich_text column so coordinates can be cached
    on the row (geocode once per address, ever)."""
    try:
        requests.patch(f"https://api.notion.com/v1/databases/{db_id}",
                       headers=HEADERS,
                       json={"properties": {"Geo": {"rich_text": {}}}},
                       timeout=30)
    except Exception as e:
        print(f"  ⚠ could not ensure Geo field (caching disabled): {e}")


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    print("Geo-tagger — per-event distance-based newsletter tagging")
    print(f"  Anchors ({RADIUS_MILES:g}-mi radius): "
          f"{[(n, round(a, 3), round(b, 3)) for n, a, b in ANCHORS]}")
    _ensure_geo_field(WEEKEND_EVENTS_DB_ID)
    pages = query_database(WEEKEND_EVENTS_DB_ID) or []
    today = date.today()
    print(f"  Loaded {len(pages)} rows\n")

    retagged = already = unplaceable = skipped_past = geocoded = 0
    for page in pages:
        props = page.get("properties", {})
        # Only tag upcoming events — past rows are archived/irrelevant and not
        # worth geocoding.
        dstr = ((props.get("Date") or {}).get("date") or {}).get("start", "")[:10]
        if dstr and dstr < today.isoformat():
            skipped_past += 1
            continue

        address = _rt(props, "Address")
        # Existing rows with a blank Address: build one from the venue (+ city)
        # so they can be placed and so every event ends up with an address.
        built_address = ""
        if not address:
            built_address = ", ".join(p for p in (_rt(props, "Location"),
                                                   _rt(props, "City")) if p)
            address = built_address
        current = (props.get("Newsletter", {}).get("select") or {}).get("name", "")
        cached  = _parse_geo(_rt(props, "Geo"))
        coords, newly = event_coords(address, cached)
        if newly:
            geocoded += 1
        target = tag_for_coords(coords)
        if target is None:
            unplaceable += 1
            continue

        update_props: dict = {}
        if target != current:
            update_props["Newsletter"] = {"select": {"name": target}}
        if newly and coords:
            update_props["Geo"] = {"rich_text": [{"text": {"content": f"{coords[0]},{coords[1]}"}}]}
        if built_address and coords:
            update_props["Address"] = {"rich_text": [{"text": {"content": built_address[:200]}}]}
        if not update_props:
            already += 1
            continue
        title = _rt(props, "Event Name")[:50] or "?"
        try:
            update_page(page["id"], update_props)
            if "Newsletter" in update_props:
                retagged += 1
                print(f"  ↻ {current or '∅'} → {target:16s} {title}")
        except Exception as e:
            print(f"  ✗ failed to update {title}: {e}")

    print(f"\n✓ Done. Retagged {retagged}, {already} already correct, "
          f"{unplaceable} unplaceable (left as-is), {skipped_past} past skipped. "
          f"({geocoded} addresses geocoded this run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
