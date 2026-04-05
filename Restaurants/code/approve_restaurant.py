#!/usr/bin/env python3
"""
Approve restaurant — Notion sync only.
The gh-pages JSON is updated client-side by the review app.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'Code'))
from notion_helper import approve_restaurant_in_notion

APPROVED_PLACE_ID = os.environ["APPROVED_PLACE_ID"]

approve_restaurant_in_notion(APPROVED_PLACE_ID)
print("✓ Notion updated")
