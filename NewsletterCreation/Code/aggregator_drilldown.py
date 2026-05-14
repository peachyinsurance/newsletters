#!/usr/bin/env python3
"""
Shared aggregator-drilldown for event pipelines.

When a search result hails from a known aggregator / news-roundup site
(e.g. eastcobbnews.com's "Taste of East Cobb announces 2026 restaurants"
article links to the official tasteofeastcobb.com page), we want to
drill in and use the OFFICIAL/primary URL as the canonical source. The
primary site is where the real, up-to-date event details live — and is
what we should be running our date filter against.

Public API:
    AGGREGATOR_DOMAINS                     — set[str] of host substrings
    is_aggregator_url(url)                 -> bool
    find_primary_url(aggregator_url, title='') -> str | None
    fetch_page_text(url)                   -> str
    drill_down_candidate(c)                -> dict (mutated in place)
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

AGGREGATOR_DOMAINS: set[str] = {
    # Local news / community blogs that summarize events with primary links
    "eastcobbnews.com",
    "patch.com",
    "eastcobber.com",
    "atlantaparent.com",
    "atlantaonthecheap.com",
    "macaronikid.com",
    "mommypoppins.com",
    "northfulton.com",
    "accessatlanta.com",
    "cobbcountyevents.com",
    "northwestgeorgianews.com",
    "ajc.com",
    "11alive.com",
    "fox5atlanta.com",
    "wsbtv.com",
    "atlantaintownpaper.com",
    "cobbcountycourier.com",
    "mdjonline.com",
    "365atl.com",
    "cwpr.com",
    "appen.media",
    "roughdraftatlanta.com",
    "atlantamagazine.com",
    "creativeloafing.com",
    # Texas / Lewisville coverage
    "starlocalmedia.com",
    "crosstimbersgazette.com",
    "dallasnews.com",
    "guidelive.com",
    # PR distribution wires
    "morningstar.com",
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "accesswire.com",
    "finance.yahoo.com",
    "news.yahoo.com",
    "streetinsider.com",
    # Travel guides / listicle aggregators
    "tripster.com",
    "tripadvisor.com",
    "thrillist.com",
    "timeout.com",
    "yelp.com",
    "discoveratlanta.com",
    "atlantatrails.com",
    "exploregeorgia.org",
    "artsatl.org",
}

# Anchor text that tells us nothing — skip when picking primary URLs.
GENERIC_ANCHOR_TEXT: set[str] = {
    "click here", "here", "click", "more", "more info", "more information",
    "read more", "learn more", "register", "register here", "sign up",
    "tickets", "get tickets", "buy tickets", "details", "visit", "visit site",
    "website", "link", "see more", "view", "more details", "info", "rsvp",
    "facebook", "twitter", "instagram", "x.com",
}

# Hosts we never want to drill INTO (social, redirects, maps, share widgets).
_SKIP_TARGET_HOSTS = (
    # Social platforms
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "threads.net", "pinterest.com",
    "reddit.com",
    # Maps / directions — these often appear under event headings
    # (address-as-link), but they're not the event's source page.
    "google.com", "goo.gl", "maps.app.goo.gl", "maps.apple.com",
    # Universal share-button widgets — cobbcountycourier.com and
    # mdjonline.com embed these under every event section, so they score
    # high on heading-proximity but are share URLs, not real primaries.
    "addtoany.com", "addthis.com", "sharethis.com",
    "wa.me", "api.whatsapp.com", "t.me",
    # Affiliate redirect networks
    "jdoqocy.com", "dpbolvw.net", "tkqlhce.com", "anrdoezrs.net",
    # App download / store CTAs ("Download our app")
    "itunes.apple.com", "apps.apple.com", "play.google.com",
    # News archive / search portals — drilling MDJ etc. sometimes lands
    # on the archive search ("Search the MDJ's archives") instead of a
    # real event page. These are not primary sources.
    "newsbank.com", "archive.org", "web.archive.org",
    # Newsletter signup widgets (Mailchimp, Constant Contact, etc.) —
    # local-news sites embed a "Send Us Your News!" signup form under the
    # masthead, which scores high on heading proximity for unrelated events.
    "list-manage.com", "mailchi.mp", "constantcontact.com",
    "campaign-archive.com", "lp.constantcontactpages.com",
    # Generic email/print actions
    "mailto:", "javascript:",
)


def _hostname(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        # Strip the literal "www." prefix (NOT lstrip, which treats the
        # argument as a character set and would mangle hosts like "wa.me"
        # into "a.me" by stripping the leading 'w').
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _host_in(host: str, domain_set: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domain_set)


def is_aggregator_url(url: str) -> bool:
    """True if the URL's host is a known aggregator / news-roundup."""
    return _host_in(_hostname(url), AGGREGATOR_DOMAINS)


def _browser_get(url: str, timeout: int = 10):
    """Fetch a URL using curl_cffi with Chrome TLS impersonation when
    available — defeats Cloudflare bot detection that blocks plain
    `requests` calls. Falls back to `requests` if curl_cffi isn't
    installed in the environment.

    Returns the response object (curl_cffi or requests) or None on error."""
    try:
        from curl_cffi import requests as _cffi
        try:
            return _cffi.get(url, impersonate="chrome120",
                             timeout=timeout, allow_redirects=True)
        except Exception:
            pass
    except ImportError:
        pass
    # Fallback to plain requests
    try:
        return requests.get(url, headers={"User-Agent": BROWSER_UA},
                            timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return None


def fetch_page_text(url: str, timeout: int = 10) -> str:
    """Fetch a page's body text (HTML stripped). Empty string on error."""
    r = _browser_get(url, timeout=timeout)
    if not r or r.status_code >= 400 or not r.text:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("article") or soup.find("main") or soup.body or soup
        return body.get_text(" ", strip=True)
    except Exception:
        # bs4 unavailable — strip tags crudely
        return re.sub(r"<[^>]+>", " ", r.text)


def find_primary_url(aggregator_url: str, title: str = "") -> str | None:
    """Fetch an aggregator article and return its single best primary URL —
    the most relevant non-aggregator external link.

    Three matching signals (combined):
      1. **URL-slug match** — link's URL contains event-title tokens
         (mariettagreekfestival.com matches 'Marietta Greek Festival')
      2. **Heading proximity** — link sits in the section under an <h2>/<h3>
         whose text matches the event title (catches "Get tickets"-style
         generic anchors in listicle layouts)
      3. **Anchor-text match** — link's anchor text matches event title
         (original heuristic, still works for inline mentions)

    Score per link: sum of (slug_overlap×4) + (heading_overlap×4) + (anchor_overlap×3) + length_bonus.
    Need a minimum score to return a match — otherwise we'd assign the
    wrong primary URL when no real match exists.
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    r = _browser_get(aggregator_url, timeout=10)
    if not r or r.status_code >= 400 or not r.text:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    # Use the LARGEST <article> or <main> container by link count — some
    # pages have multiple <article> elements (sidebar widgets, related
    # stories, the actual body), and the first one in document order is
    # often a sidebar that doesn't contain the real content. Falling back
    # to the full document is fine because our host/aggregator/social
    # filters drop sidebar noise anyway.
    src_host = _hostname(aggregator_url)
    candidate_bodies = soup.find_all(["article", "main"])
    if candidate_bodies:
        body = max(candidate_bodies, key=lambda el: len(el.find_all("a", href=True)))
    else:
        body = soup

    title_tokens = {t for t in re.findall(r"\w+", (title or "").lower())
                    if len(t) > 3 and not t.isdigit()}

    def _tokens_of(text: str) -> set[str]:
        return {t for t in re.findall(r"\w+", (text or "").lower())
                if len(t) > 3 and not t.isdigit()}

    def _overlap(other_tokens: set[str]) -> int:
        return len(title_tokens & other_tokens) if title_tokens else 0

    # Build a map: for each <a>, find its containing section by walking up
    # to the previous heading (h1/h2/h3). The heading's text is the section
    # label; links share heading-context if they fall under the same one.
    HEADING_TAGS = ("h1", "h2", "h3")

    def _section_heading_for(elem) -> str:
        """Walk previous siblings/parents to find the nearest preceding
        heading. Returns the heading text (or '')."""
        # Try previous siblings first (typical listicle: <h2>X</h2><p>...<a>...</a>)
        cur = elem
        for _ in range(50):  # cap walk depth
            prev = cur.find_previous(HEADING_TAGS)
            if prev:
                return prev.get_text(strip=True)
            break
        return ""

    candidates: list[tuple[int, str, str, str]] = []  # (score, url, anchor, heading)
    seen_urls: set[str] = set()

    for a in body.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            href = urljoin(aggregator_url, href)
        if not href.startswith("http"):
            continue
        host = _hostname(href)
        if not host or host == src_host:
            continue
        if _host_in(host, AGGREGATOR_DOMAINS):
            continue
        if any(h in host for h in _SKIP_TARGET_HOSTS):
            continue
        url_clean = href.split("#")[0].rstrip("/")
        if url_clean in seen_urls:
            continue
        seen_urls.add(url_clean)

        anchor = (a.get_text(strip=True) or "")[:200]
        anchor_lower = anchor.lower().strip()
        # Generic anchors don't disqualify here (a "Get tickets" link under
        # the right heading is still the right URL) — but anchor scoring is
        # 0 for them so they need the heading or URL-slug signal to win.
        is_generic = len(anchor_lower) < 4 or anchor_lower in GENERIC_ANCHOR_TEXT

        anchor_tokens = set() if is_generic else _tokens_of(anchor)
        anchor_score = _overlap(anchor_tokens) * 3

        # URL slug match — split host + path on non-word chars
        slug_tokens = _tokens_of(re.sub(r"[^\w]+", " ", href))
        slug_score = _overlap(slug_tokens) * 4

        # Heading proximity
        heading_text = _section_heading_for(a)
        heading_tokens = _tokens_of(heading_text)
        heading_score = _overlap(heading_tokens) * 4

        length_bonus = 0 if is_generic else min(len(anchor), 40) // 10

        score = anchor_score + slug_score + heading_score + length_bonus
        if score > 0:
            candidates.append((score, url_clean, anchor, heading_text))

    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    best = candidates[0]
    # Minimum score: any single ≥1-token match in EITHER slug, heading, or
    # anchor gets a score of 3 or 4. We require ≥4 to ensure we have either
    # a URL-slug or heading match (the strong signals), not just a weak
    # anchor coincidence.
    MIN_SCORE = 4
    if best[0] < MIN_SCORE:
        print(f"      ⚠ drill found no strong match for '{title[:50]}' in "
              f"{_hostname(aggregator_url)} (best score {best[0]}) — keeping aggregator URL")
        return None
    via = []
    if best[2]: via.append(f"anchor='{best[2][:30]}'")
    if best[3]: via.append(f"heading='{best[3][:30]}'")
    print(f"      ↳ drilled '{aggregator_url[:60]}…' → '{best[1][:80]}' "
          f"({', '.join(via)}, score={best[0]})")
    return best[1]


def drill_down_candidate(candidate: dict) -> dict:
    """For aggregator candidates: fetch the article body, find a primary
    embedded URL if available, and stash everything we can use for
    downstream date extraction.

    Mutates and returns `candidate`. Adds:
      candidate['article_text']    — body text of the aggregator article
                                     (always populated for aggregator URLs)
      candidate['original_url']    — the aggregator URL (when we swap)
      candidate['drilled']         — True iff candidate.url was replaced
      candidate['primary_text']    — body text of the primary URL (when found)

    Why both: many aggregator articles contain the event details in
    their body but link only to a venue homepage that lacks the event
    page (e.g. eastcobbnews.com → mariettahistory.org/). The article
    body is the only source for the actual date in those cases.
    """
    url = candidate.get("url", "")
    if not url or not is_aggregator_url(url):
        candidate["drilled"] = False
        return candidate

    # Always pull the aggregator article's body text so the date filter
    # can scan it for explicit dates ("Saturday, June 27th, at 2:00 p.m.").
    candidate["article_text"] = fetch_page_text(url)

    primary = find_primary_url(url, title=candidate.get("title", ""))
    if not primary:
        candidate["drilled"] = False
        return candidate

    candidate["original_url"] = url
    candidate["url"] = primary
    candidate["source"] = _hostname(primary)
    candidate["drilled"] = True
    # Fetch primary content too — sometimes it has a clean event page
    # whose date differs from anything mentioned in the aggregator body.
    candidate["primary_text"] = fetch_page_text(primary)
    return candidate
