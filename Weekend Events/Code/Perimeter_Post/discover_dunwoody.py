#!/usr/bin/env python3
"""discoverdunwoody.com Simpleview Tempest scraper for Perimeter Post.
Thin wrapper around _shared/simpleview_tempest.run_simpleview_tempest."""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
from simpleview_tempest import run_simpleview_tempest  # noqa: E402

if __name__ == "__main__":
    sys.exit(run_simpleview_tempest(
        sitemap_url="https://www.discoverdunwoody.com/sitemaps-1-event-default-1-sitemap.xml",
        newsletter=os.environ.get("NEWSLETTER", "Perimeter_Post"),
    ))
