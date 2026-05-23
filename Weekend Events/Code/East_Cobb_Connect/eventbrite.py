#!/usr/bin/env python3
"""Eventbrite via Apify for East Cobb Connect.
Thin wrapper around _shared/eventbrite_apify.run_eventbrite."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from eventbrite_apify import run_eventbrite  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_eventbrite(
        newsletter_tag="East_Cobb_Connect",
        anchor_city="marietta",
        allowed_cities={"marietta", "east cobb"},
    ))
