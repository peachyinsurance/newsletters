---
name: newsletter-subject-line-auto
description: Generate a punchy email subject line for the weekly Beehiiv newsletter. Subject lines drive open rates — keep them short, specific, and benefit-driven. Output is a single string, no quotes, no formatting.
---

# Newsletter Subject Line Writer (Automated)

## Purpose
Write ONE subject line per issue. Subject line is the single biggest lever on open rate, so this matters more than any other line of copy in the newsletter.

Output is a single plain string (no quotes, no markdown, no JSON wrapper). The pipeline will use it directly as the email subject in Beehiiv.

## Voice
Casual, neighborly, specific. Like a text from a friend, not a press release.

## Rules
- **6–12 words** max. Anything longer gets truncated in inbox previews.
- **Specific over generic.** "Free Earth Day at Brook Run" beats "Things to do this weekend".
- **One concrete hook**, not a list. The subject teases the most interesting thing in the issue, not all of it.
- **No clickbait.** Don't promise something the issue doesn't deliver.
- **No exclamation points** (high spam score) and no ALL CAPS.
- **No emojis** in the subject. Beehiiv handles emojis better in preview text.
- **No "Newsletter" or "Weekly Digest"** language. Reader knows it's a newsletter.

## Priority order for hooks (pick the strongest available)
1. **Featured event** with a specific date (e.g., "Ina Garten at Cobb Energy Friday")
2. **Tier 1 restaurant** if it's notably new or unique
3. **Top news headline** from Local Lowdown if it's high-impact ("Kroger Parkaire $23M expansion")
4. **Adoptable pet** (lighter, only when other hooks are weak)
5. **Free event** if it's a major free thing this weekend

## Input Format

You will receive a JSON object with the issue's content summaries:

```json
{
  "newsletter_name": "East_Cobb_Connect",
  "publication_date": "2026-04-30",
  "featured_event": {"name": "...", "date": "...", "venue": "..."},
  "tier1_restaurant": {"name": "...", "cuisine": "..."},
  "top_news_headline": "Kroger Parkaire expansion",
  "pet": {"name": "Mandy", "animal_type": "cat"},
  "free_event": {"name": "...", "when": "..."}
}
```

Any field can be empty if that section isn't ready.

## Output

Return ONLY the subject-line string itself. No quotes, no JSON, no preamble, no explanation.

Example output:
```
Ina Garten Friday plus a Sandy Springs taco spot
```

NOT:
```
"Ina Garten Friday plus a Sandy Springs taco spot"
```

## Quality gates
- Word count 6–12
- No exclamation points
- No emojis
- No ALL CAPS
- Specific hook, not generic
- Reader can guess what the email is about from the subject alone

## What good looks like
- "Ina Garten at Cobb Energy this Friday"
- "Kroger Parkaire just got a $23M facelift"
- "Free Earth Day Festival Saturday at Brook Run"
- "Marietta Diner regulars know what's up"
- "A Thai spot worth the strip-mall vibe"

## What bad looks like (rewrite these)
- "Lots of great things happening this week!" (vague + exclamation)
- "EAST COBB CONNECT — APRIL 30 EDITION" (caps + zero hook)
- "🎉 Don't miss it 🎉" (emojis + clickbait)
- "Your weekly digest of East Cobb news, events, and more" (generic + too long)
