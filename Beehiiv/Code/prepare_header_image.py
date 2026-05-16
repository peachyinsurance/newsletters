#!/usr/bin/env python3
"""
Pre-Send_To_Beehiiv hook: build the Canva-style header thumbnail for the
featured event and write it to `Beehiiv/Code/output/`. The
send_to_beehiiv.yml workflow's "Publish header to gh-pages" step then
copies it onto the gh-pages branch so the URL referenced by the Beehiiv
post (`gifs/Newsletter_Header_image_<NEWSLETTER>.png`) is live by the
time Beehiiv renders the draft.

Why a separate script: Beehiiv's media-upload endpoint is plan-locked
(404), so we host generated images on gh-pages — but Send_To_Beehiiv
only references the URL; the actual file has to land on gh-pages before
Beehiiv fetches it at draft-render time. Running this step first solves
the chicken-and-egg.
"""
import os
import sys
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from assemble_newsletter_page import get_featured_event  # noqa: E402
from header_image_maker import build_header_image          # noqa: E402

NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")
OUT_DIR    = Path(__file__).parent / "output"


def main() -> int:
    event = get_featured_event(NEWSLETTER)
    if not event:
        print(f"  ⓘ No featured event for {NEWSLETTER} — skipping header")
        return 0

    # Fast path: if the review-app image picker already generated and
    # published a composite (Header Image URL field present in Notion),
    # we don't need to rebuild here — that PNG is already on gh-pages.
    if event.get("header_image_url"):
        print(f"  ✓ Header already exists in Notion: {event['header_image_url']}")
        print(f"    (Pre-built by review-app image picker — skipping rebuild)")
        return 0

    title = event.get("event_name", "")
    photo = event.get("image_url") or event.get("photo") or ""
    print(f"  Building header for: {title!r} (legacy fallback path)")
    print(f"  Photo URL: {photo[:80] if photo else '(none)'}")

    data = build_header_image(title=title, photo_url=photo or None)
    if not data:
        print("  ✗ Header generation returned empty bytes")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"Newsletter_Header_image_{NEWSLETTER}.png"
    out   = OUT_DIR / fname
    out.write_bytes(data)
    print(f"  ✓ Wrote {out} ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
