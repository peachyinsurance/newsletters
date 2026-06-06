---
name: free-events-writer-auto
description: Automated Free Activity of the Week writer for East Cobb Connect, Perimeter Post, and Lewisville Lake Lookout newsletters. Picks ONE strong free activity from the candidate pool and writes a multi-section recommendation (~400-600 words) with hook, what-it-is, planning notes, on-site experience, logistics, and a heads-up. Output is structured JSON containing both per-event fields AND a pre-formatted `body_markdown` blob that the assembler renders into Notion blocks.
---

# Newsletter Free Activity of the Week Writer (Automated)

> **HARD RULE: NO EM DASHES.** Never output an em dash character (`—`, U+2014) anywhere in your response. Use commas, periods, parens, semicolons, or "and" instead. This is a non-negotiable house style rule across every section of every newsletter. Em dashes are a strong AI-generated tell, and Andrew has explicitly banned them. (En dashes `–` for ranges like "10am–4pm" are fine.)

## Purpose
Pick ONE real free activity from the candidate pool and write a substantive multi-section recommendation for it. Claude returns the top 5 ranked candidates with scores; downstream code decides the final winner using time-sensitivity score plus source-quality signals.

This is **not** a 1-sentence calendar entry — it's a 400-600 word neighbor-style guide that helps a reader actually plan and use the activity. Think of it as the kind of recommendation you'd type out for a friend who's new to the area.

Output must be valid JSON for downstream processing.

## Voice and Style

> **Voice:** Tone, rhythm, and style come from the house voice guide provided above this skill at generation time. Apply it. The rules below are this section's specifics: format, length, structure, selection, and output.

**DO:**
- Start with a 3-5 sentence hook paragraph that captures the vibe, not just the facts
- Use concrete details from the candidate data — actual addresses, hours, parking specifics
- Use bold (`**bold**`) for inline scannable bits inside paragraphs (don't bold whole paragraphs)
- Use bold-prefix labels for each section: **What it is:** ... **Plan it (when):** ... etc
- Include practical specifics: parking, fees, restrooms, dogs, "best time to go," what to bring
- Be honest about quirks ("the trail gets muddy after rain") — readers trust honesty

**DON'T:**
- Use em dashes (use commas, periods, parens, or "and")
- Use hype words ("exciting," "amazing," "must-see," "you won't want to miss")
- Invent facts not in the candidate data — if you don't know parking specifics, don't fabricate them; describe what IS in the source instead
- Return URLs in your output (we attach them from `candidate_index`)

## Readability
- Eighth-to-ninth-grade reading level
- Mix short sentences with occasional longer ones for rhythm
- Total body length: **400-600 words** across all sections (not counting the metadata line and link line that the assembler adds)

## Selection Rules

From the candidate pool, evaluate ones that are:

1. **Actually free** — no admission fee. "Free with $5 parking" is fine. Any required ticket purchase is NOT free.
2. **Open or recurring on/around the publication date** — for activities (parks, museums, trails) that are open year-round or on a regular schedule, "happening now" satisfies time relevance. For specific dated events, must be on or after publication date.
3. **A real, specific place or activity** — parks, libraries, nature centers, free museums, public trails, recurring community events. Not a business's general hours, not a one-time sale.
4. **In or close to the coverage area** — within ~25 miles is fine for a weekend-trip-worthy anchor.

**Drop candidates that:**
- Require any ticket cost (even $1)
- Are not clearly free / are advertisements / news-only / job listings / past events
- Duplicate another already-scored candidate
- Are too vague to write meaningfully about (i.e., the candidate summary doesn't give you enough specifics for a 400-word recommendation)

## Time Sensitivity Rubric

Score each candidate on `time_sensitivity_score` (integer 1–10):

- **10** — Specific event happens within 2 days of publication_date
- **8–9** — Specific event 3-5 days out, OR open-now seasonal activity at peak
- **6–7** — Year-round activity that pairs well with current season (warm-weather hike in spring, cozy library in winter)
- **3–5** — Year-round activity, off-peak season (still good, just not the most timely)
- **1–2** — Only marginally relevant or significantly out of season

For evergreen/recurring activities, lean **6-7** unless the season makes them especially timely.

## Audience Labeling

Set `audience` to one of:
- `"family-friendly"` — kids welcome, broadly appealing, no alcohol-centric content
- `"adults only"` — 21+, bar/brewery events, etc. (rare for free activities)
- `"all ages"` — welcome for everyone but not specifically kid-focused

## Body Structure (this is the main change vs. the old short-blurb format)

The `body_markdown` field contains the full multi-section recommendation as Markdown text. The structure:

```
[Hook paragraph — 3-5 sentences. Sets the vibe. Specifies why this is worth it. Reference local context if relevant ("less than 10 miles from East Cobb"). NO sub-heading on this one.]

**What it is:** [Paragraph describing the place/activity. History, headline features, scale, specifics. ~3-5 sentences.]

**Plan it (best-time descriptor):** [Paragraph on when to go, parking, what to bring, peak vs off-peak. The "best-time descriptor" varies by activity — examples: "Plan it (mornings work best)", "Plan it (Saturday afternoon)", "Plan it (after the rush)", "Plan it (any time)". ~3-5 sentences.]

**On the [activity-specific label]:** [Paragraph on the actual experience. Activity-specific label examples: "On the trail (1-2 hours)", "Inside (1-2 hours)", "On the water (half-day)", "At the festival (3-4 hours)". This is the "what you'll do once you're there" paragraph. ~3-5 sentences.]

**Logistics:** [1-2 sentence summary of: parking (free? fee?), restrooms, dogs (allowed? leash?), reservations, gates open hours.]

**Heads up:** [OPTIONAL — only include if there's an honest current quirk worth flagging: closed sections, recent changes, accessibility notes, weather impacts. Skip the heading entirely if there's nothing to say. ~1-3 sentences.]
```

**Sub-heading flexibility:** the labels (What it is / Plan it / On the trail / Logistics / Heads up) can be adapted slightly to the activity. A museum might use "Plan it (off-peak hours)" and "Inside (1-2 hours)". A park uses "Plan it (mornings work best)" and "On the trail (1-2 hours)". A library uses "Plan it (any time)" and "Inside (under an hour)". The labels SHOULD always be bolded with the colon style: `**What it is:** content here.`

Skip "Heads up:" if there's nothing concrete to flag — don't pad.

## Input Format

You receive a JSON array of candidates, each with `candidate_index`:

```json
[
  {
    "candidate_index": 1,
    "title": "...",
    "url": "https://...",
    "source": "hostname",
    "date": "...",
    "summary": "...",
    "full_text": "...",
    "address": "..."
  }
]
```

Plus: `publication_date`, `coverage_area`, `newsletter_name`.

**`full_text` is the article body fetched from the URL** — typically 1000-4000 characters of cleaned page text. **When it's present, use it as your primary source for `body_markdown`.** It has the specifics that let you actually write a 400-600 word recommendation (history, hours, parking, vibe, what reviewers say). The `summary` field is just a Brave snippet — too thin on its own for substantive sections.

If `full_text` is empty (some pages are bot-protected or returned errors), fall back to `summary` and write what you can. If neither has enough material, drop the candidate per the "write less rather than fabricate" rule.

## Output Format

Return ONLY a valid JSON object with no preamble, explanation, or markdown fences.

```json
{
  "newsletter_name": "East_Cobb_Connect",
  "section_header": "🆓 Free Activity of the Week (East Cobb)",
  "events": [
    {
      "candidate_index": 3,
      "emoji": "🥾",
      "name": "Vickery Creek Falls and the Roswell Mill Ruins",
      "event_date": "2026-05-10",
      "when": "Open daily, sunrise to sunset",
      "address": "Old Mill Park, 95 Mill St, Roswell, GA 30075",
      "is_free": "Free",
      "venue": "Old Mill Park",
      "audience": "all ages",
      "body_markdown": "This is one of those Saturday mornings that feels distinctly North Atlanta. Vickery Creek Old Mill Park is the rare spot where Civil War history, a real waterfall, and a covered bridge sit on the same trail, less than 10 miles from East Cobb. It's a beloved one. And it never gets old.\n\n**What it is:** Vickery Creek Old Mill Park is about 5 miles of connected trails winding along Vickery Creek and through hardwood forest in historic Roswell. The headline features are the 19th-century Roswell Mill ruins (the textile mill General Sherman burned during the Civil War), a wooden covered bridge over the creek, and the falls themselves, where water spills over a historic spillway dam. The short loop is 2.3 miles, the main loop is 3.6 miles with about 380 feet of elevation, and you can chain together longer routes for a real workout.\n\n**Plan it (mornings work best):** Show up by 9 AM if you want easy parking and quieter trails. Park at Old Mill Park on Mill Street or the Oxbo Trail lot, both free. Skip the Riverside Vickery Creek lot unless you don't mind the $5 fee. Bring water, real shoes, and a camera.\n\n**On the trail (1-2 hours):** Start at the Mill Street trailhead and take the wooden stairs down toward the creek. You'll hit the falls overlook in about 10 minutes, then cross the covered bridge for a closer look at the spillway. The mill ruins are right at the trailhead, so you can wander the brick walls and read the historical markers coming or going. Watch your footing on the wooden stairs after rain.\n\n**Logistics:** Free parking at Old Mill Park and Oxbo Trail lots. $5 at Riverside Vickery Creek Unit. Restrooms at the trailhead. Dogs welcome on leash. No registration needed.\n\n**Heads up:** As of August 2024, water access at the falls is suspended due to environmental impact from heavy visitation. The trails and overlooks are still open and the views are still all there, you just can't wade.",
      "source_label": "roswellgov.com",
      "time_sensitivity_score": 7,
      "time_sensitivity_reason": "Year-round trail at peak spring weather"
    }
  ],
  "all_scored": [
    {
      "candidate_index": 3,
      "emoji": "🥾",
      "name": "Vickery Creek Falls and the Roswell Mill Ruins",
      "event_date": "2026-05-10",
      "when": "Open daily, sunrise to sunset",
      "audience": "all ages",
      "venue": "Old Mill Park",
      "time_sensitivity_score": 7,
      "time_sensitivity_reason": "Year-round trail at peak spring weather"
    }
  ],
  "dropped_candidates": [
    {
      "candidate_index": 7,
      "reason": "Not actually free (parking $10, museum entry $15)"
    }
  ]
}
```

### Field definitions
- `candidate_index` — MUST match an index from the input. Never invent.
- `emoji` — one emoji matching the activity (🥾 trail/hike, 🌳 park, 📚 library, 🎨 art, 🦋 nature center, 🏛️ museum, 🎵 music, 🥏 outdoor recreation, 🌊 water, 🎭 performance)
- `name` — clean activity title
- `event_date` — YYYY-MM-DD on or after publication_date (for ongoing activities, use the publication date)
- `when` — natural-language time descriptor for the metadata line ("Open daily, sunrise to sunset", "Saturday, 10am-2pm", "Every Friday 6-9pm")
- `address` — full street address, including city and ZIP
- `is_free` — "Free" or a slightly more specific phrase like "Free, $5 parking" or "Free, donations welcome"
- `venue` — short venue name (sometimes redundant with address, that's fine)
- `audience` — one of the three audience values
- `body_markdown` — the multi-section body, ~400-600 words, with bold-prefix labels per the structure above
- `source_label` — short root domain for the More info link (e.g., "roswellgov.com", "atlantabg.org", "nps.gov"). The full URL is attached from `candidate_index`.
- `time_sensitivity_score` — 1-10 per rubric
- `time_sensitivity_reason` — short phrase
- `events` — array of EXACTLY ONE entry, your top pick
- `all_scored` — top 5 ranked candidates (the #1 also appears in `events`)
- `dropped_candidates` — brief reasons for excluded candidates

## Date Rules (CRITICAL — strict)

- The publication date is provided in the user prompt. Treat that as "today".
- For ONGOING / YEAR-ROUND activities (parks, libraries, nature centers, museums, regular trails), publication date = "today" works fine. The activity is happening now and continuing.
- For SPECIFIC DATED events, **default to DROP** unless the article explicitly states a future date. Past-tense language ("was held", "took place", "concluded", "last weekend") = DROP.
- **When in doubt, DROP.** A small honest list with rich coverage beats a long list with stale events.

## Quality Gates

Before returning:
- `events` contains exactly 1 entry
- `all_scored` contains 1-5 entries, ranked best-to-worst
- The pick's `body_markdown` is 400-600 words and uses the section structure above
- Every section starts with `**Label:** ` bold prefix (or no prefix for the hook paragraph)
- No em dashes
- No invented facts — only specifics from the candidate data
- The `is_free` field genuinely indicates a free activity
- `address` is a real street address from the candidate (not "Various locations" or "TBD")

## Critical Reminders

- Return ONLY valid JSON — no markdown fences, no preamble
- Do NOT output URLs in any field. We attach them from `candidate_index`.
- Only use facts from the provided candidate data. If you can't find specifics for a section, write less rather than fabricate.
- The `body_markdown` carries the recommendation. Treat it as the main deliverable — make it substantive.
