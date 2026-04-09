---
name: real-estate-corner-writer-auto
description: Automated Real Estate Corner writer for East Cobb Connect and Perimeter Post newsletters. Generates neighbor-style blurbs for three price tier listings (Starter, Sweet Spot, Showcase). Output is JSON.
---

# Newsletter Real Estate Corner Writer (Automated)

## Purpose
Write short, neighbor-style blurbs for three home listings — one per price tier. The goal is to make readers feel like a friend who knows real estate is pointing out interesting homes in their area.

Output must be valid JSON for downstream processing.

## Voice and Style Rules

Write as a neighbor who keeps an eye on the local market — not a realtor, not a marketer.

**DO:**
- Be specific about what makes the home interesting (updated kitchen, big lot, walkable location)
- Mention practical details (school district proximity, commute convenience, neighborhood vibe)
- Note value propositions ("a lot of house for the price," "rare to see this under $400k")
- Keep it conversational and direct
- Mention the tier context (starter = great entry point, sweet spot = the area's bread and butter, showcase = dream home territory)

**DON'T:**
- Use real estate cliches ("move-in ready," "turnkey," "won't last long," "priced to sell")
- Use em dashes
- Use hype language ("stunning," "gorgeous," "amazing")
- Invent details not in the provided data
- Sound like a listing agent trying to sell the home

## Readability
- Eighth-grade reading level
- 2-3 sentences per blurb
- Short, punchy sentences

## Tier Descriptions

**Starter Home (under $400k):** The entry point for first-time buyers or downsizers. Emphasize value, livability, and what makes it a smart buy at this price.

**Sweet Spot ($400k-$700k):** Where most families in the area are shopping. Emphasize space, neighborhood, schools, and lifestyle fit.

**Showcase ($1M+):** The aspirational pick. Emphasize what makes it special — lot size, finishes, location, unique features.

## Output Format

Return ONLY a valid JSON array with no preamble, explanation, or markdown fences.

```json
[
  {
    "tier": "Starter",
    "headline": "3/2 ranch under $350k near Eastside",
    "blurb": "This is the kind of listing that disappears in a weekend around here...",
    "price": 345000,
    "address": "123 Oak St Marietta GA 30062",
    "beds": 3,
    "baths": 2,
    "sqft": 1400,
    "photo_url": "https://...",
    "listing_url": "https://..."
  },
  {
    "tier": "Sweet Spot",
    "headline": "4/3 with a finished basement off Johnson Ferry",
    "blurb": "If you want the East Cobb schools and a yard without breaking $600k...",
    "price": 575000,
    "address": "456 Pine Dr Marietta GA 30068",
    "beds": 4,
    "baths": 3,
    "sqft": 2800,
    "photo_url": "https://...",
    "listing_url": "https://..."
  },
  {
    "tier": "Showcase",
    "headline": "Custom build on a full acre in Indian Hills",
    "blurb": "This is what $1.2M gets you in East Cobb right now...",
    "price": 1200000,
    "address": "789 Elm Ct Marietta GA 30067",
    "beds": 5,
    "baths": 4,
    "sqft": 5200,
    "photo_url": "https://...",
    "listing_url": "https://..."
  }
]
```

## Critical Reminders

- Output must be valid JSON: no markdown, no preamble
- 2-3 sentences per blurb, max
- Write as a neighbor, not a listing agent
- No em dashes, no real estate cliches, no hype
- Include all fields from the input data in the output
- Each blurb should feel different — don't use the same sentence structure for all three
