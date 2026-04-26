#!/usr/bin/env python3
"""
Redo section selection — Notion sync only.
The gh-pages JSON is updated client-side by the review app;
this script resets statuses in Notion to keep it in sync.

Env vars:
  NEWSLETTER_NAME  – e.g. "East_Cobb_Connect"
  SECTION          – "pets", "restaurants", or "events"
"""
import os
import sys

sys.path.append(os.path.dirname(__file__))
from notion_helper import (
    redo_pet_selection,
    redo_restaurant_selection,
    redo_event_selection,
)

NEWSLETTER_NAME = os.environ["NEWSLETTER_NAME"]
SECTION         = os.environ["SECTION"]

SECTION_CONFIG = {
    "pets": {
        "redo_fn": redo_pet_selection,
        "label":   "pets",
    },
    "restaurants": {
        "redo_fn": redo_restaurant_selection,
        "label":   "restaurants",
    },
    "events": {
        "redo_fn": redo_event_selection,
        "label":   "events",
    },
}

if SECTION not in SECTION_CONFIG:
    print(f"Unknown section: {SECTION}. Expected one of: {list(SECTION_CONFIG.keys())}")
    sys.exit(1)

cfg = SECTION_CONFIG[SECTION]

cfg["redo_fn"](NEWSLETTER_NAME)
print(f"✓ Notion statuses reset for {cfg['label']}")
