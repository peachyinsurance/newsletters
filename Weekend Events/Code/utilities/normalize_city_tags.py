#!/usr/bin/env python3
"""Post-scrape sweep: flip per-newsletter rows to shared tags based on
the venue's City.

Every scraper saves rows with a single newsletter tag matching its
file location (East_Cobb_Connect/cobb_county.py → East_Cobb_Connect,
Perimeter_Post/eventbrite.py → Perimeter_Post). For events whose
venue city falls in a shared coverage area, we then flip the tag to
the ECC_PP shared tag so both newsletters' pickers see them.

Without this sweep, an Eventbrite event held in Roswell would only
show up in Perimeter Post (since the PP wrapper tagged it), even
though ECC readers would also want it. The sandy_springs, visit_roswell,
and roswell_365 wrappers tag ECC_PP at write time; this sweep extends
the same logic to anything scraped elsewhere whose City happens to
be shared.

Configuration: SHARED_CITY_TAGS maps a normalized city name to the
target shared tag. Edit there to add more shared cities (Smyrna,
Vinings, etc. if ECC and PP overlap further).

Idempotent — only updates rows whose current Newsletter tag isn't
already the target tag.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import query_database, update_page  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")

# Lowercase city name → newsletter tag the row should end up under.
# Add cities here as ECC/PP coverage overlap grows.
SHARED_CITY_TAGS: dict[str, str] = {
    "roswell": "ECC_PP",
}

# Newsletters we're willing to retag. Don't touch rows under unrelated
# tags (LLL, archived, manual statuses, etc.).
RETAG_FROM_TAGS = {"East_Cobb_Connect", "Perimeter_Post"}


def _rt(props: dict, key: str) -> str:
    rt = (props.get(key) or {}).get("rich_text") or []
    return (rt[0].get("text", {}).get("content", "") if rt else "").strip()


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    print(f"City-tag normalizer — scanning Weekend Events DB {WEEKEND_EVENTS_DB_ID[:8]}…")
    print(f"  Shared cities: {dict(SHARED_CITY_TAGS)}")
    print(f"  Retag-eligible source tags: {sorted(RETAG_FROM_TAGS)}")
    print()

    pages = query_database(WEEKEND_EVENTS_DB_ID) or []
    print(f"  Loaded {len(pages)} rows from the DB\n")

    retagged = 0
    scanned = 0
    skipped_no_city = 0
    skipped_wrong_tag = 0
    already_correct = 0
    for page in pages:
        props = page.get("properties", {})
        city = _rt(props, "City").lower()
        current = (props.get("Newsletter", {}).get("select") or {}).get("name", "")
        if not city:
            skipped_no_city += 1
            continue
        scanned += 1
        target = SHARED_CITY_TAGS.get(city)
        if not target:
            continue
        if current == target:
            already_correct += 1
            continue
        if current not in RETAG_FROM_TAGS:
            skipped_wrong_tag += 1
            continue
        # Flip it.
        title = _rt(props, "Event Name")[:60] or "?"
        try:
            update_page(page["id"],
                        {"Newsletter": {"select": {"name": target}}})
            retagged += 1
            print(f"  ↻ {current} → {target}   {city!r:18s}  {title}")
        except Exception as e:
            print(f"  ✗ failed to retag {title}: {e}")

    print()
    print(f"✓ Done. Retagged {retagged} row(s).")
    print(f"   {scanned} had a city set, {skipped_no_city} had no city")
    print(f"   {already_correct} already on the right tag, "
          f"{skipped_wrong_tag} on a non-retag-eligible tag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
