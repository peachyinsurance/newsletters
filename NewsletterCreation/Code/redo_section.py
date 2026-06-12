#!/usr/bin/env python3
"""
Redo section selection — Notion sync only.
The gh-pages JSON is updated client-side by the review app;
this script resets statuses in Notion to keep it in sync.

Env vars:
  NEWSLETTER_NAME  – e.g. "East_Cobb_Connect"
  SECTION          – one of SECTION_CONFIG below (aliases accepted)
"""
import os
import sys

sys.path.append(os.path.dirname(__file__))
from notion_helper import (
    redo_pet_selection,
    redo_restaurant_selection,
    redo_event_selection,
    redo_tip_selection,
    redo_business_brief_selection,
    redo_in_search_of_selection,
    redo_meme_selection,
)

NEWSLETTER_NAME = os.environ["NEWSLETTER_NAME"]
SECTION         = os.environ["SECTION"].strip().lower()

SECTION_CONFIG = {
    "pets":           {"redo_fn": redo_pet_selection,            "label": "pets"},
    "restaurants":    {"redo_fn": redo_restaurant_selection,     "label": "restaurants"},
    "events":         {"redo_fn": redo_event_selection,          "label": "events"},
    "tip":            {"redo_fn": redo_tip_selection,            "label": "insurance tips"},
    "business_brief": {"redo_fn": redo_business_brief_selection, "label": "business briefs"},
    "in_search_of":   {"redo_fn": redo_in_search_of_selection,   "label": "in search of"},
    "memes":          {"redo_fn": redo_meme_selection,           "label": "memes"},
}

# Friendly aliases → canonical section keys, so reviewers can type the
# natural name (e.g. "tips", "jobs", "brief") instead of the exact key.
ALIASES = {
    "pet": "pets", "restaurant": "restaurants", "event": "events",
    "tips": "tip", "insurance_tip": "tip", "insurance_tips": "tip",
    "business_briefs": "business_brief", "brief": "business_brief", "briefs": "business_brief",
    "jobs": "in_search_of", "search": "in_search_of", "insearchof": "in_search_of",
    "meme": "memes",
}
SECTION = ALIASES.get(SECTION, SECTION)

if SECTION not in SECTION_CONFIG:
    print(f"Unknown section: {SECTION}. Expected one of: {list(SECTION_CONFIG.keys())}")
    sys.exit(1)

cfg = SECTION_CONFIG[SECTION]

cfg["redo_fn"](NEWSLETTER_NAME)
print(f"✓ Notion statuses reset for {cfg['label']}")
