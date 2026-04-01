#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'Code'))
from notion_helper import redo_restaurant_selection

NEWSLETTER_NAME = os.environ["NEWSLETTER_NAME"]

redo_restaurant_selection(NEWSLETTER_NAME)
print("✓ Restaurant selection reset")
