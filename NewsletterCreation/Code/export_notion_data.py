#!/usr/bin/env python3
"""
Export pending pets and restaurants from Notion to JSON files
for the review app to consume without needing an API key.
"""

import os
import sys
import json
sys.path.append(os.path.dirname(__file__))
from notion_helper import query_database, NOTION_PETS_DB_ID, NOTION_RESTAURANTS_DB_ID, NOTION_EVENTS_DB_ID

# Mojibake markers — characters that almost always indicate a UTF-8 string
# was decoded as Latin-1 somewhere upstream (`–` → `â`, `🎟` → `ðŸŽŸ`, etc.).
# If we see one of these in a field we attempt the inverse re-encode.
_MOJIBAKE_MARKERS = ("â", "Ã", "ð", "Â", "â€", "ï¿½")


def _parse_image_candidates(s):
    """Parse the JSON-encoded image_candidates list stored in Notion's
    rich_text field. Returns a list of URLs (empty list on any error)."""
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [u for u in v if isinstance(u, str) and u]
    except Exception:
        pass
    return []


def _fix_mojibake(s):
    """If the string looks like UTF-8 that was decoded as Latin-1, flip it back.

    The classic round-trip: `s.encode('latin-1').decode('utf-8')` undoes the
    accidental Latin-1 decode. We only attempt it when (a) at least one
    mojibake marker is present and (b) the round-trip actually succeeds —
    otherwise we return the original string untouched.
    """
    if not isinstance(s, str) or not any(m in s for m in _MOJIBAKE_MARKERS):
        return s
    try:
        return s.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def extract_text(prop) -> str:
    if not prop:
        return ""
    if prop.get("type") == "rich_text":
        items = prop.get("rich_text", [])
        return _fix_mojibake("".join(i.get("text", {}).get("content", "") for i in items))
    if prop.get("type") == "title":
        items = prop.get("title", [])
        return _fix_mojibake("".join(i.get("text", {}).get("content", "") for i in items))
    if prop.get("type") == "url":
        return prop.get("url") or ""
    if prop.get("type") == "select":
        s = prop.get("select")
        return _fix_mojibake(s.get("name", "")) if s else ""
    if prop.get("type") == "status":
        s = prop.get("status")
        return _fix_mojibake(s.get("name", "")) if s else ""
    if prop.get("type") == "number":
        return prop.get("number") or 0
    if prop.get("type") == "checkbox":
        return prop.get("checkbox", False)
    if prop.get("type") == "date":
        d = prop.get("date")
        return d.get("start", "") if d else ""
    return ""

def export_pets():
    pages = query_database(NOTION_PETS_DB_ID)
    pets = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        # Skip archived statuses so they don't show up as candidates in the review UI
        if status_val in ("approved - old", "rejected"):
            continue
        pets.append({
           "source_url":  extract_text(props.get("Source URL", {})),
            "listing_url": extract_text(props.get("Listing URL", {})),
            "pet_name": extract_text(props.get("Name", {})).split(" - ", 1)[-1],
            "shelter_name":       extract_text(props.get("Shelter", {})),
            "blurb":              extract_text(props.get("Blurb", {})),
            "shelter_address":    extract_text(props.get("Shelter Address", {})),
            "shelter_phone":      extract_text(props.get("Shelter Phone", {})),
            "shelter_email":      extract_text(props.get("Shelter Email", {})),
            "shelter_hours":      extract_text(props.get("Shelter Hours", {})),
            "photo_url":          extract_text(props.get("Photo URL", {})),
            "date_generated":     extract_text(props.get("Date Generated", {})),
            "status":             status_val,
            "newsletter_name":    extract_text(props.get("Newsletter", {})),
            "total_score":        str(extract_text(props.get("Total Score", {}))),
            "adoptability_score": str(extract_text(props.get("Adoptability Score", {}))),
            "story_score":        str(extract_text(props.get("Story Score", {}))),
            "shelter_time_score": str(extract_text(props.get("Shelter Time Score", {}))),
            "scoring_notes":      extract_text(props.get("Scoring Notes", {})),
            "default_winner":     "yes" if extract_text(props.get("Default Winner", {})) else "",
            "cat_default":        "yes" if extract_text(props.get("Cat Default", {})) else "",
            "dog_default":        "yes" if extract_text(props.get("Dog Default", {})) else "",
            "animal_type":        extract_text(props.get("Animal Type", {})),
        })

    with open("pets.json", "w", encoding="utf-8") as f:
        json.dump(pets, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(pets)} pets to pets.json")


def export_restaurants():
    pages = query_database(NOTION_RESTAURANTS_DB_ID)
    restaurants = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        # Skip archived statuses so they don't show up as candidates in the review UI
        if status_val == "approved - old":
            continue
        restaurants.append({
            "place_id":               extract_text(props.get("Place ID", {})),
            "restaurant_name": extract_text(props.get("Name", {})).split(" - ", 1)[-1],
            "cuisine_type":           extract_text(props.get("Cuisine", {})),
            "blurb":                  extract_text(props.get("Blurb", {})),
            "address":                extract_text(props.get("Address", {})),
            "phone":                  extract_text(props.get("Phone", {})),
            "hours":                  extract_text(props.get("Hours", {})),
            "website_url":            extract_text(props.get("Website", {})),
            "google_maps_url":        extract_text(props.get("Google Maps URL", {})),
            "photo_url":              extract_text(props.get("Photo URL", {})),
            "rating":                 str(extract_text(props.get("Rating", {}))),
            "review_count":           str(extract_text(props.get("Review Count", {}))),
            "price_level":            extract_text(props.get("Price Level", {})),
            "date_generated":         extract_text(props.get("Date Generated", {})),
            "status":                 status_val,
            "newsletter_name":        extract_text(props.get("Newsletter", {})),
            "total_score":            str(extract_text(props.get("Total Score", {}))),
            "appeal_score":           str(extract_text(props.get("Appeal Score", {}))),
            "uniqueness_score":       str(extract_text(props.get("Uniqueness Score", {}))),
            "neighborhood_fit_score": str(extract_text(props.get("Neighborhood Fit Score", {}))),
            "festive_score":          str(extract_text(props.get("Festive Score", {}))),
            "scoring_notes":          extract_text(props.get("Scoring Notes", {})),
            "default_winner":         "yes" if extract_text(props.get("Default Winner", {})) else "",
            "festive_promoted":       "yes" if extract_text(props.get("Festive Promoted", {})) else "",
        })

    with open("restaurants.json", "w", encoding="utf-8") as f:
        json.dump(restaurants, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(restaurants)} restaurants to restaurants.json")

def export_events(): 
    pages = query_database(NOTION_EVENTS_DB_ID)
    events = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        # Skip archived statuses so they don't show up as candidates in the review UI
        if status_val in ("approved - old", "rejected"):
            continue
        events.append({
            "source_url":            extract_text(props.get("Source URL", {})),
            "event_name":            extract_text(props.get("Event Name", {})),
            "date":                  extract_text(props.get("Date", {})),
            "time":                  extract_text(props.get("Time", {})),
            "venue":                 extract_text(props.get("Venue", {})),
            "price":                 extract_text(props.get("Price", {})),
            "blurb":                 extract_text(props.get("Blurb", {})),
            "ticket_url":            extract_text(props.get("Ticket URL", {})),
            "date_generated":        extract_text(props.get("Date Generated", {})),
            "status":                status_val,
            "newsletter_name":       extract_text(props.get("Newsletter", {})),
            "total_score":           str(extract_text(props.get("Total Score", {}))),
            "demographic_fit_score": str(extract_text(props.get("Demographic Fit Score", {}))),
            "uniqueness_score":      str(extract_text(props.get("Uniqueness Score", {}))),
            "audience_match_score":  str(extract_text(props.get("Audience Match Score", {}))),
            "scoring_notes":         extract_text(props.get("Scoring Notes", {})),
            "image_url":             extract_text(props.get("Image URL", {})),
            "image_candidates":      _parse_image_candidates(extract_text(props.get("Image Candidates", {}))),
            "header_image_url":      extract_text(props.get("Header Image URL", {})),
            "gif_url":               extract_text(props.get("GIF URL", {})),
            "default_winner":        "yes" if extract_text(props.get("Default Winner", {})) else "",
        })

    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(events)} events to events.json")

def export_business_briefs():
    """Export Business Brief candidates for the review app. Skips
    `approved - old` so archived weeks don't appear. The review tile
    surfaces image_candidates so reviewers can swap the photo too."""
    db_id = os.environ.get("NOTION_BUSINESS_BRIEF_DB_ID", "")
    if not db_id:
        print("⚠ NOTION_BUSINESS_BRIEF_DB_ID empty — skipping business briefs export")
        return
    from notion_helper import query_database  # noqa: E402
    pages = query_database(db_id)
    briefs = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        if status_val == "approved - old":
            continue
        ic_text = extract_text(props.get("Image Candidates", {}))
        try:
            image_candidates = json.loads(ic_text) if ic_text else []
            if not isinstance(image_candidates, list):
                image_candidates = []
        except Exception:
            image_candidates = []
        briefs.append({
            "source_url":       extract_text(props.get("Source URL", {})),
            "business_name":    extract_text(props.get("Business Name", {})),
            "city":             extract_text(props.get("City", {})),
            "blurb":            extract_text(props.get("Blurb", {})),
            "price_level":      extract_text(props.get("Price Level", {})),
            "hours":            extract_text(props.get("Hours", {})),
            "address":          extract_text(props.get("Address", {})),
            "source_domain":    extract_text(props.get("Source Domain", {})),
            "image_url":        extract_text(props.get("Photo URL", {})),
            "image_candidates": image_candidates,
            "scoring_notes":    extract_text(props.get("Scoring Notes", {})),
            "relevance_score":  str(extract_text(props.get("Relevance Score", {}))),
            "total_score":      str(extract_text(props.get("Relevance Score", {}))),  # alias for sort
            "date_generated":   extract_text(props.get("Date Generated", {})),
            "status":           status_val,
            "newsletter_name":  extract_text(props.get("Newsletter", {})),
            "default_winner":   "yes" if extract_text(props.get("Default Winner", {})) else "",
        })

    with open("business_briefs.json", "w", encoding="utf-8") as f:
        json.dump(briefs, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(briefs)} business briefs to business_briefs.json")


def export_memes():
    """Export Meme Corner candidates for the review app. Skips
    `approved - old` so archived weeks don't appear, but keeps
    `pending`, `approved`, and `rejected` so reviewers see context."""
    db_id = os.environ.get("NOTION_MEMES_DB_ID", "")
    if not db_id:
        print("⚠ NOTION_MEMES_DB_ID empty — skipping memes export")
        return
    from notion_helper import query_database  # local import (avoid top-of-file change)
    pages = query_database(db_id)
    memes = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        if status_val == "approved - old":
            continue
        memes.append({
            "permalink":       extract_text(props.get("Reddit Permalink", {})),
            "image_url":       extract_text(props.get("Image URL", {})),
            "caption":         extract_text(props.get("Caption", {})),
            "subreddit":       extract_text(props.get("Subreddit", {})),
            "reddit_author":   extract_text(props.get("Reddit Author", {})),
            "score":           str(extract_text(props.get("Score", {}))),
            "date_generated":  extract_text(props.get("Date Generated", {})),
            "status":          status_val,
            "newsletter_name": extract_text(props.get("Newsletter", {})),
        })

    with open("memes.json", "w", encoding="utf-8") as f:
        json.dump(memes, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(memes)} memes to memes.json")


def export_insurance_tips():
    """Export Insurance Tip candidates for the review app. Skips
    `approved - old` so archived weeks don't appear."""
    db_id = os.environ.get("NOTION_TIPS_DB_ID", "")
    if not db_id:
        print("⚠ NOTION_TIPS_DB_ID empty — skipping insurance tips export")
        return
    from notion_helper import query_database  # local import (avoid top-of-file change)
    pages = query_database(db_id)
    tips = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        if status_val == "approved - old":
            continue
        tips.append({
            "tip_title":           extract_text(props.get("Tip Title", {})),
            "topic":               extract_text(props.get("Topic", {})),
            "blurb":               extract_text(props.get("Blurb", {})),
            "summary":             extract_text(props.get("Summary", {})),
            "source_url":          extract_text(props.get("Source URL", {})),
            "source_name":         extract_text(props.get("Source Name", {})),
            "sponsor_name":        extract_text(props.get("Sponsor Name", {})),
            "category":            extract_text(props.get("Category", {})),
            "total_score":         str(extract_text(props.get("Total Score", {}))),
            "relevance_score":     str(extract_text(props.get("Relevance Score", {}))),
            "actionability_score": str(extract_text(props.get("Actionability Score", {}))),
            "timeliness_score":    str(extract_text(props.get("Timeliness Score", {}))),
            "scoring_notes":       extract_text(props.get("Scoring Notes", {})),
            "date_generated":      extract_text(props.get("Date Generated", {})),
            "status":              status_val,
            "newsletter_name":     extract_text(props.get("Newsletter", {})),
            "default_winner":      "yes" if extract_text(props.get("Default Winner", {})) else "",
        })

    with open("insurance_tips.json", "w", encoding="utf-8") as f:
        json.dump(tips, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(tips)} insurance tips to insurance_tips.json")


def export_in_search_of():
    """Export In Search Of rows (job postings + employer spotlights) for the
    review app. Skips approved-old."""
    db_id = os.environ.get("NOTION_IN_SEARCH_OF_DB_ID", "")
    jobs = []
    pages = []
    if not db_id:
        # In Search Of may not have a DB yet — still write an empty file so the
        # deploy's cp step never fails and the review app shows "no candidates".
        print("⚠ NOTION_IN_SEARCH_OF_DB_ID empty — writing empty in_search_of.json")
    else:
        from notion_helper import query_database  # local import (avoid top-of-file change)
        pages = query_database(db_id)
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
        if status_val == "approved - old":
            continue
        jobs.append({
            "employer":         extract_text(props.get("Employer", {})),
            "job_listings_url": extract_text(props.get("Job Listings URL", {})),
            "scraped_snippet":  extract_text(props.get("Scraped Snippet", {})),
            "description":      extract_text(props.get("Description", {})),
            "roles":            extract_text(props.get("Roles", {})),
            "city":             extract_text(props.get("City", {})),
            "image_url":        extract_text(props.get("Image URL", {})),
            "bonus":            "yes" if extract_text(props.get("Bonus", {})) else "",
            "date_generated":   extract_text(props.get("Date Generated", {})),
            "status":           status_val,
            "newsletter_name":  extract_text(props.get("Newsletter", {})),
        })

    with open("in_search_of.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    print(f"Exported {len(jobs)} In Search Of rows to in_search_of.json")


if __name__ == "__main__":
    export_pets()
    export_restaurants()
    export_events()
    export_business_briefs()
    export_memes()
    export_insurance_tips()
    export_in_search_of()
