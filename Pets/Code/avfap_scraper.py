#!/usr/bin/env python3
"""A Voice For All Paws (LLL shelter) — adoptables scraper.

avoiceforallpaws.com embeds a Petstablished widget which redirects to
Wagtopia's SPA. The underlying Adoptapet partner API is paid-only,
so we render the SPA via Apify's web-scraper actor and extract pet
cards from the rendered DOM.

Output shape matches Furry_Friends_Marietta.py's pipeline (one dict per
pet with name, species, breed, age, gender, description, photos,
shelter_name, shelter_address, etc.) so an AVFAP block can plug into
ORG_PLAN as a custom-shelter fallback for the LLL newsletter.

Usage (standalone — prints 3 cats and 3 dogs):
    APIFY_API_KEY=... python3 "Pets/Code/avfap_scraper.py"

Or import `fetch_avfap_pets(target_per_species=3)` from another module.
"""
import os
import re
import sys
import json
import time
import requests

ORG_ID  = 499656
# Wagtopia URL template. The SPA's filter UI is a multiselect; trying
# the URL-param form first (`&species=Cat`). If the param turns out to
# be ignored, the pageFunction's click-the-chip fallback kicks in.
WIDGET_URL = (f"https://www.wagtopia.com/search/org"
              f"?id={ORG_ID}&iframe=normal&page={{page}}&sort=default"
              f"&name=A+Voice+for+All+Paws"
              f"&species={{species}}")
APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "")

# Source-of-record shelter info (Wagtopia card doesn't repeat shelter
# address per pet; we hard-code it from avoiceforallpaws.com).
SHELTER_INFO = {
    "shelter_name":    "A Voice For All Paws",
    "shelter_address": "Dallas, TX",
    "shelter_phone":   "",
    "shelter_email":   "info@avoiceforallpaws.com",
}


# ---------------------------------------------------------------------------
# Apify pageFunction — runs inside the rendered Chromium page (web-scraper
# evaluates pageFunction in the page context).
# Wagtopia card shape (confirmed from rendered DOM):
#   .pets-item
#     .pet-info
#       h3                  → pet name
#       .basic              → "Female Tabby Domestic Shorthair Kitten"
#     .popper-description   → full bio (hidden via CSS — use textContent
#                             since innerText returns '' on display:none)
#     .widget-buttons a.btn-primary[href]  → detail URL
#     img                   → photo
# ---------------------------------------------------------------------------
PAGE_FUNCTION = r"""
async function pageFunction(context) {
    const { request, log, customData } = context;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    // customData.species is "Cat" or "Dog" — what we want this run to keep.
    const wantSpecies = (customData && customData.species) || '';

    // Wait up to 40s for .pets-item to appear in the DOM.
    const deadline = Date.now() + 40000;
    while (Date.now() < deadline) {
        if (document.querySelectorAll('.pets-item').length > 0) break;
        await sleep(500);
    }
    const beforeCount = document.querySelectorAll('.pets-item').length;

    // Belt-and-suspenders filter: try clicking the multiselect option for
    // the requested species. If the URL `&species=` param was honored the
    // count won't change; if it was ignored, this click narrows the
    // results client-side and we wait for the re-render.
    let clicked = false;
    if (wantSpecies) {
        const opt = document.querySelector(
            `#multiselect-option-${wantSpecies}`
        );
        if (opt) {
            opt.click();
            clicked = true;
            await sleep(2500);  // re-render
        }
    }

    // Scroll to trigger any lazy loading.
    for (let i = 0; i < 6; i++) {
        window.scrollTo(0, document.body.scrollHeight);
        await sleep(1200);
    }
    const afterCount = document.querySelectorAll('.pets-item').length;

    // textContent (not innerText) so we can read .popper-description even
    // when it's display:none. Whitespace-collapse to single spaces.
    function _text(el) {
        if (!el) return '';
        return (el.textContent || '').trim().replace(/\s+/g, ' ');
    }

    const cards = [];
    for (const card of document.querySelectorAll('.pets-item')) {
        const nameEl  = card.querySelector('.pet-info h3');
        const basicEl = card.querySelector('.pet-info .basic');
        const descEl  = card.querySelector('.popper-description');
        // Detail link — try .widget-buttons first (the canonical "View
        // Full Profile" button), fall back to any /search/pet?id= anchor.
        const linkEl  = card.querySelector('.widget-buttons a.btn-primary[href]')
                       || card.querySelector('a[href*="/search/pet"]');
        const imgEl   = card.querySelector('img');

        cards.push({
            name:           _text(nameEl),
            basic:          _text(basicEl),   // gender + breed + age tokens
            description:    _text(descEl),
            detail_url:     linkEl ? linkEl.getAttribute('href') : '',
            detail_url_abs: linkEl ? linkEl.href : '',
            photo_url:      imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '',
            photo_alt:      imgEl ? (imgEl.getAttribute('alt') || '') : '',
        });
    }

    return {
        url:           request.url,
        want_species:  wantSpecies,
        clicked_chip:  clicked,
        before_count:  beforeCount,
        after_count:   afterCount,
        card_count:    cards.length,
        cards,
    };
}
"""


def _apify_run(start_url: str, species: str = "") -> dict:
    """POST one URL through apify~web-scraper and return the parsed item.
    `species` is passed into pageFunction via customData so it can click
    the matching filter chip post-render. Returns {} on error."""
    payload = {
        "startUrls":           [{"url": start_url}],
        "pageFunction":        PAGE_FUNCTION,
        "customData":          {"species": species},
        "maxConcurrency":      1,
        "maxRequestsPerCrawl": 1,
        "pageLoadTimeoutSecs": 120,
        "useChrome":           True,
        "headless":            True,
        "proxyConfiguration":  {"useApifyProxy": True},
    }
    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/apify~web-scraper/run-sync-get-dataset-items",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {APIFY_API_KEY}"},
            json=payload, timeout=300,
        )
    except Exception as e:
        print(f"  ✗ Apify request error: {e}")
        return {}
    if r.status_code not in (200, 201):
        print(f"  ✗ Apify HTTP {r.status_code}: {r.text[:400]}")
        return {}
    items = r.json()
    if not items:
        return {}
    data = items[0]
    if data.get("#error"):
        dbg = data.get("#debug") or {}
        for m in (dbg.get("errorMessages") or [])[:3]:
            print(f"  ✗ Apify error: {m.splitlines()[0]}")
        return {}
    return data


# ---------------------------------------------------------------------------
# Card-to-pet normalization
# ---------------------------------------------------------------------------
# Token-classification tables for parsing the Wagtopia .basic string,
# e.g. "Female Tabby Domestic Shorthair Kitten" → gender / age / breed.
_GENDER_TOKENS = {"male", "female", "unknown"}
_AGE_TOKENS    = {"baby", "young", "adult", "senior", "kitten", "puppy"}
# Species derivation: age word first (Kitten/Puppy = unambiguous), then
# breed keyword. CAT_BREED_HINTS catches common cat breed tokens; same
# for DOG. Mixed/unclassifiable returns ''.
_CAT_BREED_HINTS = {
    "shorthair", "longhair", "tabby", "calico", "tortie", "tortoiseshell",
    "persian", "siamese", "maine coon", "ragdoll", "bengal", "russian blue",
    "domestic", "burmese", "abyssinian",
}
_DOG_BREED_HINTS = {
    "retriever", "labrador", "lab", "shepherd", "terrier", "poodle",
    "bulldog", "pit bull", "pitbull", "chihuahua", "beagle", "boxer",
    "husky", "doberman", "rottweiler", "dachshund", "mastiff", "collie",
    "spaniel", "hound", "corgi", "schnauzer", "pug", "yorkie", "akita",
    "doodle", "shih tzu", "great dane", "border collie", "australian",
    "german", "golden",
}


def _parse_basic(s: str) -> dict:
    """Parse Wagtopia's .basic string into {gender, age, breed, species}.

    The string is just space-separated tokens with no labels — e.g.
    'Female Tabby Domestic Shorthair Kitten' — so we classify each token
    by membership in known sets, treat everything else as breed text."""
    if not s:
        return {}
    out: dict[str, str] = {}
    breed_tokens: list[str] = []
    for tok in s.split():
        low = tok.lower().strip(",.;:")
        if low in _GENDER_TOKENS and not out.get("gender"):
            out["gender"] = tok
        elif low in _AGE_TOKENS and not out.get("age"):
            out["age"] = tok
            if low == "kitten":
                out["species"] = "Cat"
            elif low == "puppy":
                out["species"] = "Dog"
        elif low == "cat" and not out.get("species"):
            out["species"] = "Cat"
        elif low == "dog" and not out.get("species"):
            out["species"] = "Dog"
        else:
            breed_tokens.append(tok)
    out["breed"] = " ".join(breed_tokens).strip()
    # Species fallback: derive from breed tokens.
    if not out.get("species"):
        breed_low = out["breed"].lower()
        if any(h in breed_low for h in _CAT_BREED_HINTS):
            out["species"] = "Cat"
        elif any(h in breed_low for h in _DOG_BREED_HINTS):
            out["species"] = "Dog"
    return out


def _detect_species(card: dict) -> str:
    """Return 'cat' or 'dog' for a card, else ''. Reuses _parse_basic so
    the breed-heuristic table is in one place."""
    parsed = _parse_basic(card.get("basic", ""))
    sp = (parsed.get("species") or "").lower()
    if sp in ("cat", "dog"):
        return sp
    # Last resort: scan name + description for cat/dog/kitten/puppy.
    blob = f"{card.get('name','')} {card.get('description','')}".lower()
    if re.search(r"\b(kitten|cat|feline)\b", blob):
        return "cat"
    if re.search(r"\b(puppy|dog|canine)\b", blob):
        return "dog"
    return ""


def _card_to_pet(card: dict, species: str) -> dict:
    """Map a raw card dict (from the pageFunction) → the same dict shape
    that Furry_Friends_Marietta.py expects from RescueGroups."""
    parsed = _parse_basic(card.get("basic", ""))
    return {
        "name":             card.get("name") or "Unknown",
        "species":          species.capitalize(),
        "breed":            parsed.get("breed", ""),
        "age":              parsed.get("age", ""),
        "gender":           parsed.get("gender", ""),
        "size":             "",
        "description":      (card.get("description") or "")[:2000],
        "url":              card.get("detail_url_abs") or card.get("detail_url") or "",
        "photos":           [card["photo_url"]] if card.get("photo_url") else [],
        **SHELTER_INFO,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch_avfap_pets(target_per_species: int = 3,
                     max_pages: int = 3) -> dict[str, list[dict]]:
    """Scrape AVFAP's adoptables widget and return up to `target_per_species`
    cats and `target_per_species` dogs. Two passes — one per species —
    each filtered both via `&species=` URL param AND a post-render click
    on the matching multiselect chip (whichever the SPA actually honors).
    Walks pages until quota fills or `max_pages` is hit."""
    if not APIFY_API_KEY:
        print("✗ APIFY_API_KEY not set in env.")
        return {"cat": [], "dog": []}

    result: dict[str, list[dict]] = {"cat": [], "dog": []}

    for species_label in ("Cat", "Dog"):
        species_key  = species_label.lower()
        kept: list[dict] = []
        seen_urls: set[str] = set()

        for page in range(1, max_pages + 1):
            if len(kept) >= target_per_species:
                break
            url = WIDGET_URL.format(page=page, species=species_label)
            print(f"\n━━ {species_label} · page {page} ━━  {url[:95]}…")
            t0 = time.time()
            data = _apify_run(url, species=species_label)
            print(f"  Apify replied in {time.time() - t0:.1f}s  "
                  f"before/after click={data.get('before_count')}→{data.get('after_count')}  "
                  f"clicked_chip={data.get('clicked_chip')}  "
                  f"card_count={data.get('card_count', 0)}")
            cards = data.get("cards") or []
            if not cards:
                break

            kept_this_page = 0
            for c in cards:
                url_key = c.get("detail_url_abs") or c.get("detail_url") or c.get("name", "")
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                # Trust the page-level filter; verify with our own parser
                # only as a sanity check (skip if it strongly disagrees).
                detected = _detect_species(c)
                if detected and detected != species_key:
                    continue
                pet = _card_to_pet(c, species_key)
                kept.append(pet)
                kept_this_page += 1
                if len(kept) >= target_per_species:
                    break
            print(f"  → kept {kept_this_page} new {species_key}(s)  "
                  f"(running total: {len(kept)})")
            if kept_this_page == 0:
                # Page produced no new in-species pets — stop early.
                break

        result[species_key] = kept

    return result


def main() -> int:
    print(f"Scraping AVFAP via Wagtopia (Apify-rendered)…")
    result = fetch_avfap_pets(target_per_species=3, max_pages=3)
    print(f"\n=== Final: {len(result['cat'])} cat(s), {len(result['dog'])} dog(s) ===")
    for species in ("cat", "dog"):
        for pet in result[species]:
            print(f"\n  [{pet['species']}] {pet['name']}")
            print(f"    breed:   {pet.get('breed','')}")
            print(f"    age:     {pet.get('age','')}")
            print(f"    gender:  {pet.get('gender','')}")
            print(f"    photo:   {(pet.get('photos') or [''])[0][:80]}")
            print(f"    url:     {pet.get('url','')[:80]}")
            if pet.get("description"):
                print(f"    desc:    {pet['description'][:200]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
