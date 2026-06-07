"""Adzuna job-postings source for the In Search Of section.

Queries the Adzuna API for recent LOCAL job postings near a newsletter's
coverage area and returns rows in the same shape `save_job` expects. The
structured posting facts (title, employer, location, salary) are packed
into `scraped_snippet` so the In Search Of skill can rewrite them, and the
posting's apply URL is the dedup key (`job_listings_url`).

Requires a free Adzuna API key (developer.adzuna.com):
  ADZUNA_APP_ID, ADZUNA_APP_KEY   (GitHub repo secrets)
Returns [] (never raises) when the keys are missing, so the pipeline keeps
working off the curated employer spotlights alone.
"""
from __future__ import annotations

import os
import re
from html import unescape

import requests

ADZUNA_APP_ID  = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")

_BASE         = "https://api.adzuna.com/v1/api/jobs/us/search/1"
DISTANCE_KM   = 24    # ~15 miles
MAX_DAYS_OLD  = 21
RESULTS_PAGE  = 30    # pull a page, then validate + cap

_VALIDATE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


_DEAD_STATUSES = {404, 410}


def _url_is_live(url: str) -> bool:
    """Keep a URL unless it is CONFIRMED dead (404 / 410). We follow redirects
    with a browser UA; ambiguous results (403 bot-walls, 5xx, timeouts, network
    errors) are KEPT rather than risk dropping a live posting. The goal is to
    keep broken 'Apply' links out of the newsletter, not to over-prune."""
    if not url:
        return False
    try:
        r = requests.head(url, headers=_VALIDATE_HEADERS, timeout=12, allow_redirects=True)
        if r.status_code in (403, 405):
            # Host won't answer HEAD — confirm with a bodyless GET.
            r = requests.get(url, headers=_VALIDATE_HEADERS, timeout=15,
                             allow_redirects=True, stream=True)
        return r.status_code not in _DEAD_STATUSES
    except Exception:
        return True   # network hiccup — don't drop a possibly-live link


def _location_for(newsletter: dict) -> str:
    """Adzuna `where` string. Prefers an explicit `job_location`; else the
    first real-city search_area; else display_area. Normalizes 'Marietta GA'
    → 'Marietta, GA'."""
    raw = (newsletter.get("job_location")
           or (newsletter.get("search_areas") or [None])[0]
           or newsletter.get("display_area", "") or "").strip()
    m = re.match(r"^(.*?)[\s,]+([A-Z]{2})$", raw)
    return f"{m.group(1).strip()}, {m.group(2)}" if m else raw


def _salary_str(j: dict) -> str:
    lo, hi = j.get("salary_min"), j.get("salary_max")
    predicted = str(j.get("salary_is_predicted")) in ("1", "True", "true")
    def _f(v):
        return f"${int(v):,}"
    if lo and hi and int(lo) != int(hi):
        s = f"{_f(lo)}-{_f(hi)}/yr"
    elif lo or hi:
        s = f"{_f(lo or hi)}/yr"
    else:
        return ""
    return s + (" (estimated)" if predicted else "")


def fetch_adzuna_jobs(newsletter: dict, limit: int = 8) -> list[dict]:
    """Return up to `limit` recent local postings as save_job-ready rows."""
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        print("  ⚠ ADZUNA_APP_ID / ADZUNA_APP_KEY not set — skipping Adzuna postings")
        return []
    where = _location_for(newsletter)
    if not where:
        print("  ⚠ No location to query Adzuna with — skipping")
        return []
    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "where":            where,
        "distance":         DISTANCE_KM,
        "results_per_page": RESULTS_PAGE,
        "max_days_old":     MAX_DAYS_OLD,
        "sort_by":          "date",
    }
    try:
        r = requests.get(_BASE, params=params, timeout=20)
        if r.status_code != 200:
            print(f"  ⚠ Adzuna HTTP {r.status_code}: {r.text[:160]}")
            return []
        results = (r.json() or {}).get("results", []) or []
    except Exception as e:
        print(f"  ⚠ Adzuna request failed: {e}")
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    dead = 0
    for j in results:
        url     = (j.get("redirect_url") or "").strip()
        title   = unescape((j.get("title") or "").strip())
        company = ((j.get("company") or {}).get("display_name") or "").strip()
        loc     = ((j.get("location") or {}).get("display_name") or "").strip()
        desc    = unescape(re.sub(r"<[^>]+>", " ", j.get("description") or "")).strip()
        if not (url and title) or url in seen:
            continue
        seen.add(url)

        # Only pass through live apply links — drop ads Adzuna no longer serves.
        if not _url_is_live(url):
            dead += 1
            continue

        salary = _salary_str(j)
        parts = [f"JOB POSTING: {title}"]
        if company:
            parts.append(f"Employer: {company}")
        if loc:
            parts.append(f"Location: {loc}")
        if salary:
            parts.append(f"Salary: {salary}")
        if desc:
            parts.append(f"Details: {desc}")
        snippet = ". ".join(parts)

        rows.append({
            "employer":         company or title,
            "scraped_snippet":  snippet[:2000],
            "job_listings_url": url,
            "image_url":        "",
            "city":             (loc.split(",")[0].strip().lower()
                                 if loc else (newsletter.get("display_area", "") or "").lower()),
            "is_resource_hint": False,
        })
        if len(rows) >= limit:
            break

    print(f"  → Adzuna: {len(rows)} live posting(s) near {where}"
          + (f" ({dead} dead link(s) skipped)" if dead else ""))
    return rows
