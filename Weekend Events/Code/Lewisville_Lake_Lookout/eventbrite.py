#!/usr/bin/env python3
"""Eventbrite via Apify for Lewisville Lake Lookout.
Thin wrapper around _shared/eventbrite_apify.run_eventbrite.

Not yet wired into a workflow — flip on when LLL launches: add a step
in weekend_events.yml that runs this file."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from eventbrite_apify import run_eventbrite  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_eventbrite(
        newsletter_tag="Lewisville_Lake_Lookout",
        anchor_city="lewisville",
        allowed_cities={
            "lewisville", "flower mound", "highland village",
            "lake dallas", "little elm", "the colony",
        },
        required_state="TX",
    ))
