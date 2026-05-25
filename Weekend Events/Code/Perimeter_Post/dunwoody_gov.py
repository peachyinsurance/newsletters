#!/usr/bin/env python3
"""dunwoodyga.gov city calendar scraper for Perimeter Post.
Thin wrapper around _shared/vision_internet.run_vision_internet_tiles.

Vision Internet CMS — see _shared/vision_internet.py for parsing
details. curl_cffi is required (Vision Internet TLS-fingerprints
plain requests)."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from vision_internet import run_vision_internet_tiles  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_vision_internet_tiles(
        source_url="https://www.dunwoodyga.gov/community/city-calendar/-toggle-allupcoming",
        newsletter=os.environ.get("NEWSLETTER", "Perimeter_Post"),
        default_city="dunwoody",
        location_prefix="Dunwoody — ",
        default_address="Dunwoody, GA",
    ))
