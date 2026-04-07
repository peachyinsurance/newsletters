#!/usr/bin/env python3
"""
Cleanup restaurants: delete entries older than 8 weeks from Notion.
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import cleanup_old_restaurants_notion

cleanup_old_restaurants_notion()
print("✓ Restaurant cleanup complete")
