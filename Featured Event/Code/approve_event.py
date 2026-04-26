#!/usr/bin/env python3

"""Approve an event for publication in the newsletter.
The gh pages JSON is updated client-side by the review app"""

import os
import sys 

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import approve_event_in_notion

APPROVED_URL = os.environ["APPROVED_URL"]

approve_event_in_notion(APPROVED_URL)
print("✓ Notion updated")
