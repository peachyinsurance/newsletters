#!/usr/bin/env python3
"""visitlewisville.com events scraper for Lewisville Lake Lookout.
Thin wrapper around _shared/vision_internet.run_vision_internet_tiles.

Vision Internet CMS — same `vi-events-tiles-item` markup as
dunwoodyga.gov. curl_cffi handles the TLS-fingerprint block."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from vision_internet import run_vision_internet_tiles  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_vision_internet_tiles(
        source_url="https://www.visitlewisville.com/events",
        newsletter=os.environ.get("NEWSLETTER", "Lewisville_Lake_Lookout"),
        default_city="lewisville",
        location_prefix="Lewisville — ",
        default_address="Lewisville, TX",
    ))
