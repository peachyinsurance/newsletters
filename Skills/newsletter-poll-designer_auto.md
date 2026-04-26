---
name: newsletter-poll-designer-auto
description: Automated reader-poll designer for East Cobb Connect and Perimeter Post newsletters. Generates a 4-option Beehiiv poll where each option maps to a sponsorable local-business category. Output is JSON for downstream processing.
---

# Newsletter Poll Designer (Automated)

## Purpose
Design ONE reader poll per newsletter edition. Each poll has:
- A warm, neighborly question
- 4 tappable answer options
- Each option maps to **distinct local-business categories** so vote totals double as ad-pitch intel.

You will receive an `excluded_categories` list — categories used in the past 8 weeks. Avoid them when picking new options. Build a heat map across many categories over time, not the same ones repeatedly.

Output must be valid JSON for downstream processing.

## Why polls matter (context)

These polls serve two audiences at once:
1. **Reader-facing:** A casual community question that feels natural in a neighborly newsletter.
2. **Business-facing:** Each answer maps to a category of advertiser ("37% of readers said they're looking for a contractor this spring").

The reader should never feel like they're being surveyed for ad targeting. Framing matters as much as options.

## Platform context (Beehiiv)

- Tap-to-vote (no replies, no A/B/C/D labels)
- Exactly 4 answer choices
- Keep option text short — these render as tappable buttons
- One emoji in the question header is fine. **No emojis on the options themselves.**

## Step 1: Choose a Framing

Pick a framing appropriate to the publication date / season / what's already in the newsletter.

### Tier 1 — High versatility, works any edition
- **"What's on your to-do list this [season]?"** — surfaces service businesses, appointments, home, camps, restaurants. Best for spring/fall.
- **"If someone handed you a $200 gift card to a local business, where's it going?"** — surfaces restaurants, retail, fitness, spa, home, groceries. Always works.

### Tier 2 — Strong but more targeted
- **"What's been on your 'I should really handle that' list?"** — surfaces home services (HVAC, gutters, pressure washing), medical/dental, insurance, contractors, auto. Best for editions with home-prep tips.
- **"What's stressing you out most about your house right now?"** — surfaces lawn care, plumbers, electricians, remodelers, HVAC, pest, cleaning. Best when real-estate or home content is in the issue.
- **"What's your biggest parenting spend this time of year?"** — surfaces sports, party venues, camps, tutoring, childcare, kids' clothes, orthodontists. Best for back-to-school or camp season.

### Tier 3 — Lifestyle / mood-based
- **"What kind of weekend are you craving right now?"** — surfaces coffee, restaurants, outdoor gear, date-night, indoor play, live music.
- **"If a new business opened walking distance from your house, what would you want it to be?"** — surfaces coffee, wine bars, fitness studios, breakfast, bakeries, urgent care, pet stores. Pair with new-opening news in the issue.

## Step 2: Pick 4 options that map to FRESH business categories

Selection rules:
1. **Match the edition's content** — if the newsletter has a pollen tip, include a home/HVAC option. If there's a camp event, include a camp option.
2. **Each option maps to its own distinct category set** — no overlap between options.
3. **No category appearing in `excluded_categories`** — these were used in the past 8 weeks. If you cannot find 4 fresh categories, use the OLDEST recently-used (the pipeline will warn but accept it). Always note the recycled category in `dropped_categories` with reason "recycled (oldest)".
4. **Mix high-frequency (restaurants, groceries) with high-value (contractors, medical, camps)** to balance vote totals and ad revenue.

### Category reference (option text → category set)

| Option text (example) | Category set |
|---|---|
| Getting the yard or house back in shape | landscaping, painting, pressure washing, contractors, hvac |
| Locking down summer camps for the kids | day camps, specialty camps, sports leagues, swim lessons, enrichment |
| Scheduling overdue appointments (dentist, doctor, etc.) | dentists, orthodontists, dermatologists, eye doctors, pediatricians, med spas |
| Finding a new go-to restaurant | restaurants, food halls, brunch spots |
| Booking a family trip | travel agencies, cabin rentals, family resorts |
| Tackling a home project | contractors, remodelers, kitchen/bath, interior designers |
| Getting back into fitness | gyms, yoga, barre, personal trainers, running stores |
| Updating wardrobe or shopping local | boutiques, consignment, local retail |
| Sorting out finances | cpas, financial advisors, insurance agents, estate planners |
| A really good coffee shop nearby | coffee shops, bakeries, cafes |
| A neighborhood wine bar | breweries, wine bars, tasting rooms |
| A kids' activity center | indoor playgrounds, enrichment centers, trampoline parks |
| Birthday parties | party venues, indoor play spaces, gift shops |
| Childcare or babysitters | nanny services, babysitter platforms, after-school care |
| Medical stuff (braces, glasses, etc.) | orthodontists, pediatric dentists, optometrists |
| Something's broken at the house | handymen, plumbers, electricians |
| Energy bills are getting ridiculous | hvac, insulation, solar, window replacement |
| Pest control | pest control companies |
| Car maintenance | auto detailing, mechanics, tire shops |

Use lowercased single-word-or-hyphenated category names (e.g., `hvac`, `coffee shops`, `pest control`) so the exclusion check is reliable.

## Input Format

You receive a JSON object:

```json
{
  "newsletter_name": "East_Cobb_Connect",
  "publication_date": "2026-04-30",
  "excluded_categories": ["camps", "hvac", "dentists", "restaurants", "coffee shops"]
}
```

`excluded_categories` is the union of every category used by an `approved` or `approved - old` poll for this newsletter in the past 8 weeks. Avoid all of them.

## Output Format

Return ONLY a valid JSON object with no preamble, explanation, or markdown fences.

```json
{
  "newsletter_name": "East_Cobb_Connect",
  "framing": "What's on your to-do list this spring?",
  "question": "What's on your to-do list this spring?",
  "options": [
    {
      "text": "Tackling a home project",
      "categories": ["contractors", "remodelers", "kitchen/bath", "interior designers"]
    },
    {
      "text": "Getting back into fitness",
      "categories": ["gyms", "yoga", "barre", "personal trainers"]
    },
    {
      "text": "A neighborhood wine bar",
      "categories": ["wine bars", "breweries", "tasting rooms"]
    },
    {
      "text": "Sorting out finances",
      "categories": ["cpas", "financial advisors", "insurance agents"]
    }
  ],
  "dropped_categories": [
    {"category": "camps", "reason": "in excluded_categories (used within past 8 weeks)"}
  ],
  "ad_intel_mapping": [
    "Tackling a home project → contractors, remodelers, kitchen/bath, interior designers",
    "Getting back into fitness → gyms, yoga, barre studios, personal trainers",
    "A neighborhood wine bar → wine bars, breweries, tasting rooms",
    "Sorting out finances → CPAs, financial advisors, insurance agents"
  ]
}
```

### Field definitions
- `framing` — which framing tier you chose (one of the headings above)
- `question` — the rendered question text. May match the framing exactly or adapt for the season.
- `options` — array of EXACTLY 4. Each item has `text` (button label, ≤10 words) and `categories` (lowercased category strings used for exclusion tracking)
- `dropped_categories` — categories you skipped because they were in `excluded_categories`, with brief reason
- `ad_intel_mapping` — human-readable Option → Category list. This is shown to the editorial team in Notion alongside the poll for ad-sales context.

## Quality Gates

Before returning:
- Exactly 4 options
- Each option's `categories` set is non-empty and disjoint from every other option's categories
- Combined categories across all 4 options have minimal overlap with `excluded_categories` (zero overlap if possible; if forced to recycle, the recycled category goes in `dropped_categories` with reason `"recycled (oldest available)"`)
- No emojis on individual options
- Option text is conversational, ≤10 words
- No "Reply with..." or "A/B/C/D" labels — Beehiiv is tap-to-vote
- `ad_intel_mapping` reflects the chosen options

## Critical Reminders

- Output must be valid JSON: no markdown fences, no preamble.
- Categories should be lowercased and consistent (e.g., always `hvac`, not `HVAC` or `Hvac`).
- A single category appearing in `excluded_categories` should knock out the WHOLE option that contains it. Pick a different option entirely.
- The reader should never feel surveyed for ad targeting. Keep the question warm.
