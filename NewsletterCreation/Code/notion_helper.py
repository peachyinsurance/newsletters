#!/usr/bin/env python3
"""
Shared Notion API helper for Newsletter Automation.
Handles creating/updating pages in Pets and Restaurants databases.
"""

import os
import json
import requests
from datetime import datetime, timedelta

NOTION_API_KEY           = os.environ["NOTION_API_KEY"]
NOTION_PETS_DB_ID        = os.environ["NOTION_PETS_DB_ID"]
NOTION_RESTAURANTS_DB_ID = os.environ["NOTION_RESTAURANTS_DB_ID"]
NOTION_LOWDOWN_DB_ID     = os.environ.get("NOTION_LOWDOWN_DB_ID", "")
NOTION_RE_DB_ID          = os.environ.get("NOTION_RE_DB_ID", "")
NOTION_EVENTS_DB_ID      = os.environ.get("NOTION_EVENTS_DB_ID", "")
NOTION_INTRO_DB_ID       = os.environ.get("NOTION_INTRO_DB_ID", "")

HEADERS = {
    "Authorization":  f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type":   "application/json"
}

# ---------------------------------------------------------------------------
# GENERIC HELPERS
# ---------------------------------------------------------------------------
def query_database(db_id: str, filters: dict = None) -> list:
    url     = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {"filter": filters} if filters else {}
    results = []
    has_more = True
    cursor   = None

    while has_more:
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        r.raise_for_status()
        data     = r.json()
        results += data.get("results", [])
        has_more = data.get("has_more", False)
        cursor   = data.get("next_cursor")

    return results

def update_page(page_id: str, properties: dict) -> dict:
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        json={"properties": properties},
        timeout=30
    )
    r.raise_for_status()
    return r.json()

def create_page(db_id: str, properties: dict) -> dict:
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=HEADERS,
        json={"parent": {"database_id": db_id}, "properties": properties},
        timeout=30
    )
    if not r.ok:
        print(f"  Notion error: {r.text[:500]}")
    r.raise_for_status()
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
                {"name": "pending"}
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
            "Newsletter":             {"select": {"options": [
                {"name": "East_Cobb_Connect", "color": "purple"},
                {"name": "Perimeter_Post",    "color": "pink"}
            ]}},
            "Date Generated":         {"date": {}},
            "Status":                 {"select": {"options": [
                {"name": "pending",  "color": "yellow"},
                {"name": "approved", "color": "green"},
                {"name": "rejected", "color": "red"},
                {"name": "approved - old", "color": "gray"}
            ]}},
            "Total Score":            {"number": {"format": "number"}},
            "Demographic Fit Score":  {"number": {"format": "number"}},
            "Uniqueness Score":       {"number": {"format": "number"}},
            "Audience Match Score":   {"number": {"format": "number"}},
            "Scoring Notes":          {"rich_text": {}},
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

# ---------------------------------------------------------------------------
# PETS HELPERS
# ---------------------------------------------------------------------------
def get_approved_pet_urls() -> set:
    """Get source URLs of approved and previously approved pets (for exclusion from candidates)."""
    urls = set()
    for status in ("approved", "approved - old"):
        try:
            pages = query_database(NOTION_PETS_DB_ID, filters={
                "property": "Status",
                "status":   {"equals": status}
            })
            for page in pages:
                url = page["properties"].get("Source URL", {}).get("url", "")
                if url:
                    urls.add(url)
        except Exception:
            pass
    print(f"Loaded {len(urls)} previously approved pet URLs to exclude")
    return urls
    
def save_pets_to_notion(results: list, newsletter_name: str) -> None:
    print(f"Saving {len(results)} pets to Notion...")
    existing_urls = get_existing_pet_urls(newsletter_name)
    print(f"  Found {len(existing_urls)} existing entries to skip")
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

def approve_pet_in_notion(source_url: str) -> None:
    """Set approved pet to approved, all others in same newsletter to rejected."""
    source_url = (source_url or "").strip()
    if not source_url:
        print("✗ No source_url provided — aborting approval to avoid updating all rows")
        return

    pages = query_database(NOTION_PETS_DB_ID)

    # First find the approved pet to get its newsletter
    approved_newsletter = None
    approved_page_id = None
    for page in pages:
        props    = page["properties"]
        page_url = props.get("Source URL", {}).get("url", "")
        if page_url and page_url == source_url:
            newsletter = props.get("Newsletter", {}).get("select", {})
            approved_newsletter = newsletter.get("name", "") if newsletter else ""
            approved_page_id = page["id"]
            break

    if not approved_newsletter:
        print(f"✗ No pet found with source_url '{source_url}' — aborting")
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
    pages = query_database(NOTION_PETS_DB_ID, filters={
        "property": "Newsletter",
        "select":   {"equals": newsletter_name}
    })
    urls = set()
    for page in pages:
        props  = page["properties"]
        status = props.get("Status", {}).get("select", {})
        if status and status.get("name") == "rejected":
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

def cleanup_pets_notion() -> None:
    """Delete all pet entries that are not approved."""
    pages = query_database(NOTION_PETS_DB_ID)
    count = 0
    for page in pages:
        status = page["properties"].get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""
        if status_name == "approved":
            continue
        name = page["properties"].get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        archive_page(page["id"])
        print(f"  Archived: {name} (status: {status_name})")
        count += 1
    print(f"Archived {count} non-approved pets")

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
    
def approve_restaurant_in_notion(place_id: str) -> None:
    """Set selected restaurant to Tier 1 Winner, others in same newsletter to Tier 2 Winner."""
    place_id = (place_id or "").strip()
    if not place_id:
        print("✗ No place_id provided — aborting approval to avoid updating all rows")
        return

    pages = query_database(NOTION_RESTAURANTS_DB_ID)

    # First find the selected restaurant to get its newsletter
    approved_newsletter = None
    approved_page_id = None
    for page in pages:
        props     = page["properties"]
        pid_prop  = props.get("Place ID", {}).get("rich_text", [])
        page_place_id = pid_prop[0].get("text", {}).get("content", "") if pid_prop else ""
        if page_place_id and page_place_id == place_id:
            newsletter = props.get("Newsletter", {}).get("select", {})
            approved_newsletter = newsletter.get("name", "") if newsletter else ""
            approved_page_id = page["id"]
            break

    if not approved_newsletter:
        print(f"✗ No restaurant found with place_id '{place_id}' — aborting")
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

def cleanup_old_restaurants_notion() -> None:
    """Delete restaurant entries older than 8 weeks."""
    cutoff = (datetime.today() - timedelta(weeks=8)).strftime("%Y-%m-%d")
    pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
        "property": "Date Generated",
        "date":     {"before": cutoff}
    })
    count = 0
    for page in pages:
        name = page["properties"].get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        date_prop = page["properties"].get("Date Generated", {}).get("date", {})
        date_str = date_prop.get("start", "") if date_prop else ""
        archive_page(page["id"])
        print(f"  Archived: {name} (generated: {date_str})")
        count += 1
    print(f"Archived {count} restaurants older than 8 weeks")

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
        has_manual_edit = any(
            p["properties"].get("Manually Edited", {}).get("checkbox", False)
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
# ---------------------------------------------------------------------------
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
        has_manual_edit = any(
            p["properties"].get("Manually Edited", {}).get("checkbox", False)
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
