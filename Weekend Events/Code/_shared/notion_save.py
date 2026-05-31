"""Notion upsert for Weekend Events DB rows.

Per-occurrence-row model: each row represents ONE occurrence of an event
on ONE date, carrying its own native `Source URL` and `Date`. Recurring
events (a daily Cobb County exhibit, a Fri–Sun festival) are stored as
multiple rows — one per in-window date — rather than a single row with a
JSON {date: url} map. This guarantees a per-day card downstream links to
the exact day's URL with no map lookup or earliest-occurrence fallback.

Two public functions:

  existing_source_urls(db_id, newsletter=None)
      Returns dict[(url, iso_date) -> page_id]. The dedup key is the
      (Source URL, Date) pair, NOT url alone — single-page sources reuse
      one URL across many dates, so url alone would collapse distinct
      occurrences. Per-newsletter scope when `newsletter` is provided —
      essential when the same source URL can land under multiple
      newsletters (e.g. a Roswell event that ECC and PP both cover).

  save_event(db_id, ev, newsletter, page_id=None)
      Create a new row, OR refresh an existing row when page_id is
      given. Update mode preserves Source URL, Newsletter, Status,
      Manually Edited; refreshes Event Name, Description, Image URL,
      Location, Address, Time, Dates (single ISO date), Date, Date
      Generated. `ev` must be a single occurrence (one start_date).

Both functions self-heal against Notion DB schema gaps via the missing-
property retry loop in `_send_with_heal`.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime

import requests

# Import notion_helper from NewsletterCreation/Code for HEADERS + query.
# Path is `Weekend Events/Code/_shared/notion_save.py` → ../../../NewsletterCreation/Code.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402


def existing_source_urls(db_id: str,
                         newsletter: str | None = None
                         ) -> dict[tuple[str, str], str]:
    """Return mapping of (Source URL, Date ISO) → Notion page_id for rows
    in the Weekend Events DB.

    The key is the (url, date) pair so distinct occurrences that share one
    detail page (single-page recurring sources) each get their own row
    instead of clobbering one another. Rows with no Date are keyed
    (url, "") so a legacy/dateless row still dedups by URL alone.

    `newsletter` scopes the lookup to rows tagged with that newsletter.
    Crucial when the same source URL can legitimately surface under
    multiple newsletters — without scoping, an East_Cobb_Connect run
    that encountered a URL already saved under Perimeter_Post would
    update the PP row in place, corrupting cross-newsletter state.
    Defaults to None (all newsletters) for single-newsletter scrapers."""
    filters = None
    if newsletter:
        filters = {"property": "Newsletter", "select": {"equals": newsletter}}
    pages = query_database(db_id, filters=filters) or []
    out: dict[tuple[str, str], str] = {}
    for p in pages:
        props = p.get("properties", {})
        url = (props.get("Source URL", {}).get("url") or "").strip()
        if not url:
            continue
        iso = ((props.get("Date") or {}).get("date") or {}).get("start") or ""
        out[(url, iso[:10])] = p.get("id", "")
    return out


def save_event(db_id: str, ev: dict, newsletter: str,
               page_id: str | None = None) -> bool:
    """Create a new event row, OR update an existing row when `page_id`
    is provided.

    Update mode refreshes content fields (Event Name, Description, Image
    URL, Location, Address, Time, Dates, Date, Date Generated) while
    leaving Newsletter, Status, Source URL, and Manually Edited intact
    so manual curation isn't clobbered on re-scrape.

    Per-occurrence model: `ev` is a single occurrence with one
    `start_date`. The `Dates` field holds that single ISO date — the
    Weekend Planner parses the ISO match to map the row to its target-
    weekend day. Recurring events are saved as multiple calls, one per
    in-window date, by the scraper."""
    if not ev.get("source_url"):
        return False

    # `Dates` mirrors the single occurrence date in ISO form so the
    # Weekend Planner's ISO parser maps the row to its weekend day.
    dates_display = ev.get("dates_display") or ""
    if not dates_display and ev.get("start_date"):
        dates_display = ev["start_date"].isoformat()

    # `city` is the venue's normalized city name (lowercase, single word
    # or "two words" — e.g. "roswell" / "sandy springs"). Scrapers extract
    # from JSON-LD addressLocality / hardcode for single-city sources.
    # Consumed by utilities/normalize_city_tags.py to flip rows to ECC_PP
    # when the city falls in a shared coverage area.
    city = (ev.get("city") or "").strip().lower()

    content = {
        "Event Name":     {"rich_text": [{"text": {"content": ev["event_name"][:200]}}]},
        "Description":    {"rich_text": [{"text": {"content": ev["description"][:2000]}}]},
        "Image URL":      {"url": ev["image_url"] or None},
        "Location":       {"rich_text": [{"text": {"content": ev["location"][:200]}}]},
        "Address":        {"rich_text": [{"text": {"content": ev["address"][:200]}}]},
        "City":           {"rich_text": [{"text": {"content": city[:80]}}]},
        "Time":           {"rich_text": [{"text": {"content": ev["time"][:80]}}]},
        "Dates":          {"rich_text": [{"text": {"content": dates_display[:500]}}]},
        "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
    }
    if ev.get("start_date"):
        date_prop = {"start": ev["start_date"].isoformat()}
        if ev.get("end_date") and ev["end_date"] != ev["start_date"]:
            date_prop["end"] = ev["end_date"].isoformat()
        content["Date"] = {"date": date_prop}

    def _send_with_heal(method: str, url: str, props: dict) -> requests.Response:
        if method == "POST":
            body = {"parent": {"database_id": db_id}, "properties": props}
        else:
            body = {"properties": props}
        r = requests.request(method, url, headers=NOTION_HEADERS,
                             json=body, timeout=30)
        attempts = 0
        while (not r.ok and r.status_code == 400
               and "is not a property that exists" in r.text
               and attempts < 5):
            attempts += 1
            m = (re.search(r"`([^`]+)` is not a property", r.text)
                 or re.search(r'"message":"([^"]+?) is not a property', r.text))
            if not m:
                break
            bad_prop = m.group(1)
            if bad_prop not in props:
                break
            print(f"    ⚠ dropping unknown Notion property '{bad_prop}' and retrying")
            props.pop(bad_prop, None)
            body["properties"] = props
            r = requests.request(method, url, headers=NOTION_HEADERS,
                                 json=body, timeout=30)
        return r

    if page_id:
        r = _send_with_heal(
            "PATCH", f"https://api.notion.com/v1/pages/{page_id}",
            dict(content),
        )
        if not r.ok:
            print(f"    ✗ update failed: {r.status_code} {r.text[:200]}")
            return False
        return True

    create_props = dict(content)
    create_props["Name"]       = {"title": [{"text": {"content": ev["event_name"][:200] or "(unnamed event)"}}]}
    create_props["Source URL"] = {"url": ev["source_url"]}
    create_props["Newsletter"] = {"select": {"name": newsletter}}
    create_props["Status"]     = {"select": {"name": "pending"}}
    r = _send_with_heal("POST", "https://api.notion.com/v1/pages", create_props)
    if not r.ok:
        print(f"    ✗ save failed: {r.status_code} {r.text[:200]}")
        return False
    return True
