#!/usr/bin/env python3
"""
Shared date-floor filtering for event-driven newsletter sections
(Featured Event, Free Events, Weekend Planner).

When the pipeline runs on Monday, the issue won't reach readers until later
in the week — anything dated before this week's Friday is past by send
time and must be excluded. We do this in two layers:

1. **Pre-Claude:** scan each candidate's title + summary for date strings.
   If every parseable date is before the floor, drop the candidate. If no
   dates are parseable, keep it (Claude can sort out vague "this weekend"
   wording without us false-dropping a real event).

2. **Post-Claude:** parse the `date` field on each event Claude returned
   and drop anything dated before the floor (belt-and-suspenders against
   the model leaking past events past the prompt-level instruction).

Public API:
    upcoming_friday(today=None)               -> date
    parse_event_date(text, today=None)        -> date | None
    extract_dates_from_text(text, today=None) -> list[date]
    filter_candidates_by_date(candidates, floor) -> (kept, dropped_urls)
    filter_past_events(events, floor)         -> kept events
"""
import os
import re
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Friday floor
# ---------------------------------------------------------------------------
def upcoming_friday(today: date | None = None) -> date:
    """Next Friday on or after `today` (today if today is Friday).
    When `today` is None, calls `effective_today()` which transparently
    honors the ISSUE_DATE env override. This means callers don't need
    to thread issue_date through manually."""
    today = today if today is not None else effective_today()
    return today + timedelta(days=(4 - today.weekday()) % 7)


def effective_today() -> date:
    """The 'today' that all event-section date math anchors to. When
    ISSUE_DATE env var is set (MM/DD/YYYY), returns that date instead of
    the real today. Lets workflows target a future or past issue."""
    issue_date = parse_issue_date(os.environ.get("ISSUE_DATE"))
    return issue_date or date.today()


# ---------------------------------------------------------------------------
# Issue-date override (workflow_dispatch input)
# ---------------------------------------------------------------------------
def parse_issue_date(arg: str | None) -> date | None:
    """Parse an MM/DD/YYYY string from the ISSUE_DATE env var / workflow
    input. Returns None for empty / missing input. Raises ValueError on a
    bad format so the workflow fails loudly instead of silently falling
    back to today.

    Most sections shouldn't call this directly — use `effective_today()`,
    `section_date_window()`, or `brave_freshness_for_issue()` which
    consume ISSUE_DATE transparently."""
    if not arg or not arg.strip():
        return None
    s = arg.strip()
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        raise ValueError(
            f"Invalid ISSUE_DATE '{s}' — expected MM/DD/YYYY (e.g. 05/21/2026)"
        ) from None


def section_date_window(default_days: int = 14) -> tuple[date, date | None]:
    """Returns (floor, ceiling) for a section's date window.

    With ISSUE_DATE set: (issue_date, issue_date + 6 days). That's
    Thursday (issue date) through next Wednesday inclusive — a strict
    7-day window targeting that issue's coverage week.

    Without ISSUE_DATE: (upcoming_friday(), None). Ceiling is None so
    sections can apply their own default upper bound (typically
    floor + default_days for the Notion query) without enforcing it
    as a hard post-Claude filter — preserves current 'this week-ish'
    behavior.

    Used by Featured Event and Free Events."""
    issue_date = parse_issue_date(os.environ.get("ISSUE_DATE"))
    if issue_date:
        return (issue_date, issue_date + timedelta(days=6))
    return (upcoming_friday(), None)


def brave_freshness_for_issue(lookback_days: int = 10) -> str:
    """Brave news API `freshness` param. With ISSUE_DATE set, returns
    'YYYY-MM-DDtoYYYY-MM-DD' for [issue_date - lookback_days, issue_date].
    Without ISSUE_DATE, returns 'pw' (past week, Brave preset).

    Used by Local Lowdown so news lookback anchors to the issue's
    Thursday instead of today."""
    issue_date = parse_issue_date(os.environ.get("ISSUE_DATE"))
    if not issue_date:
        return "pw"
    lookback_start = issue_date - timedelta(days=lookback_days)
    return f"{lookback_start.isoformat()}to{issue_date.isoformat()}"


def filter_events_to_window(events: list[dict], floor: date,
                            ceiling: date | None = None,
                            date_key: str = "date") -> list[dict]:
    """Unified post-Claude date-window filter. Drops events whose
    `date_key` parses outside [floor, ceiling]. When ceiling is None,
    only the floor is enforced (equivalent to filter_past_events).
    Events with unparseable dates are KEPT (same convention as the
    other filters — pre-Claude is the primary gate).

    Replaces the floor-only vs floor+ceiling branching that FE / Free
    Events previously had inline."""
    kept = []
    for e in events:
        parsed = parse_event_date(e.get(date_key, ""))
        if parsed:
            if parsed < floor:
                print(f"  ✗ Dropping past-dated event: "
                      f"{e.get('event_name', e.get('title','?'))} "
                      f"({e.get(date_key, '?')} → {parsed})")
                continue
            if ceiling is not None and parsed > ceiling:
                print(f"  ✗ Dropping out-of-range event: "
                      f"{e.get('event_name', e.get('title','?'))} "
                      f"({e.get(date_key, '?')} → {parsed}; "
                      f"window {floor}..{ceiling})")
                continue
        kept.append(e)
    return kept


# ---------------------------------------------------------------------------
# Single-string date parser (used post-Claude on the `date` field)
# ---------------------------------------------------------------------------
_DATE_FORMATS = [
    "%A, %B %d, %Y", "%A, %B %d", "%B %d, %Y", "%B %d",
    "%a, %b %d, %Y",  "%a, %b %d",  "%b %d, %Y",  "%b %d",
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d", "%m-%d-%Y", "%m-%d",
]


def parse_event_date(text: str, today: date | None = None) -> date | None:
    """Best-effort parse of free-form date strings (e.g.
    'Saturday, May 10', 'May 17 2026', '5/22'). Returns None if nothing
    parses. Year-less inputs are anchored to current year, bumping to next
    year only if the result is more than 60 days in the past."""
    if not text:
        return None
    today = today or date.today()
    s = text.strip().rstrip(",.")
    # Strip range tails — keep first chunk
    for sep in (" to ", " - ", "–", "—"):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
            break
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=today.year)
            if (today - parsed).days > 60:
                parsed = parsed.replace(year=today.year + 1)
        return parsed
    return None


# ---------------------------------------------------------------------------
# Multi-date extractor (used pre-Claude on title + summary)
# ---------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July",
     "August","September","October","November","December"], 1)}
_MONTHS.update({m.lower(): i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul",
     "Aug","Sep","Sept","Oct","Nov","Dec"], 1)})

_MONTH_DAY_RE = re.compile(
    r"\b(?P<mon>january|february|march|april|may|june|july|august|"
    r"september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|oct|nov|dec)\.?"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:[\s,-]+(?P<year>\d{4}))?",
    re.IGNORECASE,
)
# Matches "May 16-17" / "May 16 - 17" / "May 16th-17th" — same-month ranges.
# day2 must be numerically > day1 and ≤ 31. We expand the range so each
# day in [day1..day2] is yielded as its own date.
_MONTH_DAY_RANGE_RE = re.compile(
    r"\b(?P<mon>january|february|march|april|may|june|july|august|"
    r"september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|oct|nov|dec)\.?"
    r"\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s*[-–—]\s*(?P<d2>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:[\s,-]+(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_SLASH_DATE_RE = re.compile(
    r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b"
)
_ISO_DATE_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")


def extract_dates_from_text(text: str, today: date | None = None) -> list[date]:
    """Return all dates we can parse out of `text`. Year-less dates anchor
    to the current year, bumping to next year if the result is >60 days in
    the past — keeps Dec → Jan rollover sane."""
    if not text:
        return []
    today = today or date.today()
    found: list[date] = []

    def _anchor_year(d: date) -> date:
        if (today - d).days > 60:
            return d.replace(year=today.year + 1)
        return d

    # Run range matcher FIRST and track spans we've covered so the single-
    # day matcher doesn't also add the start day separately (would be
    # duplicative). For each range, expand to every day in between.
    range_spans: list[tuple[int, int]] = []
    for m in _MONTH_DAY_RANGE_RE.finditer(text):
        mo = _MONTHS.get(m.group("mon").lower())
        if not mo:
            continue
        try:
            d1 = int(m.group("d1"))
            d2 = int(m.group("d2"))
            if not (1 <= d1 <= 31 and 1 <= d2 <= 31 and d2 >= d1):
                continue
            year = int(m.group("year")) if m.group("year") else today.year
            anchor = date(year, mo, d1)
            if not m.group("year"):
                anchor = _anchor_year(anchor)
            year_used = anchor.year
            for d_num in range(d1, d2 + 1):
                try:
                    found.append(date(year_used, mo, d_num))
                except (ValueError, TypeError):
                    continue
            range_spans.append(m.span())
        except (ValueError, TypeError):
            continue

    for m in _MONTH_DAY_RE.finditer(text):
        # Skip matches that fall inside a range we already expanded
        if any(s <= m.start() and m.end() <= e for s, e in range_spans):
            continue
        mo = _MONTHS.get(m.group("mon").lower())
        if not mo:
            continue
        try:
            day = int(m.group("day"))
            year = int(m.group("year")) if m.group("year") else today.year
            d = date(year, mo, day)
            if not m.group("year"):
                d = _anchor_year(d)
            found.append(d)
        except (ValueError, TypeError):
            continue

    for m in _SLASH_DATE_RE.finditer(text):
        try:
            mo, da = int(m.group("m")), int(m.group("d"))
            yr = m.group("y")
            if yr:
                yr = int(yr)
                if yr < 100:
                    yr += 2000
            else:
                yr = today.year
            d = date(yr, mo, da)
            if not m.group("y"):
                d = _anchor_year(d)
            found.append(d)
        except (ValueError, TypeError):
            continue

    for m in _ISO_DATE_RE.finditer(text):
        try:
            found.append(date(int(m.group("y")), int(m.group("m")), int(m.group("d"))))
        except (ValueError, TypeError):
            continue

    return found


# ---------------------------------------------------------------------------
# Filter functions
# ---------------------------------------------------------------------------
def _recover_date_via_page_fetch(url: str, max_chars: int = 60_000) -> list[date]:
    """For a candidate with no parseable date in its title/summary/etc.,
    fetch the source page and scan the body for date mentions.

    Free (one HTTP request per missing-date candidate). Catches common
    "event detail" pages whose body has `Saturday, May 24, 2026` but the
    title/summary doesn't.
    """
    if not url:
        return []
    try:
        import requests as _r
        resp = _r.get(url, timeout=10,
                      headers={"User-Agent": "Mozilla/5.0 (newsletter-bot)"},
                      allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            return []
        # Strip tags cheaply — we just need body text
        import re as _re
        body = _re.sub(r"<[^>]+>", " ", resp.text[:max_chars])
        return extract_dates_from_text(body)
    except Exception:
        return []


def filter_candidates_by_date(candidates: list[dict], floor: date,
                              text_keys: tuple[str, ...] = ("title", "summary", "url", "source_url", "listicle_date_hint"),
                              recover_missing: bool = True,
                              max_page_fetches: int = 20,
                              ) -> tuple[list[dict], list[str]]:
    """Drop candidates whose ONLY parseable dates are < floor.

    Returns (kept, dropped_urls). Candidates with no parseable dates are
    kept (we'd rather forward a borderline candidate to Claude than
    false-drop a real upcoming event with vague wording).

    `text_keys` — fields on the candidate dict to scan. Defaults to title +
    summary; pass ('title','summary','full_text') if the section enriches
    candidates with article body before filtering.

    `recover_missing` — if True, for candidates with no parseable date in
    text_keys, fetch the source URL and scan its body for dates. Bounded
    by `max_page_fetches` to keep the run cheap.
    """
    kept, dropped_urls = [], []
    fetches_used = 0
    for c in candidates:
        text = " ".join(str(c.get(k, "") or "") for k in text_keys)
        dates = extract_dates_from_text(text)
        # Try page-fetch recovery for candidates with NO parseable dates.
        if not dates and recover_missing and fetches_used < max_page_fetches:
            url = c.get("url") or c.get("source_url") or ""
            dates = _recover_date_via_page_fetch(url)
            fetches_used += 1
            if dates:
                # Stash on the candidate so later stages see it too
                c["dates_found_via_page_fetch"] = [d.isoformat() for d in dates]
                print(f"    ↳ recovered dates via page fetch: "
                      f"{(c.get('title') or '')[:60]!r} → {[d.isoformat() for d in dates]}")
        if dates and all(d < floor for d in dates):
            print(f"    ✗ past-only candidate dropped: "
                  f"{(c.get('title') or c.get('event_name') or '')[:70]!r} "
                  f"(dates={[d.isoformat() for d in dates]})")
            dropped_urls.append(c.get("url") or c.get("source_url") or "")
            continue
        kept.append(c)
    if fetches_used:
        print(f"    [date-recovery] fetched {fetches_used} page(s) to look for missing dates")
    return kept, [u for u in dropped_urls if u]


def filter_past_events(events: list[dict], floor: date,
                       date_key: str = "date") -> list[dict]:
    """Post-Claude filter: drop events whose `date_key` field parses to a
    date < floor. Events with unparseable dates are kept."""
    kept = []
    for e in events:
        parsed = parse_event_date(e.get(date_key, ""))
        if parsed and parsed < floor:
            print(f"  ✗ Dropping past-dated event: "
                  f"{e.get('event_name', e.get('title','?'))} "
                  f"({e.get(date_key, '?')} → {parsed})")
            continue
        kept.append(e)
    return kept


def filter_events_in_date_range(events: list[dict], start: date, end: date,
                                date_key: str = "date") -> list[dict]:
    """Post-Claude filter: drop events whose `date_key` field parses to a
    date OUTSIDE [start, end] inclusive. Events with unparseable dates are
    KEPT (same convention as filter_past_events) — the candidate-side
    pre-filter is the primary date gate, this is belt-and-suspenders.

    Used by Featured Event + Free Events when an issue_date override pins
    the section to a specific Thu..next-Wed window."""
    kept = []
    for e in events:
        parsed = parse_event_date(e.get(date_key, ""))
        if parsed and not (start <= parsed <= end):
            print(f"  ✗ Dropping out-of-range event: "
                  f"{e.get('event_name', e.get('title','?'))} "
                  f"({e.get(date_key, '?')} → {parsed}; window {start}..{end})")
            continue
        kept.append(e)
    return kept


def filter_candidates_in_date_range(candidates: list[dict],
                                    start: date, end: date,
                                    text_keys: tuple[str, ...] = ("title", "summary", "url", "source_url", "listicle_date_hint")
                                    ) -> tuple[list[dict], list[str]]:
    """Stricter sibling of `filter_candidates_by_date`. Keeps a candidate
    ONLY if at least one parsed date in its text falls inside [start, end]
    inclusive.

    Used for Weekend Planner: we want only events happening THIS Fri-Sun,
    not anything later in the month. Candidates with no parseable dates
    are KEPT (we still let Claude evaluate them — many real events use
    vague wording like 'this weekend')."""
    kept, dropped_urls = [], []
    for c in candidates:
        text = " ".join(str(c.get(k, "") or "") for k in text_keys)
        dates = extract_dates_from_text(text)
        if not dates:
            kept.append(c)
            continue
        if any(start <= d <= end for d in dates):
            kept.append(c)
            continue
        # Has dates but NONE in the target range — drop
        dropped_urls.append(c.get("url") or c.get("source_url") or "")
        print(f"    ✗ out-of-range candidate dropped: "
              f"{(c.get('title') or c.get('event_name') or '')[:70]!r} "
              f"(dates={[d.isoformat() for d in dates]}, range={start}..{end})")
    return kept, [u for u in dropped_urls if u]
