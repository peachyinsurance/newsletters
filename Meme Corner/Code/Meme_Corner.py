"""
Meme Corner scraper. Pulls top-of-the-week posts from a curated list of
subreddits, filters for SFW image memes, and writes candidates to the
Notion Meme Corner DB for editorial review.

Reddit access via Apify (was app-only OAuth). Reddit's OAuth endpoints
started throttling/blocking our GitHub Actions IP ranges even with
valid client_credentials, so we switched to the same Apify pattern we
use for Eventbrite. Apify's trudax/reddit-scraper-lite actor walks
each subreddit through residential proxies and returns post data.

Cost: ~$4 per 1000 posts at Apify list price. Our run pulls ~75 posts
(3 subs × 25 each), so ~$0.30 per scrape. Cheap.

Filters per sub:
  - over_18 == False        (SFW; also enforced by includeNSFW=False)
  - score >= MIN_SCORE
  - post_hint == "image" OR url ends in jpg/jpeg/png/gif/gifv
  - not removed
  - For r/Atlanta only: flair must match ALLOWED_FLAIRS

Env vars:
  APIFY_API_KEY            (replaces REDDIT_CLIENT_ID/SECRET)
  NOTION_API_KEY
  NOTION_MEMES_DB_ID
  NEWSLETTER  — East_Cobb_Connect | Perimeter_Post | Lewisville_Lake_Lookout | all
  MEME_DEBUG=1             (optional, dumps first Apify item's keys)
"""
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

APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "").strip()
APIFY_ACTOR_ID = "trudax~reddit-scraper-lite"
DEBUG = os.environ.get("MEME_DEBUG", "") == "1"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".webp")


def fetch_top(subreddit: str, _ignored=None,
              limit: int = PER_SUB_LIMIT,
              window: str = "week") -> list[dict]:
    """Pull top posts for one subreddit via Apify Reddit scraper.

    `_ignored` is a leftover token-shaped param from the old OAuth
    code path — kept so the signature doesn't break callers. Returns
    a list of dicts shaped like the old Reddit JSON `data` payloads
    (title, url, score, over_18, post_hint, permalink, ...) so the
    existing filter and save logic works unchanged.

    Empty list on Apify error or no posts. We rely on Apify's
    residential proxies to bypass Reddit's IP block on cloud egress."""
    if not APIFY_API_KEY:
        print(f"    ✗ APIFY_API_KEY not set in env")
        return []

    # `time` filter knob ('day' | 'week' | 'month' | 'year') — pass our
    # `window` through. `sort=top` is implicit because the startUrl
    # already includes /top/.
    payload = {
        "startUrls":      [{"url": f"https://www.reddit.com/r/{subreddit}/top/?t={window}"}],
        "skipComments":   True,
        "skipUserPosts":  True,
        "skipCommunity":  True,
        "maxItems":       limit,
        "maxPostCount":   limit,
        "includeNSFW":    False,
        "sort":           "top",
        "time":           window,
        "proxy":          {"useApifyProxy": True,
                           "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    try:
        r = requests.post(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items",
            headers={"Authorization": f"Bearer {APIFY_API_KEY}",
                     "Content-Type":  "application/json"},
            json=payload, timeout=600,
        )
    except Exception as e:
        print(f"    ✗ r/{subreddit} Apify error: {e}")
        return []
    if r.status_code not in (200, 201):
        print(f"    ✗ r/{subreddit} Apify HTTP {r.status_code}: {r.text[:200]}")
        return []
    items = r.json() or []
    print(f"    → {len(items)} item(s) from Apify")
    if DEBUG and items:
        print(f"    [DEBUG] first item keys: {sorted(items[0].keys())}")
        print(f"    [DEBUG] sample: {str(items[0])[:600]}")

    posts = [_normalize_apify_post(it, subreddit) for it in items]
    return [p for p in posts if p]


def _normalize_apify_post(item: dict, subreddit: str) -> dict | None:
    """Map an Apify Reddit item to the dict shape the rest of
    Meme_Corner.py expects (title / url / score / over_18 / post_hint /
    permalink / link_flair_text). Defensive on field names since the
    Apify actor's schema is undocumented — multiple key fallbacks per
    field. First real run with MEME_DEBUG=1 prints the actual keys for
    verification.

    Returns None for posts missing title or url (filtered out)."""
    if not isinstance(item, dict):
        return None

    title = (item.get("title") or item.get("name")
             or item.get("postTitle") or "").strip()

    # URL: the image / media URL we render as the meme. Falls back to
    # post URL if no image-shaped URL is present.
    url = (item.get("url") or item.get("mediaUrl") or item.get("imageUrl")
           or item.get("image") or item.get("thumbnail") or "")
    if isinstance(url, dict):
        url = url.get("url") or url.get("src") or ""

    if not title or not url:
        return None

    score = (item.get("score") or item.get("ups") or item.get("upvotes")
             or item.get("upVotes") or item.get("upvoteCount") or 0)
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 0

    nsfw = bool(item.get("over18") or item.get("over_18") or item.get("nsfw")
                or item.get("isNsfw") or item.get("isOver18"))
    is_video = bool(item.get("isVideo") or item.get("video")
                    or item.get("is_video"))

    permalink = (item.get("permalink") or item.get("url")
                 or item.get("postUrl") or "")
    # Reddit-API used to return permalinks as relative paths
    # (/r/foo/comments/xyz/...). Apify may return absolute URLs — strip
    # the host if so to match the legacy shape expected downstream.
    if permalink.startswith("http"):
        permalink = "/" + permalink.split("/", 3)[-1] if "/" in permalink else permalink

    flair = (item.get("linkFlairText") or item.get("flair")
             or item.get("flairText") or item.get("postFlair") or "")
    if isinstance(flair, dict):
        flair = flair.get("text") or flair.get("name") or ""

    # post_hint: 'image' tells the existing filter the URL is renderable
    # as an image. Apify might not set this; infer from extension.
    post_hint = item.get("postHint") or item.get("post_hint") or ""
    if not post_hint and url:
        bare = url.lower().split("?", 1)[0]
        if bare.endswith(IMAGE_EXTS):
            post_hint = "image"

    return {
        "title":              title,
        "url":                url,
        "score":              score,
        "over_18":            nsfw,
        "removed_by_category": item.get("removedBy") or item.get("removed_by_category"),
        "post_hint":          post_hint,
        "is_video":           is_video,
        "permalink":          permalink,
        "link_flair_text":    flair,
        "subreddit":          item.get("subreddit") or subreddit,
    }


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


def scrape_for_newsletter(newsletter_name: str) -> int:
    print(f"\n{'=' * 60}")
    print(f"  Scraping memes for {newsletter_name}")
    print(f"{'=' * 60}")
    if not NOTION_MEMES_DB_ID:
        print("  ⚠ NOTION_MEMES_DB_ID is empty — saves will be skipped")
    if not APIFY_API_KEY:
        print("  ✗ APIFY_API_KEY not set — no scrapes will succeed")
        return 0

    already_saved = existing_permalinks(newsletter_name)
    print(f"  {len(already_saved)} existing permalinks to skip")

    saved_total = 0
    for sub_url_name, sub_label in SUBREDDITS:
        print(f"\n  → r/{sub_url_name}")
        posts = fetch_top(sub_url_name)
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
    if NEWSLETTER.lower() == "all":
        targets = ["East_Cobb_Connect", "Perimeter_Post", "Lewisville_Lake_Lookout"]
    else:
        targets = [NEWSLETTER]
    for nl in targets:
        scrape_for_newsletter(nl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
