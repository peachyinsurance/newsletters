#!/usr/bin/env python3
"""ONE-OFF diagnostic — does the Tribe scraper's fetch layer get past Cloudflare
on THIS runner? Calls the real tribe_events REST + HTML fetchers for a few
Cloudflare-fronted sites and prints how many events come back. READ-ONLY: it
never writes to Notion, so it's safe to run anywhere. Delete this file (and
test_tribe_scrape.yml) once the runner question is settled.
"""
import datetime
import os
import sys

# We only exercise the FETCH layer; stub any secrets the module wants at import
# so it loads without real Notion/Claude keys (we make zero API calls to them).
for _k in ("NOTION_API_KEY", "NOTION_WEEKEND_EVENTS_DB_ID", "CLAUDE_API_KEY",
           "BRAVE_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
           "NOTION_PARENT_PAGE_ID", "NOTION_PETS_DB_ID", "NOTION_RESTAURANTS_DB_ID"):
    os.environ.setdefault(_k, "x")

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "_shared"))
import tribe_events as T  # noqa: E402

SITES = [
    "https://visitmariettaga.com/events/",
    "https://www.kennesaw-ga.gov/events/category/events/",
    "https://travelcobb.org/cobb-county-events/",
    "https://batteryatl.com/events/",
]


def _public_ip() -> str:
    """Best-effort: show the egress IP so it's obvious whether this runner is
    on a datacenter IP (cloud) or a residential one (Mac Mini)."""
    try:
        return T._http_get("https://api.ipify.org", timeout=10).text.strip()
    except Exception as e:
        return f"(unknown: {e})"


def main() -> int:
    today = datetime.date.today()
    end = today + datetime.timedelta(days=20)
    # Touch the fetcher once so the curl_cffi session is created before we report.
    ip = _public_ip()
    print("=" * 64)
    print(f"  Runner:        {os.environ.get('RUNNER_NAME', '?')} "
          f"({os.environ.get('RUNNER_OS', '?')})")
    print(f"  Egress IP:     {ip}")
    print(f"  curl_cffi:     {'YES (Chrome impersonation)' if T._CFFI_SESSION else 'NO (plain requests)'}")
    print(f"  Window:        {today} → {end}")
    print("=" * 64)

    any_ok = False
    for url in SITES:
        print(f"\n━━ {url} ━━")
        try:
            rest = T.fetch_events_rest(url, today, end)
            if rest is None:
                print("  REST  → unavailable (None); would fall back to HTML")
            else:
                print(f"  REST  → {len(rest)} event(s)  {'✓' if rest else '(empty window)'}")
                any_ok = any_ok or bool(rest)
        except Exception as e:
            print(f"  REST  → ERROR {e}")
        try:
            html_evs = T.fetch_page_events(url, 1)
            print(f"  HTML  → {len(html_evs)} JSON-LD event(s) on page 1")
            any_ok = any_ok or bool(html_evs)
        except Exception as e:
            print(f"  HTML  → ERROR {e}")

    print("\n" + "=" * 64)
    print(f"  VERDICT: {'✅ this runner can scrape Tribe sites' if any_ok else '❌ blocked — got nothing'}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
