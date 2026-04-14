---
name: newsletter-featured-event
description: Pick the best featured event for a local newsletter based on neighborhood demographics, then write a polished blurb. Use when the user asks for a featured event for their newsletter — Claude automatically searches for upcoming local events rather than waiting for the user to provide a list.
---

# Newsletter Featured Event Picker

## What This Skill Does

1. Takes a newsletter name or neighborhood from the user
2. Searches the web for upcoming local events automatically
3. Looks up local demographics using web search
4. Recommends the top 2-3 events with reasoning
5. Waits for the user to pick one
6. Writes a polished featured event blurb

---

## Step 1: Find Upcoming Events

When the user provides a newsletter name or neighborhood, immediately search the web for upcoming local events. Do NOT ask the user to provide a list — go find them.

Search queries to use (run several to get broad coverage):
- "[neighborhood/area name] events this weekend"
- "[neighborhood/area name] events next week"
- "[neighborhood/area name] things to do [month year]"
- "[neighborhood/area name] concerts shows festivals [month year]"
- "events near [neighborhood/area name] Georgia [month year]"
- Check local event aggregators like Eventbrite, Patch, and local arts/entertainment sites

Aim to compile at least 8-10 candidate events before moving to evaluation. Include a mix of event types: arts, food, music, community, family, sports, etc. For each event, capture:
- Event name
- Date and time
- Venue / location
- Ticket price (or free)
- A brief description of what it is
- URL / source

If the user provides a list of events in addition to asking for a pick, use their list AND supplement with your own search results to make sure nothing great is being missed.

---

## Step 2: Get Demographics

When the user provides a newsletter name or neighborhood, use web search to find:

- Median household income
- Median age
- Family vs. adult skew (are there lots of kids in the home, or are people past that stage?)
- Homeownership rate
- Education level (if available)

Search queries to use:
- "[neighborhood name] demographics median income age"
- "[zip code] Georgia demographics" (if zip is known)

Summarize the demographic profile in 3-4 sentences before moving to event selection. Keep it factual and brief.

---

## Step 3: Pick the Top 2-3 Events

Evaluate every event on the list using these four factors, in this priority order:

1. **Demographic fit** — Does this event match the age, income, lifestyle, and interests of the audience?
2. **Uniqueness / can't-miss factor** — Is this a one-night-only event? Is it rare or special? Or is it something the reader could do any weekend?
3. **Family vs. adult skew** — Match the event type to the newsletter's audience. If the newsletter skews toward empty nesters, adult events beat family events even if the family events are well-attended.
4. **Ticket price relative to local income** — A $138 ticket is a non-issue for a $150K household. For a $60K household, flag it. Don't disqualify expensive events for high-income audiences, and don't assume low-income audiences only want free events.

Present 2-3 recommendations. For each one, include:
- Event name, date, time, price
- A 2-3 sentence explanation of WHY it fits this audience
- Any trade-offs or reasons it might not be the right pick

Then ask the user to choose before writing the blurb.

---

## Step 4: Write the Blurb

Once the user picks an event, write the featured event blurb using these rules:

### Format
```
⭐ **Featured Event: [Event Name]**
📅 [Day], [Month Date] | [Time] | [Venue]
🎟️ [Ticket price or Free] | [Hyperlinked call to action: "Get Tickets" or "Learn More"]

[Blurb body]
```

### Length
- Default: 4-5 sentences
- Go to 6-8 sentences only if the event has enough substance to warrant it (rich backstory, multiple selling points, strong emotional hook)
- Never pad to hit a word count. If there are only 4 sentences worth of things to say, write 4 sentences.

### Writing Style
- Fourth-grade reading level. Short sentences. Plain words.
- No AI-speak. No em dashes. No words like "vibrant," "seamless," "delve," "bustling," or "tapestry."
- No bullet points inside the blurb. Prose only.
- Write like a neighbor telling another neighbor about something worth doing — warm, direct, and honest.
- The closer should have energy. It should create mild urgency or a reason to act now. Do not end flat.
- Do not stuff in logistical details (address, parking, etc.) unless they are genuinely useful to the reader.

### Owner's Voice — Style Notes
The owner has a distinct voice. Bake this into every blurb:

- **Inject personality and casual language.** Words like "aka" and phrases like "so take advantage of that!" make it feel like a real person talking, not a newsletter bot. Use them when they fit naturally.
- **Use emphasis to create energy.** ALL CAPS on a key word (like "STEAL") does more work than the word alone. Use it sparingly — once per blurb max — on the single most exciting fact.
- **Be precise, not hedging.** Don't say "just a movie" when you mean "just a typical movie." Small word choices matter.
- **Run-on energy is okay.** Writing the way people actually talk — even if it bends grammar rules — is a feature, not a bug. "Nearly 40,000 people go every year aka...this isn't a small community event" works because it sounds human.
- **Don't be stiff.** If a sentence sounds like it came from a press release, rewrite it.

### Tone Reference
Here is a strong example of the right tone (Ina Garten blurb):

> If you've ever stood in your kitchen on a Sunday, glass of wine in hand, making her roast chicken while the Barefoot Contessa plays in the background — this one's for you.
>
> Ina Garten is coming to Marietta for one night only. She'll be talking about her new memoir, sharing the stories behind the recipes, the TV show, and the life she built from scratch. Then she opens it up to the crowd for Q&A. It's personal, it's warm, and it's exactly what you'd expect from her — like being invited over, except there are a few hundred of you.
>
> This is not a cooking demo or a book signing line. It's a real night out. The kind you'll actually talk about over dinner the next week.
>
> Tickets start at $138. They are selling. If you've been on the fence, get off it.

What makes this work:
- Opens with a specific, relatable image (not a generic compliment)
- Explains what the event actually IS in plain terms
- Makes the reader feel something
- Closes with urgency without being pushy or fake

Here is a strong example of the owner's voice baked in (AJFF blurb):

> AJFF is the largest Jewish film festival in the world and one of the biggest film festivals in Atlanta. Nearly 40,000 people go every year aka...this isn't a small community event. It's a legitimate festival that happens to be in your backyard, so take advantage of that!
>
> This year's lineup pulls from 20 countries across drama, documentary, and comedy. Several screenings end with a live Q&A with the filmmaker, which makes it feel like more than just a typical movie. You get context, conversation, and something to talk about over dinner after.
>
> The best part this weekend? You don't have to go anywhere. It's right at City Springs. Walk in, grab a drink, find your seat. At under $20 a ticket its a STEAL. Browse the schedule at AJFF.org, pick something that sounds good, and bring someone worth sitting next to in the dark.

What makes this work:
- "aka" keeps it casual and conversational
- "STEAL" in caps punches the value point
- "typical movie" is more precise than "just a movie"
- Closer is warm and social, not transactional

---

## Notes

- If the user already knows the demographics or provides them, skip Step 2 and use what they give you.
- If the user provides their own list of events, use it — but still supplement with a web search to make sure nothing great is being missed.
- If the user tells you the newsletter skews a certain way (e.g., "heavy empty nesters" or "lots of young families"), weight that heavily in Step 3 — it overrides what the raw demographics suggest.
- Never recommend an event just because it's free. Free is not a selling point for high-income audiences.
- Never penalize an event for having a ticket price if the audience can clearly afford it.
- If two events are close in score, default to the one with higher uniqueness. Readers can find a farmers market any weekend. They can't always see Ina Garten.
- Always research whether a recurring event is truly unique before recommending it as a featured event. A monthly series is not a can't-miss pick.