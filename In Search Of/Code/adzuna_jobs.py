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

_COUNTRY      = "us"
_BASE         = f"https://api.adzuna.com/v1/api/jobs/{_COUNTRY}/search/1"
_CATEGORIES   = f"https://api.adzuna.com/v1/api/jobs/{_COUNTRY}/categories"
DISTANCE_KM   = 24    # ~15 miles
MAX_DAYS_OLD  = 21
RESULTS_PAGE  = 25    # per category query

# ── Demo targeting (module defaults; override per newsletter via
#    cfg["job_categories"] / cfg["job_exclude"]) ───────────────────────────
# Keywords Adzuna excludes from every query (`what_exclude`, space-separated;
# a posting containing any is dropped). Targets: no truck/CDL driving, no
# staffing-agency reposts, no work-from-home. ('remote' is intentionally
# omitted — postings often say "not a remote role", which would self-exclude;
# the where+distance radius already does the locality filtering.)
DEFAULT_EXCLUDE = ["truck", "trucking", "cdl", "freight",
                   "staffing", "recruiter", "recruiting", "headhunter",
                   "telecommute", "telework", "work-from-home"]

# Adzuna category tags to pull (one query each, merged + interleaved) so the
# pool skews to the readership: healthcare & care, and local professional /
# admin / teaching / trades. Validated against the live /categories list at run
# time; any tag the API doesn't recognize is skipped. (Part-time is NOT a
# category — it's a separate contract-time filter, handled below — so it isn't
# listed here; that keeps full-time healthcare/professional roles in scope.)
DEFAULT_CATEGORIES = ["healthcare-nursing-jobs",
                      "social-work-jobs",
                      "domestic-help-cleaning-jobs",
                      "teaching-jobs",
                      "admin-jobs",
                      "trade-construction-jobs"]

# Also run one part-time query (across all fields) so flexible/part-time roles
# — for parents, students, retirees — surface alongside the career categories.
INCLUDE_PART_TIME = True

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


# Hard exclusion for truck-driving roles — a reliable backstop to Adzuna's
# keyword `what_exclude`, which can miss titles like "Class A Driver" or "OTR".
# Matched against the posting TITLE (explicit) so we don't false-drop an office
# job that merely mentions "valid driver's license" in its description.
_TRUCK_RE = re.compile(
    r"\b("
    r"truck\s*driver|truck\s*driving|trucking|"
    r"cdl|tractor[\s-]?trailer|owner[\s-]?operator|"
    r"18[\s-]?wheeler|semi[\s-]?truck|\botr\b|"
    r"class\s*[ab]\s*(?:driver|cdl)|cdl[\s-]?[ab]"
    r")\b",
    re.IGNORECASE,
)


def _is_truck_driving(title: str) -> bool:
    return bool(_TRUCK_RE.search(title or ""))


def _job_to_row(j: dict, newsletter: dict) -> dict | None:
    """Turn one Adzuna result into a save_job-ready row (or None if unusable)."""
    url     = (j.get("redirect_url") or "").strip()
    title   = unescape((j.get("title") or "").strip())
    if not (url and title):
        return None
    company = ((j.get("company") or {}).get("display_name") or "").strip()
    loc     = ((j.get("location") or {}).get("display_name") or "").strip()
    desc    = unescape(re.sub(r"<[^>]+>", " ", j.get("description") or "")).strip()
    salary  = _salary_str(j)
    parts = [f"JOB POSTING: {title}"]
    if company: parts.append(f"Employer: {company}")
    if loc:     parts.append(f"Location: {loc}")
    if salary:  parts.append(f"Salary: {salary}")
    if desc:    parts.append(f"Details: {desc}")
    return {
        "employer":         company or title,
        "scraped_snippet":  ". ".join(parts)[:2000],
        "job_listings_url": url,
        "image_url":        "",
        "city":             (loc.split(",")[0].strip().lower()
                             if loc else (newsletter.get("display_area", "") or "").lower()),
        "is_resource_hint": False,
    }


def _valid_category_tags() -> set:
    """The set of category tags Adzuna actually recognizes (from /categories),
    so a typo'd / renamed tag is skipped instead of erroring the run. Empty set
    on failure → caller treats all configured tags as usable (best effort)."""
    try:
        r = requests.get(_CATEGORIES, params={"app_id": ADZUNA_APP_ID,
                                              "app_key": ADZUNA_APP_KEY}, timeout=15)
        if r.status_code == 200:
            return {c.get("tag") for c in (r.json() or {}).get("results", []) if c.get("tag")}
    except Exception as e:
        print(f"  ⚠ Adzuna /categories lookup failed ({e}); using configured tags as-is")
    return set()


def _query(where: str, exclude: str, *, category: str | None = None,
           part_time: bool = False, label: str = "") -> list[dict]:
    """One Adzuna search call. Returns its results list ([] on any error)."""
    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "where":            where,
        "distance":         DISTANCE_KM,
        "results_per_page": RESULTS_PAGE,
        "max_days_old":     MAX_DAYS_OLD,
        "sort_by":          "date",
    }
    if exclude:
        params["what_exclude"] = exclude
    if category:
        params["category"] = category
    if part_time:
        params["part_time"] = 1
    try:
        r = requests.get(_BASE, params=params, timeout=20)
        if r.status_code != 200:
            print(f"    ⚠ Adzuna HTTP {r.status_code} ({label}): {r.text[:120]}")
            return []
        return (r.json() or {}).get("results", []) or []
    except Exception as e:
        print(f"    ⚠ Adzuna request failed ({label}): {e}")
        return []


def fetch_adzuna_jobs(newsletter: dict, limit: int = 8) -> list[dict]:
    """Return up to `limit` recent LOCAL postings, targeted to the newsletter's
    readership: one query per configured category (healthcare/care, part-time,
    local professional/admin/teaching/trades), an exclusion list (truck/CDL,
    staffing agencies, work-from-home), within the where+distance radius.
    Results are merged, deduped, and interleaved across categories for variety,
    then live-link-validated and capped at `limit`."""
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        print("  ⚠ ADZUNA_APP_ID / ADZUNA_APP_KEY not set — skipping Adzuna postings")
        return []
    where = _location_for(newsletter)
    if not where:
        print("  ⚠ No location to query Adzuna with — skipping")
        return []

    exclude = " ".join(newsletter.get("job_exclude") or DEFAULT_EXCLUDE)
    categories = list(newsletter.get("job_categories") or DEFAULT_CATEGORIES)
    valid = _valid_category_tags()
    if valid:
        kept = [c for c in categories if c in valid]
        dropped = [c for c in categories if c not in valid]
        if dropped:
            print(f"  ⚠ ignoring unknown Adzuna categories: {dropped}")
        categories = kept

    # Build the query specs: one per target category, plus one part-time query.
    specs: list[dict] = [{"label": c, "category": c} for c in categories]
    if INCLUDE_PART_TIME:
        specs.append({"label": "part-time", "part_time": True})
    # No usable specs (all categories invalid + part-time off) → a single plain
    # local query so we still return jobs.
    if not specs:
        specs = [{"label": "all"}]

    # Run each spec; keep results grouped so we can interleave for a mix.
    per_cat: list[list[dict]] = []
    for spec in specs:
        res = _query(where, exclude, category=spec.get("category"),
                     part_time=spec.get("part_time", False), label=spec["label"])
        per_cat.append(res)
        print(f"    · {spec['label']}: {len(res)} result(s)")

    # Round-robin interleave across categories (so one big category doesn't
    # crowd the rest), dedup by apply URL.
    ordered: list[dict] = []
    seen_urls: set[str] = set()
    for i in range(max((len(c) for c in per_cat), default=0)):
        for cat_results in per_cat:
            if i < len(cat_results):
                j = cat_results[i]
                u = (j.get("redirect_url") or "").strip()
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    ordered.append(j)

    rows: list[dict] = []
    seen_emp: set[str] = set()
    dead = trucks = 0
    for j in ordered:
        # Hard-drop truck-driving postings (belt-and-suspenders to what_exclude).
        if _is_truck_driving(unescape(j.get("title") or "")):
            trucks += 1
            continue
        row = _job_to_row(j, newsletter)
        if not row:
            continue
        # One posting per employer for variety in the section.
        emp = row["employer"].lower()
        if emp in seen_emp:
            continue
        # Only pass through live apply links — drop ads Adzuna no longer serves.
        if not _url_is_live(row["job_listings_url"]):
            dead += 1
            continue
        seen_emp.add(emp)
        rows.append(row)
        if len(rows) >= limit:
            break

    print(f"  → Adzuna: {len(rows)} live posting(s) near {where} across "
          f"{len(specs)} targeted quer(y/ies)"
          + (f" ({trucks} truck-driving skipped)" if trucks else "")
          + (f" ({dead} dead link(s) skipped)" if dead else ""))
    return rows
