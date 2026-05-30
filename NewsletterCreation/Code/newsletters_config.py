"""
Canonical newsletter configuration — single source of truth.

ADDING A NEW NEWSLETTER
  1. Add a new entry to the NEWSLETTERS dict below.
  2. Fill in every field. No other code edits needed.
  3. Add the newsletter's secrets to GitHub Actions secrets:
       BEEHIIV_<TAG>_PUBLICATION_ID
       BEEHIIV_<TAG>_TEMPLATE_POST_ID
     where <TAG> is the short upper-case tag you set in `beehiiv_env_tag`.

Every per-newsletter knob in the pipeline reads from this dict. Field reference:

  name                        Notion Newsletter select value + filename slug
  landing_page_id             Notion page ID of the canonical "Current Edition"
                              landing page. The assembler updates this exact page
                              in place instead of searching by title (Notion's
                              /v1/search is eventually-consistent and was missing
                              existing pages, causing duplicate pages each run).
                              Omit for a new newsletter; the assembler will create
                              the page and print its ID to paste in here.
  display_area                Human-readable area name (used in prompts + UI)
  notion_color                Notion select-option color (purple/pink/blue/red/…)
  state                       2-letter US state code (Pets, geo queries)
  zip                         Anchor zip for Google Places (Restaurants, Pets)
  lat / lng                   Anchor lat/lng (Restaurants, Free Events)
  search_areas                Town names used in Brave queries (Featured Event,
                              Weekend Planner, Free Events)
  lowdown_search_terms        Primary news search queries for Local Lowdown
  lowdown_retry_terms         Fallback news queries if primary returns too few
  realtor_location            Realtor.com search location ("city:Name, ST")
  real_estate_tiers           List of {name, label, max_price, min_price, min_beds,
                              min_baths, type_filter} per real-estate tier
  demographics                Dict of audience profile fields (median_income,
                              median_age, family_skew, homeownership, education)
  website                     Public newsletter website (used as fallback link
                              target and email domain context)
  beehiiv_env_tag             Short upper-case tag for the Beehiiv env var names.
                              e.g. "ECC" → reads BEEHIIV_ECC_PUBLICATION_ID and
                              BEEHIIV_ECC_TEMPLATE_POST_ID from environment.
  poll_vote_base              URL template for poll vote tracking — {slug} placeholder
  excluded_venues             List of venue names (lowercased substring match) that
                              are out of range for this newsletter. Used by Weekend
                              Planner as a HARD exclusion — Claude never picks an
                              event whose venue or address contains one of these.
  excluded_cities             List of city names (lowercased substring match) that
                              are out of range for this newsletter. Same HARD
                              exclusion semantics as excluded_venues.
"""

# Real-estate tiers shared across newsletters today. If a market needs different
# thresholds later, give it its own list inline instead of pointing here.
STANDARD_RE_TIERS = [
    {"name": "Starter",    "label": "🏠 Starter Home", "max_price": 400000, "min_price": 0,       "min_beds": 3, "min_baths": 2, "type_filter": None},
    {"name": "Sweet Spot", "label": "🏡 Sweet Spot",   "max_price": 700000, "min_price": 400000,  "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
    {"name": "Showcase",   "label": "🏰 Showcase",     "max_price": None,   "min_price": 1000000, "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
]


# ---------------------------------------------------------------------------
# Master dict — keyed by `name`. Every per-newsletter parameter lives here.
# ---------------------------------------------------------------------------
NEWSLETTERS_DICT = {
    "East_Cobb_Connect": {
        "name":                 "East_Cobb_Connect",
        "landing_page_id":      "370bf42b-7fd6-81a2-aab6-f4d6925fdec2",
        "display_area":         "East Cobb",
        "notion_color":         "purple",
        "state":                "GA",
        "zip":                  "30062",
        "lat":                  33.9773,
        "lng":                  -84.5130,
        "search_areas":         ["East Cobb GA", "Marietta GA", "Roswell GA"],
        "lowdown_search_terms": ["East Cobb GA news"],
        "lowdown_retry_terms":  ["Marietta GA news", "Cobb County GA news"],
        "realtor_location":     "city:Marietta, GA",
        "real_estate_tiers":    STANDARD_RE_TIERS,
        "demographics": {
            "median_income": "$118,000",
            "median_age":    "42",
            "family_skew":   "Mix of established families and empty nesters. Many kids are teens or college-age.",
            "homeownership": "78%",
            "education":     "65% bachelor's degree or higher",
        },
        "website":              "https://www.eastcobbconnect.com/",
        "beehiiv_env_tag":      "ECC",
        "poll_vote_base":       "https://peachyinsurance.github.io/newsletters/poll-thanks.html?vote={slug}",
        "excluded_venues":      [],  # to be populated when Jason sends the ECC list
        "excluded_cities":      [],
    },

    "Perimeter_Post": {
        "name":                 "Perimeter_Post",
        "landing_page_id":      "370bf42b-7fd6-810e-83a2-d8e7dc54fe3a",
        "display_area":         "Perimeter",
        "notion_color":         "pink",
        "state":                "GA",
        "zip":                  "30328",
        "lat":                  33.9207,
        "lng":                  -84.3882,
        "search_areas":         ["Dunwoody GA", "Sandy Springs GA", "Brookhaven GA"],
        "lowdown_search_terms": ["Dunwoody Sandy Springs news"],
        "lowdown_retry_terms":  ["Sandy Springs GA news", "Dunwoody GA news", "Perimeter Atlanta news"],
        "realtor_location":     "city:Dunwoody, GA",
        "real_estate_tiers":    STANDARD_RE_TIERS,
        "demographics": {
            "median_income": "$105,000",
            "median_age":    "38",
            "family_skew":   "Mix of young professionals, young families, and empty nesters. More adult-skewing than East Cobb.",
            "homeownership": "55%",
            "education":     "70% bachelor's degree or higher",
        },
        "website":              "https://www.perimeterpost.com/",
        "beehiiv_env_tag":      "PP",
        "poll_vote_base":       "https://peachyinsurance.github.io/newsletters/poll-thanks.html?vote={slug}",
        "excluded_venues": [
            "truist park",
            "the battery atlanta",
            "the battery",
            "center for puppetry arts",
            "fernbank museum",
            "children's museum of atlanta",
            "ameris bank amphitheatre",
            "atlanta botanical garden",
            "high museum of art",
            "atlanta symphony hall",
            "fox theatre",
            "asw distillery",
        ],
        "excluded_cities": ["roswell", "alpharetta"],
    },

    "Lewisville_Lake_Lookout": {
        "name":                 "Lewisville_Lake_Lookout",
        "landing_page_id":      "370bf42b-7fd6-81b1-969e-efbd366bbc42",
        "display_area":         "Lewisville Lake",
        "notion_color":         "blue",
        "state":                "TX",
        "zip":                  "75067",
        "lat":                  33.0462,
        "lng":                  -96.9942,
        # Order matters for Weekend Planner: front half feeds primary queries,
        # back half feeds retry queries for geographic variety.
        "search_areas":         ["Lewisville TX", "Flower Mound TX", "The Colony TX",
                                  "Little Elm TX", "Highland Village TX", "Hickory Creek TX",
                                  "Lake Dallas TX", "Corinth TX", "Shady Shores TX",
                                  "Lakewood Village TX"],
        "lowdown_search_terms": ["Lewisville TX news"],
        "lowdown_retry_terms":  ["Flower Mound TX news", "Denton County TX news"],
        "realtor_location":     "city:Lewisville, TX",
        "real_estate_tiers":    STANDARD_RE_TIERS,
        "demographics": {
            "median_income": "$95,000",
            "median_age":    "36",
            "family_skew":   "Strongly family-heavy with mixed income brackets — middle-income diverse suburbs (Lewisville, Little Elm, The Colony), affluent suburbs (Flower Mound, Highland Village), lake-lifestyle communities (Lake Dallas, Hickory Creek), plus a college-adjacent younger skew near UNT/TWU.",
            "homeownership": "65%",
            "education":     "50% bachelor's degree or higher",
        },
        "website":              "https://www.lewisvillelakelookout.com/",
        "beehiiv_env_tag":      "LLL",
        "poll_vote_base":       "https://peachyinsurance.github.io/newsletters/poll-thanks.html?vote={slug}",
        "excluded_venues":      [],  # to be populated when Jason sends the LLL list
        "excluded_cities":      [],
    },
}


# ---------------------------------------------------------------------------
# Back-compat: list shape that all existing callers iterate over
# (`for nl in NEWSLETTERS: ...` keeps working).
# ---------------------------------------------------------------------------
NEWSLETTERS = list(NEWSLETTERS_DICT.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_newsletter(name: str) -> dict | None:
    """Return the newsletter dict matching `name`, or None if not found."""
    return NEWSLETTERS_DICT.get(name)


def newsletter_names() -> list[str]:
    """Return just the list of newsletter names."""
    return list(NEWSLETTERS_DICT.keys())


def beehiiv_credentials(newsletter_name: str) -> dict:
    """Return {publication_id, template_post_id} for a newsletter, resolved
    from env vars whose names are derived from beehiiv_env_tag.

    e.g. beehiiv_env_tag='ECC' → reads BEEHIIV_ECC_PUBLICATION_ID and
    BEEHIIV_ECC_TEMPLATE_POST_ID. Auto-prefixes 'post_' on the template
    post id so the Beehiiv API accepts either UUID-only or post_-prefixed
    secret values."""
    import os
    nl = get_newsletter(newsletter_name)
    if not nl:
        return {"publication_id": "", "template_post_id": ""}
    tag = nl.get("beehiiv_env_tag", "").strip().upper()
    pub = os.environ.get(f"BEEHIIV_{tag}_PUBLICATION_ID", "").strip()
    post = os.environ.get(f"BEEHIIV_{tag}_TEMPLATE_POST_ID", "").strip()
    if post and not post.startswith("post_"):
        post = "post_" + post
    return {"publication_id": pub, "template_post_id": post}


def filter_by_env(env_var: str = "NEWSLETTER") -> list[dict]:
    """Return NEWSLETTERS filtered by an env var (default: process all).

    Used by section pipelines that support a per-newsletter workflow_dispatch
    input. The env var should hold a single newsletter name (e.g.
    "East_Cobb_Connect") or "all" / unset to process every newsletter.

    Falls back to all if the env var holds an unrecognized value, with a
    warning printed."""
    import os
    arg = (os.environ.get(env_var) or "all").strip()
    if arg.lower() == "all":
        return NEWSLETTERS
    nl = NEWSLETTERS_DICT.get(arg)
    if nl:
        return [nl]
    print(f"  [WARN] Unknown {env_var} '{arg}'. Falling back to all. Known: {newsletter_names()}")
    return NEWSLETTERS
