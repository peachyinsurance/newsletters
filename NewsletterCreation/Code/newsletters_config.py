"""
Canonical newsletter configuration — single source of truth.

Every section pipeline imports NEWSLETTERS from this module. Adding a new
newsletter (or tweaking demographics, search areas, real-estate tiers, etc.)
is now a one-file change here instead of editing every section.

Schema is the union of all fields any section currently uses. Sections read
the keys they need and ignore the rest.

Field reference (which sections use what):
  name              all sections (Notion select + filtering)
  display_area      Featured Event, Insurance Tip, Weekend Planner, Welcome
                    Intro, Local Lowdown, Real Estate, Poll
  notion_color      Notion DB select-option color (purple/pink/blue/etc.)
  state             Pets (state filter for RescueGroups search)
  zip               Restaurants (Google Places anchor), Pets (RescueGroups)
  lat / lng         Restaurants, Free Events (Google Places anchor)
  search_areas      Featured Event, Weekend Planner, Free Events (Brave queries)
  lowdown_search_terms / lowdown_retry_terms   Local Lowdown (news search)
  realtor_location  Real Estate (Realtor.com search format)
  real_estate_tiers Real Estate (per-newsletter price tiers)
  demographics      Featured Event, Insurance Tip, Weekend Planner (Claude
                    audience context)
"""

# Real-estate tiers are identical across newsletters today; defined once here
# and reused. If a market needs different thresholds later, give it its own
# `real_estate_tiers` value instead of pointing to STANDARD_RE_TIERS.
STANDARD_RE_TIERS = [
    {"name": "Starter",    "label": "🏠 Starter Home", "max_price": 400000, "min_price": 0,       "min_beds": 3, "min_baths": 2, "type_filter": None},
    {"name": "Sweet Spot", "label": "🏡 Sweet Spot",   "max_price": 700000, "min_price": 400000,  "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
    {"name": "Showcase",   "label": "🏰 Showcase",     "max_price": None,   "min_price": 1000000, "min_beds": 0, "min_baths": 0, "type_filter": "single_family"},
]


NEWSLETTERS = [
    {
        "name":               "East_Cobb_Connect",
        "display_area":       "East Cobb",
        "notion_color":       "purple",
        "state":              "GA",
        "zip":                "30062",
        "lat":                33.9773,
        "lng":                -84.5130,
        "search_areas":       ["East Cobb GA", "Marietta GA", "Roswell GA"],
        "lowdown_search_terms": ["East Cobb GA news"],
        "lowdown_retry_terms":  ["Marietta GA news", "Cobb County GA news"],
        "realtor_location":   "city:Marietta, GA",
        "real_estate_tiers":  STANDARD_RE_TIERS,
        "demographics": {
            "median_income":    "$118,000",
            "median_age":       "42",
            "family_skew":      "Mix of established families and empty nesters. Many kids are teens or college-age.",
            "homeownership":    "78%",
            "education":        "65% bachelor's degree or higher",
        },
    },
    {
        "name":               "Perimeter_Post",
        "display_area":       "Perimeter",
        "notion_color":       "pink",
        "state":              "GA",
        "zip":                "30328",
        "lat":                33.9207,
        "lng":                -84.3882,
        "search_areas":       ["Dunwoody GA", "Sandy Springs GA", "Brookhaven GA"],
        "lowdown_search_terms": ["Dunwoody Sandy Springs news"],
        "lowdown_retry_terms":  ["Sandy Springs GA news", "Dunwoody GA news", "Perimeter Atlanta news"],
        "realtor_location":   "city:Dunwoody, GA",
        "real_estate_tiers":  STANDARD_RE_TIERS,
        "demographics": {
            "median_income":    "$105,000",
            "median_age":       "38",
            "family_skew":      "Mix of young professionals, young families, and empty nesters. More adult-skewing than East Cobb.",
            "homeownership":    "55%",
            "education":        "70% bachelor's degree or higher",
        },
    },
    {
        "name":               "Lewisville_Lake_Lookout",
        "display_area":       "Lewisville Lake",
        "notion_color":       "blue",
        "state":              "TX",
        "zip":                "75067",
        "lat":                33.0462,
        "lng":                -96.9942,
        # Order matters for Weekend Planner: front half feeds primary queries,
        # back half feeds retry queries for geographic variety.
        "search_areas":       ["Lewisville TX", "Flower Mound TX", "The Colony TX",
                               "Little Elm TX", "Highland Village TX", "Hickory Creek TX",
                               "Lake Dallas TX", "Corinth TX", "Shady Shores TX",
                               "Lakewood Village TX"],
        "lowdown_search_terms": ["Lewisville TX news"],
        "lowdown_retry_terms":  ["Flower Mound TX news", "Denton County TX news"],
        "realtor_location":   "city:Lewisville, TX",
        "real_estate_tiers":  STANDARD_RE_TIERS,
        "demographics": {
            "median_income":    "$95,000",
            "median_age":       "36",
            "family_skew":      "Strongly family-heavy with mixed income brackets — middle-income diverse suburbs (Lewisville, Little Elm, The Colony), affluent suburbs (Flower Mound, Highland Village), lake-lifestyle communities (Lake Dallas, Hickory Creek), plus a college-adjacent younger skew near UNT/TWU.",
            "homeownership":    "65%",
            "education":        "50% bachelor's degree or higher",
        },
    },
]


def get_newsletter(name: str) -> dict | None:
    """Return the newsletter dict matching `name`, or None if not found."""
    for nl in NEWSLETTERS:
        if nl["name"] == name:
            return nl
    return None


def newsletter_names() -> list[str]:
    """Return just the list of newsletter names (for sections that only need names)."""
    return [nl["name"] for nl in NEWSLETTERS]


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
    matches = [nl for nl in NEWSLETTERS if nl["name"] == arg]
    if matches:
        return matches
    print(f"  [WARN] Unknown {env_var} '{arg}'. Falling back to all. Known: {newsletter_names()}")
    return NEWSLETTERS
