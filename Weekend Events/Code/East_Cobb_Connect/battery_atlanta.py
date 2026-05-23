#!/usr/bin/env python3
"""The Battery Atlanta Tribe Events scraper for East Cobb Connect.
Thin wrapper around _shared/tribe_events.run_tribe_source.

The Battery is in Cumberland (close to East Cobb and Sandy Springs).
Currently tagged East_Cobb_Connect only — flip the NEWSLETTER env
or move this file to _shared/ if Perimeter Post should also get it."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from tribe_events import run_tribe_source  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_tribe_source(
        source_url="https://batteryatl.com/events-calendar/",
        newsletter=os.environ.get("NEWSLETTER", "East_Cobb_Connect"),
    ))
