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
import re
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Friday floor
# ---------------------------------------------------------------------------
def upcoming_friday(today: date | None = None) -> date:
    """Next Friday on or after `today` (today if today is Friday)."""
    today = today or date.today()
    return today + timedelta(days=(4 - today.weekday()) % 7)


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
def filter_candidates_by_date(candidates: list[dict], floor: date,
                              text_keys: tuple[str, ...] = ("title", "summary", "url", "source_url")
                              ) -> tuple[list[dict], list[str]]:
    """Drop candidates whose ONLY parseable dates are < floor.

    Returns (kept, dropped_urls). Candidates with no parseable dates are
    kept (we'd rather forward a borderline candidate to Claude than
    false-drop a real upcoming event with vague wording).

    `text_keys` — fields on the candidate dict to scan. Defaults to title +
    summary; pass ('title','summary','full_text') if the section enriches
    candidates with article body before filtering."""
    kept, dropped_urls = [], []
    for c in candidates:
        text = " ".join(str(c.get(k, "") or "") for k in text_keys)
        dates = extract_dates_from_text(text)
        if dates and all(d < floor for d in dates):
            print(f"    ✗ past-only candidate dropped: "
                  f"{(c.get('title') or c.get('event_name') or '')[:70]!r} "
                  f"(dates={[d.isoformat() for d in dates]})")
            dropped_urls.append(c.get("url") or c.get("source_url") or "")
            continue
        kept.append(c)
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


def filter_candidates_in_date_range(candidates: list[dict],
                                    start: date, end: date,
                                    text_keys: tuple[str, ...] = ("title", "summary", "url", "source_url")
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
