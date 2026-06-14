"""HTML / text utilities shared across every weekend-event scraper.

Lives in _shared/ so per-newsletter scraper folders (East_Cobb_Connect,
Perimeter_Post, Lewisville_Lake_Lookout) can all import the same
parsing logic without duplicating code.

Functions:
  _clean_html(s)       — strip tags, decode all HTML entities, normalize ws
  format_dates_human   — "May 22nd, 29th, June 5th" from a set of dates
  _normalize_title     — punctuation-stripped lowercase title key for dedup
  _parse_iso_date      — ISO-8601 → date (timezone-tolerant)
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime


def _clean_html(s: str) -> str:
    """Strip HTML tags and decode HTML entities. Description and name
    fields arrive HTML-escaped inside JSON-LD (literal `&#8217;`, `&amp;`,
    etc.) — html.unescape handles named, decimal, and hex entities in one
    pass. Tags are stripped after decoding.

    Also strips stray `\\'` and `\\"` sequences — batteryatl.com (and a
    few other Tribe Events sites) emit invalid JSON escapes that
    json.loads passes through as literal backslash-quote pairs."""
    if not s:
        return ""
    s = html.unescape(s)
    s = s.replace("\\'", "'").replace('\\"', '"')
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def format_dates_human(dates) -> str:
    """Format an iterable of date objects as 'May 22nd, 29th, June 5th'.
    Groups consecutive same-month entries under one month name and adds
    English ordinal suffixes to the day numbers."""
    seen = sorted(set(d for d in dates if d))
    if not seen:
        return ""

    def _ord(n: int) -> str:
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suffix}"

    chunks: list[str] = []
    cur_key: tuple[int, int] | None = None
    cur_month_name = ""
    cur_days: list[str] = []
    for d in seen:
        key = (d.year, d.month)
        if key != cur_key:
            if cur_days:
                chunks.append(f"{cur_month_name} {', '.join(cur_days)}")
            cur_key = key
            cur_month_name = d.strftime("%B")
            cur_days = [_ord(d.day)]
        else:
            cur_days.append(_ord(d.day))
    if cur_days:
        chunks.append(f"{cur_month_name} {', '.join(cur_days)}")
    return ", ".join(chunks)


def _normalize_title(t: str) -> str:
    """Lowercased, punctuation-stripped title key used for cross-source
    dedup. 'Marietta Greek Festival 2026' and 'The Marietta Greek
    Festival' both reduce to 'marietta greek festival' so they collide
    in the (title, date) dedup set."""
    if not t:
        return ""
    s = t.lower()
    s = re.sub(r"\b20\d{2}\b", "", s)         # strip 4-digit years
    s = re.sub(r"[^a-z0-9 ]+", " ", s)        # strip punctuation
    s = re.sub(r"^(the|a|an)\s+", "", s)      # leading article
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_iso_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


# Currency code → symbol for the few we'd ever see; default to the bare code.
_CURRENCY_SYMBOLS = {"USD": "$", "CAD": "$", "GBP": "£", "EUR": "€"}


def _fmt_amount(value, currency: str) -> str:
    """Format a single numeric price: '0' → 'Free', else '$25' / '$25.50'."""
    try:
        amt = float(value)
    except (TypeError, ValueError):
        return ""
    if amt <= 0:
        return "Free"
    sym = _CURRENCY_SYMBOLS.get((currency or "USD").upper(), (currency or "").upper() + " ")
    # Drop trailing .0 so whole-dollar prices read '$25' not '$25.00'.
    return f"{sym}{amt:.2f}".rstrip("0").rstrip(".") if amt % 1 else f"{sym}{int(amt)}"


def parse_jsonld_price(event: dict) -> str:
    """Extract a human display price from a schema.org Event's `offers`.

    Handles the shapes seen in the wild:
      - a single Offer dict: {"price": "0"|"25", "priceCurrency": "USD"}
      - a list of Offers (take the min non-zero, or 'Free' if any is 0)
      - an AggregateOffer: {"lowPrice": "10", "highPrice": "30"}
    Returns 'Free' for a 0 price, '$25' for a single price, '$10–$30' for a
    range, or '' when no price is published (caller leaves the field blank).
    A published price of 0 is the strongest 'this event is free' signal we
    can get — far better than scanning the blurb for the word 'free'."""
    offers = event.get("offers")
    if not offers:
        return ""
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return ""

    amounts: list[float] = []
    currency = "USD"
    saw_free = False
    for off in offers:
        if not isinstance(off, dict):
            continue
        currency = off.get("priceCurrency") or currency
        # AggregateOffer range
        lo, hi = off.get("lowPrice"), off.get("highPrice")
        if lo is not None or hi is not None:
            lo_s = _fmt_amount(lo, currency) if lo is not None else ""
            hi_s = _fmt_amount(hi, currency) if hi is not None else ""
            if lo_s == "Free" and not hi_s:
                saw_free = True
            elif lo_s and hi_s and lo_s != hi_s:
                return f"{lo_s.replace('Free', '$0')}–{hi_s}"
            elif lo_s or hi_s:
                return (lo_s or hi_s)
            continue
        price = off.get("price")
        if price is None:
            continue
        try:
            amt = float(price)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            saw_free = True
        else:
            amounts.append(amt)

    if amounts:
        return _fmt_amount(min(amounts), currency)
    if saw_free:
        return "Free"
    return ""
