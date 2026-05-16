#!/usr/bin/env python3
"""
Shared Notion API helper for Newsletter Automation.
Handles creating/updating pages in Pets and Restaurants databases.
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Notion rate-limited HTTP wrapper.
# Notion's REST API limits to ~3 req/sec averaged. Bursting hits 429s.
# Retries 429/5xx up to 5 times with exponential backoff (honoring
# Retry-After when present) so a multi-newsletter run doesn't crash mid-save.
# ---------------------------------------------------------------------------
def _notion_request(method: str, url: str, *, json_body: dict | None = None,
                    timeout: int = 30, max_attempts: int = 5) -> requests.Response:
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.request(method, url, headers=HEADERS,
                                 json=json_body, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            wait = min(2 ** attempt, 30)
            print(f"  ⚠ Notion network error (attempt {attempt}/{max_attempts}): {e} — sleeping {wait}s")
            time.sleep(wait)
            continue

        # Retry on rate-limit / transient server errors
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            try:
                wait = float(r.headers.get("Retry-After", "")) or min(2 ** attempt, 30)
            except (ValueError, TypeError):
                wait = min(2 ** attempt, 30)
            print(f"  ⚠ Notion {r.status_code} (attempt {attempt}/{max_attempts}) — sleeping {wait}s")
            time.sleep(wait)
            continue

        r.raise_for_status()
        return r
    if last_exc:
        raise last_exc
    raise RuntimeError("Notion request failed after retries")

NOTION_API_KEY           = os.environ["NOTION_API_KEY"]
NOTION_PETS_DB_ID        = os.environ.get("NOTION_PETS_DB_ID", "")
NOTION_RESTAURANTS_DB_ID = os.environ.get("NOTION_RESTAURANTS_DB_ID", "")
NOTION_LOWDOWN_DB_ID     = os.environ.get("NOTION_LOWDOWN_DB_ID", "")
NOTION_RE_DB_ID          = os.environ.get("NOTION_RE_DB_ID", "")
NOTION_EVENTS_DB_ID      = os.environ.get("NOTION_EVENTS_DB_ID", "")
NOTION_INTRO_DB_ID       = os.environ.get("NOTION_INTRO_DB_ID", "")
NOTION_TIPS_DB_ID        = os.environ.get("NOTION_TIPS_DB_ID", "")
NOTION_FREE_EVENTS_DB_ID = os.environ.get("NOTION_FREE_EVENTS_DB_ID", "")
NOTION_POLLS_DB_ID       = os.environ.get("NOTION_POLLS_DB_ID", "")
NOTION_WEEKEND_PLANNER_DB_ID = os.environ.get("NOTION_WEEKEND_PLANNER_DB_ID", "")
NOTION_WEEKEND_EVENTS_DB_ID  = os.environ.get("NOTION_WEEKEND_EVENTS_DB_ID", "")
NOTION_BUSINESS_BRIEF_DB_ID = os.environ.get("NOTION_BUSINESS_BRIEF_DB_ID", "")

HEADERS = {
    "Authorization":  f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    # charset=utf-8 is required — without it, some receivers default to
    # ISO-8859-1 for application/json bodies, which double-encodes emojis
    # (⭐ → Ã¢Â­Â etc.) on the round-trip and corrupts stored text.
    "Content-Type":   "application/json; charset=utf-8",
}

# ---------------------------------------------------------------------------
# GENERIC HELPERS
# ---------------------------------------------------------------------------
def query_database(db_id: str, filters: dict = None) -> list:
    """Query a Notion database with optional filter, paginating until done.

    If a filter is provided and Notion returns HTTP 400 (typically because
    a `select` filter references an option that doesn't yet exist in the
    schema — e.g. a newsletter name introduced this run), retry once
    without the filter and let the caller filter results in Python.
    Returns an empty list if both attempts fail."""
    url     = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"filter": filters} if filters else {}
    results = []
    has_more = True
    cursor   = None

    while has_more:
        if cursor:
            payload["start_cursor"] = cursor
        try:
            r = _notion_request("POST", url, json_body=payload)
        except requests.exceptions.HTTPError as e:
            if filters and e.response is not None and e.response.status_code == 400:
                print(f"  ⚠ Notion 400 on filtered query of {db_id[:8]}… — "
                      f"retrying unfiltered (callers should filter in Python)")
                return query_database(db_id, filters=None)
            raise
        data     = r.json()
        results += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor   = data.get("next_cursor")

    return results

def update_page(page_id: str, properties: dict) -> dict:
    r = _notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        json_body={"properties": properties},
    )
    return r.json()

def create_page(db_id: str, properties: dict) -> dict:
    """Create a Notion page. On 400 with a property-not-found error,
    automatically drop the offending property and retry once — keeps a
    save batch alive when the DB schema is missing a newly-added column.

    Logs which property was dropped so it's visible in run output (run
    Setup Notion Databases to add it permanently)."""
    import json as _json
    try:
        r = _notion_request(
            "POST",
            "https://api.notion.com/v1/pages",
            json_body={"parent": {"database_id": db_id}, "properties": properties},
        )
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 400:
            raise
        # Try to parse Notion's "property not found" error and retry without it
        try:
            err = e.response.json()
            msg = err.get("message", "") or e.response.text
        except Exception:
            msg = e.response.text
        # Notion's typical message: "X is not a property that exists" or
        # "X is not a property of this database"
        import re as _re
        m = _re.search(r"^([^\n]+?)\s+is not (?:a property|a valid|of this)", msg)
        if not m:
            print(f"  Notion 400 error (not a missing-property issue): {msg[:500]}")
            raise
        bad_prop = m.group(1).strip().strip("'\"")
        if bad_prop not in properties:
            print(f"  Notion 400 mentioned property '{bad_prop}' but it's not in our payload: {msg[:300]}")
            raise
        print(f"  ⚠ Notion DB missing column '{bad_prop}' — saving row without it (run Setup Notion Databases to add the column)")
        retry_props = {k: v for k, v in properties.items() if k != bad_prop}
        r = _notion_request(
            "POST",
            "https://api.notion.com/v1/pages",
            json_body={"parent": {"database_id": db_id}, "properties": retry_props},
        )
        return r.json()

def archive_page(page_id: str) -> dict:
    """Archive (soft-delete) a Notion page."""
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"archived": True},
        timeout=30
    )
    r.raise_for_status()
    return r.json()

def safe_str(value) -> str:
    """Convert any value to a safe non-null string."""
    if value is None:
        return ""
    return str(value).strip()
    
def setup_notion_databases():
    """Create all required properties in both Notion databases."""

    # Pets database properties
    pets_properties = {
        "Name":               {"title": {}},
        "Source URL":         {"url": {}},
        "Listing URL":        {"url": {}},
        "Shelter":            {"rich_text": {}},
        "Blurb":              {"rich_text": {}},
        "Shelter Address":    {"rich_text": {}},
        "Shelter Phone":      {"rich_text": {}},
        "Shelter Email":      {"rich_text": {}},
        "Shelter Hours":      {"rich_text": {}},
        "Photo URL":          {"url": {}},
        "GIF URL":            {"url": {}},
        "Date Generated":     {"date": {}},
        "Status":             {"select": {"options": [
            {"name": "pending",  "color": "yellow"},
            {"name": "approved", "color": "green"},
            {"name": "rejected", "color": "red"},
            {"name": "approved - old", "color": "gray"}
        ]}},
        "Section":            {"select": {"options": [{"name": "pet_blurb", "color": "blue"}]}},
        "Newsletter":         {"select": {"options": [
            {"name": "East_Cobb_Connect", "color": "purple"},
            {"name": "Perimeter_Post",    "color": "pink"}
        ]}},
        "Total Score":        {"number": {"format": "number"}},
        "Adoptability Score": {"number": {"format": "number"}},
        "Story Score":        {"number": {"format": "number"}},
        "Shelter Time Score": {"number": {"format": "number"}},
        "Scoring Notes":      {"rich_text": {}},
        "Default Winner":     {"checkbox": {}},
        "Cat Default":        {"checkbox": {}},
        "Dog Default":        {"checkbox": {}},
        "Animal Type":        {"select": {"options": [
            {"name": "cat", "color": "orange"},
            {"name": "dog", "color": "brown"}
        ]}},
        "Manually Edited":    {"checkbox": {}},
    }

    # Restaurants database properties
    restaurants_properties = {
        "Name":                   {"title": {}},
        "Place ID":               {"rich_text": {}},
        "Cuisine":                {"select": {}},
        "Blurb":                  {"rich_text": {}},
        "Address":                {"rich_text": {}},
        "Phone":                  {"rich_text": {}},
        "Hours":                  {"rich_text": {}},
        "Website":                {"url": {}},
        "Google Maps URL":        {"url": {}},
        "Photo URL":              {"url": {}},
        "GIF URL":                {"url": {}},
        "Rating":                 {"number": {"format": "number"}},
        "Review Count":           {"number": {"format": "number"}},
        "Price Level":            {"select": {}},
        "Date Generated":         {"date": {}},
        "Status":                 {"select": {"options": [
            {"name": "pending"},
            {"name": "Tier 1 Winner"},
            {"name": "Tier 2 Winner"},
            {"name": "approved - old"}
        ]}},
        "Section":                {"select": {"options": [{"name": "restaurant_blurb", "color": "blue"}]}},
        "Newsletter":             {"select": {"options": [
            {"name": "East_Cobb_Connect", "color": "purple"},
            {"name": "Perimeter_Post",    "color": "pink"}
        ]}},
        "Total Score":            {"number": {"format": "number"}},
        "Appeal Score":           {"number": {"format": "number"}},
        "Uniqueness Score":       {"number": {"format": "number"}},
        "Neighborhood Fit Score": {"number": {"format": "number"}},
        "Festive Score":          {"number": {"format": "number"}},
        "Scoring Notes":          {"rich_text": {}},
        "Default Winner":         {"checkbox": {}},
        "Manually Edited":        {"checkbox": {}},
        # Set when a row is brought back from approved-old via the festive
        # cuisine swap. UI uses this to render the row differently (grayed).
        "Festive Promoted":       {"checkbox": {}},
    }

    # Update Pets database schema
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_PETS_DB_ID}",
        headers=HEADERS,
        json={"properties": pets_properties},
        timeout=30
    )
    if r.ok:
        print("✓ Pets database schema created")
    else:
        print(f"✗ Pets schema error: {r.text[:300]}")

    # Update Restaurants database schema
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_RESTAURANTS_DB_ID}",
        headers=HEADERS,
        json={"properties": restaurants_properties},
        timeout=30
    )
    if r.ok:
        print("✓ Restaurants database schema created")
    else:
        print(f"✗ Restaurants schema error: {r.text[:300]}")

    # Local Lowdown database properties
    if NOTION_LOWDOWN_DB_ID:
        lowdown_properties = {
            "Name":            {"title": {}},
            "Newsletter":      {"select": {"options": [
                {"name": "East_Cobb_Connect", "color": "purple"},
                {"name": "Perimeter_Post",    "color": "pink"}
            ]}},
            "Date Generated":  {"date": {}},
            "Status":          {"select": {"options": [
                {"name": "pending",  "color": "yellow"},
                {"name": "approved", "color": "green"}
            ]}},
            "Section Header":  {"rich_text": {}},
            "Stories Count":   {"number": {"format": "number"}},
            "Full Section":    {"rich_text": {}},
            "Manually Edited": {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_LOWDOWN_DB_ID}",
            headers=HEADERS,
            json={"properties": lowdown_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Local Lowdown database schema created")
        else:
            print(f"✗ Local Lowdown schema error: {r.text[:300]}")

    # Real Estate Corner database properties
    if NOTION_RE_DB_ID:
        re_properties = {
            "Name":           {"title": {}},
            "Tier":           {"select": {"options": [
                {"name": "Starter"},
                {"name": "Sweet Spot"},
                {"name": "Showcase"}
            ]}},
            "Price":          {"number": {"format": "dollar"}},
            "Address":        {"rich_text": {}},
            "Beds":           {"number": {"format": "number"}},
            "Baths":          {"number": {"format": "number"}},
            "Sqft":           {"number": {"format": "number"}},
            "Headline":       {"rich_text": {}},
            "Blurb":          {"rich_text": {}},
            "Photo URL":      {"url": {}},
            "GIF URL":        {"url": {}},
            "Template Image": {"url": {}},
            "Listing URL":    {"url": {}},
            "Newsletter":     {"select": {"options": [
                {"name": "East_Cobb_Connect"},
                {"name": "Perimeter_Post"}
            ]}},
            "Date Generated": {"date": {}},
            "Status":         {"select": {"options": [
                {"name": "approved"},
                {"name": "pending"},
                {"name": "approved - old"}
            ]}},
            "Manually Edited": {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_RE_DB_ID}",
            headers=HEADERS,
            json={"properties": re_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Real Estate Corner database schema created")
        else:
            print(f"✗ Real Estate schema error: {r.text[:300]}")

    # Featured Event database properties
    if NOTION_EVENTS_DB_ID:
        events_properties = {
            "Name":                   {"title": {}},
            "Event Name":             {"rich_text": {}},
            "Date":                   {"rich_text": {}},
            "Time":                   {"rich_text": {}},
            "Venue":                  {"rich_text": {}},
            "Price":                  {"rich_text": {}},
            "Blurb":                  {"rich_text": {}},
            "Source URL":             {"url": {}},
            "Ticket URL":            {"url": {}},
            # Colors omitted: existing Featured Event DB already has these options
            # with manually-set colors that Notion's PATCH refuses to change.
            # Same fix Candice applied to the Insurance Tip Category select (5449326).
            "Newsletter":             {"select": {"options": [
                {"name": "East_Cobb_Connect"},
                {"name": "Perimeter_Post"},
                {"name": "Lewisville_Lake_Lookout"},
            ]}},
            "Date Generated":         {"date": {}},
            # No `color` keys — Notion rejects PATCH attempts to change
            # the color of existing select options (the schema PATCH
            # always re-sends every option, so colors must be omitted).
            "Status":                 {"select": {"options": [
                {"name": "pending"},
                {"name": "approved"},
                {"name": "rejected"},
                {"name": "approved - old"},
            ]}},
            "Total Score":            {"number": {"format": "number"}},
            "Demographic Fit Score":  {"number": {"format": "number"}},
            "Uniqueness Score":       {"number": {"format": "number"}},
            "Audience Match Score":   {"number": {"format": "number"}},
            "Scoring Notes":          {"rich_text": {}},
            "Image URL":              {"url": {}},
            "Image Candidates":       {"rich_text": {}},
            "Header Image URL":       {"url": {}},
            "GIF URL":                {"url": {}},
            "Default Winner":         {"checkbox": {}},
            "Manually Edited":        {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_EVENTS_DB_ID}",
            headers=HEADERS,
            json={"properties": events_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Featured Event database schema created")
        else:
            print(f"✗ Featured Event schema error: {r.text[:300]}")

    # Welcome Intro database properties
    if NOTION_INTRO_DB_ID:
        intro_properties = {
            "Name":              {"title": {}},
            "Newsletter":        {"select": {"options": [
                {"name": "East_Cobb_Connect", "color": "purple"},
                {"name": "Perimeter_Post",    "color": "pink"}
            ]}},
            "Date Generated":    {"date": {}},
            "Status":            {"select": {"options": [
                {"name": "approved", "color": "green"},
            ]}},
            "Greeting":          {"rich_text": {}},
            "Blurb":             {"rich_text": {}},
            "Word Count":        {"number": {"format": "number"}},
            "Review Score":      {"number": {"format": "number"}},
            "Review Violations": {"rich_text": {}},
            "Manually Edited":   {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_INTRO_DB_ID}",
            headers=HEADERS,
            json={"properties": intro_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Welcome Intro database schema created")
        else:
            print(f"✗ Welcome Intro schema error: {r.text[:300]}")

    # Free Events database properties
    if NOTION_FREE_EVENTS_DB_ID:
        free_events_properties = {
            "Name":             {"title": {}},
            "Newsletter":       {"select": {"options": [
                {"name": "East_Cobb_Connect", "color": "purple"},
                {"name": "Perimeter_Post",    "color": "pink"}
            ]}},
            "Date Generated":   {"date": {}},
            "Status":           {"select": {"options": [
                {"name": "approved",       "color": "green"},
                {"name": "approved - old", "color": "gray"}
            ]}},
            "Section Header":   {"rich_text": {}},
            "Events Count":     {"number": {"format": "number"}},
            "Full Section":     {"rich_text": {}},
            "Event URLs":       {"rich_text": {}},
            "Manually Edited":  {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_FREE_EVENTS_DB_ID}",
            headers=HEADERS,
            json={"properties": free_events_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Free Events database schema created")
        else:
            print(f"✗ Free Events schema error: {r.text[:300]}")

    # Reader Poll database properties
    if NOTION_POLLS_DB_ID:
        poll_properties = {
            "Name":              {"title": {}},
            "Newsletter":        {"select": {"options": [
                {"name": "East_Cobb_Connect", "color": "purple"},
                {"name": "Perimeter_Post",    "color": "pink"}
            ]}},
            "Date Generated":    {"date": {}},
            "Status":            {"select": {"options": [
                {"name": "approved",       "color": "green"},
                {"name": "approved - old", "color": "gray"}
            ]}},
            "Framing":           {"rich_text": {}},
            "Question":          {"rich_text": {}},
            "Options":           {"rich_text": {}},
            "Target Businesses": {"rich_text": {}},
            "Ad Intel Mapping":  {"rich_text": {}},
            "Manually Edited":   {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_POLLS_DB_ID}",
            headers=HEADERS,
            json={"properties": poll_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Reader Poll database schema created")
        else:
            print(f"✗ Reader Poll schema error: {r.text[:300]}")

    # Weekend Planner database properties
    if NOTION_WEEKEND_PLANNER_DB_ID:
        weekend_planner_properties = {
            "Name":             {"title": {}},
            "Newsletter":       {"select": {"options": [
                {"name": "East_Cobb_Connect",     "color": "purple"},
                {"name": "Perimeter_Post",        "color": "pink"},
                {"name": "Lewisville_Lake_Lookout", "color": "blue"},
            ]}},
            "Audience":         {"select": {"options": [
                {"name": "Family", "color": "green"},
                {"name": "Adult",  "color": "orange"},
            ]}},
            "Day":              {"select": {"options": [
                {"name": "Friday",   "color": "yellow"},
                {"name": "Saturday", "color": "red"},
                {"name": "Sunday",   "color": "blue"},
            ]}},
            "Date":             {"date": {}},
            "Emoji":            {"rich_text": {}},
            "Event Name":       {"rich_text": {}},
            "Venue":            {"rich_text": {}},
            "Address":          {"rich_text": {}},
            "Time":             {"rich_text": {}},
            "Price":            {"rich_text": {}},
            "Source URL":       {"url": {}},
            "Image URL":        {"url": {}},
            "Description":      {"rich_text": {}},
            "Status":           {"select": {"options": [
                {"name": "pending",        "color": "yellow"},
                {"name": "approved",       "color": "green"},
                {"name": "rejected",       "color": "red"},
                {"name": "approved - old", "color": "gray"},
            ]}},
            "Date Generated":   {"date": {}},
            "Scoring Notes":    {"rich_text": {}},
            "Manually Edited":  {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_WEEKEND_PLANNER_DB_ID}",
            headers=HEADERS,
            json={"properties": weekend_planner_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Weekend Planner database schema created")
        else:
            print(f"✗ Weekend Planner schema error: {r.text[:300]}")

    # Weekend Events database properties (scraped from per-newsletter sources)
    if NOTION_WEEKEND_EVENTS_DB_ID:
        weekend_events_properties = {
            "Name":             {"title": {}},
            "Newsletter":       {"select": {"options": [
                {"name": "East_Cobb_Connect"},
                {"name": "Perimeter_Post"},
                {"name": "Lewisville_Lake_Lookout"},
                # ECC_PP — events that apply to BOTH East Cobb Connect
                # AND Perimeter Post (Sandy Springs sits between the two
                # coverage areas). Featured Event ORs this with the
                # primary newsletter tag at query time.
                {"name": "ECC_PP"},
            ]}},
            "Event Name":       {"rich_text": {}},
            "Description":      {"rich_text": {}},
            "Date":             {"date": {}},
            # `Dates` is the human-readable multi-occurrence display
            # ("May 22nd, 29th, June 5th") for recurring events that the
            # scraper aggregated under one title. `Date` stays as the
            # earliest in-window occurrence so date filtering still works.
            "Dates":            {"rich_text": {}},
            "Time":             {"rich_text": {}},
            "Location":         {"rich_text": {}},
            "Address":          {"rich_text": {}},
            "Source URL":       {"url": {}},
            "Image URL":        {"url": {}},
            "Status":           {"select": {"options": [
                {"name": "pending"},
                {"name": "approved"},
                {"name": "rejected"},
                {"name": "archived"},
                # System-set when an event is chosen by a downstream picker.
                # 'featured' = Featured Event picked it; 'wp_used' = Weekend
                # Planner picked it. Both pickers exclude rows in these
                # statuses so events don't get double-used in the same issue.
                {"name": "featured"},
                {"name": "wp_used"},
            ]}},
            "Date Generated":   {"date": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_WEEKEND_EVENTS_DB_ID}",
            headers=HEADERS,
            json={"properties": weekend_events_properties},
            timeout=30,
        )
        if r.ok:
            print("✓ Weekend Events database schema created")
        else:
            print(f"✗ Weekend Events schema error: {r.text[:300]}")

    # Business Brief database properties
    if NOTION_BUSINESS_BRIEF_DB_ID:
        business_brief_properties = {
            "Name":             {"title": {}},
            "Newsletter":       {"select": {"options": [
                {"name": "East_Cobb_Connect",      "color": "purple"},
                {"name": "Perimeter_Post",         "color": "pink"},
                {"name": "Lewisville_Lake_Lookout", "color": "blue"},
            ]}},
            "Business Name":    {"rich_text": {}},
            "City":             {"rich_text": {}},
            "Outside Coverage": {"checkbox": {}},
            "Blurb":            {"rich_text": {}},
            "Price Level":      {"select": {"options": [
                {"name": "$",    "color": "green"},
                {"name": "$$",   "color": "yellow"},
                {"name": "$$$",  "color": "orange"},
                {"name": "$$$$", "color": "red"},
            ]}},
            "Hours":            {"rich_text": {}},
            "Address":          {"rich_text": {}},
            "Source URL":       {"url": {}},
            "Source Domain":    {"rich_text": {}},
            "Date Generated":   {"date": {}},
            "Status":           {"select": {"options": [
                {"name": "pending",        "color": "yellow"},
                {"name": "approved",       "color": "green"},
                {"name": "rejected",       "color": "red"},
                {"name": "approved - old", "color": "gray"},
            ]}},
            "Relevance Score":  {"number": {"format": "number"}},
            "Scoring Notes":    {"rich_text": {}},
            "Default Winner":   {"checkbox": {}},
            "Manually Edited":  {"checkbox": {}},
        }
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{NOTION_BUSINESS_BRIEF_DB_ID}",
            headers=HEADERS,
            json={"properties": business_brief_properties},
            timeout=30
        )
        if r.ok:
            print("✓ Business Brief database schema created")
        else:
            print(f"✗ Business Brief schema error: {r.text[:300]}")

# ---------------------------------------------------------------------------
# PETS HELPERS
# ---------------------------------------------------------------------------
def get_approved_pet_urls() -> set:
    """Get source URLs of approved and previously approved pets (for exclusion from candidates)."""
    urls = set()
    # Pull all pets then filter in Python — avoids status-vs-select filter mismatches
    try:
        pages = query_database(NOTION_PETS_DB_ID)
    except Exception as e:
        print(f"  Warning: could not load approved pet URLs: {e}")
        return urls

    keep_statuses = {"approved", "approved - old"}
    for page in pages:
        status = (page["properties"].get("Status", {}).get("select") or {}).get("name", "")
        if status not in keep_statuses:
            continue
        url = page["properties"].get("Source URL", {}).get("url", "")
        if url:
            # Normalize: strip trailing slash and /details suffix for matching
            u = url.strip().rstrip("/")
            if u.endswith("/details"):
                u = u[:-len("/details")]
            urls.add(u)
    print(f"Loaded {len(urls)} previously approved pet URLs to exclude")
    return urls
    
def save_pets_to_notion(results: list, newsletter_name: str) -> None:
    print(f"Saving {len(results)} pets to Notion...")
    existing_urls = get_existing_pet_urls(newsletter_name)
    print(f"  Found {len(existing_urls)} existing entries to skip")

    # Flip previous 'approved' rows for this newsletter to 'approved - old'
    # so the assembler picks this week's batch instead of last week's
    # manual approval. Matches the real-estate save pattern.
    try:
        existing_pages = query_database(NOTION_PETS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        flipped = 0
        for page in existing_pages:
            status = (page["properties"].get("Status", {}).get("select") or {}).get("name", "")
            if status == "approved":
                update_page(page["id"], {"Status": {"select": {"name": "approved - old"}}})
                flipped += 1
        if flipped:
            print(f"  Flipped {flipped} previous approved pet(s) → 'approved - old' for {newsletter_name}")
    except Exception as e:
        print(f"  Warning: could not flip old approved pets: {e}")

    saved = 0
    for data in results:
        source_url = data.get("source_url") or data.get("listing_url", "")
        if source_url and source_url in existing_urls:
            print(f"  ✗ Skipping duplicate: {data.get('pet_name')}")
            continue
        properties = {
            "Name":               {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {data.get('pet_name', '')}"}}]},
            "Source URL":         {"url": data.get("source_url") or None},
            "Listing URL":        {"url": data.get("listing_url") or None},
            "Shelter":            {"rich_text": [{"text": {"content": safe_str(data.get("shelter_name"))}}]},
            "Blurb":              {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
            "Shelter Address":    {"rich_text": [{"text": {"content": safe_str(data.get("shelter_address"))}}]},
            "Shelter Phone":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_phone"))}}]},
            "Shelter Email":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_email"))}}]},
            "Shelter Hours":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_hours"))}}]},
            "Photo URL":          {"url": data.get("photo_url") or None},
            "GIF URL":            {"url": data.get("gif_url") or None},
            "Date Generated":     {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":             {"select": {"name": "pending"}},
            "Section":            {"select": {"name": "pet_blurb"}},
            "Newsletter":         {"select": {"name": newsletter_name}},
            "Total Score":        {"number": int(data.get("total_score", 0) or 0)},
            "Adoptability Score": {"number": int(data.get("adoptability_score", 0) or 0)},
            "Story Score":        {"number": int(data.get("story_score", 0) or 0)},
            "Shelter Time Score": {"number": int(data.get("shelter_time_score", 0) or 0)},
            "Scoring Notes":      {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
            "Default Winner":     {"checkbox": data.get("default_winner", "") == "yes"},
            "Cat Default":        {"checkbox": data.get("cat_default", "") == "yes"},
            "Dog Default":        {"checkbox": data.get("dog_default", "") == "yes"},
            "Animal Type":        {"select": {"name": data.get("animal_type", "cat")}},
        }
        create_page(NOTION_PETS_DB_ID, properties)
        print(f"  ✓ {data.get('pet_name')}")
        saved += 1
    print(f"Saved {saved} new pets to Notion")

def approve_pet_in_notion(source_url: str, newsletter_hint: str = "") -> None:
    """Set approved pet to approved, all others in same newsletter to rejected.

    `newsletter_hint` — scopes the lookup so a pet that appears under
    multiple newsletters (same RescueGroups source_url shared across rows)
    gets approved on the correct newsletter."""
    source_url = (source_url or "").strip()
    if not source_url:
        print("✗ No source_url provided — aborting approval to avoid updating all rows")
        return

    pages = query_database(NOTION_PETS_DB_ID)
    newsletter_hint = (newsletter_hint or "").strip()

    # Find the approved pet — when newsletter_hint is provided, only accept
    # a row matching that newsletter to prevent cross-newsletter approvals.
    approved_newsletter = None
    approved_page_id = None
    for page in pages:
        props    = page["properties"]
        page_url = props.get("Source URL", {}).get("url", "")
        if not (page_url and page_url == source_url):
            continue
        newsletter = props.get("Newsletter", {}).get("select", {})
        page_newsletter = newsletter.get("name", "") if newsletter else ""
        if newsletter_hint and page_newsletter != newsletter_hint:
            continue
        approved_newsletter = page_newsletter
        approved_page_id = page["id"]
        break

    if not approved_newsletter:
        scope = f" in newsletter '{newsletter_hint}'" if newsletter_hint else ""
        print(f"✗ No pet found with source_url '{source_url}'{scope} — aborting")
        return

    print(f"Approving for newsletter: {approved_newsletter}")

    for page in pages:
        page_id     = page["id"]
        props       = page["properties"]
        status      = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""

        if status_name != "pending":
            continue

        # Only touch pets from the same newsletter
        newsletter      = props.get("Newsletter", {}).get("select", {})
        newsletter_name = newsletter.get("name", "") if newsletter else ""
        if newsletter_name != approved_newsletter:
            continue

        # Match by unique Notion page id to prevent edge cases where URLs might duplicate
        new_status = "approved" if page_id == approved_page_id else "rejected"
        update_page(page_id, {"Status": {"select": {"name": new_status}}})
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        print(f"{new_status}: {name}")
        
def get_existing_pet_urls(newsletter_name: str) -> set:
    """Return source URLs of ALL non-rejected pet rows for this newsletter.

    Used at save time to prevent duplicates. Includes `pending` rows from
    previous runs — without that, the same shelter pet (e.g., a long-stay
    dog that appears every week in RescueGroups) gets saved as a fresh
    `pending` row every week, accumulating duplicates indefinitely.

    Only `rejected` rows are excluded — those were explicitly turned down
    in the review app and shouldn't block future consideration if the
    candidate scoring/blurb changes."""
    import requests as _r
    try:
        pages = query_database(NOTION_PETS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except _r.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            print(f"  ⓘ Filter failed for '{newsletter_name}' (likely select option not in schema yet) — falling back to client-side filter")
            pages = query_database(NOTION_PETS_DB_ID)
            pages = [p for p in pages
                     if (p["properties"].get("Newsletter", {}).get("select") or {}).get("name") == newsletter_name]
        else:
            raise
    urls = set()
    for page in pages:
        props  = page["properties"]
        status = (props.get("Status", {}).get("select") or {}).get("name", "")
        if status == "rejected":
            continue
        url = props.get("Source URL", {}).get("url", "")
        if url:
            urls.add(url)
    return urls

def redo_pet_selection(newsletter_name: str) -> None:
    """Reset all approved/rejected pets for a newsletter back to pending."""
    pages = query_database(NOTION_PETS_DB_ID)
    count = 0
    for page in pages:
        page_id    = page["id"]
        props      = page["properties"]
        
        # Only touch pages for this newsletter
        newsletter = props.get("Newsletter", {}).get("select", {})
        if not newsletter or newsletter.get("name") != newsletter_name:
            continue
        
        status     = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""
        
        if status_name in ("approved", "rejected"):
            update_page(page_id, {"Status": {"select": {"name": "pending"}}})
            name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
            print(f"  Reset to pending: {name}")
            count += 1
    print(f"Reset {count} pets to pending for {newsletter_name}")

def cleanup_pets_notion(approved_old_weeks: int = 8) -> None:
    """Weekly cleanup of pets database.

    Rules:
      - 'approved' rows (this week's featured pet) → flipped to 'approved - old'.
        This advances the cycle so next week we don't re-feature the same pet.
      - 'approved - old' rows newer than `approved_old_weeks` weeks → kept (exclusion history).
      - 'approved - old' rows older than the cutoff → archived.
      - 'pending' and 'rejected' rows → archived (stale candidates).
    """
    pages = query_database(NOTION_PETS_DB_ID)
    cutoff = (datetime.today() - timedelta(weeks=approved_old_weeks)).strftime("%Y-%m-%d")
    print(f"  Cutoff for 'approved - old' archival: {cutoff} ({approved_old_weeks} weeks ago)")
    print(f"  Scanning {len(pages)} rows…")
    archived = 0
    flipped = 0
    kept_old = 0
    for page in pages:
        page_id = page["id"]
        props = page["properties"]
        status = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""
        date_str = (props.get("Date Generated", {}).get("date") or {}).get("start", "") or ""
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")

        if status_name == "approved":
            # Flip current week's pet to 'approved - old' so it stays in the exclusion list
            # but the slot is freed for next week.
            update_page(page_id, {"Status": {"select": {"name": "approved - old"}}})
            print(f"  🔄 Flipped 'approved' → 'approved - old': {name}")
            flipped += 1
            continue
        if status_name == "approved - old":
            if not date_str or date_str >= cutoff:
                kept_old += 1
                print(f"  🔒 Keeping 'approved - old': {name} (date: {date_str})")
                continue  # within window — keep for exclusion

        archive_page(page_id)
        print(f"  Archived: {name} (status: '{status_name}', date: {date_str})")
        archived += 1
    print(f"\nFlipped  {flipped} 'approved' → 'approved - old'")
    print(f"Kept     {kept_old} 'approved - old' rows within {approved_old_weeks}-week window")
    print(f"Archived {archived} stale pet entries (pending / rejected / >{approved_old_weeks}w old)")

# ---------------------------------------------------------------------------
# RESTAURANTS HELPERS
# ---------------------------------------------------------------------------
def get_featured_place_ids(newsletter_name: str) -> set:
    """Get ALL place IDs in Notion for this newsletter to prevent duplicates."""
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return set()
    place_ids = set()
    for page in pages:
        pid = page["properties"].get("Place ID", {}).get("rich_text", [{}])
        if pid:
            place_ids.add(pid[0].get("text", {}).get("content", ""))
    print(f"Loaded {len(place_ids)} existing restaurants to exclude")
    return place_ids

def get_existing_place_ids(newsletter_name: str) -> set:
    """Get ALL place IDs in Notion for this newsletter to avoid duplicates."""
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        place_ids = set()
        for page in pages:
            pid = page["properties"].get("Place ID", {}).get("rich_text", [])
            if pid:
                place_ids.add(pid[0].get("text", {}).get("content", ""))
        return place_ids
    except Exception as e:
        print(f"  Warning: could not load existing place IDs: {e}")
        return set()

def save_restaurants_to_notion(results: list, newsletter_name: str) -> None:
    print(f"Saving {len(results)} restaurants to Notion...")
    existing_ids = get_existing_place_ids(newsletter_name)
    print(f"  Found {len(existing_ids)} existing entries to skip")

    saved = 0
    for data in results:
        place_id = data.get("place_id", "")
        if place_id and place_id in existing_ids:
            print(f"  ✗ Skipping duplicate: {data.get('restaurant_name')}")
            continue

        properties = {
            "Name": {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {data.get('restaurant_name', '')}"}}]},
            "Place ID":               {"rich_text": [{"text": {"content": safe_str(data.get("place_id"))}}]},
            "Cuisine":                {"select": {"name": data.get("cuisine_type", "Restaurant")[:100]}},
            "Blurb":                  {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
            "Address":                {"rich_text": [{"text": {"content": safe_str(data.get("address"))}}]},
            "Phone":                  {"rich_text": [{"text": {"content": safe_str(data.get("phone"))}}]},
            "Hours":                  {"rich_text": [{"text": {"content": safe_str(data.get("hours"))[:2000]}}]},
            "Website":                {"url": data.get("website_url") or None},
            "Google Maps URL":        {"url": data.get("google_maps_url") or None},
            "Photo URL":              {"url": data.get("photo_url") or None},
            "GIF URL":                {"url": data.get("gif_url") or None},
            "Rating":                 {"number": float(data.get("rating", 0) or 0)},
            "Review Count":           {"number": int(data.get("review_count", 0) or 0)},
            "Price Level":            {"select": {"name": data.get("price_level", "")[:100] if data.get("price_level") else "Unknown"}},
            "Date Generated":         {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":                 {"select": {"name": "pending"}},
            "Section":                {"select": {"name": "restaurant_blurb"}},
            "Newsletter":             {"select": {"name": newsletter_name}},
            "Total Score":            {"number": int(data.get("total_score", 0) or 0)},
            "Appeal Score":           {"number": int(data.get("appeal_score", 0) or 0)},
            "Uniqueness Score":       {"number": int(data.get("uniqueness_score", 0) or 0)},
            "Neighborhood Fit Score": {"number": int(data.get("neighborhood_fit_score", 0) or 0)},
            "Festive Score":          {"number": int(data.get("festive_score", 0) or 0)},
            "Scoring Notes":          {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
            "Default Winner":         {"checkbox": data.get("default_winner", "") == "yes"},
        }
        create_page(NOTION_RESTAURANTS_DB_ID, properties)
        print(f"  ✓ {data.get('restaurant_name')}")
        saved += 1
    print(f"Saved {saved} new restaurants to Notion")
    
def approve_restaurant_in_notion(place_id: str, newsletter_hint: str = "") -> None:
    """Set selected restaurant to Tier 1 Winner, others in same newsletter to Tier 2 Winner.

    `newsletter_hint` — when provided, scopes the search to that newsletter
    so chain restaurants (which exist as separate rows under each newsletter
    with the same place_id) get approved on the right newsletter instead of
    whichever row the DB query returned first."""
    place_id = (place_id or "").strip()
    if not place_id:
        print("✗ No place_id provided — aborting approval to avoid updating all rows")
        return

    pages = query_database(NOTION_RESTAURANTS_DB_ID)
    newsletter_hint = (newsletter_hint or "").strip()

    # First find the selected restaurant to get its newsletter.
    # When `newsletter_hint` is provided, ONLY accept a row matching that
    # newsletter — prevents cross-newsletter approvals when the same
    # place_id exists in multiple newsletters' rows.
    approved_newsletter = None
    approved_page_id = None
    for page in pages:
        props     = page["properties"]
        pid_prop  = props.get("Place ID", {}).get("rich_text", [])
        page_place_id = pid_prop[0].get("text", {}).get("content", "") if pid_prop else ""
        if not (page_place_id and page_place_id == place_id):
            continue
        newsletter = props.get("Newsletter", {}).get("select", {})
        page_newsletter = newsletter.get("name", "") if newsletter else ""
        if newsletter_hint and page_newsletter != newsletter_hint:
            continue
        approved_newsletter = page_newsletter
        approved_page_id = page["id"]
        break

    if not approved_newsletter:
        scope = f" in newsletter '{newsletter_hint}'" if newsletter_hint else ""
        print(f"✗ No restaurant found with place_id '{place_id}'{scope} — aborting")
        return

    print(f"Selecting Tier 1 for newsletter: {approved_newsletter}")

    for page in pages:
        page_id    = page["id"]
        props      = page["properties"]
        status     = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""

        if status_name != "pending":
            continue

        newsletter  = props.get("Newsletter", {}).get("select", {})
        newsletter_name = newsletter.get("name", "") if newsletter else ""
        if newsletter_name != approved_newsletter:
            continue

        # Match by unique Notion page id (place_ids can duplicate if Claude hallucinates)
        new_status = "Tier 1 Winner" if page_id == approved_page_id else "Tier 2 Winner"
        update_page(page_id, {"Status": {"select": {"name": new_status}}})
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        print(f"{new_status}: {name}")

def redo_restaurant_selection(newsletter_name: str) -> None:
    """Reset all Tier 1/Tier 2 restaurants for a newsletter back to pending."""
    pages = query_database(NOTION_RESTAURANTS_DB_ID)
    count = 0
    for page in pages:
        page_id    = page["id"]
        props      = page["properties"]

        # Only touch pages for this newsletter
        newsletter = props.get("Newsletter", {}).get("select", {})
        if not newsletter or newsletter.get("name") != newsletter_name:
            continue

        status     = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""

        if status_name in ("Tier 1 Winner", "Tier 2 Winner"):
            update_page(page_id, {"Status": {"select": {"name": "pending"}}})
            name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
            print(f"  Reset to pending: {name}")
            count += 1
    print(f"Reset {count} restaurants to pending for {newsletter_name}")

def cleanup_old_restaurants_notion(approved_old_weeks: int = 8) -> None:
    """Weekly cleanup of restaurants database.

    Lifecycle:
      - 'Tier 1 Winner' / 'Tier 2 Winner' (this week's picks) → flipped to
        'approved - old' (frees the slots; row stays in exclusion list).
      - 'approved - old' newer than `approved_old_weeks` weeks → kept (exclusion).
      - 'approved - old' older than the cutoff → archived.
      - 'pending' / 'rejected' / blank → archived (stale candidates).
    """
    pages = query_database(NOTION_RESTAURANTS_DB_ID)
    cutoff = (datetime.today() - timedelta(weeks=approved_old_weeks)).strftime("%Y-%m-%d")
    print(f"  Cutoff for 'approved - old' archival: {cutoff} ({approved_old_weeks} weeks ago)")
    print(f"  Scanning {len(pages)} restaurant rows…")
    flipped = archived = kept_old = 0
    for page in pages:
        page_id = page["id"]
        props = page["properties"]
        status_obj = props.get("Status", {}).get("select") or {}
        status_name = status_obj.get("name", "") if isinstance(status_obj, dict) else ""
        date_str = (props.get("Date Generated", {}).get("date") or {}).get("start", "") or ""
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")

        if status_name in ("Tier 1 Winner", "Tier 2 Winner"):
            update_page(page_id, {"Status": {"select": {"name": "approved - old"}}})
            print(f"  🔄 Flipped '{status_name}' → 'approved - old': {name}")
            flipped += 1
            continue

        if status_name == "approved - old":
            if not date_str or date_str >= cutoff:
                kept_old += 1
                print(f"  🔒 Keeping 'approved - old': {name} (date: {date_str})")
                continue

        archive_page(page_id)
        print(f"  Archived: {name} (status: '{status_name}', date: {date_str})")
        archived += 1

    print(f"\nFlipped  {flipped} winners → 'approved - old'")
    print(f"Kept     {kept_old} 'approved - old' rows within {approved_old_weeks}-week window")
    print(f"Archived {archived} stale restaurant entries (pending / rejected / >{approved_old_weeks}w old)")

# ---------------------------------------------------------------------------
# LOCAL LOWDOWN HELPERS
# ---------------------------------------------------------------------------
_lowdown_schema_setup = False

def _ensure_lowdown_schema():
    """Create properties on the Local Lowdown database if needed (runs once)."""
    global _lowdown_schema_setup
    if _lowdown_schema_setup:
        return
    props = {
        "Name":            {"title": {}},
        "Newsletter":      {"select": {"options": [
            {"name": "East_Cobb_Connect", "color": "purple"},
            {"name": "Perimeter_Post",    "color": "pink"}
        ]}},
        "Date Generated":  {"date": {}},
        "Status":          {"select": {"options": [
            {"name": "pending",  "color": "yellow"},
            {"name": "approved", "color": "green"}
        ]}},
        "Section Header":  {"rich_text": {}},
        "Stories Count":   {"number": {"format": "number"}},
        "Full Section":    {"rich_text": {}},
        "Manually Edited": {"checkbox": {}},
    }
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_LOWDOWN_DB_ID}",
        headers=HEADERS,
        json={"properties": props},
        timeout=30,
    )
    if r.ok:
        print("  ✓ Local Lowdown database schema ready")
    else:
        print(f"  ✗ Schema setup error: {r.text[:300]}")
    _lowdown_schema_setup = True


def save_lowdown_to_notion(result: dict, newsletter_name: str) -> None:
    """Save the Local Lowdown section to Notion. Replaces any existing entry for this newsletter."""
    if not NOTION_LOWDOWN_DB_ID:
        print("  No NOTION_LOWDOWN_DB_ID set, skipping Notion save")
        return

    _ensure_lowdown_schema()

    # Check for manually edited rows — preserve them and skip saving new content
    try:
        existing = query_database(NOTION_LOWDOWN_DB_ID, filters={
            "property": "Newsletter",
            "select": {"equals": newsletter_name}
        })
        # Only block on manual edits to a row that's still 'current'
        # (status = approved/pending). Archived ('approved - old'/'rejected')
        # AND blank-status rows are treated as not-current — blank typically
        # means a legacy row pre-status-tracking, which shouldn't permanently
        # block the section.
        def _row_is_current(p):
            st = (p["properties"].get("Status", {}).get("select") or {}).get("name", "")
            return st not in ("", "approved - old", "rejected")
        has_manual_edit = any(
            p["properties"].get("Manually Edited", {}).get("checkbox", False)
            and _row_is_current(p)
            for p in existing
        )
        if has_manual_edit:
            print(f"  🔒 Manually edited Local Lowdown exists for {newsletter_name} — preserving, skipping save")
            return

        for page in existing:
            archive_page(page["id"])
        if existing:
            print(f"  Archived {len(existing)} old Local Lowdown entries for {newsletter_name}")
    except Exception:
        pass

    stories = result.get("stories", [])
    section_header = result.get("section_header", "")

    # Build full section markdown for easy copy-paste
    section_text = ""
    for story in stories:
        emoji = story.get("emoji", "")
        headline = story.get("headline", "")
        body = story.get("body", "").replace("\\n\\n", "\n\n").replace("\\n", "\n")
        sources = story.get("source_urls", [])
        source_links = " | ".join(f"[{s['label']}]({s['url']})" for s in sources)

        section_text += f"### {emoji} {headline}\n\n"
        section_text += f"{body}\n\n"
        if source_links:
            section_text += f"More: {source_links}\n\n"

    # Notion rich_text has a 2000 char limit per text block — split into chunks
    CHUNK_SIZE = 1900  # under 2000 to account for multi-byte characters
    chunks = []
    for i in range(0, len(section_text), CHUNK_SIZE):
        chunks.append({"text": {"content": section_text[i:i + CHUNK_SIZE]}})

    properties = {
        "Name":           {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - Local Lowdown - {datetime.today().strftime('%Y-%m-%d')}"}}]},
        "Newsletter":     {"select": {"name": newsletter_name}},
        "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
        "Status":         {"select": {"name": "approved"}},
        "Section Header": {"rich_text": [{"text": {"content": safe_str(section_header)}}]},
        "Stories Count":  {"number": len(stories)},
        "Full Section":   {"rich_text": chunks},
        "Manually Edited": {"checkbox": False},
    }

    create_page(NOTION_LOWDOWN_DB_ID, properties)
    print(f"  ✓ Saved Local Lowdown to Notion ({len(stories)} stories)")


# ---------------------------------------------------------------------------
# FEATURED EVENT HELPERS
def approve_event_in_notion(source_url: str, newsletter_hint: str = "") -> None:
    """Set selected event to approved, others in same newsletter to rejected.

    `newsletter_hint` — scopes the lookup so an event that appears under
    multiple newsletters (same source URL shared across rows) gets
    approved on the correct newsletter."""
    source_url = (source_url or "").strip()
    if not source_url:
        print("✗ No source_url provided — aborting approval to avoid updating all rows")
        return

    pages = query_database(NOTION_EVENTS_DB_ID)
    newsletter_hint = (newsletter_hint or "").strip()
    approved_newsletter = None
    approved_page_id = None
    for page in pages:
        props = page["properties"]
        page_url = props.get("Source URL", {}).get("url", "")
        if not (page_url and page_url == source_url):
            continue
        newsletter = props.get("Newsletter", {}).get("select", {})
        page_newsletter = newsletter.get("name", "") if newsletter else ""
        if newsletter_hint and page_newsletter != newsletter_hint:
            continue
        approved_newsletter = page_newsletter
        approved_page_id = page["id"]
        break

    if not approved_newsletter:
        scope = f" in newsletter '{newsletter_hint}'" if newsletter_hint else ""
        print(f"✗ No event found with source_url '{source_url}'{scope} — aborting")
        return

    print(f"Approving for newsletter: {approved_newsletter}")

    for page in pages:
        page_id = page["id"]
        props = page["properties"]
        status = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""

        if status_name != "pending":
            continue

        newsletter = props.get("Newsletter", {}).get("select", {})
        newsletter_name = newsletter.get("name", "") if newsletter else ""
        if newsletter_name != approved_newsletter:
            continue

        new_status = "approved" if page_id == approved_page_id else "rejected"
        update_page(page_id, {"Status": {"select": {"name": new_status}}})
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        print(f"{new_status}: {name}")


def redo_event_selection(newsletter_name: str) -> None:
    """Reset all approved/rejected featured events for a newsletter back to pending."""
    if not NOTION_EVENTS_DB_ID:
        print("  NOTION_EVENTS_DB_ID not set — skipping")
        return
    pages = query_database(NOTION_EVENTS_DB_ID)
    count = 0
    for page in pages:
        page_id = page["id"]
        props = page["properties"]

        newsletter = props.get("Newsletter", {}).get("select", {})
        if not newsletter or newsletter.get("name") != newsletter_name:
            continue

        status = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""

        if status_name in ("approved", "rejected"):
            update_page(page_id, {"Status": {"select": {"name": "pending"}}})
            name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
            print(f"  Reset to pending: {name}")
            count += 1
    print(f"Reset {count} events to pending for {newsletter_name}")


def get_existing_event_urls(newsletter_name: str) -> set:
    """Get source URLs of existing events for this newsletter to avoid duplicates."""
    if not NOTION_EVENTS_DB_ID:
        return set()
    try:
        pages = query_database(NOTION_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        urls = set()
        for page in pages:
            url = page["properties"].get("Source URL", {}).get("url", "")
            if url:
                urls.add(url)
        return urls
    except Exception:
        return set()


def save_events_to_notion(results: list, newsletter_name: str) -> None:
    """Save featured event candidates to Notion."""
    if not NOTION_EVENTS_DB_ID:
        print("  No NOTION_EVENTS_DB_ID set, skipping Notion save")
        return

    print(f"  Saving {len(results)} events to Notion...")
    existing_urls = get_existing_event_urls(newsletter_name)
    print(f"  Found {len(existing_urls)} existing entries to skip")

    saved = 0
    for data in results:
        source_url = data.get("source_url", "")
        if source_url and source_url in existing_urls:
            print(f"  ✗ Skipping duplicate: {data.get('event_name')}")
            continue

        properties = {
            "Name":                  {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {data.get('event_name', '')}"}}]},
            "Event Name":            {"rich_text": [{"text": {"content": safe_str(data.get("event_name"))}}]},
            "Date":                  {"rich_text": [{"text": {"content": safe_str(data.get("date"))}}]},
            "Time":                  {"rich_text": [{"text": {"content": safe_str(data.get("time"))}}]},
            "Venue":                 {"rich_text": [{"text": {"content": safe_str(data.get("venue"))}}]},
            "Price":                 {"rich_text": [{"text": {"content": safe_str(data.get("price"))}}]},
            "Blurb":                 {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
            "Source URL":            {"url": data.get("source_url") or None},
            "Ticket URL":           {"url": data.get("ticket_url") or None},
            "Image URL":             {"url": data.get("image_url") or None},
            "Image Candidates":      {"rich_text": [{"text": {"content": json.dumps(data.get("image_candidates") or [])[:2000]}}]},
            "Header Image URL":      {"url": data.get("header_image_url") or None},
            "GIF URL":               {"url": data.get("gif_url") or None},
            "Newsletter":            {"select": {"name": newsletter_name}},
            "Date Generated":        {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":                {"select": {"name": "pending"}},
            "Total Score":           {"number": int(data.get("total_score", 0) or 0)},
            "Demographic Fit Score": {"number": int(data.get("demographic_fit_score", 0) or 0)},
            "Uniqueness Score":      {"number": int(data.get("uniqueness_score", 0) or 0)},
            "Audience Match Score":  {"number": int(data.get("audience_match_score", 0) or 0)},
            "Scoring Notes":         {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
            "Default Winner":        {"checkbox": data.get("default_winner", "") == "yes"},
        }
        create_page(NOTION_EVENTS_DB_ID, properties)
        print(f"  ✓ {data.get('event_name')}")
        saved += 1
    print(f"  Saved {saved} new events to Notion")

    

# ---------------------------------------------------------------------------
# WELCOME INTRO HELPERS
# ---------------------------------------------------------------------------

def _ensure_intro_schema():
    """Create Welcome Intro database properties if they don't exist."""
    if not NOTION_INTRO_DB_ID:
        return
    intro_properties = {
        "Name":              {"title": {}},
        "Newsletter":        {"select": {"options": [
            {"name": "East_Cobb_Connect", "color": "purple"},
            {"name": "Perimeter_Post",    "color": "pink"}
        ]}},
        "Date Generated":    {"date": {}},
        "Status":            {"select": {"options": [
            {"name": "approved", "color": "green"},
        ]}},
        "Greeting":          {"rich_text": {}},
        "Blurb":             {"rich_text": {}},
        "Word Count":        {"number": {"format": "number"}},
        "Review Score":      {"number": {"format": "number"}},
        "Review Violations": {"rich_text": {}},
        "Manually Edited":   {"checkbox": {}},
    }
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_INTRO_DB_ID}",
        headers=HEADERS,
        json={"properties": intro_properties},
        timeout=30,
    )
    if not r.ok:
        print(f"  Warning: intro schema update failed: {r.text[:200]}")


def save_intro_to_notion(result: dict, newsletter_name: str) -> None:
    """Save the Welcome Intro blurb to Notion. Replaces any existing entry for this newsletter."""
    if not NOTION_INTRO_DB_ID:
        print("  No NOTION_INTRO_DB_ID set, skipping Notion save")
        return

    _ensure_intro_schema()

    # Check for manually edited rows — preserve them and skip saving new content
    try:
        existing = query_database(NOTION_INTRO_DB_ID, filters={
            "property": "Newsletter",
            "select": {"equals": newsletter_name}
        })
        # Only block on manual edits to a row that's still 'current'
        # (status = approved/pending). Archived ('approved - old'/'rejected')
        # AND blank-status rows are treated as not-current — blank typically
        # means a legacy row pre-status-tracking, which shouldn't permanently
        # block the section.
        def _row_is_current(p):
            st = (p["properties"].get("Status", {}).get("select") or {}).get("name", "")
            return st not in ("", "approved - old", "rejected")
        has_manual_edit = any(
            p["properties"].get("Manually Edited", {}).get("checkbox", False)
            and _row_is_current(p)
            for p in existing
        )
        if has_manual_edit:
            print(f"  🔒 Manually edited Welcome Intro exists for {newsletter_name} — preserving, skipping save")
            return

        for page in existing:
            archive_page(page["id"])
        if existing:
            print(f"  Archived {len(existing)} old Welcome Intro entries for {newsletter_name}")
    except Exception:
        pass

    blurb_text = result.get("blurb", "")

    # Notion rich_text has a 2000 char limit per text block — split into chunks
    CHUNK_SIZE = 1900
    chunks = []
    for i in range(0, len(blurb_text), CHUNK_SIZE):
        chunks.append({"text": {"content": blurb_text[i:i + CHUNK_SIZE]}})
    if not chunks:
        chunks = [{"text": {"content": ""}}]

    violations_text = result.get("review_violations", "")

    properties = {
        "Name":              {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - Welcome Intro - {datetime.today().strftime('%Y-%m-%d')}"}}]},
        "Newsletter":        {"select": {"name": newsletter_name}},
        "Date Generated":    {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
        "Status":            {"select": {"name": "approved"}},
        "Greeting":          {"rich_text": [{"text": {"content": safe_str(result.get("greeting", ""))}}]},
        "Blurb":             {"rich_text": chunks},
        "Word Count":        {"number": int(result.get("word_count", 0))},
        "Review Score":      {"number": int(result.get("review_score", 0))},
        "Review Violations": {"rich_text": [{"text": {"content": safe_str(violations_text)[:2000]}}]},
        "Manually Edited":   {"checkbox": False},
    }

    create_page(NOTION_INTRO_DB_ID, properties)
    print(f"  ✓ Saved Welcome Intro to Notion ({result.get('word_count', '?')} words, score {result.get('review_score', '?')}/10)")


# ---------------------------------------------------------------------------
# INSURANCE TIP HELPERS
# ---------------------------------------------------------------------------

_tips_schema_setup = False

def _ensure_tips_schema():
    """Create properties on the Insurance Tip database if needed (runs once per process)."""
    global _tips_schema_setup
    if _tips_schema_setup:
        return
    if not NOTION_TIPS_DB_ID:
        return
    props = {
        "Name":                 {"title": {}},
        "Tip Title":            {"rich_text": {}},
        "Topic":                {"rich_text": {}},
        "Category":             {"select": {"options": [
            {"name": "auto"},
            {"name": "home"},
            {"name": "flood"},
            {"name": "umbrella"},
            {"name": "life"},
            {"name": "seasonal"},
            {"name": "life_event"},
        ]}},
        "Blurb":                {"rich_text": {}},
        "Summary":              {"rich_text": {}},
        "Source URL":           {"url": {}},
        "Source Name":          {"rich_text": {}},
        "Newsletter":           {"select": {"options": [
            {"name": "East_Cobb_Connect"},
            {"name": "Perimeter_Post"},
            {"name": "Lewisville_Lake_Lookout"},
        ]}},
        "Date Generated":       {"date": {}},
        "Status":               {"select": {"options": [
            {"name": "pending",        "color": "yellow"},
            {"name": "approved",       "color": "green"},
            {"name": "rejected",       "color": "red"},
            {"name": "approved - old", "color": "gray"},
        ]}},
        "Total Score":          {"number": {"format": "number"}},
        "Relevance Score":      {"number": {"format": "number"}},
        "Actionability Score":  {"number": {"format": "number"}},
        "Timeliness Score":     {"number": {"format": "number"}},
        "Scoring Notes":        {"rich_text": {}},
        "Default Winner":       {"checkbox": {}},
        "Manually Edited":      {"checkbox": {}},
    }
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_TIPS_DB_ID}",
        headers=HEADERS,
        json={"properties": props},
        timeout=30,
    )
    if r.ok:
        print("  ✓ Insurance Tip database schema ready")
    else:
        print(f"  ✗ Insurance Tip schema setup error: {r.text[:300]}")
    _tips_schema_setup = True


def get_existing_tip_urls(newsletter_name: str) -> set:
    """Get source URLs of existing tips for this newsletter to avoid duplicates."""
    if not NOTION_TIPS_DB_ID:
        return set()
    try:
        pages = query_database(NOTION_TIPS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        urls = set()
        for page in pages:
            url = page["properties"].get("Source URL", {}).get("url", "")
            if url:
                urls.add(url)
        return urls
    except Exception:
        return set()


def get_existing_tip_subjects(newsletter_name: str, months_back: int = 6) -> list:
    """Return recent tip subjects for this newsletter so Claude can avoid repeats.

    Each item: {topic, tip_title, summary, date}. Summary falls back to a
    truncated blurb if the Summary column is empty (e.g., rows created before
    Summary was added). Date filter uses Date Generated; rows missing that
    field are included (they predate the filter, treat as recent-enough)."""
    if not NOTION_TIPS_DB_ID:
        return []
    cutoff = (datetime.today() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")
    try:
        pages = query_database(NOTION_TIPS_DB_ID, filters={
            "and": [
                {"property": "Newsletter",    "select": {"equals": newsletter_name}},
                {"property": "Date Generated", "date":   {"on_or_after": cutoff}},
            ]
        })
    except Exception:
        return []

    subjects = []
    for page in pages:
        props = page.get("properties", {})

        def _rt(field: str) -> str:
            items = props.get(field, {}).get("rich_text", [])
            return items[0].get("text", {}).get("content", "") if items else ""

        topic     = _rt("Topic")
        tip_title = _rt("Tip Title")
        summary   = _rt("Summary")
        blurb     = _rt("Blurb")
        date_prop = props.get("Date Generated", {}).get("date", {}) or {}
        date_str  = date_prop.get("start", "") if date_prop else ""

        if not summary and blurb:
            summary = blurb[:300]

        if topic or tip_title or summary:
            subjects.append({
                "topic":     topic,
                "tip_title": tip_title,
                "summary":   summary,
                "date":      date_str,
            })
    return subjects


def save_tips_to_notion(results: list, newsletter_name: str) -> None:
    """Save insurance tip candidates to Notion for this newsletter.
    Called once per newsletter with the same `results` — the tips are shared
    across both newsletters, but each newsletter gets its own Notion row."""
    if not NOTION_TIPS_DB_ID:
        print("  No NOTION_TIPS_DB_ID set, skipping Notion save")
        return

    _ensure_tips_schema()

    print(f"  Saving {len(results)} tips to Notion for {newsletter_name}...")
    existing_urls = get_existing_tip_urls(newsletter_name)
    print(f"  Found {len(existing_urls)} existing entries to skip")

    saved = 0
    for data in results:
        source_url = data.get("source_url", "")
        if source_url and source_url in existing_urls:
            print(f"  ✗ Skipping duplicate: {data.get('tip_title')}")
            continue

        category = safe_str(data.get("category"))
        properties = {
            "Name":                 {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {data.get('tip_title', '')}"}}]},
            "Tip Title":            {"rich_text": [{"text": {"content": safe_str(data.get("tip_title"))}}]},
            "Topic":                {"rich_text": [{"text": {"content": safe_str(data.get("topic"))}}]},
            "Blurb":                {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
            "Summary":              {"rich_text": [{"text": {"content": safe_str(data.get("summary"))[:500]}}]},
            "Source URL":           {"url": data.get("source_url") or None},
            "Source Name":          {"rich_text": [{"text": {"content": safe_str(data.get("source_name"))}}]},
            "Newsletter":           {"select": {"name": newsletter_name}},
            "Date Generated":       {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":               {"select": {"name": "pending"}},
            "Total Score":          {"number": int(data.get("total_score", 0) or 0)},
            "Relevance Score":      {"number": int(data.get("relevance_score", 0) or 0)},
            "Actionability Score":  {"number": int(data.get("actionability_score", 0) or 0)},
            "Timeliness Score":     {"number": int(data.get("timeliness_score", 0) or 0)},
            "Scoring Notes":        {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
            "Default Winner":       {"checkbox": data.get("default_winner", "") == "yes"},
            "Manually Edited":      {"checkbox": False},
        }
        if category:
            properties["Category"] = {"select": {"name": category}}

        create_page(NOTION_TIPS_DB_ID, properties)
        print(f"  ✓ {data.get('tip_title')}")
        saved += 1
    print(f"  Saved {saved} new tips to Notion for {newsletter_name}")


# ---------------------------------------------------------------------------
# FREE EVENTS HELPERS
# ---------------------------------------------------------------------------

def _ensure_free_events_schema():
    """Create Free Events database properties if they don't exist (idempotent)."""
    if not NOTION_FREE_EVENTS_DB_ID:
        return
    props = {
        "Name":             {"title": {}},
        "Newsletter":       {"select": {"options": [
            {"name": "East_Cobb_Connect", "color": "purple"},
            {"name": "Perimeter_Post",    "color": "pink"}
        ]}},
        "Date Generated":   {"date": {}},
        "Status":           {"select": {"options": [
            {"name": "approved",       "color": "green"},
            {"name": "approved - old", "color": "gray"}
        ]}},
        "Section Header":   {"rich_text": {}},
        "Events Count":     {"number": {"format": "number"}},
        "Full Section":     {"rich_text": {}},
        "Event URLs":       {"rich_text": {}},
        "Image URL":        {"url": {}},
        "Manually Edited":  {"checkbox": {}},
    }
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_FREE_EVENTS_DB_ID}",
        headers=HEADERS,
        json={"properties": props},
        timeout=30,
    )
    if not r.ok:
        print(f"  Warning: free events schema update failed: {r.text[:200]}")


def save_free_events_to_notion(result: dict, newsletter_name: str) -> None:
    """Save the Free Events section to Notion.
    Previous 'approved' rows are flipped to 'approved - old' (kept for exclusion, not archived).
    Manually edited rows are preserved as-is."""
    if not NOTION_FREE_EVENTS_DB_ID:
        print("  No NOTION_FREE_EVENTS_DB_ID set, skipping Notion save")
        return

    _ensure_free_events_schema()

    # Flip previous auto-generated "approved" rows to "approved - old" so they stay for exclusion
    try:
        existing = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        # Only block on manual edits to a row that's still 'current'
        # (status = approved/pending). Archived ('approved - old'/'rejected')
        # AND blank-status rows are treated as not-current — blank typically
        # means a legacy row pre-status-tracking, which shouldn't permanently
        # block the section.
        def _row_is_current(p):
            st = (p["properties"].get("Status", {}).get("select") or {}).get("name", "")
            return st not in ("", "approved - old", "rejected")
        has_manual_edit = any(
            p["properties"].get("Manually Edited", {}).get("checkbox", False)
            and _row_is_current(p)
            for p in existing
        )
        if has_manual_edit:
            print(f"  🔒 Manually edited Free Events exists for {newsletter_name} — preserving, skipping save")
            return

        flipped = 0
        for page in existing:
            status = (page["properties"].get("Status", {}).get("select") or {}).get("name", "")
            if status == "approved":
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page['id']}",
                    headers=HEADERS,
                    json={"properties": {"Status": {"select": {"name": "approved - old"}}}},
                    timeout=30,
                )
                flipped += 1
        if flipped:
            print(f"  Flipped {flipped} previous Free Events entries to 'approved - old' for {newsletter_name}")
    except Exception as e:
        print(f"  Warning: could not process existing Free Events: {e}")

    events = result.get("events", [])
    section_header = result.get("section_header", "")

    # Build full section markdown
    section_text = ""
    for ev in events:
        emoji         = ev.get("emoji", "")
        name          = ev.get("name", "")
        when          = ev.get("when", "")
        address       = ev.get("address", "")
        is_free       = ev.get("is_free", "Free")
        venue         = ev.get("venue", "")
        body_markdown = ev.get("body_markdown", "")
        # Backward compatibility: if Claude returns the old short blurb format
        # without body_markdown, fall back to the previous render path.
        legacy_blurb  = ev.get("blurb", "")
        url           = ev.get("source_url", "")
        source_label  = ev.get("source_label", "") or ev.get("source", "") or "Details"

        # Header line with emoji + name (rendered as heading_3 by assembler)
        section_text += f"### {emoji} {name}\n\n"

        # Metadata line: when | address | price (or fall back to venue / audience)
        metadata_parts = []
        if when:                       metadata_parts.append(when)
        if address:                    metadata_parts.append(address)
        elif venue:                    metadata_parts.append(venue)
        if is_free:                    metadata_parts.append(is_free)
        if metadata_parts:
            section_text += " | ".join(metadata_parts) + "\n\n"

        # Body — prefer the rich body_markdown, fall back to old single-line blurb
        if body_markdown:
            section_text += body_markdown.strip() + "\n\n"
        elif legacy_blurb:
            section_text += legacy_blurb.strip() + "\n\n"

        # More info link
        if url:
            section_text += f"**More info:** [{source_label}]({url})\n\n"

    # Chunk to respect Notion's 2000-char rich_text limit
    CHUNK_SIZE = 1900
    chunks = []
    for i in range(0, len(section_text), CHUNK_SIZE):
        chunks.append({"text": {"content": section_text[i:i + CHUNK_SIZE]}})
    if not chunks:
        chunks = [{"text": {"content": ""}}]

    # Collect all event URLs for easy exclusion on next run
    event_urls = [ev.get("source_url", "") for ev in events if ev.get("source_url")]
    urls_text = " | ".join(event_urls)[:2000]  # respect Notion rich_text limit

    # Image URL: take the first event's image (we currently only feature 1)
    image_url = next((ev.get("image_url") for ev in events if ev.get("image_url")), "") or None

    properties = {
        "Name":           {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - Free Events - {datetime.today().strftime('%Y-%m-%d')}"}}]},
        "Newsletter":     {"select": {"name": newsletter_name}},
        "Date Generated": {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
        "Status":         {"select": {"name": "approved"}},
        "Section Header": {"rich_text": [{"text": {"content": safe_str(section_header)}}]},
        "Events Count":   {"number": len(events)},
        "Full Section":   {"rich_text": chunks},
        "Event URLs":     {"rich_text": [{"text": {"content": urls_text}}]},
        "Image URL":      {"url": image_url},
        "Manually Edited": {"checkbox": False},
    }

    create_page(NOTION_FREE_EVENTS_DB_ID, properties)
    print(f"  ✓ Saved Free Events to Notion ({len(events)} events)")


def get_used_free_event_urls(newsletter_name: str) -> set:
    """Collect URLs of previously featured free events (approved + approved - old)
    for this newsletter. Used to exclude repeats on the next run."""
    urls = set()
    if not NOTION_FREE_EVENTS_DB_ID:
        return urls
    try:
        pages = query_database(NOTION_FREE_EVENTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return urls

    keep_statuses = {"approved", "approved - old"}
    for page in pages:
        status = (page["properties"].get("Status", {}).get("select") or {}).get("name", "")
        if status not in keep_statuses:
            continue
        urls_rt = page["properties"].get("Event URLs", {}).get("rich_text", [])
        blob = "".join(chunk.get("text", {}).get("content", "") for chunk in urls_rt)
        for u in blob.split("|"):
            u = u.strip().rstrip("/")
            if u:
                urls.add(u)
    print(f"  Loaded {len(urls)} previously featured free event URLs to exclude")
    return urls


# ---------------------------------------------------------------------------
# READER POLL HELPERS
# ---------------------------------------------------------------------------

def _ensure_polls_schema():
    """Create Polls database properties if they don't exist (idempotent)."""
    if not NOTION_POLLS_DB_ID:
        return
    props = {
        "Name":              {"title": {}},
        "Newsletter":        {"select": {"options": [
            {"name": "East_Cobb_Connect", "color": "purple"},
            {"name": "Perimeter_Post",    "color": "pink"}
        ]}},
        "Date Generated":    {"date": {}},
        "Status":            {"select": {"options": [
            {"name": "approved",       "color": "green"},
            {"name": "approved - old", "color": "gray"}
        ]}},
        "Framing":           {"rich_text": {}},
        "Question":          {"rich_text": {}},
        "Options":           {"rich_text": {}},
        "Target Businesses": {"rich_text": {}},
        "Ad Intel Mapping":  {"rich_text": {}},
        "Manually Edited":   {"checkbox": {}},
    }
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{NOTION_POLLS_DB_ID}",
        headers=HEADERS,
        json={"properties": props},
        timeout=30,
    )
    if not r.ok:
        print(f"  Warning: poll schema update failed: {r.text[:200]}")


def save_poll_to_notion(result: dict, newsletter_name: str) -> None:
    """Save the weekly reader poll to Notion. Previous 'approved' rows for this newsletter
    are flipped to 'approved - old' (kept for the 8-week exclusion lookback). Manually-edited
    rows are preserved as-is."""
    if not NOTION_POLLS_DB_ID:
        print("  No NOTION_POLLS_DB_ID set, skipping Notion save")
        return

    _ensure_polls_schema()

    # Flip existing approved rows to approved-old (keep for exclusion history)
    try:
        existing = query_database(NOTION_POLLS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        # Only block on manual edits to a row that's still 'current'
        # (status = approved/pending). Archived ('approved - old'/'rejected')
        # AND blank-status rows are treated as not-current — blank typically
        # means a legacy row pre-status-tracking, which shouldn't permanently
        # block the section.
        def _row_is_current(p):
            st = (p["properties"].get("Status", {}).get("select") or {}).get("name", "")
            return st not in ("", "approved - old", "rejected")
        has_manual_edit = any(
            p["properties"].get("Manually Edited", {}).get("checkbox", False)
            and _row_is_current(p)
            for p in existing
        )
        if has_manual_edit:
            print(f"  🔒 Manually edited Poll exists for {newsletter_name} — preserving, skipping save")
            return

        flipped = 0
        for page in existing:
            status = (page["properties"].get("Status", {}).get("select") or {}).get("name", "")
            if status == "approved":
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page['id']}",
                    headers=HEADERS,
                    json={"properties": {"Status": {"select": {"name": "approved - old"}}}},
                    timeout=30,
                )
                flipped += 1
        if flipped:
            print(f"  Flipped {flipped} previous Poll entries to 'approved - old' for {newsletter_name}")
    except Exception as e:
        print(f"  Warning: could not process existing Poll rows: {e}")

    framing = result.get("framing", "")
    question = result.get("question", "")
    options = result.get("options", []) or []

    # Render Options as a markdown bullet list (newline-separated, with bullets)
    options_text = "\n".join(f"- {opt.get('text', '').strip()}" for opt in options)

    # Collect all categories pipe-separated
    all_categories = []
    for opt in options:
        for c in (opt.get("categories") or []):
            c = c.strip().lower()
            if c and c not in all_categories:
                all_categories.append(c)
    categories_text = " | ".join(all_categories)[:2000]

    # Build the human-readable Ad Intel Mapping
    intel_lines = result.get("ad_intel_mapping") or [
        f"{opt.get('text', '?')} → {', '.join(opt.get('categories') or [])}"
        for opt in options
    ]
    intel_text = "\n".join(intel_lines)[:2000]

    properties = {
        "Name":              {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - Poll - {datetime.today().strftime('%Y-%m-%d')}"}}]},
        "Newsletter":        {"select": {"name": newsletter_name}},
        "Date Generated":    {"date": {"start": datetime.today().strftime("%Y-%m-%d")}},
        "Status":            {"select": {"name": "approved"}},
        "Framing":           {"rich_text": [{"text": {"content": safe_str(framing)[:2000]}}]},
        "Question":          {"rich_text": [{"text": {"content": safe_str(question)[:2000]}}]},
        "Options":           {"rich_text": [{"text": {"content": options_text[:2000]}}]},
        "Target Businesses": {"rich_text": [{"text": {"content": categories_text}}]},
        "Ad Intel Mapping":  {"rich_text": [{"text": {"content": intel_text}}]},
        "Manually Edited":   {"checkbox": False},
    }

    create_page(NOTION_POLLS_DB_ID, properties)
    print(f"  ✓ Saved Reader Poll to Notion ({len(options)} options, {len(all_categories)} categories)")


def get_used_poll_categories(newsletter_name: str, lookback_weeks: int = 8) -> set:
    """Return the set of target-business categories used by polls for this newsletter
    in the past `lookback_weeks` (across approved + approved - old rows)."""
    used = set()
    if not NOTION_POLLS_DB_ID:
        return used
    cutoff = (datetime.today() - timedelta(weeks=lookback_weeks)).strftime("%Y-%m-%d")
    try:
        pages = query_database(NOTION_POLLS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
    except Exception:
        return used

    keep_statuses = {"approved", "approved - old"}
    for page in pages:
        props = page["properties"]
        status = (props.get("Status", {}).get("select") or {}).get("name", "")
        if status not in keep_statuses:
            continue
        date_str = (props.get("Date Generated", {}).get("date") or {}).get("start", "")
        if date_str and date_str < cutoff:
            continue
        cat_rt = props.get("Target Businesses", {}).get("rich_text", [])
        blob = "".join(chunk.get("text", {}).get("content", "") for chunk in cat_rt)
        for c in blob.split("|"):
            c = c.strip().lower()
            if c:
                used.add(c)
    return used


# ---------------------------------------------------------------------------
# WEEKEND PLANNER HELPERS
# ---------------------------------------------------------------------------

def get_existing_weekend_event_urls(newsletter_name: str) -> set:
    """Get source URLs of existing weekend events for this newsletter to avoid duplicates."""
    if not NOTION_WEEKEND_PLANNER_DB_ID:
        return set()
    try:
        pages = query_database(NOTION_WEEKEND_PLANNER_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        urls = set()
        for page in pages:
            url = page["properties"].get("Source URL", {}).get("url", "")
            if url:
                urls.add(url)
        return urls
    except Exception:
        return set()


def _normalize_weekend_url(u: str) -> str:
    """Normalize a URL for cross-run dedup. Strips query strings, trailing
    slashes, case, and 'www.' so the same event saved with different URL
    variants matches as a duplicate.
    e.g. 'https://www.dreamhack.com/atlanta/tickets/?utm=x'
       → 'https://dreamhack.com/atlanta/tickets'"""
    if not u:
        return ""
    from urllib.parse import urlparse
    p = urlparse(u.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    path = (p.path or "/").rstrip("/").lower()
    return f"{p.scheme}://{host}{path}"


def save_weekend_events_to_notion(results: list, newsletter_name: str) -> None:
    """Save weekend event candidates to Notion. Each row is one event×day.
    Expected fields per item: audience, day, date, emoji, event_name, venue,
    address, time, price, source_url, description, scoring_notes.

    Dedup key is (normalized_url, audience, day) — a multi-day event
    legitimately gets one row per day, but the SAME (event, audience, day)
    combination from a previous run is treated as a duplicate."""
    if not NOTION_WEEKEND_PLANNER_DB_ID:
        print("  No NOTION_WEEKEND_PLANNER_DB_ID set, skipping Notion save")
        return

    print(f"  Saving {len(results)} weekend events to Notion for {newsletter_name}...")

    # Pull existing rows with full (url, audience, day) tuples for dedup.
    # Falls back to URL-only matching if any of those fields are missing.
    existing_keys: set[tuple[str, str, str]] = set()
    try:
        pages = query_database(NOTION_WEEKEND_PLANNER_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        for page in pages:
            props = page["properties"]
            u   = props.get("Source URL", {}).get("url", "") or ""
            aud = (props.get("Audience", {}).get("select") or {}).get("name", "")
            day = (props.get("Day", {}).get("select") or {}).get("name", "")
            existing_keys.add((_normalize_weekend_url(u), aud, day))
    except Exception:
        pass
    print(f"  Found {len(existing_keys)} existing (url, audience, day) tuples to skip")

    saved = 0
    for data in results:
        source_url = data.get("source_url", "")
        key = (
            _normalize_weekend_url(source_url),
            data.get("audience", ""),
            data.get("day", ""),
        )
        if source_url and key in existing_keys:
            print(f"  ✗ Skipping duplicate: {data.get('event_name')} ({key[1]}/{key[2]})")
            continue

        nl_display = newsletter_name.replace("_", " ")
        title = f"{nl_display} - {data.get('audience', '')} {data.get('day', '')} - {data.get('event_name', '')}"

        properties = {
            "Name":             {"title":     [{"text": {"content": title}}]},
            "Newsletter":       {"select":    {"name": newsletter_name}},
            "Audience":         {"select":    {"name": safe_str(data.get("audience"))}},
            "Day":              {"select":    {"name": safe_str(data.get("day"))}},
            "Emoji":            {"rich_text": [{"text": {"content": safe_str(data.get("emoji"))}}]},
            "Event Name":       {"rich_text": [{"text": {"content": safe_str(data.get("event_name"))}}]},
            "Venue":            {"rich_text": [{"text": {"content": safe_str(data.get("venue"))}}]},
            "Address":          {"rich_text": [{"text": {"content": safe_str(data.get("address"))}}]},
            "Time":             {"rich_text": [{"text": {"content": safe_str(data.get("time"))}}]},
            "Price":            {"rich_text": [{"text": {"content": safe_str(data.get("price"))}}]},
            "Source URL":       {"url":       data.get("source_url") or None},
            "Image URL":        {"url":       data.get("image_url") or None},
            "Description":      {"rich_text": [{"text": {"content": safe_str(data.get("description"))[:2000]}}]},
            "Status":           {"select":    {"name": "pending"}},
            "Date Generated":   {"date":      {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Scoring Notes":    {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
            "Manually Edited":  {"checkbox":  False},
        }
        # Only attach Date if we got a valid ISO date — Notion rejects empty start
        date_val = safe_str(data.get("date"))
        if date_val:
            properties["Date"] = {"date": {"start": date_val}}

        create_page(NOTION_WEEKEND_PLANNER_DB_ID, properties)
        # Add this row's tuple to the in-memory dedup set so a later item
        # in the same batch with the same (url, audience, day) gets skipped.
        existing_keys.add(key)
        saved += 1
    print(f"  Saved {saved} new weekend events to Notion for {newsletter_name}")


# ---------------------------------------------------------------------------
# BUSINESS BRIEF HELPERS
# ---------------------------------------------------------------------------

def get_existing_business_brief_urls(newsletter_name: str) -> set:
    """Get source URLs of existing business briefs for this newsletter to avoid duplicates."""
    if not NOTION_BUSINESS_BRIEF_DB_ID:
        return set()
    try:
        pages = query_database(NOTION_BUSINESS_BRIEF_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        urls = set()
        for page in pages:
            url = page["properties"].get("Source URL", {}).get("url", "")
            if url:
                urls.add(url)
        return urls
    except Exception:
        return set()


def save_business_briefs_to_notion(results: list, newsletter_name: str) -> None:
    """Save Business Brief candidates to Notion. Each row is one business.
    Expected fields per item: name, city, is_outside_coverage, blurb, price_level,
    hours, address, source_url, source, relevance_score, scoring_notes, default_winner."""
    if not NOTION_BUSINESS_BRIEF_DB_ID:
        print("  No NOTION_BUSINESS_BRIEF_DB_ID set, skipping Notion save")
        return

    print(f"  Saving {len(results)} business briefs to Notion for {newsletter_name}...")
    existing_urls = get_existing_business_brief_urls(newsletter_name)
    print(f"  Found {len(existing_urls)} existing entries to skip")

    saved = 0
    for data in results:
        source_url = data.get("source_url", "")
        if source_url and source_url in existing_urls:
            print(f"  ✗ Skipping duplicate: {data.get('name')}")
            continue

        nl_display = newsletter_name.replace("_", " ")
        title = f"{nl_display} - Business Brief - {data.get('name', '')}"

        properties = {
            "Name":             {"title":     [{"text": {"content": title}}]},
            "Newsletter":       {"select":    {"name": newsletter_name}},
            "Business Name":    {"rich_text": [{"text": {"content": safe_str(data.get("name"))}}]},
            "City":             {"rich_text": [{"text": {"content": safe_str(data.get("city"))}}]},
            "Outside Coverage": {"checkbox":  bool(data.get("is_outside_coverage", False))},
            "Blurb":            {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
            "Hours":            {"rich_text": [{"text": {"content": safe_str(data.get("hours"))}}]},
            "Address":          {"rich_text": [{"text": {"content": safe_str(data.get("address"))}}]},
            "Source URL":       {"url":       data.get("source_url") or None},
            "Source Domain":    {"rich_text": [{"text": {"content": safe_str(data.get("source"))}}]},
            "Date Generated":   {"date":      {"start": datetime.today().strftime("%Y-%m-%d")}},
            "Status":           {"select":    {"name": "pending"}},
            "Relevance Score":  {"number":    int(data.get("relevance_score", 0) or 0)},
            "Scoring Notes":    {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
            "Default Winner":   {"checkbox":  data.get("default_winner", "") == "yes"},
            "Manually Edited":  {"checkbox":  False},
        }

        # Price Level select — only set if Claude returned a valid value
        price = safe_str(data.get("price_level"))
        if price in ("$", "$$", "$$$", "$$$$"):
            properties["Price Level"] = {"select": {"name": price}}

        create_page(NOTION_BUSINESS_BRIEF_DB_ID, properties)
        saved += 1
    print(f"  Saved {saved} new business briefs to Notion for {newsletter_name}")
