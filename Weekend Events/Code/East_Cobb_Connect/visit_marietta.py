#!/usr/bin/env python3
"""visitmariettaga.com Tribe Events scraper for East Cobb Connect.
Thin wrapper around _shared/tribe_events.run_tribe_source."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from tribe_events import run_tribe_source  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_tribe_source(
        source_url="https://visitmariettaga.com/events/",
        newsletter=os.environ.get("NEWSLETTER", "East_Cobb_Connect"),
    ))
