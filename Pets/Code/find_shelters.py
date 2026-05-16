#!/usr/bin/env python3
"""Discover RescueGroups shelters near a newsletter's zip code.

Used when setting up ORG_PLAN for a new newsletter. Lists every
shelter that has at least one available animal within a configurable
radius of the newsletter's anchor zip, sorted by total available
animals (cats + dogs). Prints city/state/website/contact info so you
can pick which orgs to wire into Pets/Code/Furry_Friends_Marietta.py.

Usage:
    RESCUE_GROUP_API_KEY=... python3 "Pets/Code/find_shelters.py" \\
        [NEWSLETTER_NAME] [RADIUS_MILES]

Defaults: NEWSLETTER_NAME=Lewisville_Lake_Lookout, RADIUS_MILES=25.

Examples:
    python3 "Pets/Code/find_shelters.py"
    python3 "Pets/Code/find_shelters.py" Lewisville_Lake_Lookout 35
    python3 "Pets/Code/find_shelters.py" Perimeter_Post 30

Read-only — no Notion writes, no env vars beyond RESCUE_GROUP_API_KEY
and (optionally) NEWSLETTER override.
"""
import os
import sys
from collections import defaultdict

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "NewsletterCreation", "Code"))
from newsletters_config import get_newsletter, newsletter_names  # noqa: E402

API_KEY  = os.environ.get("RESCUE_GROUP_API_KEY", "")
API_BASE = "https://api.rescuegroups.org/v5/public"
PAGE_LIMIT = 100


def _attr(d: dict, *keys) -> str:
    """Return the first non-empty attribute value across `keys`.
    RescueGroups org attribute names vary (e.g. `city` vs `addressCity`)
    so we try several before giving up."""
    a = d.get("attributes") or {}
    for k in keys:
        v = a.get(k)
        if v:
            return str(v)
    return ""


def main() -> int:
    if not API_KEY:
        print("✗ RESCUE_GROUP_API_KEY not set in env.")
        return 1
    nl_name = sys.argv[1] if len(sys.argv) > 1 else "Lewisville_Lake_Lookout"
    radius  = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    nl = get_newsletter(nl_name)
    if not nl:
        print(f"✗ Unknown newsletter: {nl_name}")
        print(f"  Known: {newsletter_names()}")
        return 1

    zipc = nl["zip"]
    print(f"Searching RescueGroups for adoptable animals within {radius} mi of "
          f"{zipc} ({nl['display_area']})\n")

    orgs: dict[str, dict] = {}                       # org_id -> attrs
    counts: dict[str, dict] = defaultdict(           # org_id -> {cat, dog, total}
        lambda: {"cat": 0, "dog": 0, "total": 0})

    page = 1
    total_pages = 1
    while page <= total_pages:
        body = {"data": {"filterRadius": {"miles": radius, "postalcode": zipc}}}
        params = {"include": "orgs", "page": page, "limit": PAGE_LIMIT}
        try:
            r = requests.post(
                f"{API_BASE}/animals/search/available",
                headers={"Authorization":  API_KEY,
                         "Content-Type":   "application/vnd.api+json"},
                params=params, json=body, timeout=30,
            )
        except Exception as e:
            print(f"  ✗ network error: {e}")
            break
        if r.status_code != 200:
            print(f"  ✗ HTTP {r.status_code}: {r.text[:300]}")
            break
        j = r.json()

        # Index any org records included in the response.
        for inc in (j.get("included") or []):
            if inc.get("type") != "orgs":
                continue
            oid = str(inc.get("id"))
            if oid in orgs:
                continue
            orgs[oid] = {
                "name":   _attr(inc, "name", "orgName"),
                "city":   _attr(inc, "city", "addressCity"),
                "state":  _attr(inc, "state", "addressState"),
                "zip":    _attr(inc, "postalcode", "postalCode", "addressPostalCode"),
                "url":    _attr(inc, "url", "website", "websiteUrl"),
                "email":  _attr(inc, "email"),
                "phone":  _attr(inc, "phone"),
            }

        # Tally per-org cat/dog counts from this page of animals.
        for d in (j.get("data") or []):
            attr = d.get("attributes") or {}
            rels = d.get("relationships") or {}
            org_rel = (rels.get("orgs") or {}).get("data") or []
            oid = str((org_rel[0] or {}).get("id")) if org_rel else ""
            if not oid:
                continue
            species = (attr.get("speciesSingular") or attr.get("species") or "").lower()
            counts[oid]["total"] += 1
            if species == "cat":
                counts[oid]["cat"] += 1
            elif species == "dog":
                counts[oid]["dog"] += 1

        meta = (j.get("meta") or {}).get("pagination") or {}
        total_pages = meta.get("totalPages", 1)
        if page == 1:
            print(f"  RescueGroups reports {meta.get('totalRecords', 0)} "
                  f"available animals in radius ({total_pages} page(s))")
        page += 1

    if not orgs:
        print("\nNo shelters found.")
        return 0

    items = sorted(
        orgs.items(),
        key=lambda kv: counts[kv[0]]["total"],
        reverse=True,
    )
    print(f"\n{len(orgs)} unique shelter(s) with adoptable animals "
          f"(sorted by total available):\n")
    print(f"  {'TOTAL':>5} {'CAT':>4} {'DOG':>4}  "
          f"{'SHELTER':45}  {'CITY, ST':22}  WEBSITE")
    print(f"  {'-'*5} {'-'*4} {'-'*4}  "
          f"{'-'*45}  {'-'*22}  {'-'*40}")
    for oid, o in items:
        c = counts[oid]
        loc = ", ".join(p for p in (o["city"], o["state"]) if p)
        print(f"  {c['total']:>5} {c['cat']:>4} {c['dog']:>4}  "
              f"{o['name'][:45]:45}  {loc[:22]:22}  {o['url'][:40]}")

    # Detail block: contact info for the top 15 so you can vet them
    # against ORG_PLAN's name_filter / url_template format.
    print(f"\n--- Contact details (top {min(15, len(items))}) ---")
    for oid, o in items[:15]:
        c = counts[oid]
        print(f"\n  {o['name']}  [id={oid}]")
        print(f"    Available: {c['total']} ({c['cat']} cats, {c['dog']} dogs)")
        loc = ", ".join(p for p in (o["city"], o["state"], o["zip"]) if p)
        if loc:   print(f"    Location:  {loc}")
        if o["url"]:   print(f"    Website:   {o['url']}")
        if o["phone"]: print(f"    Phone:     {o['phone']}")
        if o["email"]: print(f"    Email:     {o['email']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
