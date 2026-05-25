#!/usr/bin/env python3
"""Patch.com Dunwoody edition (patch.com/georgia/dunwoody) — thin
wrapper around _shared/patch_events.run_patch_source.

Tagged Perimeter_Post (not ECC_PP) because Patch's regional editor
curates the Dunwoody calendar around Dunwoody / Brookhaven /
Sandy Springs etc., which all fall in PP coverage only. If a Roswell
event slips through, the post-scrape normalize_city_tags.py sweep
will flip it to ECC_PP based on city.

Cost: $0 (direct HTTP to Patch's Next.js data endpoint)."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from patch_events import run_patch_source  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_patch_source(
        patch_slug="georgia/dunwoody",
        newsletter=os.environ.get("NEWSLETTER", "Perimeter_Post"),
    ))
