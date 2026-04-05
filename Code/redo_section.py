#!/usr/bin/env python3
"""
Redo section selection:
1. Reset statuses in Notion (source of truth)
2. Update the section JSON on gh-pages branch directly (skip full rebuild)

Env vars:
  NEWSLETTER_NAME  – e.g. "East_Cobb_Connect"
  SECTION          – "pets" or "restaurants"
  GITHUB_TOKEN     – PAT with repo scope
"""
import os
import sys
import json
import base64
import requests

sys.path.append(os.path.dirname(__file__))
from notion_helper import redo_pet_selection, redo_restaurant_selection

NEWSLETTER_NAME = os.environ["NEWSLETTER_NAME"]
SECTION         = os.environ["SECTION"]
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER    = "couch2coders"
GITHUB_REPO     = "NewsletterAutomation"
BRANCH          = "gh-pages"

SECTION_CONFIG = {
    "pets": {
        "data_file": "pets.json",
        "redo_fn":   redo_pet_selection,
        "label":     "pets",
    },
    "restaurants": {
        "data_file": "restaurants.json",
        "redo_fn":   redo_restaurant_selection,
        "label":     "restaurants",
    },
}

if SECTION not in SECTION_CONFIG:
    print(f"Unknown section: {SECTION}. Expected one of: {list(SECTION_CONFIG.keys())}")
    sys.exit(1)

cfg = SECTION_CONFIG[SECTION]

# 1. Reset in Notion
cfg["redo_fn"](NEWSLETTER_NAME)
print(f"✓ Notion statuses reset for {cfg['label']}")

# 2. Fetch current JSON from gh-pages
headers  = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
file_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{cfg['data_file']}?ref={BRANCH}"
res = requests.get(file_url, headers=headers)
res.raise_for_status()
file_info = res.json()

content = json.loads(base64.b64decode(file_info["content"]).decode("utf-8"))

# 3. Update statuses for this newsletter back to pending
changed = 0
for item in content:
    if item.get("newsletter_name") == NEWSLETTER_NAME and item.get("status") in ("approved", "rejected", "Approved", "Rejected"):
        item["status"] = "pending"
        changed += 1

print(f"✓ Reset {changed} {cfg['label']} to pending in JSON")

# 4. Commit updated JSON back to gh-pages
updated_content = base64.b64encode(json.dumps(content, indent=2).encode("utf-8")).decode("utf-8")
commit_res = requests.put(
    file_url,
    headers=headers,
    json={
        "message": f"redo: reset {NEWSLETTER_NAME} {cfg['label']} to pending",
        "content": updated_content,
        "sha": file_info["sha"],
        "branch": BRANCH,
    },
)
commit_res.raise_for_status()
print("✓ JSON updated on gh-pages")
