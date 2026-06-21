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
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

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


def _looks_like_image_bytes(chunk: bytes) -> bool:
    """True if `chunk` begins with a known image magic number (JPEG/PNG/GIF/
    WebP). Eventbrite's cdn.evbuc.com serves real images as
    `binary/octet-stream`, so a Content-Type check alone wrongly rejects them —
    sniffing the bytes recovers those (and any other mislabeled CDN image)."""
    if not chunk:
        return False
    return (chunk[:3] == b"\xff\xd8\xff"                       # JPEG
            or chunk[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
            or chunk[:6] in (b"GIF87a", b"GIF89a")             # GIF
            or (chunk[:4] == b"RIFF" and chunk[8:12] == b"WEBP"))  # WebP


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
        chunk = next(r.iter_content(8192), b"")
        # Accept a genuine image even when the CDN mislabels its Content-Type
        # (e.g. cdn.evbuc.com → binary/octet-stream).
        if not (ct.startswith("image/") or _looks_like_image_bytes(chunk)):
            return False
        size = int(r.headers.get("Content-Length") or 0) or len(chunk)
        return size >= 5_000
    except Exception:
        return False


def fetch_event_image(source_url: str,
                      *, validate: bool = False,
                      allow_root_fallback: bool = True,
                      _html: str | None = None) -> str:
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

    if _html is not None:
        # Caller already fetched the page (e.g. best_detail_image) — reuse it
        # instead of issuing a second GET against the same URL.
        html = _html
    else:
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
        # Unwrap Next.js image proxies (/_next/image?url=…) and Eventbrite's
        # signed img.evbuc.com proxy to the stable underlying CDN URL. Cobb
        # County (every Family event source) serves its event flyer through a
        # Next.js proxy wrapping an Eventbrite image; the proxy is UA-flaky, the
        # underlying cdn.evbuc.com URL is stable + re-hostable (validates via
        # the octet-stream byte-sniff in _image_looks_real).
        url = _unwrap_evbuc(_unwrap_next_image(_absolutize(url, source_url)))
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


_SRCSET_RE = re.compile(r'([^\s,]+)\s+(\d+)w')

# CTA-button intent ranking. The event's primary "go here for the real
# thing" button links to a page whose hero/og:image is the actual event
# art — whether that's a promo page (mlb.com/braves), a signup page
# (Eventbrite, whose og:image is the event's own flyer), or a ticket page.
# Earlier phrases rank higher. A button whose text hits _CTA_SKIP is a
# utility link (mailing list, directions, calendar share) and is never
# followed — those go to generic pages.
_CTA_RANK = (
    "more information", "more info", "learn more", "full details",
    "details", "read more", "event details", "see more",
    "get tickets", "buy tickets", "tickets", "register", "registration",
    "sign up", "rsvp", "reserve", "book now", "purchase",
)
_CTA_SKIP = (
    "mailing list", "newsletter", "subscribe", "directions", "parking",
    "get here", "getting here", "calendar", "add to", "google", "outlook",
    "ical", "share", "facebook", "instagram", "twitter", "menu", "contact",
)


def _best_from_srcset(srcset: str) -> str:
    """Return the highest-resolution URL from a srcset / data-srcset value.
    Lazyload heroes (e.g. mlbstatic.com) put the real, full-res image in a
    width-descriptor srcset; pick the largest `w` so the card art is crisp."""
    best_url, best_w = "", -1
    for m in _SRCSET_RE.finditer(srcset or ""):
        try:
            w = int(m.group(2))
        except ValueError:
            continue
        if w > best_w:
            best_w, best_url = w, m.group(1).strip()
    return best_url


def _unwrap_next_image(url: str) -> str:
    """Unwrap a Next.js image-optimizer proxy URL (`/_next/image?url=<enc>`)
    to the real underlying image. Eventbrite serves its og:image through this
    proxy; the real flyer is the percent-encoded `url` query param."""
    if "/_next/image" in url and "url=" in url:
        try:
            q = parse_qs(urlparse(url).query)
            if q.get("url"):
                return unquote(q["url"][0])
        except Exception:
            pass
    return url


def _unwrap_evbuc(url: str) -> str:
    """Unwrap Eventbrite's signed image proxy to the raw CDN original.

    Eventbrite serves transformed images via `img.evbuc.com/<inner-cdn-url>?
    …&s=<signature>`. That signed/transformed proxy URL 403s without the live
    signature AND is hotlink-protected — so it renders nowhere outside a live
    browser session (broken image in email). The INNER `cdn.evbuc.com/...` URL
    is the ORIGINAL, unsigned, publicly fetchable image (verified 200 with a
    plain request). Extract it (dropping the proxy host + transform/signature
    query) so we get a stable, re-hostable, renderable URL."""
    if "img.evbuc.com/" not in (url or ""):
        return url
    inner = unquote(url.split("img.evbuc.com/", 1)[1])
    if inner.startswith("http"):
        return inner.split("?", 1)[0]
    return url


def _cta_link_hero(html: str, source_url: str) -> str:
    """Follow the event's primary CTA button to its target page and return
    that page's hero image.

    The Battery's listing thumbnail is unreliable, but every Battery event
    page has a prominent <a class="button"> CTA whose target carries the real
    art: a promo page ("Click here for more information!" → mlb.com/braves,
    hero in data-srcset / mlbstatic), or a signup page ("Sign up for your
    spot here!" → Eventbrite, whose og:image is the event's own flyer). We
    pick the highest-intent external CTA (see _CTA_RANK), skipping utility
    links (mailing list, directions, calendar — see _CTA_SKIP), follow it,
    and extract the hero (largest srcset, else og:image, Next.js-unwrapped).

    Returns "" when there's no qualifying CTA or no usable target image.
    """
    src_host = (urlparse(source_url).hostname or "").lower().removeprefix("www.")
    best_rank, target = len(_CTA_RANK), ""
    for m in re.finditer(
        r'<a\b[^>]*\bclass=["\'][^"\']*\bbutton\b[^"\']*["\'][^>]*>(.*?)</a>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        text = re.sub(r'<[^>]+>', '', m.group(1)).strip().lower()
        if not text or any(s in text for s in _CTA_SKIP):
            continue
        hm = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.IGNORECASE)
        if not hm:
            continue
        cand = _absolutize(hm.group(1).strip().replace("&amp;", "&"), source_url)
        if not cand.startswith("http"):
            continue
        host = (urlparse(cand).hostname or "").lower().removeprefix("www.")
        # Only follow buttons that leave the event site — an on-site button
        # (e.g. the event's own permalink) wouldn't improve on the detail
        # page we already have.
        if not host or host == src_host:
            continue
        rank = next((i for i, kw in enumerate(_CTA_RANK) if kw in text), None)
        if rank is None:
            continue  # unranked CTA text — don't guess
        if rank < best_rank:
            best_rank, target = rank, cand
    if not target:
        return ""

    try:
        r = requests.get(
            target, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124 Safari/537.36"},
            allow_redirects=True,
        )
        if r.status_code != 200 or not r.text:
            return ""
        page = r.text
    except Exception:
        return ""

    # 1. Largest (data-)srcset hero — lazyload <img> real source (mlbstatic).
    for attr in ("data-srcset", "srcset"):
        for sm in re.finditer(attr + r'=["\']([^"\']+)["\']', page, re.IGNORECASE):
            best = _best_from_srcset(sm.group(1))
            if not best:
                continue
            best = _unwrap_evbuc(_unwrap_next_image(_absolutize(best, target)))
            if best.startswith("data:"):
                continue
            if any(skip in best.lower() for skip in SKIP_TOKENS):
                continue
            return best
    # 2. og:image / twitter:image (Eventbrite flyer), proxy-unwrapped.
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        mm = re.search(pat, page, re.IGNORECASE)
        if mm:
            img = _unwrap_evbuc(_unwrap_next_image(_absolutize(mm.group(1).strip(), target)))
            if img and not img.startswith("data:") and \
               not any(skip in img.lower() for skip in SKIP_TOKENS):
                return img
    # 3. Generic hero scan as a last resort.
    return fetch_event_image(target, allow_root_fallback=False)


def best_detail_image(source_url: str) -> str:
    """Best hero image for an event whose detail page is more reliable than
    its listing thumbnail. Fetches the detail page once, then prefers a
    'more information' button's external promo hero; otherwise the detail
    page's own og:image / JSON-LD image. Returns "" on failure so callers
    can keep the existing image."""
    if not source_url:
        return ""
    try:
        r = requests.get(source_url, timeout=10,
                         headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        if r.status_code != 200 or not r.text:
            return ""
        html = r.text
    except Exception:
        return ""
    hero = _cta_link_hero(html, source_url)
    if hero:
        return hero
    return fetch_event_image(source_url, _html=html, allow_root_fallback=False)


def upgrade_detail_images(events: list[dict],
                          *, source_url_key: str = "source_url",
                          image_url_key: str = "image_url",
                          max_workers: int = 6) -> int:
    """Override each event's image with its detail-page hero (more-info
    button target, else detail og:image). For sources whose LISTING
    thumbnails are unreliable (e.g. The Battery, where the listing JSON-LD
    image is often a shared placeholder or the wrong event's thumbnail).
    Only overrides when a detail image is actually found, so a transient
    fetch failure never blanks an existing image. Returns count upgraded."""
    if not events:
        return 0

    def _one(idx_ev):
        idx, ev = idx_ev
        url = ev.get(source_url_key) or ev.get("url") or ""
        if not url:
            return idx, ""
        return idx, best_detail_image(url)

    upgraded = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for idx, img in pool.map(_one, list(enumerate(events))):
            if img and img != (events[idx].get(image_url_key) or ""):
                events[idx][image_url_key] = img
                upgraded += 1
    return upgraded
