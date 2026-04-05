#!/usr/bin/env python3
"""
Cleanup pets: delete all non-approved pet entries from Notion.
Keeps only approved pets as historical record.
"""
import os
import sys

sys.path.append(os.path.dirname(__file__))
from notion_helper import cleanup_pets_notion

cleanup_pets_notion()
print("✓ Pet cleanup complete")
