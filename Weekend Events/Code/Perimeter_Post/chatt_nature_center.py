#!/usr/bin/env python3
"""Chattahoochee Nature Center (chattnaturecenter.org) — Modern Events
Calendar (MEC) WordPress plugin scraper.

Tag: ECC_PP (shared) — CNC is in Roswell, GA, the same shared coverage
city as visit_roswell.py and roswell_365.py. Both East Cobb Connect and
Perimeter Post readers might drive there. Venue city / state are
hardcoded to avoid the 'address has no city' parsing trap (their
detail pages only render the street address, not city/state)."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from mec_events import run_mec_source  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_mec_source(
        site_url="https://chattnaturecenter.org",
        newsletter=os.environ.get("NEWSLETTER", "ECC_PP"),
        venue_name="Chattahoochee Nature Center",
        venue_city="roswell",
        venue_state="GA",
    ))
