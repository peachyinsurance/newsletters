#!/usr/bin/env python3
import os, sys
sys.path.append(os.path.dirname(__file__))
from notion_helper import redo_pet_selection

NEWSLETTER_NAME = os.environ["NEWSLETTER_NAME"]

redo_pet_selection(NEWSLETTER_NAME)
print("✓ Pet selection reset")
