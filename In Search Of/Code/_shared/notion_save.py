"""Notion upsert for In Search Of DB rows.

Mirrors the Weekend Events `_shared/notion_save.py` pattern but for
jobs. Two public functions:

  existing_source_urls(db_id, newsletter=None)
      Returns dict[url -> page_id]. Per-newsletter scope so the same
      URL (e.g. governmentjobs.com) can land as separate rows in
      different newsletters without colliding.

  save_job(db_id, row, newsletter, page_id=None)
      Create a new pending row, OR refresh an existing row's scraped
      content when page_id is given. Update mode leaves Newsletter,
      Status, Manually Edited, and Description (Claude's blurb) intact
      so reviewer state and AI-written copy aren't clobbered on
      re-scrape; only Scraped Snippet, Image URL, Roles, City, and
      Date Generated refresh.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import HEADERS as NOTION_HEADERS, query_database  # noqa: E402


def existing_source_urls(db_id: str,
                         newsletter: str | None = None) -> dict[str, str]:
    """Return mapping of Job Listings URL → Notion page_id for rows in
    the In Search Of DB.

    `newsletter` scopes the lookup so per-newsletter rows don't collide
    when the same URL appears for multiple newsletters."""
    filters = None
    if newsletter:
        filters = {"property": "Newsletter", "select": {"equals": newsletter}}
    pages = query_database(db_id, filters=filters) or []
    out: dict[str, str] = {}
    for p in pages:
        url = (p.get("properties", {}).get("Job Listings URL", {}).get("url") or "").strip()
        if url:
            out[url] = p.get("id", "")
    return out


_schema_ensured = False


def _ensure_in_search_of_schema(db_id: str) -> None:
    """Create the In Search Of DB properties if they don't exist (idempotent;
    runs once per process). Notion's PATCH adds missing properties and leaves
    existing ones — and your data — untouched. The title property is left
    alone (a database already has exactly one)."""
    global _schema_ensured
    if _schema_ensured or not db_id:
        return
    props = {
        "Employer":         {"rich_text": {}},
        "Job Listings URL": {"url": {}},
        "Scraped Snippet":  {"rich_text": {}},
        "Image URL":        {"url": {}},
        "City":             {"rich_text": {}},
        "Roles":            {"rich_text": {}},
        "Description":      {"rich_text": {}},
        "Date Generated":   {"date": {}},
        "Bonus":            {"checkbox": {}},
        "Manually Edited":  {"checkbox": {}},
        "Newsletter":       {"select": {"options": [
            {"name": "East_Cobb_Connect",       "color": "purple"},
            {"name": "Perimeter_Post",          "color": "pink"},
            {"name": "Lewisville_Lake_Lookout", "color": "blue"},
        ]}},
        "Status":           {"select": {"options": [
            {"name": "pending",        "color": "yellow"},
            {"name": "approved",       "color": "green"},
            {"name": "rejected",       "color": "red"},
            {"name": "approved - old", "color": "gray"},
        ]}},
    }
    try:
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{db_id}",
            headers=NOTION_HEADERS,
            json={"properties": props},
            timeout=30,
        )
        if r.ok:
            print("  ✓ In Search Of schema ready")
        else:
            print(f"  ⚠ In Search Of schema update failed: {r.text[:200]}")
    except Exception as e:
        print(f"  ⚠ In Search Of schema update error: {e}")
    _schema_ensured = True


def save_job(db_id: str, row: dict, newsletter: str,
             page_id: str | None = None) -> bool:
    """Create a new In Search Of row, OR update an existing row when
    `page_id` is provided.

    Update mode refreshes scrape-side fields (Scraped Snippet, Image
    URL, City, Date Generated) while preserving Newsletter, Status,
    Manually Edited, Description (Claude blurb), Roles, and Bonus."""
    if not row.get("job_listings_url"):
        return False

    _ensure_in_search_of_schema(db_id)

    employer = (row.get("employer") or "").strip()
    snippet  = (row.get("scraped_snippet") or "").strip()
    image    = (row.get("image_url") or "").strip()
    city     = (row.get("city") or "").strip().lower()

    refresh_props = {
        "Scraped Snippet": {"rich_text": [{"text": {"content": snippet[:2000]}}]},
        "Image URL":       {"url": image or None},
        "City":            {"rich_text": [{"text": {"content": city[:80]}}]},
        "Date Generated":  {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
    }

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
            dict(refresh_props),
        )
        if not r.ok:
            print(f"    ✗ update failed: {r.status_code} {r.text[:200]}")
            return False
        return True

    create_props = dict(refresh_props)
    create_props["Name"]             = {"title": [{"text": {"content": employer[:200] or "(unnamed employer)"}}]}
    create_props["Employer"]         = {"rich_text": [{"text": {"content": employer[:200]}}]}
    create_props["Job Listings URL"] = {"url": row["job_listings_url"]}
    create_props["Newsletter"]       = {"select": {"name": newsletter}}
    create_props["Status"]           = {"select": {"name": "pending"}}
    # Description starts empty; Claude fills it during the curator pass.
    create_props["Description"]      = {"rich_text": [{"text": {"content": ""}}]}
    # Bonus checkbox seeded from the source registry's hint; the skill
    # may still flip this if Claude detects an obvious resource page.
    if row.get("is_resource_hint"):
        create_props["Bonus"] = {"checkbox": True}

    r = _send_with_heal("POST", "https://api.notion.com/v1/pages", create_props)
    if not r.ok:
        print(f"    ✗ save failed: {r.status_code} {r.text[:200]}")
        return False
    return True
