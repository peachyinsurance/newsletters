#!/usr/bin/env python3
"""
Pull Beehiiv click data for the most recently sent post, filter to poll
vote-tracking URLs (those with `?vote=<slug>`), aggregate by option, pair
with the question + option labels from Notion, and write a JSON summary
to `review-app/public/poll-counts.json` for the landing page to read.

Run on a schedule (every 30 min) via .github/workflows/update_poll_counts.yml.

Output JSON shape:
{
  "updated_at": "2026-05-09T14:30:00Z",
  "post_title": "...",
  "post_id":    "post_...",
  "question":   "What's your weekend plan?",
  "options": [
    {"slug": "tacos",       "label": "Tacos",       "count": 23},
    {"slug": "garden-tour", "label": "Garden tour", "count": 19},
    ...
  ],
  "total_votes": 50
}
"""
import os
import sys
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

# Reuse the existing Notion helper for the question + option labels
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from assemble_newsletter_page import get_latest_poll  # noqa: E402

BEEHIIV_API_KEY = os.environ["BEEHIIV_API_KEY"]
BEEHIIV_PUB_ID  = os.environ["BEEHIIV_ECC_PUBLICATION_ID"]
NEWSLETTER_NAME = "East_Cobb_Connect"

OUTPUT_PATH = (Path(__file__).parent.parent.parent
               / "review-app" / "public" / "poll-counts.json")


def get_latest_sent_post() -> dict | None:
    """Return the most recent non-draft post from Beehiiv, or None."""
    r = requests.get(
        f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/posts",
        headers={"Authorization": f"Bearer {BEEHIIV_API_KEY}"},
        params={"limit": "10"},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  Posts list error {r.status_code}: {r.text[:200]}")
        return None
    for p in r.json().get("data", []):
        if p.get("status") != "draft":
            return p
    return None


def get_post_clicks(post_id: str) -> list[dict]:
    """Fetch post detail with stats expanded; return click records list."""
    r = requests.get(
        f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/posts/{post_id}",
        headers={"Authorization": f"Bearer {BEEHIIV_API_KEY}"},
        params={"expand[]": ["stats"]},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"  Post detail error {r.status_code}: {r.text[:200]}")
        return []
    stats = (r.json().get("data") or {}).get("stats", {}) or {}
    return stats.get("clicks") or []


def aggregate_vote_counts(clicks: list[dict]) -> dict[str, int]:
    """Sum click counts per `?vote=<slug>` value."""
    counts: dict[str, int] = defaultdict(int)
    for c in clicks:
        if not isinstance(c, dict):
            continue
        url = c.get("url") or c.get("link") or ""
        if "?vote=" not in url and "&vote=" not in url:
            continue
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:
            continue
        vote_slug = (qs.get("vote") or [""])[0]
        if not vote_slug:
            continue
        n = c.get("clicks") or c.get("total_clicks") or c.get("count") or 1
        try:
            counts[vote_slug] += int(n)
        except (TypeError, ValueError):
            counts[vote_slug] += 1
    return dict(counts)


def main():
    print(f"=== update_poll_counts ({datetime.now(timezone.utc).isoformat()}) ===")

    post = get_latest_sent_post()
    if not post:
        print("  No sent post found in last 10. Writing empty result.")
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps({
            "updated_at":  datetime.now(timezone.utc).isoformat(),
            "post_title":  "",
            "post_id":     "",
            "question":    "",
            "options":     [],
            "total_votes": 0,
        }, indent=2))
        return

    print(f"  Latest sent post: {(post.get('title') or '')[:80]} ({post['id']})")
    clicks = get_post_clicks(post["id"])
    print(f"  Total click records: {len(clicks)}")

    vote_counts = aggregate_vote_counts(clicks)
    print(f"  Unique poll slugs found: {len(vote_counts)}")
    for slug, count in sorted(vote_counts.items(), key=lambda x: -x[1]):
        print(f"    {slug}: {count}")

    # Pull question + labels from Notion (so the landing page shows them in the
    # exact form the editor wrote, not the slug-mangled form).
    poll = get_latest_poll(NEWSLETTER_NAME)
    options: list[dict] = []
    question = ""
    if poll:
        question = poll.get("question", "")
        for label in (poll.get("options") or []):
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower().strip()).strip("-")
            options.append({
                "slug":  slug,
                "label": label,
                "count": vote_counts.get(slug, 0),
            })
    else:
        # Notion has no current poll → just expose what Beehiiv saw
        for slug, count in vote_counts.items():
            options.append({
                "slug":  slug,
                "label": slug.replace("-", " ").title(),
                "count": count,
            })

    total = sum(o["count"] for o in options)
    output = {
        "updated_at":  datetime.now(timezone.utc).isoformat(),
        "post_title":  post.get("title", ""),
        "post_id":     post.get("id", ""),
        "question":    question,
        "options":     options,
        "total_votes": total,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"  ✓ Wrote {OUTPUT_PATH}")
    print(f"  Total votes: {total}")


if __name__ == "__main__":
    main()
