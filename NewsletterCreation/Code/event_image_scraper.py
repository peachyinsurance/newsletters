"""
Shared og:image / JSON-LD fallback image scraper for event sources.

Used by Weekend Events scrapers (Cobb County, Sandy Springs,
travelcobb/visitmariettaga/kennesaw) when their structured-data path
returns no image_url. Also used by Free Events directly.

The Free_Events.py original was per-page HEAD-validated, which is too
slow when run across 30+ events at scrape time. This shared version
accepts a `validate=False` flag (default) that skips HEAD validation
for speed — the trade-off is occasionally surfacing a broken-ish URL,
which is no worse than the no-image fallback we'd otherwise ship.

A `backfill_images(events)` helper runs the per-page fetch
concurrently across a list of event dicts via a ThreadPoolExecutor.

Also exposes `is_cancelled_event(title, description)` — a content-based
filter the weekend-events scrapers and the Weekend Planner pool fetcher
both call to drop events whose title/description marks them cancelled.
Lives here because it's the same shared-utility module the scrapers
already sys.path-import for image backfill — no point introducing a new
module for one 4-line helper.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse

import requests

USER_AGENT = "Mozilla/5.0 (newsletter-automation)"

# Word-boundary match on the -ed / -ation variants only. "cancel" by
# itself (no suffix) is intentionally NOT matched, since legitimate
# event names like "Cancel Culture Comedy Night" use it as a noun.
# Cancellations always carry the -ed form in practice ("CANCELLED",
# "[Cancelled]", "Cancelled - Storytime", etc.).
_CANCEL_RE = re.compile(
    r"\b(?:cancelled|canceled|cancellation|cancelation)\b",
    re.IGNORECASE,
)


def is_cancelled_event(title: str, description: str = "") -> bool:
    """True if the event title or description marks it as cancelled.
    Used by both the weekend-events scrapers (to skip pre-save) and the
    Weekend Planner pool fetcher (to skip pre-Claude, catching legacy
    rows already in Notion)."""
    return bool(_CANCEL_RE.search(f"{title or ''} | {description or ''}"))


# Adult / NSFW content patterns we never want in a family newsletter.
# Word-boundary anchored on terms that are unambiguous in event-listing
# context (a "strip club night" or "burlesque" show). Patterns avoid
# false positives on benign uses: "stripped" (in "stripped-down acoustic"),
# "naked" (only matches "naked party" / "naked yoga night"-style phrases
# isn't worth chasing — we'd rather drop a legit naked-yoga event than
# ship a stripper show).
_INAPPROPRIATE_RE = re.compile(
    r"\b(?:"
    r"strip[- ]?club|stripper|strippers|stripping[- ]?(?:show|party|night)|"
    r"exotic[- ]?dancer|gentlemen'?s?[- ]?club|gentleman'?s?[- ]?club|"
    r"burlesque|"
    r"nude|topless|"
    r"fetish|kink|kinky|bdsm|"
    r"swingers?|swinger[- ]?(?:party|club|night)|sex[- ]?party|"
    r"orgy|orgies|"
    r"adult[- ]?(?:only|entertainment|event)|adults[- ]?only|"
    r"xxx|x-rated|"
    r"playboy[- ]?(?:party|night|club)|"
    r"lingerie[- ]?(?:party|show|night)|"
    r"pole[- ]?dance(?:r|ing)?|"
    r"escort[- ]?(?:service|agency)|sugar[- ]?(?:baby|daddy)|"
    r"hookup|hookups|"
    r"hookah"            # tobacco-product events; user-requested exclusion
    r")\b",
    re.IGNORECASE,
)


def is_inappropriate_event(title: str, description: str = "",
                           venue: str = "") -> bool:
    """True if the event reads as adult / NSFW for a family newsletter.
    Scans title + description + venue against a curated blocklist of
    unambiguous markers (strip club, fetish, etc.). Pole dance fitness
    classes get caught — that's an acceptable false positive vs. the
    risk of shipping an explicit event.

    Called from the weekend-events scrapers (pre-save) and the Weekend
    Planner pool fetcher (pre-Claude). Claude no longer filters events,
    so this filter has to be airtight at the data layer."""
    haystack = f"{title or ''} | {description or ''} | {venue or ''}"
    return bool(_INAPPROPRIATE_RE.search(haystack))


# Senior-citizen / older-adult events. Excluded at the user's request — the
# newsletters skew toward families and younger readers. The keyword list
# deliberately AVOIDS a bare "senior"/"seniors" match, which would wrongly
# catch high-school seniors ("senior prom", "senior night", "senior year").
# It fires only on an unambiguous senior-citizen qualifier (senior citizen /
# center, older adult, AARP, SilverSneakers, active adult), a "seniors +
# activity" phrase, or an explicit 55+/60+/65+ age cue. The standalone "55+"
# patterns omit "50+" because festivals routinely advertise "50+ vendors".
_SENIOR_RE = re.compile(
    r"(?:"
    r"\bsenior[- ]?citizens?\b|"
    r"\bsenior[- ]?cent(?:er|re)\b|"
    r"\bolder[- ]?adults?\b|"
    r"\baarp\b|"
    r"\bsilver[- ]?sneakers\b|"
    r"\bactive[- ]?adults?\b|"
    r"\bseniors?[- ]?(?:only|bingo|luncheon|lunch|social|socials|club|day|"
    r"program|programs|group|games?|yoga|fitness|exercise|dance|expo|fair|"
    r"series|meetup|coffee|breakfast|brunch|trip|trips)\b|"
    r"\bages?\s?(?:55|60|62|65)\b|"
    # Bare "55+/60+" as an age cue, but NOT when it's a count of things a
    # festival advertises ("55+ vendors", "60+ artists/booths/breweries").
    r"\b(?:55|60|62|65)\s?\+(?!\s*(?:vendors?|artists?|exhibitors?|booths?|"
    r"breweries|wineries|restaurants?|stores?|shops?|items?|attendees|"
    r"participants|guests|acts?|bands?|films?|movies?|games?|rides?))|"
    r"\b(?:50|55|60|62|65)[- ]?(?:plus\b|and[- ](?:older|over|up)\b)|"
    r"\b(?:50|55|60|62|65)[- ]?years?[- ](?:and[- ])?(?:older|over|up)\b"
    r")",
    re.IGNORECASE,
)


def is_senior_event(title: str, description: str = "",
                    age_tags: str = "") -> bool:
    """True if the event targets senior citizens / older adults and should be
    excluded from the family-oriented newsletters.

    Two independent signals — either one triggers exclusion:
      • `age_tags`: a free-text dump of whatever structured age labels the
        source exposes (e.g. Cobb County's `eventAge` -> "Seniors (ages 60+)").
        If "senior" appears here, the source itself tagged it senior — the
        most reliable signal, so we honor it even when the event is ALSO
        tagged for adults (an AARP driving course is still a senior event).
      • title + description keyword scan (`_SENIOR_RE`) for the many sources
        that expose no structured age data, so the exclusion still applies to
        EVERY scraper.

    Called at the data layer (notion_save.save_event for all scrapers, plus
    the Cobb scraper's pre-save and the Weekend Planner pool fetcher) so
    senior events never reach the newsletter regardless of origin."""
    if age_tags and re.search(r"\bseniors?\b", age_tags, re.IGNORECASE):
        return True
    haystack = f"{title or ''} | {description or ''}"
    return bool(_SENIOR_RE.search(haystack))


# Affiliate CDNs / generic icons / tracking pixels we never want to
# pick up as a hero image.
SKIP_TOKENS = (
    "logo", "favicon", "sprite", "icon-", "/icons/",
    "placeholder", "spacer", "tracker", "pixel.gif",
    "1x1", "blank.gif", "transparent.png",
    "grouponcdn.com", "groupon.com/image",
    "jdoqocy.com", "dpbolvw.net", "tkqlhce.com",
    "anrdoezrs.net", "kqzyfj.com",
    "amazon-adsystem", "doubleclick",
    "googlesyndication", "googleadservices",
    "rakuten.com/img", "shareasale.com/image",
    "impactradius", "linksynergy.com",
)

# Marketplace / aggregator hosts where the root domain's og:image is
# almost never the actual event (it's the marketplace's brand banner).
# Skip the root-fallback step on these.
MARKETPLACE_HOSTS = (
    "eventbrite.com", "ticketmaster.com", "axs.com", "stubhub.com",
    "seatgeek.com", "meetup.com", "allevents.in", "facebook.com",
    "ticketweb.com", "bigtickets.com", "etix.com", "vivenu.com",
    "tixr.com", "freshtix.com",
)


def _absolutize(url: str, base_url: str) -> str:
    """Resolve //, relative, and absolute URLs against the page's base URL."""
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{url}"
    return url


def _image_looks_real(url: str) -> bool:
    """HEAD/GET-validate that a URL actually returns an image. Slow."""
    if not url:
        return False
    try:
        r = requests.get(
            url, timeout=8, allow_redirects=True, stream=True,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return False
        ct = (r.headers.get("Content-Type") or "").lower()
        if not ct.startswith("image/"):
            return False
        size = int(r.headers.get("Content-Length") or 0)
        if size == 0:
            chunk = next(r.iter_content(8192), b"")
            size = len(chunk)
        return size >= 5_000
    except Exception:
        return False


def fetch_event_image(source_url: str,
                      *, validate: bool = False,
                      allow_root_fallback: bool = True) -> str:
    """Scrape a hero image URL from `source_url`. Tries (in order):
       1. og:image / twitter:image / image_src meta tags
       2. JSON-LD schema.org Event.image
       3. First reasonably-large <img> in body (width >= 400)
       4. If `allow_root_fallback` and nothing found, retry once
          against the site's root URL (skipped for marketplace hosts).

    `validate=True` re-checks each candidate via HEAD/GET (content-type
    is image/* AND size >= 5KB). Slow — only enable for one-off picks.
    Default False is the fast path used by bulk scrapers.

    Returns "" if nothing usable is found.
    """
    if not source_url:
        return ""

    def _root_fallback(reason: str) -> str:
        if not allow_root_fallback:
            return ""
        try:
            parsed = urlparse(source_url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if host and not any(host == m or host.endswith("." + m) for m in MARKETPLACE_HOSTS):
                root = urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
                if root and root != source_url:
                    return fetch_event_image(
                        root, validate=validate, allow_root_fallback=False,
                    )
        except Exception:
            pass
        return ""

    try:
        r = requests.get(
            source_url, timeout=10,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.text:
            return _root_fallback(f"HTTP {r.status_code}")
        html = r.text
    except Exception:
        return _root_fallback("fetch error")

    candidates: list[str] = []

    # 1. Meta tags — concentrated in the <head>, capped for speed.
    # Match both `property="og:image"` (OpenGraph standard) AND the
    # non-standard `name="og:image"` form. Cobb County's Next.js site
    # uses the `name=` variant — without this pattern, every cobbcounty.gov
    # event's clean canonical image gets missed and we fall back to the
    # Next.js image-optimization proxy URL, which validates inconsistently.
    head = html[:200_000]
    for pat in (
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, head, re.IGNORECASE)
        if m:
            candidates.append(m.group(1).strip())

    # 2. JSON-LD: schema.org Event/Place objects often carry `image`.
    for ld_match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        try:
            blob = json.loads(ld_match.group(1).strip())
        except Exception:
            continue
        items = blob if isinstance(blob, list) else [blob]
        for item in items:
            if not isinstance(item, dict):
                continue
            img = item.get("image")
            if isinstance(img, str):
                candidates.append(img)
            elif isinstance(img, list):
                for x in img:
                    if isinstance(x, str):
                        candidates.append(x)
                    elif isinstance(x, dict) and isinstance(x.get("url"), str):
                        candidates.append(x["url"])
            elif isinstance(img, dict) and isinstance(img.get("url"), str):
                candidates.append(img["url"])

    # 3. First reasonably-large <img> tag (width >= 400) — last resort.
    for pat in (
        r'<img[^>]+width=["\']?(\d+)["\']?[^>]+src=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]+width=["\']?(\d+)["\']?',
    ):
        for m in re.finditer(pat, html, re.IGNORECASE):
            groups = m.groups()
            if pat.startswith(r'<img[^>]+width'):
                w_str, url_str = groups[0], groups[1]
            else:
                url_str, w_str = groups[0], groups[1]
            try:
                if int(w_str) >= 400:
                    candidates.append(url_str)
            except ValueError:
                continue

    # Walk candidates in priority order; return the first one that
    # isn't on the skip list. If validate is on, also HEAD-check.
    seen: set = set()
    for url in candidates:
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if url.startswith("data:"):
            continue
        url = _absolutize(url, source_url)
        ul = url.lower()
        if any(skip in ul for skip in SKIP_TOKENS):
            continue
        if validate and not _image_looks_real(url):
            continue
        return url

    return _root_fallback("no usable image")


def backfill_images(events: list[dict],
                    *, source_url_key: str = "source_url",
                    image_url_key: str = "image_url",
                    max_workers: int = 6) -> int:
    """For each event in the list whose `image_url_key` field is empty,
    fetch a fallback image from its source page. Runs concurrently across
    a thread pool to keep scrape latency manageable (typical: 30 events
    × ~5s sequential → ~25s concurrent at 6 workers). Returns the count
    of events that got a new image."""
    needs = [(i, e) for i, e in enumerate(events)
             if not (e.get(image_url_key) or "")]
    if not needs:
        return 0

    def _one(idx_ev):
        idx, ev = idx_ev
        url = ev.get(source_url_key) or ev.get("url") or ""
        if not url:
            return idx, ""
        return idx, fetch_event_image(url, validate=False)

    filled = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for idx, img in pool.map(_one, needs):
            if img:
                events[idx][image_url_key] = img
                filled += 1
    return filled
