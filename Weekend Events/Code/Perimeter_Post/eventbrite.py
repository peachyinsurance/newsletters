#!/usr/bin/env python3
"""Eventbrite via Apify for Perimeter Post.
Thin wrapper around _shared/eventbrite_apify.run_eventbrite.

Anchor city is Sandy Springs (PP's main coverage area); allow-list
spans the broader Perimeter / North-DeKalb belt — Roswell, Dunwoody,
Chamblee, Brookhaven. Eventbrite's geo filter is loose so a Sandy
Springs search returns events all the way up I-285 into these cities."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from eventbrite_apify import run_eventbrite  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_eventbrite(
        newsletter_tag="Perimeter_Post",
        anchor_city="sandy-springs",
        allowed_cities={
            "sandy springs",
            "dunwoody",
            "chamblee",
            "brookhaven",
            "roswell",
        },
    ))
