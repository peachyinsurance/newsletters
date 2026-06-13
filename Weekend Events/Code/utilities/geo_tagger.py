#!/usr/bin/env python3
"""Distance-based newsletter tagging for the Weekend Events DB.

Replaces the hardcoded city-name sweep (normalize_city_tags) with a real
straight-line distance test against each newsletter's anchor.

For every event row:
  1. Pull the event's ZIP out of its Address and look up that ZIP's centroid
     (offline, via the `pgeocode` US dataset — no API key, no cost, no limits).
  2. Haversine distance from that centroid to each newsletter's anchor
     (lat/lng in newsletters_config).
  3. Tag by which anchors are within RADIUS_MILES:
       0 anchors  -> 'untagged_group'   (used by NO newsletter)
       1 anchor   -> that newsletter's name (e.g. East_Cobb_Connect)
       2 anchors  -> the joint tag (East_Cobb_Connect + Perimeter_Post -> ECC_PP)

Events we can't place (address has no ZIP and no stored coords) are LEFT ON
THEIR CURRENT TAG (logged), not dropped — so a fixed venue whose address omits
a ZIP (e.g. The Battery) keeps the tag its scraper assigned rather than
silently vanishing.

Run:
    NOTION_WEEKEND_EVENTS_DB_ID=... python "Weekend Events/Code/utilities/geo_tagger.py"
"""
import os
import re
import sys
from math import asin, cos, radians, sin, sqrt

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import query_database, update_page  # noqa: E402
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

try:
    import pgeocode
    _NOMI = pgeocode.Nominatim("us")
except Exception as e:  # pragma: no cover
    _NOMI = None
    print(f"⚠ pgeocode unavailable ({e}) — cannot geo-tag")


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.7613  # mean earth radius, miles
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * R * asin(sqrt(a))


def zip_centroid(address: str):
    """(lat, lng) for the LAST 5-digit ZIP in `address`, or None. The ZIP is
    taken last so a leading street number ('12345 Main St … 30301') doesn't
    masquerade as the ZIP."""
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
    if lat != lat or lng != lng:  # NaN → unknown ZIP
        return None
    return (lat, lng)


def tag_for_coords(coords):
    """Newsletter tag for an event at (lat, lng) per the radius rule.
    Returns None when coords is None so the caller leaves the row unchanged."""
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


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1
    if _NOMI is None:
        return 1

    print("Geo-tagger — distance-based newsletter tagging")
    print(f"  Anchors ({RADIUS_MILES:g}-mi radius): "
          f"{[(n, round(a, 3), round(b, 3)) for n, a, b in ANCHORS]}")
    pages = query_database(WEEKEND_EVENTS_DB_ID) or []
    print(f"  Loaded {len(pages)} rows\n")

    retagged = already = unplaceable = 0
    for page in pages:
        props = page.get("properties", {})
        address = _rt(props, "Address")
        current = (props.get("Newsletter", {}).get("select") or {}).get("name", "")
        target = tag_for_coords(zip_centroid(address))
        if target is None:
            unplaceable += 1
            continue
        if target == current:
            already += 1
            continue
        title = _rt(props, "Event Name")[:55] or "?"
        try:
            update_page(page["id"], {"Newsletter": {"select": {"name": target}}})
            retagged += 1
            print(f"  ↻ {current or '∅'} → {target:16s} {title}")
        except Exception as e:
            print(f"  ✗ failed to retag {title}: {e}")

    print(f"\n✓ Done. Retagged {retagged}, {already} already correct, "
          f"{unplaceable} unplaceable (left on current tag).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
