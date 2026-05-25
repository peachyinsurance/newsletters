#!/usr/bin/env python3
"""Eventbrite via Apify for Perimeter Post.
Thin wrapper around _shared/eventbrite_apify.run_eventbrite.

Anchor city is Dunwoody (geographically central to the PP coverage
belt); allow-list spans Sandy Springs / Dunwoody / Chamblee /
Brookhaven / Roswell. Eventbrite's geo filter is loose so a Dunwoody
search returns events from the whole I-285 / I-85 corridor into all
five cities."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from eventbrite_apify import run_eventbrite  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_eventbrite(
        newsletter_tag="Perimeter_Post",
        anchor_city="dunwoody",
        allowed_cities={
            "sandy springs",
            "dunwoody",
            "chamblee",
            "brookhaven",
            "roswell",
        },
        required_state="GA",
    ))
