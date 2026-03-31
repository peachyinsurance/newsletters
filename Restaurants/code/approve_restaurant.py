#!/usr/bin/env python3
import os, sys
sys.path.append(os.path.dirname(__file__))
from notion_helper import approve_restaurant_in_notion

APPROVED_PLACE_ID = os.environ["APPROVED_PLACE_ID"]
approve_restaurant_in_notion(APPROVED_PLACE_ID)
