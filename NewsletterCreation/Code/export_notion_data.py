#!/usr/bin/env python3
"""
Export pending pets and restaurants from Notion to JSON files
for the review app to consume without needing an API key.
"""

import os
import sys
import json
sys.path.append(os.path.dirname(__file__))
from notion_helper import query_database, NOTION_PETS_DB_ID, NOTION_RESTAURANTS_DB_ID

def extract_text(prop) -> str:
    if not prop:
        return ""
    if prop.get("type") == "rich_text":
        items = prop.get("rich_text", [])
        return "".join(i.get("text", {}).get("content", "") for i in items)
    if prop.get("type") == "title":
        items = prop.get("title", [])
        return "".join(i.get("text", {}).get("content", "") for i in items)
    if prop.get("type") == "url":
        return prop.get("url") or ""
    if prop.get("type") == "select":
        s = prop.get("select")
        return s.get("name", "") if s else ""
    if prop.get("type") == "status":
        s = prop.get("status")
        return s.get("name", "") if s else ""
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

    with open("pets.json", "w") as f:
        json.dump(pets, f, indent=2)
    print(f"Exported {len(pets)} pets to pets.json")


def export_restaurants():
    pages = query_database(NOTION_RESTAURANTS_DB_ID)
    restaurants = []
    for page in pages:
        props = page["properties"]
        status_val = extract_text(props.get("Status", {})) or "pending"
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
        })

    with open("restaurants.json", "w") as f:
        json.dump(restaurants, f, indent=2)
    print(f"Exported {len(restaurants)} restaurants to restaurants.json")

if __name__ == "__main__":
    export_pets()
    export_restaurants()
