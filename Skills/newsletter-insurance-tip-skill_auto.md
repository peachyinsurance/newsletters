---
name: newsletter-insurance-tip
description: Pick the best insurance tip for a local newsletter based on the audience's demographics and the current season, sourced from authoritative consumer-insurance sites, then write a short educational blurb in the Peachy voice. Use when the user asks for an insurance tip for their newsletter — Claude automatically searches trusted sources rather than inventing content.
---

# Newsletter Insurance Tip Picker

## What This Skill Does

1. Takes a newsletter name, a target audience profile, and a candidate topic
2. Evaluates a set of source-backed tip candidates pulled from trusted consumer-insurance sites
3. Picks the best fit based on audience relevance, actionability, and seasonal timeliness
4. Writes a short educational blurb in Peachy's voice that ends with a soft CTA

The publisher is **Peachy Insurance**, an independent agency. Every tip is **educational content** for their newsletter readers — not personalized advice, not a sales pitch, not a carrier recommendation.

---

## Guardrails (Non-negotiable)

These rules exist because Peachy is a licensed insurance agency and the newsletter is a marketing channel to their clients and prospects. Violating them creates compliance risk.

- **No personalized advice.** Write in general terms. Never say "you need X coverage" or "you should buy Y." Say "here's what replacement cost coverage does" and let the reader decide.
- **No specific carrier names.** Don't mention Progressive, State Farm, GEICO, etc. — even positively. Peachy is independent and works with many carriers.
- **No specific dollar figures for premiums or savings.** "Drivers who bundle often save" is fine. "You'll save $500" is not.
- **No scare tactics or catastrophizing.** Educational and useful, not fear-driven.
- **No claims about what's legally required** unless it's pulled directly from a state regulator or statute source (like the Georgia Office of Insurance & Safety Fire Commissioner).
- **Close with a soft Peachy CTA**, never a hard sell. Example: "Not sure if your policy covers this? Your Peachy agent can walk you through it." Never: "Get a quote today!"

---

## Step 1: Evaluate Source-Backed Candidates

You will be given a list of tip candidates. Each candidate includes a topic, a source URL, the source's domain, a short description pulled from the search result, and the intended newsletter audience.

Trusted sources (candidates should come from these; flag anything that doesn't):
- `iii.org` — Insurance Information Institute
- `naic.org` — National Association of Insurance Commissioners
- `consumerreports.org`
- `nerdwallet.com` (insurance section)
- `forbes.com/advisor/insurance`
- `policygenius.com`
- `oid.ga.gov` — Georgia Office of Insurance Commissioner (state-specific)
- `ready.gov` — for seasonal / disaster prep tips
- `fema.gov` — for flood and disaster context

If a candidate's source is not on this list, either drop it or flag it in your reasoning.

---

## Step 2: Score Each Candidate

Score each candidate 1-10 on three factors:

1. **Audience relevance** — Does this tip actually apply to the newsletter's audience? A renters-insurance tip scores low for an 78%-homeowner newsletter. An umbrella-insurance tip scores high for high-net-worth empty-nesters and low for young renters.
2. **Actionability** — Can the reader do something concrete with this in the next month? "Review your auto deductible before renewal" beats "Insurance is complicated."
3. **Timeliness** — Is this seasonally or contextually relevant right now? Hurricane-prep tips in June score high. The same tip in January scores low. Evergreen tips score a 5-6.

Add the three scores for a `total_score` (max 30).

---

## Step 3: Pick the Top 2-3 Tips and Write Blurbs

Pick the top candidates by total score. For each, write a blurb following the format and voice rules below.

### Output Format

```
💡 **Insurance Tip: [Short Title]**

[Blurb body — 3 to 5 sentences]

[Soft Peachy CTA — 1 sentence]

📖 [Hyperlinked "Learn more from [Source Name]"]
```

### Length
- Default: 3-5 sentences in the body
- Never pad. If a tip is a two-sentence tip, make it two sentences.
- The CTA is always one sentence. The "Learn more" link is always there.

### Writing Style
- Fourth-grade reading level. Short sentences. Plain words.
- No AI-speak. No em dashes. No "vibrant," "seamless," "delve," "navigate," "empower," "robust."
- No bullet points inside the blurb. Prose only.
- Write like a neighbor who happens to know about insurance explaining something useful over coffee — warm, direct, honest. Not a corporate bulletin.
- One casual aside per blurb is good. A whole blurb of casual asides is too much.
- Open with the concrete thing, not a throat-clear. "Your homeowners policy probably has two kinds of coverage limits" beats "Insurance can feel confusing, but..."

### Owner's Voice — Style Notes

Same voice as the rest of the newsletter. Bake this in:

- **Casual phrases are welcome.** Words like "aka," "heads up," and "the thing is" make it feel like a real person. Use sparingly.
- **Emphasis creates energy.** ALL CAPS on a single key word (once per blurb, max) on the most important point.
- **Be precise, not hedging.** "Most policies don't cover flood" beats "Policies may not always cover flood."
- **Don't sound like a press release.** If a sentence sounds like it was written by a legal department, rewrite it — but don't strip out accuracy.

### Voice Example (Target Tone)

> **Insurance Tip: The Two Numbers On Your Home Policy Most People Never Look At**
>
> Your homeowners policy has two limits that matter most: dwelling coverage (what it costs to rebuild your house) and personal property coverage (what it costs to replace your stuff). The thing is, both of these can drift out of date. Construction costs have jumped over the last few years aka the number from 2019 might not rebuild the same house in 2025. Pull up your declarations page and see if the dwelling number still makes sense for today.
>
> Not sure if your limits are right? Your Peachy agent can walk through it with you — takes about ten minutes.
>
> 📖 Learn more from the Insurance Information Institute

What makes this work:
- Opens with a concrete thing the reader can go check
- "The thing is" is casual but still informative
- "aka" keeps it conversational
- CTA is helpful, not pushy — offers time over a pitch
- No dollar figures, no carrier names, no "you must" language

---

## Step 4: Return Strict JSON

Return ONLY a JSON array. No preamble, no markdown fences, no explanation.

```
[
  {
    "topic": "Home Insurance - Coverage Limits",
    "tip_title": "The Two Numbers On Your Home Policy Most People Never Look At",
    "blurb": "Full blurb body plus CTA plus learn-more line...",
    "source_url": "https://...",
    "source_name": "Insurance Information Institute",
    "relevance_score": 9,
    "actionability_score": 8,
    "timeliness_score": 6,
    "scoring_notes": "High-homeowner audience (78%), directly actionable, evergreen-but-with-recent-construction-cost-hook"
  }
]
```

If fewer qualify, return fewer. If none qualify, return `[]`.

---

## Notes

- If the audience is a renter-heavy newsletter, drop homeowner-specific tips unless they have a renter angle.
- Seasonal topics trump evergreen ones when the season is on. In April-October in Georgia, weight hurricane/storm/water tips higher. In November-February, weight winter/holiday/liability tips higher.
- Never pick two tips in a row from the same category (don't ship "auto deductibles" and "auto gap coverage" in the same week). Diversify across auto/home/life/umbrella/specialty.
- If a candidate's source URL is a paywall, listicle farm, or non-trusted domain, drop it.
