"""HTML / text utilities shared across every weekend-event scraper.

Lives in _shared/ so per-newsletter scraper folders (East_Cobb_Connect,
Perimeter_Post, Lewisville_Lake_Lookout) can all import the same
parsing logic without duplicating code.

Functions:
  _clean_html(s)       — strip tags, decode all HTML entities, normalize ws
  _normalize_title     — punctuation-stripped lowercase title key for dedup
  _parse_iso_date      — ISO-8601 → date (timezone-tolerant)
  _parse_dt            - ISO-8601 → (date, display time)
  _normalize_image     - JSON-LD image → URL
  _normalize_location  - JSON-LD location → (name, address, city)
  fetch_with_retry     - GET with retry on transient codes → body text
"""
from __future__ import annotations

import html
import re
import time
from datetime import date, datetime

import requests


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


def _parse_dt(s) -> tuple[date | None, str]:
    """ISO datetime → (date, 'H:MM AM/PM' or '' for midnight / date-only).
    Falls back to the leading YYYY-MM-DD when the full string won't parse."""
    if not s:
        return None, ""
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date(), dt.strftime("%-I:%M %p") if (dt.hour or dt.minute) else ""
    except Exception:
        pass
    try:
        return date.fromisoformat(s[:10]), ""
    except Exception:
        return None, ""


def _normalize_image(image) -> str:
    """JSON-LD `image` (str | ImageObject dict | list of either) → URL str."""
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        return image.get("url", "") or ""
    if isinstance(image, list) and image:
        first = image[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url", "") or ""
    return ""


def _normalize_location(location) -> tuple[str, str, str]:
    """JSON-LD `location` → (location_name, address, city). City is returned
    separately for the Notion City column / shared-tag sweep."""
    if not location:
        return "", "", ""
    if isinstance(location, str):
        return _clean_html(location), "", ""
    if isinstance(location, dict):
        name = _clean_html(location.get("name", "") or "")
        city = ""
        addr_obj = location.get("address", "")
        if isinstance(addr_obj, dict):
            city = (addr_obj.get("addressLocality", "") or "").strip()
            parts = [
                addr_obj.get("streetAddress", ""),
                city,
                addr_obj.get("addressRegion", ""),
            ]
            addr = _clean_html(", ".join(p for p in parts if p))
        else:
            addr = _clean_html(str(addr_obj or ""))
        return name, addr, _clean_html(city).lower()
    return "", "", ""


def fetch_with_retry(url: str, headers: dict, timeout: int) -> str:
    """GET `url` (following redirects) and return the body text, or '' on
    failure. Retries transport errors and the transient codes some
    Cloudflare-fronted sites use as soft bot-checks (202, 429, 503)."""
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, headers=headers,
                             allow_redirects=True)
        except Exception as e:
            print(f"    fetch error (attempt {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200 and r.text:
            return r.text
        if r.status_code in (202, 429, 503) and attempt < 2:
            wait = 3 * (attempt + 1)
            print(f"    HTTP {r.status_code} — retry {attempt + 1}/3 in {wait}s")
            time.sleep(wait)
            continue
        print(f"    HTTP {r.status_code} from {url}")
        return ""
    return ""


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
