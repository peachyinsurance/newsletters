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
        "Shelter":            {"rich_text": {}},
        "Blurb":              {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
        "Shelter Address":    {"rich_text": [{"text": {"content": safe_str(data.get("shelter_address"))}}]},
        "Shelter Phone":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_phone"))}}]},
        "Shelter Email":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_email"))}}]},
        "Shelter Hours":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_hours"))}}]},
        "Photo URL":          {"url": {}},
        "Date Generated":     {"date": {}},
        "Status":             {"select": {"options": [
            {"name": "pending",  "color": "yellow"},
            {"name": "approved", "color": "green"},
            {"name": "rejected", "color": "red"}
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
        "Scoring Notes":      {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
        "Default Winner":     {"checkbox": {}},
        "Cat Default":        {"checkbox": {}},
        "Dog Default":        {"checkbox": {}},
        "Animal Type":        {"select": {"options": [
            {"name": "cat", "color": "orange"},
            {"name": "dog", "color": "brown"}
        ]}},
    }

    # Restaurants database properties
    restaurants_properties = {
        "Name":                   {"title": {}},
        "Place ID":               {"rich_text": [{"text": {"content": safe_str(data.get("place_id"))}}]},
        "Cuisine":                {"select": {}},
        "Blurb":                  {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
        "Address":                {"rich_text": [{"text": {"content": safe_str(data.get("address"))}}]},
        "Phone":                  {"rich_text": [{"text": {"content": safe_str(data.get("phone"))}}]},
        "Hours":                  {"rich_text": [{"text": {"content": safe_str(data.get("hours"))[:2000]}}]},
        "Website":                {"url": {}},
        "Google Maps URL":        {"url": {}},
        "Photo URL":              {"url": {}},
        "Rating":                 {"number": {"format": "number"}},
        "Review Count":           {"number": {"format": "number"}},
        "Price Level":            {"select": {}},
        "Date Generated":         {"date": {}},
        "Status":                 {"select": {"options": [
            {"name": "pending",  "color": "yellow"},
            {"name": "approved", "color": "green"},
            {"name": "rejected", "color": "red"}
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
        "Scoring Notes":          {"rich_text": [{"text": {"content": safe_str(data.get("scoring_notes"))}}]},
        "Default Winner":         {"checkbox": {}},
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
# ---------------------------------------------------------------------------
# PETS HELPERS
# ---------------------------------------------------------------------------
def get_approved_pet_urls() -> set:
    """Get source URLs of approved pets (for exclusion from candidates)."""
    try:
        pages = query_database(NOTION_PETS_DB_ID, filters={
            "property": "Status",
            "status":   {"equals": "approved"}
        })
    except Exception:
        # If no approved pages exist yet, return empty set
        return set()
    urls = set()
    for page in pages:
        url = page["properties"].get("Source URL", {}).get("url", "")
        if url:
            urls.add(url)
    print(f"Loaded {len(urls)} previously approved pet URLs to exclude")
    return urls
    
def save_pets_to_notion(results: list, newsletter_name: str) -> None:
    print(f"Saving {len(results)} pets to Notion...")
    existing_urls = get_existing_pet_urls(newsletter_name)
    print(f"  Found {len(existing_urls)} existing entries to skip")

    saved = 0
    for data in results:
        source_url = data.get("listing_url") or data.get("source_url", "")
        if source_url and source_url in existing_urls:
            print(f"  ✗ Skipping duplicate: {data.get('pet_name')}")
            continue

        properties = {
            "Name": {"title": [{"text": {"content": f"{newsletter_name.replace('_', ' ')} - {data.get('pet_name', '')}"}}]},
            "Source URL":         {"url": data.get("listing_url") or data.get("source_url", "") or None},
            "Shelter":            {"rich_text": [{"text": {"content": safe_str(data.get("shelter_name"))}}]},
            "Blurb":              {"rich_text": [{"text": {"content": safe_str(data.get("blurb"))[:2000]}}]},
            "Shelter Address":    {"rich_text": [{"text": {"content": safe_str(data.get("shelter_address"))}}]},
            "Shelter Phone":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_phone"))}}]},
            "Shelter Email":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_email"))}}]},
            "Shelter Hours":      {"rich_text": [{"text": {"content": safe_str(data.get("shelter_hours"))}}]},
            "Photo URL":          {"url": data.get("photo_url") or None},
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
    """Set approved pet to approved, all others pending to rejected."""
    pages = query_database(NOTION_PETS_DB_ID)

    for page in pages:
        page_id    = page["id"]
        props      = page["properties"]
        status     = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""

        if status_name != "pending":
            continue

        page_url   = props.get("Source URL", {}).get("url", "")
        new_status = "approved" if page_url == source_url else "rejected"
        update_page(page_id, {"Status": {"select": {"name": new_status}}})
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        print(f"{new_status}: {name}")
        
def get_existing_pet_urls(newsletter_name: str) -> set:
    """Get source URLs of all pending and approved pets to avoid duplicates."""
    try:
        pages = query_database(NOTION_PETS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        urls = set()
        for page in pages:
            status = page["properties"].get("Status", {}).get("select", {})
            if status and status.get("name") == "rejected":
                continue
            url = page["properties"].get("Source URL", {}).get("url", "")
            if url:
                urls.add(url)
        return urls
    except Exception as e:
        print(f"  Warning: could not load existing URLs: {e}")
        return set()
# ---------------------------------------------------------------------------
# RESTAURANTS HELPERS
# ---------------------------------------------------------------------------
def get_featured_place_ids(newsletter_name: str) -> set:
    cutoff = (datetime.today() - timedelta(weeks=8)).strftime("%Y-%m-%d")
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
            "and": [
                {"property": "Status",     "status":   {"equals": "approved"}},
                {"property": "Newsletter", "select":   {"equals": newsletter_name}},
                {"property": "Date Generated", "date": {"on_or_after": cutoff}}
            ]
        })
    except Exception:
        return set()
    place_ids = set()
    for page in pages:
        pid = page["properties"].get("Place ID", {}).get("rich_text", [{}])
        if pid:
            place_ids.add(pid[0].get("text", {}).get("content", ""))
    print(f"Loaded {len(place_ids)} featured restaurants to exclude")
    return place_ids

def get_existing_place_ids(newsletter_name: str) -> set:
    """Get place IDs of all non-rejected restaurants to avoid duplicates."""
    try:
        pages = query_database(NOTION_RESTAURANTS_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name}
        })
        place_ids = set()
        for page in pages:
            status = page["properties"].get("Status", {}).get("select", {})
            if status and status.get("name") == "rejected":
                continue
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
    """Set approved restaurant to approved, all others pending to rejected."""
    # Fetch all pending pages without filter
    pages = query_database(NOTION_RESTAURANTS_DB_ID)

    for page in pages:
        page_id    = page["id"]
        props      = page["properties"]
        status     = props.get("Status", {}).get("select", {})
        status_name = status.get("name", "") if status else ""
        
        if status_name != "pending":
            continue

        pid_prop   = props.get("Place ID", {}).get("rich_text", [])
        page_place_id = pid_prop[0].get("text", {}).get("content", "") if pid_prop else ""
        new_status = "approved" if page_place_id == place_id else "rejected"
        update_page(page_id, {"Status": {"select": {"name": new_status}}})
        name = props.get("Name", {}).get("title", [{}])[0].get("text", {}).get("content", "")
        print(f"{new_status}: {name}")
