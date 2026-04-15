#!/usr/bin/env python3
"""
Shared URL validator for newsletter pipelines.
Checks if URLs are live before saving to Notion.
Dead critical URLs → item rejected. Dead optional URLs → field blanked out.
"""
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


USER_AGENT = "Mozilla/5.0 (compatible; newsletter-bot/1.0)"
DEFAULT_TIMEOUT = 8


def validate_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """Check if a URL is live. HEAD first, fallback to GET.
    Returns True if status 200-399. Skips empty/None URLs (treated as valid)."""
    if not url or not url.strip():
        return True  # empty field is fine, not a dead link

    headers = {"User-Agent": USER_AGENT}

    try:
        # Try HEAD first (fast, no body download)
        r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code < 400:
            return True
        # Some servers reject HEAD — fallback to GET
        if r.status_code in (405, 403, 406):
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
            return r.status_code < 400
        return False
    except (requests.RequestException, Exception):
        return False


def validate_urls(url_dict: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Validate multiple URLs in parallel.
    Takes {"field_name": "url_string", ...}.
    Returns {"field_name": True/False, ...}."""
    results = {}

    # Run checks in parallel for speed
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for field, url in url_dict.items():
            futures[executor.submit(validate_url, url, timeout)] = field

        for future in as_completed(futures):
            field = futures[future]
            try:
                results[field] = future.result()
            except Exception:
                results[field] = False

    return results


def filter_valid_items(
    items: list[dict],
    critical_fields: list[str],
    optional_fields: list[str] | None = None,
    label_field: str = "name",
) -> tuple[list[dict], list[dict]]:
    """Split items into (valid, invalid) based on URL field checks.

    - critical_fields: URL fields that must be live. If any critical URL is dead,
      the item is rejected entirely.
    - optional_fields: URL fields that are nice-to-have. If dead, the field is
      blanked out but the item is kept.
    - label_field: field name used for log messages (e.g., "name", "restaurant_name")

    Returns (valid_items, rejected_items).
    """
    if optional_fields is None:
        optional_fields = []

    valid = []
    rejected = []

    for item in items:
        name = item.get(label_field, item.get("name", "unknown"))
        all_fields = critical_fields + optional_fields
        urls_to_check = {f: item.get(f, "") for f in all_fields}

        # Skip items with no URLs to check
        non_empty = {f: u for f, u in urls_to_check.items() if u}
        if not non_empty:
            valid.append(item)
            continue

        results = validate_urls(non_empty)

        # Check critical fields
        critical_dead = [f for f in critical_fields if f in results and not results[f]]
        if critical_dead:
            print(f"    ✗ Dead critical URL for {name}: {', '.join(critical_dead)}")
            for f in critical_dead:
                print(f"      {f}: {item.get(f, '')[:80]}")
            rejected.append(item)
            continue

        # Blank out dead optional fields
        for f in optional_fields:
            if f in results and not results[f]:
                print(f"    ⚠ Dead optional URL for {name}: {f} (blanked out)")
                item[f] = ""

        valid.append(item)

    print(f"  URL validation: {len(valid)} valid, {len(rejected)} rejected")
    return valid, rejected
