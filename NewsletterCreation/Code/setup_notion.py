#!/usr/bin/env python3
import os
import sys
sys.path.append(os.path.dirname(__file__))
from notion_helper import setup_notion_databases

setup_notion_databases()
