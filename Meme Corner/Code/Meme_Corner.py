"""
Meme Corner scraper. Pulls top-of-the-week posts from a curated list of
subreddits, filters for SFW image memes, and writes candidates to the
Notion Meme Corner DB for editorial review.

Reddit API access notes:
  - PREFERRED: app-only OAuth (oauth.reddit.com). Needs
    REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET env vars (register a
    "script" app at https://www.reddit.com/prefs/apps — no user
    account required for client_credentials grant). Gives ~600 req/
    10min and works from cloud IPs.
  - FALLBACK: unauthenticated www.reddit.com endpoints. 60 req/min
    per IP and Reddit aggressively blocks cloud IPs (GitHub Actions,
    AWS, GCP, etc.) with 403, so this path is for local dev only.
  - Reddit requires a descriptive User-Agent; generic ones (Mozilla,
    python-requests) get 403'd. We use "peachy-newsletter-bot/1.0".
  - Empty payloads on hot subs occasionally; we retry once.

Filters per sub:
  - over_18 == False        (SFW)
  - score >= MIN_SCORE
  - post_hint == "image" OR url ends in jpg/jpeg/png/gif/gifv
  - not removed (removed_by_category is None)
  - For r/Atlanta only: flair must match ALLOWED_FLAIRS (not every
    Atlanta post is a meme, so we lean on flair for that one).

Env vars:
  NOTION_API_KEY
  NOTION_MEMES_DB_ID
  NEWSLETTER  — East_Cobb_Connect | Perimeter_Post | Lewisville_Lake_Lookout | all
"""
import base64
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.append(str(Path(__file__).parent.parent.parent
                    / "NewsletterCreation" / "Code"))
from notion_helper import (  # noqa: E402
    create_page,
    query_database,
    NOTION_MEMES_DB_ID,
)

NEWSLETTER = os.environ.get("NEWSLETTER", "East_Cobb_Connect")

# Subreddits to pull from. Tuple shape: (name_for_url, label_for_notion).
# r/Atlanta is the "topical" pull and is gated by flair below.
SUBREDDITS: list[tuple[str, str]] = [
    ("Atlanta",        "Atlanta"),
    ("memes",          "memes"),
    ("wholesomememes", "wholesomememes"),
]

# Atlanta posts aren't all memes — keep only ones the community tagged
# as humor/meme via post flair.
ALLOWED_ATLANTA_FLAIRS = {"Meme", "Humor", "Funny", "Photo Friday"}

# Quality / volume knobs
MIN_SCORE = 500       # ≥ N upvotes
PER_SUB_LIMIT = 25    # how many top-of-week to ask for
ACCEPT_PER_SUB = 6    # max candidates we'll save per sub per run

USER_AGENT = "peachy-newsletter-bot/1.0 (by /u/peachy-newsletters)"

REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".webp")


def get_oauth_token() -> str | None:
    """Fetch an app-only OAuth token (client_credentials grant). Returns
    the bearer token on success, or None if credentials aren't set or
    Reddit refuses. Token TTL is ~1 hour, plenty for one run."""
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        return None
    auth = base64.b64encode(
        f"{REDDIT_CLIENT_ID}:{REDDIT_CLIENT_SECRET}".encode()
    ).decode()
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            headers={
                "Authorization": f"Basic {auth}",
                "User-Agent":    USER_AGENT,
            },
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  ⚠ Reddit OAuth failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        token = (r.json() or {}).get("access_token")
        if token:
            print("  ✓ Got Reddit OAuth token (app-only)")
        return token
    except Exception as e:
        print(f"  ⚠ Reddit OAuth error: {e}")
        return None


def fetch_top(subreddit: str, token: str | None,
              limit: int = PER_SUB_LIMIT,
              window: str = "week") -> list[dict]:
    """Fetch the top posts from a sub for the given time window. Uses
    oauth.reddit.com when a token is present (works from cloud IPs);
    falls back to www.reddit.com which routinely returns 403 from
    GitHub Actions / AWS / GCP IP ranges. Returns the raw `data`
    payload of each child post, or [] on failure. Retries once on
    empty payload (hot subs sometimes return partial responses)."""
    if token:
        url = f"https://oauth.reddit.com/r/{subreddit}/top"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent":    USER_AGENT,
        }
    else:
        url = f"https://www.reddit.com/r/{subreddit}/top.json"
        headers = {"User-Agent": USER_AGENT}
    params = {"t": window, "limit": limit}

    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code != 200:
                print(f"    ✗ r/{subreddit} HTTP {r.status_code}")
                time.sleep(3)
                continue
            payload = r.json()
            children = (payload.get("data") or {}).get("children") or []
            posts = [c.get("data", {}) for c in children if c.get("data")]
            if posts:
                return posts
            print(f"    · r/{subreddit} empty payload, retrying…")
            time.sleep(3)
        except Exception as e:
            print(f"    ✗ r/{subreddit} fetch error: {e}")
            time.sleep(3)
    return []


def is_image_post(post: dict) -> bool:
    """Image hint OR the URL ends in a known image extension. Excludes
    Reddit-hosted videos (those have is_video=True even when post_hint
    isn't 'hosted:video' in some old listings)."""
    if post.get("is_video"):
        return False
    if post.get("post_hint") == "image":
        return True
    url = (post.get("url") or "").lower().split("?", 1)[0]
    return url.endswith(IMAGE_EXTS)


def passes_filters(post: dict, subreddit: str) -> tuple[bool, str]:
    """Return (ok, reason). reason is just for log noise on rejects."""
    if post.get("over_18"):
        return False, "nsfw"
    if post.get("removed_by_category"):
        return False, f"removed:{post.get('removed_by_category')}"
    if (post.get("score") or 0) < MIN_SCORE:
        return False, f"score<{MIN_SCORE}"
    if not is_image_post(post):
        return False, "not-image"
    if subreddit.lower() == "atlanta":
        flair = (post.get("link_flair_text") or "").strip()
        if flair not in ALLOWED_ATLANTA_FLAIRS:
            return False, f"flair={flair!r}"
    return True, ""


def existing_permalinks(newsletter_name: str, lookback_days: int = 60) -> set:
    """Pull permalinks already in the Meme DB for this newsletter so we
    don't re-save the same meme week over week. Lookback caps the query
    cost — anything older than ~60 days is safely re-savable."""
    if not NOTION_MEMES_DB_ID:
        return set()
    try:
        pages = query_database(NOTION_MEMES_DB_ID, filters={
            "property": "Newsletter",
            "select":   {"equals": newsletter_name},
        })
    except Exception as e:
        print(f"  ⚠ couldn't query existing memes: {e}")
        return set()
    out: set = set()
    for p in pages:
        url = (p.get("properties", {}).get("Reddit Permalink", {}).get("url") or "").strip()
        if url:
            out.add(url)
    return out


def save_candidate(newsletter_name: str, post: dict, sub_label: str) -> bool:
    """Create one row in the Meme Corner DB. Returns True on success."""
    permalink = "https://reddit.com" + (post.get("permalink") or "")
    title = (post.get("title") or "").strip()[:200]
    image_url = post.get("url") or ""
    author = (post.get("author") or "")[:80]
    score = int(post.get("score") or 0)

    properties = {
        "Name":             {"title": [{"text": {"content": title or "(untitled meme)"}}]},
        "Newsletter":       {"select": {"name": newsletter_name}},
        "Subreddit":        {"select": {"name": sub_label}},
        "Image URL":        {"url": image_url or None},
        "Reddit Permalink": {"url": permalink},
        "Reddit Author":    {"rich_text": [{"text": {"content": author}}]},
        "Score":            {"number": score},
        "Caption":          {"rich_text": [{"text": {"content": title}}]},
        "Status":           {"select": {"name": "pending"}},
        "Date Generated":   {"date": {"start": datetime.today().date().isoformat()}},
    }
    try:
        create_page(NOTION_MEMES_DB_ID, properties)
        return True
    except Exception as e:
        print(f"    ✗ failed to save '{title[:60]}': {e}")
        return False


def scrape_for_newsletter(newsletter_name: str, token: str | None) -> int:
    print(f"\n{'=' * 60}")
    print(f"  Scraping memes for {newsletter_name}")
    print(f"{'=' * 60}")
    if not NOTION_MEMES_DB_ID:
        print("  ⚠ NOTION_MEMES_DB_ID is empty — saves will be skipped")
    if not token:
        print("  ⚠ No Reddit OAuth token — falling back to unauthenticated "
              "endpoints (likely to 403 from cloud IPs)")

    already_saved = existing_permalinks(newsletter_name)
    print(f"  {len(already_saved)} existing permalinks to skip")

    saved_total = 0
    for sub_url_name, sub_label in SUBREDDITS:
        print(f"\n  → r/{sub_url_name}")
        posts = fetch_top(sub_url_name, token)
        if not posts:
            print(f"    · no posts returned")
            continue

        accepted = 0
        for post in posts:
            if accepted >= ACCEPT_PER_SUB:
                break
            permalink = "https://reddit.com" + (post.get("permalink") or "")
            if permalink in already_saved:
                continue
            ok, reason = passes_filters(post, sub_url_name)
            if not ok:
                continue
            if save_candidate(newsletter_name, post, sub_label):
                accepted += 1
                already_saved.add(permalink)
                title = (post.get("title") or "")[:60]
                print(f"    ✓ saved: {title} (score {post.get('score')})")
        print(f"    Accepted {accepted}/{len(posts)} from r/{sub_url_name}")
        saved_total += accepted

    print(f"\n  ✓ Total saved: {saved_total}")
    return saved_total


def main() -> int:
    token = get_oauth_token()
    if NEWSLETTER.lower() == "all":
        targets = ["East_Cobb_Connect", "Perimeter_Post", "Lewisville_Lake_Lookout"]
    else:
        targets = [NEWSLETTER]
    for nl in targets:
        scrape_for_newsletter(nl, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
