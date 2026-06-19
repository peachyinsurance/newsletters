"""Per-newsletter job-source registry for the In Search Of section.

Each entry is one employer (or career-help resource) we scrape weekly.
The pipeline iterates this list, fetches each URL, extracts a meta
description / og:image / page title, and upserts a row into the In
Search Of Notion DB for reviewer approval.

EDITING THIS FILE:
  - URLs marked TODO are best-guess careers pages. Replace with the
    actual careers page from the employer's site as you verify them.
  - `employer` is the human-readable name that goes in the blurb header.
  - `city` is the normalized city tag — lowercase, used for filtering.
  - `is_resource_hint=True` flags career-help resources (e.g. WorkSource
    Cobb). The skill rewrites these as "Bonus help (free):" rows.

ECC-MVP scope: only East_Cobb_Connect has real sources today. PP and
LLL entries are placeholders ready to be filled in.
"""

JOB_SOURCES = {
    "East_Cobb_Connect": [
        {
            "employer": "Avenue East Cobb",
            # The Avenue is a shopping center; its jobs page aggregates tenant
            # openings (verified 2026-06: /jobs/ returns the live listing page;
            # the old /join-our-team 404'd).
            "url": "https://avenueeastcobb.com/jobs/",
            "city": "marietta",
        },
        {
            "employer": "Atlanta YMCA",
            # YMCA Atlanta careers — region-wide; the McCleskey-East
            # Cobb branch shows up in their searchable role list.
            "url": "https://ymcaatlanta.org/careers",
            "city": "marietta",
        },
        {
            "employer": "Cobb County School District",
            # /employment 404'd; the live page is /employment-opportunities
            # (verified 2026-06).
            "url": "https://www.cobbk12.org/employment-opportunities",
            "city": "marietta",
        },
        {
            "employer": "Cobb County (governmentjobs.com)",
            # NEOGOV agency page for Cobb County
            "url": "https://www.governmentjobs.com/careers/cobbcounty",
            "city": "marietta",
        },
        {
            "employer": "Cobb County Sheriff's Office",
            "url": "https://www.cobbsheriff.org/careers",  # TODO verify
            "city": "marietta",
        },
        {
            "employer": "WorkSource Cobb",
            "url": "https://www.worksourcecobb.org/",
            "city": "marietta",
            "is_resource_hint": True,
        },
    ],
    "Perimeter_Post": [
        # TODO: PP coverage (Sandy Springs / Dunwoody / Brookhaven)
        #   careers.choa.org
        #   jobs.coxenterprises.com
        #   www.mbusa.com
        #   jobs.northside.com
    ],
    "Lewisville_Lake_Lookout": [
        # TODO: LLL coverage (Lewisville / Flower Mound / The Colony /
        # Little Elm)
        #   www.lisd.net
        #   careers.hcahealthcare.com
        #   www.governmentjobs.com (Lewisville / Denton)
        #   hawaiianwaters.com (Hawaiian Falls)
    ],
}


def sources_for(newsletter_name: str) -> list[dict]:
    """Return the source list for one newsletter, or an empty list if
    the newsletter isn't yet wired."""
    return JOB_SOURCES.get(newsletter_name, [])
