#!/usr/bin/env python3
"""In Search Of pipeline — Claude blurb writer for local job listings.

Reads the In Search Of Notion DB for the target newsletter, sends each
row to Claude to rewrite the scraped snippet into a neighborly hiring
blurb following the skill's voice + format rules, and PATCHes the row's
Description field with the rewritten copy.

Approved-first / non-archived fallback (same pattern as Weekend Planner):
  1. Query rows where Status='approved'. Use these.
  2. If no approved rows, fall back to Status='pending'.
  3. Skip 'rejected' and 'archived' entirely.

The assembler reads the same DB at render time and pulls
approved/pending rows with the AI-written Description field.

Env vars:
  CLAUDE_API_KEY              required
  NOTION_API_KEY              required
  NOTION_IN_SEARCH_OF_DB_ID   required
  NEWSLETTER                  optional (default 'all')
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..",
                             "NewsletterCreation", "Code"))
from notion_helper import (  # noqa: E402
    query_database,
    update_page,
    NOTION_IN_SEARCH_OF_DB_ID,
)
from newsletters_config import filter_by_env  # noqa: E402
from claude_json import call_with_json_output  # noqa: E402


CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
from voice_helper import with_voice  # noqa: E402
SKILL_PROMPT_PATH = (Path(__file__).parent.parent.parent
                     / "Skills" / "newsletter-in-search-of-skill_auto.md")


def load_skill_prompt() -> str:
    if not SKILL_PROMPT_PATH.exists():
        raise FileNotFoundError(f"Skill not found at {SKILL_PROMPT_PATH}")
    return SKILL_PROMPT_PATH.read_text(encoding="utf-8")


def _rich_text_value(prop) -> str:
    if not isinstance(prop, dict):
        return ""
    chunks = prop.get("rich_text") or prop.get("title") or []
    return "".join(c.get("plain_text", "") for c in chunks).strip()


def fetch_jobs_pool(newsletter_name: str) -> list[dict]:
    """Pull all rows for this newsletter (any non-archived/rejected
    status). The pipeline prefers `approved` rows but falls back to
    `pending` when no approvals exist yet — mirrors the Weekend Planner
    approved-first pattern."""
    if not NOTION_IN_SEARCH_OF_DB_ID:
        print("  ⚠ NOTION_IN_SEARCH_OF_DB_ID not set; nothing to do")
        return []
    filters = {
        "and": [
            {"property": "Newsletter", "select": {"equals": newsletter_name}},
            {"property": "Status", "select": {"does_not_equal": "archived"}},
            {"property": "Status", "select": {"does_not_equal": "rejected"}},
            {"property": "Status", "select": {"does_not_equal": "approved - old"}},
        ]
    }
    pages = query_database(NOTION_IN_SEARCH_OF_DB_ID, filters=filters) or []
    out: list[dict] = []
    for p in pages:
        props = p.get("properties", {})
        out.append({
            "notion_page_id":   p.get("id"),
            "status":           (props.get("Status", {}).get("select") or {}).get("name", ""),
            "employer":         _rich_text_value(props.get("Employer")),
            "scraped_snippet":  _rich_text_value(props.get("Scraped Snippet")),
            "job_listings_url": (props.get("Job Listings URL", {}).get("url") or "").strip(),
            "image_url":        (props.get("Image URL", {}).get("url") or "").strip(),
            "city":             _rich_text_value(props.get("City")),
            "bonus":            bool((props.get("Bonus", {}) or {}).get("checkbox")),
            "current_description": _rich_text_value(props.get("Description")),
        })
    return out


def select_pool(rows: list[dict]) -> list[dict]:
    """Approved-first: if any approved rows exist, use only those.
    Otherwise fall back to pending. Caller hands us all non-archived
    non-rejected rows already; this just stratifies."""
    approved = [r for r in rows if r["status"] == "approved"]
    if approved:
        print(f"    Using {len(approved)} approved row(s)")
        return approved
    pending = [r for r in rows if r["status"] == "pending"]
    if pending:
        print(f"    No approved rows; falling back to {len(pending)} pending row(s)")
        return pending
    return []


def call_claude(pool: list[dict], newsletter_name: str, skill_prompt: str) -> list[dict]:
    """Send the pool to Claude. Returns list of {candidate_index, blurb,
    roles, bonus, drop, drop_reason}."""
    if not pool:
        return []
    indexed = [
        {
            "candidate_index": i + 1,
            "employer":         r["employer"],
            "scraped_snippet":  r["scraped_snippet"],
            "city":             r["city"],
            "newsletter":       newsletter_name,
            "is_resource_hint": r["bonus"],
        }
        for i, r in enumerate(pool)
    ]
    user_prompt = f"""
Newsletter: {newsletter_name.replace('_', ' ')}
Rows to write: {len(indexed)}

Write each row per the skill's voice + format rules. Default to including;
only drop on 404 / off-topic / inappropriate content. NEVER fabricate
salary / bonus / role specifics that aren't in the scraped_snippet.

Rows:
{json.dumps(indexed, indent=2, ensure_ascii=False)}
"""
    try:
        results = call_with_json_output(
            api_key=CLAUDE_API_KEY,
            system=with_voice(skill_prompt),
            user_content=user_prompt,
        )
    except Exception as e:
        print(f"  ✗ Claude error: {e}")
        return []
    return results or []


def apply_results(pool: list[dict], results: list[dict]) -> tuple[int, int]:
    """PATCH each pool row's Description with the Claude blurb. Returns
    (updated, dropped) counts."""
    by_index = {i + 1: r for i, r in enumerate(pool)}
    updated, dropped = 0, 0
    for res in results:
        idx = res.get("candidate_index")
        try:
            idx = int(idx) if idx is not None else None
        except Exception:
            idx = None
        row = by_index.get(idx) if idx is not None else None
        if not row:
            print(f"    ✗ invalid candidate_index {idx}; skipping")
            continue
        if res.get("drop"):
            print(f"    ✗ dropping '{row['employer']}': {res.get('drop_reason', 'no reason')}")
            dropped += 1
            # Mark Status=rejected so the next run doesn't re-process it
            try:
                update_page(row["notion_page_id"], properties={
                    "Status": {"select": {"name": "rejected"}},
                })
            except Exception as e:
                print(f"      (couldn't update Status to rejected: {e})")
            continue
        blurb = (res.get("blurb") or "").strip()
        roles = (res.get("roles") or "").strip()
        bonus = bool(res.get("bonus"))
        if not blurb:
            print(f"    ⚠ empty blurb for '{row['employer']}'; leaving row alone")
            continue
        # PATCH Description (Claude blurb) + Roles + Bonus checkbox
        try:
            update_page(row["notion_page_id"], properties={
                "Description": {"rich_text": [{"text": {"content": blurb[:2000]}}]},
                "Roles":       {"rich_text": [{"text": {"content": roles[:300]}}]},
                "Bonus":       {"checkbox": bonus},
            })
            updated += 1
            print(f"    ✓ wrote blurb for '{row['employer']}' ({len(blurb)} chars)")
        except Exception as e:
            print(f"    ✗ PATCH failed for '{row['employer']}': {e}")
    return updated, dropped


# A row's Job Listings URL is an Adzuna proxy when it's either the generic
# details page (raw API postings) OR the tokenized land/ad apply redirect (what
# an earlier drill-down may have already persisted). Both are wrong to show the
# reader ("Apply: adzuna.com") and both can be resolved to the real employer URL.
_ADZUNA_DETAILS_RE = re.compile(r"adzuna\.com/details/\d+", re.IGNORECASE)
_ADZUNA_LAND_RE    = re.compile(r"adzuna\.com/land/ad/", re.IGNORECASE)
_ADZUNA_ANY_RE     = re.compile(r"adzuna\.com/(?:details/\d+|land/ad/)", re.IGNORECASE)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# Hard ceiling on how stale a posting may be. Adzuna's own `max_days_old` filters
# by when ADZUNA indexed the ad, not when the employer posted it — a re-indexed
# ad can be months old yet look fresh. So we re-check the posting's real
# `datePosted` from the employer page's JobPosting JSON-LD and drop anything
# older than this. (We aim for ≤7 days — the scrape sorts by date so the pool
# already skews fresh — but hard-cap at 30.)
MAX_LISTING_AGE_DAYS = 30


def _parse_iso_date(s: str):
    """Parse an ISO-8601 datePosted (e.g. '2026-05-30T12:00:00Z') into a
    date. Returns None if absent/unparseable so the caller keeps the row
    rather than dropping on a parse miss."""
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(str(s).strip()[:10])
    except Exception:
        return None


def _job_posting_from_html(html: str) -> dict:
    """Pull {description, employer, date_posted} from the first JobPosting
    JSON-LD block in `html` (empty dict if none)."""
    out: dict = {}
    for blob in re.findall(
            r'application/ld\+json[^>]*>(.+?)</script>', html, re.DOTALL):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not (isinstance(it, dict) and it.get("@type") == "JobPosting"):
                continue
            desc = re.sub(r"<[^>]+>", " ", it.get("description", "") or "")
            desc = re.sub(r"\s+", " ", desc).strip()
            if desc and "description" not in out:
                out["description"] = desc
            org = it.get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                out.setdefault("employer", org["name"].strip())
            elif isinstance(org, str) and org.strip():
                out.setdefault("employer", org.strip())
            if it.get("datePosted"):
                out.setdefault("date_posted", it["datePosted"])
    return out


_CFFI_SESSION = None   # persistent curl_cffi Session (lazily created)


def _session():
    """One reused curl_cffi Session so cookies persist across requests. Adzuna
    often sets a bot-clearance cookie on the first hit that later requests need;
    fresh per-request clients never carry it, which is why ~half the CI fetches
    were getting blocked. Returns False if curl_cffi isn't installed (caller
    falls back to plain requests)."""
    global _CFFI_SESSION
    if _CFFI_SESSION is None:
        try:
            from curl_cffi import requests as _cffi
            _CFFI_SESSION = _cffi.Session(impersonate="chrome120")
        except ImportError:
            _CFFI_SESSION = False
    return _CFFI_SESSION


def _fetch(url: str) -> tuple[str, str]:
    """Fetch a URL with a real-browser TLS fingerprint + persistent cookies
    (curl_cffi Session), following redirects. Returns (final_url, html); html
    is '' on a non-2xx so callers can still use the resolved final_url.

    Adzuna bot-walls / rate-limits plain `requests` from datacenter IPs (GitHub
    Actions), so the drill-down was silently returning nothing for ~half the
    rows and stale postings survived. We impersonate Chrome, reuse one Session
    (cookies), retry with backoff, and LOG the HTTP status on final failure so
    blocks are visible in the workflow log instead of silent."""
    sess = _session()
    for attempt in range(4):
        try:
            if sess:
                r = sess.get(url, timeout=25, allow_redirects=True)
            else:
                r = requests.get(url, timeout=25, headers={"User-Agent": _BROWSER_UA},
                                 allow_redirects=True)
            final = str(getattr(r, "url", "") or url)
            if 200 <= r.status_code < 300 and r.text:
                return final, r.text
            if attempt < 3:
                time.sleep(2 * (attempt + 1))   # 2s, 4s, 6s
                continue
            print(f"      ⚠ {url[:55]} → HTTP {r.status_code}")
            return final, ""
        except Exception as e:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"      ⚠ fetch failed for {url[:55]}: {e}")
    return "", ""


def _is_adzuna_host(url: str) -> bool:
    return "adzuna." in (urlparse(url).netloc or "").lower()


def _meta_redirect(html: str) -> str:
    """Extract the destination of a client-side redirect (meta refresh or
    JS location change) from a page. Adzuna's /land/ad page doesn't HTTP-302
    to the employer — it bounces via <meta http-equiv=refresh> /
    location.replace() to an aggregator (e.g. de.jobsyn.org) that then
    redirects to the real site. Returns '' if none found."""
    if not html:
        return ""
    for pat in (
        r'http-equiv=["\']refresh["\'][^>]*url=([^"\'>\s]+)',
        r'location\.(?:replace|assign)\(["\']([^"\']+)["\']',
        r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1).replace("&amp;", "&")
    return ""


def _resolve_apply_url(land: str) -> tuple[str, str]:
    """Follow an Adzuna /land/ad link to the real employer page. The land page
    client-side-redirects to an aggregator which HTTP-redirects to the employer
    site, so we read the meta/JS destination and follow THAT. Returns
    (final_url, final_html)."""
    url, html = _fetch(land)
    dest = _meta_redirect(html)
    if dest:
        url2, html2 = _fetch(dest)
        if url2:
            return url2, html2
    return url, html


def _fetch_details(url: str) -> dict:
    """ONE fetch of the Adzuna details page → {date_posted, description,
    employer, land}. Kept to a single request so the age gate for every row
    fits under Adzuna's per-IP rate limit (it blocks the CI IP after ~9
    requests; the old all-in-one drill burned 3 requests/row and only ~3 rows
    got checked before the block). The expensive apply-URL resolution (2 more
    requests: land → aggregator → employer) is done separately, afterwards,
    for surviving rows only. `land` is the /land/ad apply-redirect URL."""
    out: dict = {}
    if _ADZUNA_DETAILS_RE.search(url):
        _, html = _fetch(url)
        if html:
            out.update(_job_posting_from_html(html))   # date_posted, description, employer
            m = re.search(
                r'href=["\'](https://www\.adzuna\.com/land/ad/[^"\']+)["\']',
                html, re.IGNORECASE)
            if m:
                out["land"] = m.group(1).replace("&amp;", "&")
    elif _ADZUNA_LAND_RE.search(url):
        out["land"] = url
    return out


def enrich_adzuna_rows(pool: list[dict]) -> None:
    """Two-phase enrichment of Adzuna-proxy rows. Mutates `pool` in place.

    Phase 1 — AGE GATE (1 fetch/row, run first). Pull the posting's real
    datePosted from the details page and drop anything older than
    MAX_LISTING_AGE_DAYS (marked rejected in Notion + removed from the pool).
    Doing the cheap age check for ALL rows before any expensive work keeps it
    under Adzuna's per-IP request cap, so stale postings are reliably caught
    even when later requests get throttled.

    Phase 2 — APPLY URL + snippet/employer backfill (2 fetches/row), only for
    rows that survived the age gate. Resolves the real employer URL (land →
    aggregator → employer site) so the card doesn't read 'Apply: adzuna.com'.
    If this gets rate-limited it only degrades the display domain — it can't
    resurrect a stale posting, which is the important guarantee."""
    today = datetime.date.today()
    adzuna_rows = [r for r in pool
                   if _ADZUNA_ANY_RE.search(r.get("job_listings_url", ""))]

    # ── Phase 1: age gate ────────────────────────────────────────────────
    dropped_ids: set = set()
    dropped_old = 0
    no_date = 0
    for row in adzuna_rows:
        time.sleep(2.0)   # be polite — back-to-back fetches trip Adzuna's rate limit
        details = _fetch_details(row.get("job_listings_url", ""))
        row["_drill"] = details   # stash for phase 2
        posted = _parse_iso_date(details.get("date_posted", ""))
        emp = row.get("employer", "?")[:40]
        if posted is None:
            # Fetch blocked or no datePosted — log it so blocks are visible
            # (we keep the row rather than risk dropping a fresh posting).
            no_date += 1
            print(f"    · age-gate: {emp} — no datePosted (fetch blocked?)")
            continue
        age = (today - posted).days
        if age > MAX_LISTING_AGE_DAYS:
            print(f"    ✗ stale ({age}d old > {MAX_LISTING_AGE_DAYS}d): "
                  f"{emp} — marking rejected")
            dropped_old += 1
            dropped_ids.add(row["notion_page_id"])
            try:
                update_page(row["notion_page_id"], properties={
                    "Status": {"select": {"name": "rejected"}}})
            except Exception as e:
                print(f"      (couldn't update Status to rejected: {e})")
        else:
            print(f"    · age-gate: {emp} — {age}d old, keep")
    if no_date:
        print(f"    ⚠ age-gate: {no_date}/{len(adzuna_rows)} row(s) had no datePosted "
              f"(blocked fetch) — these can't be aged out")

    # ── Phase 2: apply URL + snippet/employer (survivors only) ────────────
    for row in adzuna_rows:
        details = row.pop("_drill", {})
        if row["notion_page_id"] in dropped_ids:
            continue
        url = row.get("job_listings_url", "")
        patch: dict = {}
        land = details.get("land")
        if land:
            time.sleep(1.0)
            final_url, final_html = _resolve_apply_url(land)
            if final_url and not _is_adzuna_host(final_url) and final_url != url:
                row["job_listings_url"] = final_url
                patch["Job Listings URL"] = {"url": final_url}
            # employer page can backfill a missing description/employer
            if final_html:
                emp = _job_posting_from_html(final_html)
                details.setdefault("description", emp.get("description", ""))
                details.setdefault("employer", emp.get("employer", ""))
        # Fill the scraped snippet when blank so Claude has source text (the
        # 'no description' fix — raw Adzuna rows arrive with no snippet).
        if details.get("description") and not row.get("scraped_snippet"):
            row["scraped_snippet"] = details["description"][:2000]
            patch["Scraped Snippet"] = {
                "rich_text": [{"text": {"content": details["description"][:2000]}}]}
        if details.get("employer") and not row.get("employer"):
            row["employer"] = details["employer"]
            patch["Employer"] = {
                "rich_text": [{"text": {"content": details["employer"][:200]}}]}
        if patch:
            try:
                update_page(row["notion_page_id"], properties=patch)
                print(f"    ↳ adzuna drill-down: {row.get('employer', '?')[:40]} "
                      f"({', '.join(patch)})")
            except Exception as e:
                print(f"    ⚠ couldn't persist adzuna drill-down: {e}")

    if dropped_ids:
        pool[:] = [r for r in pool if r["notion_page_id"] not in dropped_ids]
    if dropped_old:
        print(f"    ↳ {dropped_old} stale posting(s) (>{MAX_LISTING_AGE_DAYS}d) dropped")


def main() -> int:
    skill_prompt = load_skill_prompt()
    print("In Search Of pipeline — Claude blurb pass")

    for newsletter in filter_by_env():
        nl_name = newsletter["name"]
        print(f"\n{'=' * 60}")
        print(f"Processing: {nl_name} ({newsletter['display_area']})")
        print(f"{'=' * 60}")

        rows = fetch_jobs_pool(nl_name)
        if not rows:
            print(f"  No In Search Of rows for {nl_name}; skipping")
            continue
        pool = select_pool(rows)
        if not pool:
            print(f"  No approved or pending rows for {nl_name}; skipping")
            continue
        # Drill raw Adzuna details pages → real apply URL + job description
        # (fills the snippet so the blurb isn't empty) before the Claude pass.
        enrich_adzuna_rows(pool)
        results = call_claude(pool, nl_name, skill_prompt)
        if not results:
            print(f"  Claude returned nothing for {nl_name}")
            continue
        updated, dropped = apply_results(pool, results)
        print(f"  ✓ {updated} updated, {dropped} dropped for {nl_name}")
        time.sleep(0.5)

    print("\nAll newsletters complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
