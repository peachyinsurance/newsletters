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

# Hosts we never want to drill INTO (social, redirects, paywalls).
_SKIP_TARGET_HOSTS = (
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "google.com",
)


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().lstrip("www.")
    except Exception:
        return ""


def _host_in(host: str, domain_set: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domain_set)


def is_aggregator_url(url: str) -> bool:
    """True if the URL's host is a known aggregator / news-roundup."""
    return _host_in(_hostname(url), AGGREGATOR_DOMAINS)


def fetch_page_text(url: str, timeout: int = 10) -> str:
    """Fetch a page's body text (HTML stripped). Empty string on error."""
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA},
                         timeout=timeout, allow_redirects=True)
        if r.status_code >= 400 or not r.text:
            return ""
    except requests.RequestException:
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

    Heuristic ranking:
      1. Anchor text overlaps the article title (longest token overlap wins)
      2. Otherwise the first prominent external link in the article body
      3. Skip generic anchors ('register', 'tickets'), social, other aggregators
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    try:
        r = requests.get(aggregator_url, headers={"User-Agent": BROWSER_UA},
                         timeout=10, allow_redirects=True)
        if r.status_code >= 400 or not r.text:
            return None
    except requests.RequestException:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.find("article") or soup.find("main") or soup
    src_host = _hostname(aggregator_url)

    title_tokens = {t for t in re.findall(r"\w+", (title or "").lower()) if len(t) > 3}

    candidates: list[tuple[int, str, str]] = []  # (score, url, anchor)
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

        anchor = (a.get_text(strip=True) or "")[:200]
        anchor_lower = anchor.lower().strip()
        if len(anchor_lower) < 4 or anchor_lower in GENERIC_ANCHOR_TEXT:
            continue

        seen_urls.add(url_clean)

        # Score: token-overlap with title (×3), then reward longer anchors
        anchor_tokens = {t for t in re.findall(r"\w+", anchor_lower) if len(t) > 3}
        overlap = len(title_tokens & anchor_tokens)
        score = overlap * 3 + min(len(anchor), 40) // 10
        candidates.append((score, url_clean, anchor))

    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    best = candidates[0]
    print(f"      ↳ drilled '{aggregator_url[:60]}…' → '{best[1][:80]}' "
          f"(anchor='{best[2][:50]}', score={best[0]})")
    return best[1]


def drill_down_candidate(candidate: dict) -> dict:
    """If `candidate['url']` is from an aggregator, swap it for the primary
    URL discovered by drilling the page. Mutates and returns `candidate`.

    Adds three keys:
      candidate['original_url']    — the aggregator URL we replaced
      candidate['drilled']         — True if a swap happened
      candidate['primary_text']    — fetched body text of the primary URL
                                     (used for downstream date extraction)
    """
    url = candidate.get("url", "")
    if not url or not is_aggregator_url(url):
        candidate["drilled"] = False
        return candidate

    primary = find_primary_url(url, title=candidate.get("title", ""))
    if not primary:
        candidate["drilled"] = False
        return candidate

    candidate["original_url"] = url
    candidate["url"] = primary
    candidate["source"] = _hostname(primary)
    candidate["drilled"] = True
    # Fetch primary content so the date filter can re-check against the
    # canonical page (e.g. the official event site showing the actual date).
    candidate["primary_text"] = fetch_page_text(primary)
    return candidate
