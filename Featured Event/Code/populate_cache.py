#!/usr/bin/env python3
"""One-shot: fetch fresh Brave candidates for one newsletter and save to
the brave_cache/ directory. Use this to seed the debug_drilldown.ipynb
notebook with a current snapshot without running the full pipeline.

Usage:
    BRAVE_NEWS_API_KEY=<key> python3 "Featured Event/Code/populate_cache.py" East_Cobb_Connect
"""
import os
import sys
import json
from pathlib import Path

# Stub the env vars Featured_Event.py imports require but we won't use
os.environ.setdefault("NOTION_API_KEY", "stub")
os.environ.setdefault("CLAUDE_API_KEY", "stub")
os.environ.setdefault("NOTION_EVENTS_DB_ID", "stub")
os.environ.setdefault("NOTION_PETS_DB_ID", "stub")
os.environ.setdefault("NOTION_RESTAURANTS_DB_ID", "stub")

if not os.environ.get("BRAVE_NEWS_API_KEY"):
    print("✗ Set BRAVE_NEWS_API_KEY in the environment first.")
    print("  Look it up in GitHub → Settings → Secrets, or your Brave dashboard.")
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
sys.path.insert(0, os.path.dirname(__file__))

from newsletters_config import NEWSLETTERS  # noqa: E402
from Featured_Event import fetch_events_brave  # noqa: E402

newsletter_name = sys.argv[1] if len(sys.argv) > 1 else "East_Cobb_Connect"
matches = [n for n in NEWSLETTERS if n["name"] == newsletter_name]
if not matches:
    print(f"✗ Unknown newsletter: {newsletter_name}")
    print(f"  Available: {[n['name'] for n in NEWSLETTERS]}")
    sys.exit(1)
newsletter = matches[0]

print(f"Fetching Brave candidates for {newsletter['name']} ({newsletter['display_area']})…")
candidates = fetch_events_brave(
    search_areas=newsletter["search_areas"],
    display_area=newsletter["display_area"],
)
print(f"  Got {len(candidates)} unique candidates")

cache_dir = Path(__file__).parent / "brave_cache"
cache_dir.mkdir(parents=True, exist_ok=True)
out_path = cache_dir / f"{newsletter['name']}_round1.json"
out_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
print(f"  ✓ Wrote {out_path}")
print()
print("Now open the notebook and re-run cells:")
print("  http://localhost:8889/tree → Featured Event/Code/debug_drilldown.ipynb")
