#!/usr/bin/env python3
"""visitroswellga.com Simpleview Tempest scraper.
Thin wrapper around _shared/simpleview_tempest.run_simpleview_tempest.

Tag: ECC_PP (shared) — Roswell events appear in both East Cobb
Connect and Perimeter Post. Roswell sits between the two coverage
areas and either newsletter's readers might drive there."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from simpleview_tempest import run_simpleview_tempest  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_simpleview_tempest(
        sitemap_url="https://www.visitroswellga.com/sitemaps-1-event-default-1-sitemap.xml",
        newsletter=os.environ.get("NEWSLETTER", "ECC_PP"),
    ))
