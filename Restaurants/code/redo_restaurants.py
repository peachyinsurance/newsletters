#!/usr/bin/env python3
import os, sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'Code'))
from notion_helper import redo_restaurant_selection
import requests

NEWSLETTER_NAME = os.environ["NEWSLETTER_NAME"]
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER    = "couch2coders"
GITHUB_REPO     = "NewsletterAutomation"

redo_restaurant_selection(NEWSLETTER_NAME)

requests.post(
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/deploy_review_app.yml/dispatches",
    headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
    json={"ref": "main"}
)
print("✓ Deploy triggered")
