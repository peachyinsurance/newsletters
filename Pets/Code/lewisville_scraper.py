#!/usr/bin/env python3
"""Lewisville Animal Services adoptables scraper.

cityoflewisville.com/.../adoptable-pets embeds the same Petango widget
data, but the city's own domain 403s our requests. The Petango widget
host (ws.petango.com) accepts direct requests with the shelter's
authkey, so we go there directly. Everything is server-rendered HTML —
no Apify / JS rendering needed.

Endpoints:
  Listing: /webservices/adoptablesearch/wsAdoptableAnimals.aspx
           ?authkey=...&species=Dog|Cat&recAmount=100
  Detail:  /webservices/adoptablesearch/wsAdoptableAnimalDetails.aspx
           ?id=<pet_id>&authkey=...

Output shape matches Furry_Friends_Marietta.py's pipeline (one dict per
pet with name, species, breed, age, gender, description, photos,
shelter_name, shelter_address, etc.) so this drops into ORG_PLAN as a
custom-shelter fallback for the LLL newsletter.

Usage (standalone — prints 3 cats and 3 dogs):
    python3 "Pets/Code/lewisville_scraper.py"

Or import `fetch_lewisville_pets(target_per_species=3)` from another
module.
"""
import re
import sys
import time
import html as _html_mod

import requests

# Lewisville Animal Services authkey — copied from cityoflewisville.com's
# embedded widget. Shelter-specific; if Lewisville rotates the key the
# scraper will start 200-ing empty pages and we'd grab a fresh authkey
# from view-source on their adoptables page.
AUTHKEY = "201c6fecae1sc1t88i0knixefmqgeibx5fymf6avp0m2o4hq1x"
BASE    = "https://ws.petango.com/webservices/adoptablesearch"

# Shelter info — Petango lists "Lewisville Animal Services" as the site;
# we hard-code the rest from cityoflewisville.com so each saved pet
# carries the right contact block.
SHELTER_INFO = {
    "shelter_name":    "Lewisville Animal Services",
    "shelter_address": "995 College Pkwy, Lewisville, TX 75067",
    "shelter_phone":   "(972) 219-3478",
    "shelter_email":   "",
}

HEADERS = {
    "User-Agent":      "Mozilla/5.0",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# HTTP fetch with one retry on transient errors
# ---------------------------------------------------------------------------
def _fetch(url: str) -> str:
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print(f"    fetch error (attempt {attempt + 1}/2): {e}")
            time.sleep(2)
            continue
        if r.status_code == 200 and r.text:
            return r.text
        if r.status_code in (429, 503) and attempt < 1:
            time.sleep(3)
            continue
        print(f"    HTTP {r.status_code} from {url}")
        return ""
    return ""


# ---------------------------------------------------------------------------
# Listing parse — extract one record per pet from the search results page
# ---------------------------------------------------------------------------
# Raw Petango uses a <table><tr><td class="list-item"> structure; some
# embedders (e.g. cityoflewisville.com) rewrite it to <ul><li>. We're
# structure-agnostic — every pet contains exactly one
# `list-animal-photo-block` div, so we split on that marker.
_NAME_RE        = re.compile(r'<div class="list-animal-name">\s*<a[^>]*>([^<]+)</a>', re.DOTALL)
_ID_RE          = re.compile(r'<div class="list-animal-id">([^<]+)</div>')
_SPECIES_RE     = re.compile(r'<div class="list-animal-species">([^<]+)</div>')
_SEX_RE         = re.compile(r'<div class="list-animal-sexSN">([^<]+)</div>')
_BREED_RE       = re.compile(r'<div class="list-animal-breed">([^<]+)</div>')
_AGE_RE         = re.compile(r'<div class="list-animal-age">([^<]+)</div>')
_PHOTO_RE       = re.compile(r'<img class="list-animal-photo[^"]*"[^>]*src="([^"]+)"')
_DETAIL_LINK_RE = re.compile(r'href="(wsAdoptableAnimalDetails2?\.aspx\?[^"]+)"')


def _strip(s: str) -> str:
    return _html_mod.unescape((s or "").strip()).strip()


def parse_list(html: str) -> list[dict]:
    """Extract a record per pet from a listing page."""
    # Split on the per-pet photo-block marker. chunks[0] is the page
    # header; each later chunk holds one pet's HTML (with the marker
    # itself stripped — that's fine, our regexes look for inner divs).
    chunks = html.split('list-animal-photo-block')
    out: list[dict] = []
    for block in chunks[1:]:
        def _grab(rx):
            m = rx.search(block)
            return _strip(m.group(1)) if m else ""
        pet_id = _grab(_ID_RE)
        if not pet_id:
            continue
        detail_rel = _DETAIL_LINK_RE.search(block)
        detail_url = (f"{BASE}/{detail_rel.group(1)}"
                      if detail_rel else "")
        sex_sn = _grab(_SEX_RE)
        # "Male/Neutered" → ("Male", "Neutered"); "Female/Spayed" similar
        if "/" in sex_sn:
            gender, altered = (s.strip() for s in sex_sn.split("/", 1))
        else:
            gender, altered = sex_sn, ""
        out.append({
            "id":         pet_id,
            "name":       _grab(_NAME_RE),
            "species":    _grab(_SPECIES_RE),
            "breed":      _grab(_BREED_RE),
            "age":        _grab(_AGE_RE),
            "gender":     gender,
            "altered":    altered,
            "photo_url":  _grab(_PHOTO_RE),
            "detail_url": detail_url,
        })
    return out


# ---------------------------------------------------------------------------
# Detail-page parse — pulls description + supplementary fields
# ---------------------------------------------------------------------------
_DETAIL_SPANS = {
    "color":       r'<span id="lblColor">([^<]+)</span>',
    "site":        r'<span id="lblSite">([^<]+)</span>',
    "intake_date": r'<span id="lblIntakeDate">([^<]+)</span>',
    "arn":         r'<span id="lbARN">([^<]*)</span>',
}
_DETAIL_PHOTO_RE = re.compile(r'id="imgAnimalPhoto"[^>]*src="([^"]+)"')
# Additional photos: <a id="lnkPhoto2" ... href="URL">2</a>
# (The onclick handler also has the URL but its apostrophes are HTML-
# encoded as &#39; in the raw response — href is the clean source.)
_EXTRA_PHOTO_RE  = re.compile(
    r'<a id="lnkPhoto\d+"[^>]*\bhref="([^"]+)"',
    re.IGNORECASE,
)
_DESCRIPTION_RE  = re.compile(r'<span id="lbDescription">(.+?)</span>', re.DOTALL)


def _br_to_newlines(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return _html_mod.unescape(s).strip()


def parse_detail(html: str) -> dict:
    """Extract description, primary photo, additional photos, and
    supplementary fields from a Petango detail page.

    Photos: `imgAnimalPhoto` is the primary; <a id="lnkPhoto2/3/4…">
    elements with onclick="loadPhoto('URL')" expose the rest of the
    gallery. We dedup by URL and preserve order (primary first)."""
    out: dict = {}
    for k, rx in _DETAIL_SPANS.items():
        m = re.search(rx, html)
        if m:
            out[k] = _strip(m.group(1))
    photos: list[str] = []
    seen: set[str] = set()
    m = _DETAIL_PHOTO_RE.search(html)
    if m:
        primary = m.group(1)
        out["photo_url_large"] = primary
        photos.append(primary)
        seen.add(primary)
    for extra in _EXTRA_PHOTO_RE.findall(html):
        if extra and extra not in seen:
            seen.add(extra)
            photos.append(extra)
    out["photos"] = photos
    m = _DESCRIPTION_RE.search(html)
    if m:
        out["description"] = _br_to_newlines(m.group(1))[:2000]
    return out


# ---------------------------------------------------------------------------
# Card-to-pet normalization (matches Furry_Friends_Marietta.py's
# _rg_pet_to_pipeline_dict output so downstream code is shape-agnostic).
# ---------------------------------------------------------------------------
def _public_pet_url(pet_id: str, species: str) -> str:
    """Build the Petplace.com public-facing pet URL. Petango is being
    phased out into Petplace; the detail page itself links here via its
    'Favorite This Pet' button. URL pattern observed on the Lewisville
    detail page:
        https://www.petplace.com/pet-adoption/{cats|dogs}/{id}/PP3942
    The 'PP3942' suffix is a Petplace referrer code; safe to hard-code
    since the page renders even without it but Petplace treats it as
    the canonical incoming link."""
    if not pet_id:
        return ""
    sp = (species or "").lower()
    species_path = "dogs" if sp == "dog" else "cats" if sp == "cat" else "other"
    return f"https://www.petplace.com/pet-adoption/{species_path}/{pet_id}/PP3942"


def _to_pipeline_pet(listing: dict, detail: dict) -> dict:
    species_norm = (listing.get("species") or "").capitalize()
    name        = listing.get("name") or "Unknown"
    breed       = listing.get("breed", "")
    age         = listing.get("age", "")
    gender      = listing.get("gender", "")
    description = detail.get("description", "")
    # Public-facing Petplace link instead of the bare Petango widget URL.
    url         = _public_pet_url(listing.get("id", ""), species_norm)
    # Full gallery from the detail page (primary first, additional after).
    # Falls back to the small listing thumbnail if the detail fetch
    # somehow turned up nothing.
    photos      = list(detail.get("photos") or [])
    if not photos:
        thumb = listing.get("photo_url") or ""
        if thumb:
            photos = [thumb]
    org_info    = {
        "name":    SHELTER_INFO["shelter_name"],
        "address": SHELTER_INFO["shelter_address"],
        "phone":   SHELTER_INFO["shelter_phone"],
        "email":   SHELTER_INFO["shelter_email"],
        "hours":   "",
    }
    # Same multi-line "profile" string the RG path emits, so
    # build_combined_profiles + the Claude blurb prompt see identical
    # input regardless of source.
    profile = (
        f"Name: {name}\n"
        f"Species: {species_norm}\n"
        f"Breed: {breed}\n"
        f"Age: {age}\n"
        f"Gender: {gender}\n"
        f"Size: \n"
        f"Description: {description}\n"
        f"Shelter: {org_info['name']}\n"
        f"Address: {org_info['address']}\n"
        f"Phone: {org_info['phone']}\n"
        f"Email: {org_info['email']}"
    )
    return {
        "url":         url,
        "listing_url": url,
        "profile":     profile,
        "photos":      photos,
        "animal_type": species_norm.lower(),
        "org_info":    org_info,
        # Source-specific extras the rest of the pipeline ignores but
        # remain useful for debugging / future enhancements.
        "name":        name,
        "species":     species_norm,
        "breed":       breed,
        "age":         age,
        "gender":      gender,
        "color":       detail.get("color", ""),
        "altered":     listing.get("altered", ""),
        "intake_date": detail.get("intake_date", ""),
        "description": description,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch_lewisville_pets(target_per_species: int = 3,
                           exclude_urls: set[str] | None = None
                           ) -> dict[str, list[dict]]:
    """Scrape Lewisville Animal Services' Petango widget. Returns up to
    `target_per_species` cats and `target_per_species` dogs.

    `exclude_urls` — detail URLs that are already approved/saved upstream
    (typically `get_approved_pet_urls()` from notion_helper). Those pets
    are skipped so a recurring run picks fresh candidates."""
    exclude_urls = exclude_urls or set()
    result: dict[str, list[dict]] = {"cat": [], "dog": []}
    for species_label in ("Cat", "Dog"):
        species_key = species_label.lower()
        list_url = (f"{BASE}/wsAdoptableAnimals.aspx"
                    f"?authkey={AUTHKEY}&species={species_label}&recAmount=100")
        print(f"\n━━ {species_label} ━━  fetching listing…")
        html = _fetch(list_url)
        if not html:
            print(f"  ✗ listing fetch failed")
            continue
        listings = parse_list(html)
        print(f"  found {len(listings)} {species_key}(s) listed")

        for listing in listings:
            if len(result[species_key]) >= target_per_species:
                break
            if listing.get("detail_url") in exclude_urls:
                continue
            detail_html = _fetch(listing["detail_url"]) if listing.get("detail_url") else ""
            detail = parse_detail(detail_html) if detail_html else {}
            pet = _to_pipeline_pet(listing, detail)
            result[species_key].append(pet)
            print(f"  ✓ {pet['name']:<25} {pet['breed'][:40]:<40} {pet['age']}")
            time.sleep(0.3)
    return result


def main() -> int:
    print("Lewisville Animal Services scraper (Petango)")
    result = fetch_lewisville_pets(target_per_species=3)
    print(f"\n=== Final: {len(result['cat'])} cat(s), {len(result['dog'])} dog(s) ===")
    for species in ("cat", "dog"):
        for pet in result[species]:
            print(f"\n  [{pet['species']}] {pet['name']}")
            print(f"    breed:   {pet.get('breed', '')}")
            print(f"    age:     {pet.get('age', '')}")
            print(f"    gender:  {pet.get('gender', '')}  ({pet.get('altered', '?')})")
            print(f"    color:   {pet.get('color', '')}")
            photos = pet.get('photos') or []
            print(f"    photos:  {len(photos)} total")
            for i, p in enumerate(photos, 1):
                print(f"             [{i}] {p[:80]}")
            print(f"    url:     {pet.get('url', '')[:90]}")
            if pet.get("description"):
                desc = pet["description"].replace("\n", " · ")
                print(f"    desc:    {desc[:250]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
