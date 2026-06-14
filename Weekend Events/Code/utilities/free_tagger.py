"""Post-scrape free-event tagging.

Runs ONCE over the Weekend Events DB AFTER every scraper has finished, and
sets a boolean `Free` checkbox on each upcoming row using the strongest
available signal:

  1. The scraped `Price` (JSON-LD offers): 'Free' / '$0' → free; any positive
     amount → NOT free. A published price is authoritative.
  2. For rows with NO scraped price, a keyword scan of Event Name +
     Description ('free' / 'no charge' / 'complimentary' / '$0'). This is the
     categorizer that used to live in Free_Events.py — moved here so free-ness
     is decided in ONE place, up front, instead of re-scanned every time the
     Free Events pipeline runs.

The Free Events pipeline then just reads the `Free` flag.

Idempotently creates the `Price` (rich_text) and `Free` (checkbox) columns.

Run:
    NOTION_WEEKEND_EVENTS_DB_ID=... python "Weekend Events/Code/utilities/free_tagger.py"
"""
import os
import re
import sys
from datetime import date

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import query_database, update_page, HEADERS  # noqa: E402

WEEKEND_EVENTS_DB_ID = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")

# Keyword patterns that mark an event free when no price was scraped. Matched
# case-insensitively against Event Name + Description. Intentionally loose
# (e.g. 'free parking' on a paid event triggers) — same trade-off as before;
# an explicit scraped price always wins over these, so they only decide the
# no-price tail.
_FREE_PATTERNS = [
    re.compile(r"\bfree\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:charge|cost|fee|admission)\b", re.IGNORECASE),
    re.compile(r"\bcomplimentary\b", re.IGNORECASE),
    re.compile(r"\$\s*0(?:\.00)?\b"),
]


def _looks_free(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    return any(p.search(blob) for p in _FREE_PATTERNS)


def _rt(props: dict, key: str) -> str:
    rt = (props.get(key) or {}).get("rich_text") or []
    return "".join(t.get("plain_text", "") or t.get("text", {}).get("content", "")
                   for t in rt).strip()


def price_is_free(price: str):
    """Interpret a scraped Price string. Returns True (free), False (paid), or
    None (no usable price → caller should fall back to the keyword scan).
      ''            → None
      'Free'        → True
      '$0' / '0'    → True
      '$25'         → False
      '$10–$30'     → False  (a range with any positive amount is paid)
    """
    p = (price or "").strip().lower()
    if not p:
        return None
    if "free" in p:
        return True
    nums = re.findall(r"\d+(?:\.\d+)?", p)
    if not nums:
        return None
    return all(float(n) == 0 for n in nums)


def _ensure_fields(db_id: str) -> None:
    """Idempotently add the `Price` (rich_text) and `Free` (checkbox) columns
    so the scrapers can store price and this tagger can store the flag."""
    try:
        requests.patch(
            f"https://api.notion.com/v1/databases/{db_id}",
            headers=HEADERS,
            json={"properties": {"Price": {"rich_text": {}},
                                 "Free":  {"checkbox": {}}}},
            timeout=30,
        )
    except Exception as e:
        print(f"  ⚠ could not ensure Price/Free fields: {e}")


def main() -> int:
    if not WEEKEND_EVENTS_DB_ID:
        print("✗ NOTION_WEEKEND_EVENTS_DB_ID is not set in env.")
        return 1

    print("Free-tagger — flag each scraped event as free / not free")
    _ensure_fields(WEEKEND_EVENTS_DB_ID)
    pages = query_database(WEEKEND_EVENTS_DB_ID) or []
    today = date.today()
    print(f"  Loaded {len(pages)} rows\n")

    free_by_price = free_by_keyword = paid = not_free = changed = skipped_past = 0
    for page in pages:
        props = page.get("properties", {})
        # Only tag upcoming events; past rows are archived/irrelevant.
        dstr = ((props.get("Date") or {}).get("date") or {}).get("start", "")[:10]
        if dstr and dstr < today.isoformat():
            skipped_past += 1
            continue

        verdict = price_is_free(_rt(props, "Price"))
        if verdict is None:
            # No scraped price → this is the row that goes through the keyword
            # categorizer (the "events without a price" pipeline).
            name = _rt(props, "Event Name") or _rt(props, "Name")
            verdict = _looks_free(name, _rt(props, "Description"))
            if verdict:
                free_by_keyword += 1
            else:
                not_free += 1
        elif verdict:
            free_by_price += 1
        else:
            paid += 1

        current = (props.get("Free") or {}).get("checkbox")
        if current == verdict:
            continue
        try:
            update_page(page["id"], {"Free": {"checkbox": bool(verdict)}})
            changed += 1
        except Exception as e:
            print(f"  ✗ failed to update {_rt(props, 'Event Name')[:50] or '?'}: {e}")

    print(f"\n✓ Done. Free: {free_by_price} via price + {free_by_keyword} via "
          f"keyword; paid (priced > 0): {paid}; not-free (no signal): {not_free}. "
          f"{changed} flag(s) updated, {skipped_past} past skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
