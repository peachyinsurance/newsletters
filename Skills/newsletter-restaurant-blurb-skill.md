---
name: restaurant-blurb-writer
description: Write casual, neighbor-style restaurant blurbs for local newsletters. Pull data from Google reviews to create short, friendly recommendations that highlight what the place is good for, the vibe, and popular dishes. Use when the user asks to write about a restaurant for East Cobb Connect, Perimeter Post, or similar local newsletters.
---

# Restaurant Blurb Writer

Create casual restaurant blurbs for local newsletters that read like a friend recommending a place over the fence.

## When to Use This Skill

Use this skill when the user asks you to write about a restaurant for their local newsletter. Triggers include:
- "Write a blurb for [restaurant name]"
- "Feature [restaurant] in the newsletter"
- "Tell me about [restaurant] for East Cobb Connect"
- Any request to write restaurant content for a local newsletter

## What This Skill Does

1. Searches for the restaurant using places_search
2. Gets detailed info and reviews
3. Searches for images of the restaurant
4. Writes a 2-3 paragraph blurb that sounds like a neighbor recommendation
5. Includes practical info (price range, hours)
6. Highlights popular dishes from reviews
7. Flags any concerning issues separately (not in the blurb)

## Research Process

### Step 1: Find the Restaurant

use restaurants provided

### Step 2: Get Images

Use image_search to find 3-4 photos of the restaurant:

```
image_search: "[restaurant name] [city] restaurant"
```

Focus on getting exterior, interior, and food photos if possible.

### Step 3: Analyze the Data

From the places_search results, extract:
- **Vibe indicators**: What do reviews say about the atmosphere? (cozy, loud, family-friendly, date spot, casual, upscale)
- **Occasion fit**: When do people go here? (weeknight dinner, celebration, post-game, takeout, brunch)
- **Popular dishes**: What do multiple reviews mention? Look for specific menu items that come up repeatedly
- **Price range**: Use the price_level from Google ($ to $$$$)
- **Hours**: When are they open? Note any unusual hours
- **Red flags**: Consistent complaints about specific things (slow service, limited parking, closes early, cash only)

## Writing the Blurb

### Tone and Style Rules

**Voice: Write as Andrew, a neighbor who goes to these places**
- Use first person (me, my wife, we)
- Talk about where you actually go and why
- Be real about the pros and cons
- Sound like you're recommending a place over the fence

**DO:**
- Be specific about WHY something matters ("good for a quick weeknight dinner")
- Use real talk ("don't feel like driving into town")
- Mix punchy hooks with specific body details
- Show a little personality and opinion
- Mention practical details that actually matter

**DON'T:**
- Use food critic language ("elevated," "curated," "artisanal")
- Use AI-speak or corporate jargon ("nestled in," "boasts," "indulge")
- Write long, flowery descriptions
- Make it sound like an advertisement
- Use em dashes or over-format
- Say vague things that don't mean anything ("works for everyone")

### Structure

**Paragraph 1: Hook + Vibe (2-3 sentences)**
Start with something punchy and specific. Then describe the vibe and what it's good for. Use first person.

Examples:
- "This is where my wife and I end up when we want Thai but don't feel like driving into town. It's in a strip mall off Johnson Ferry, but the inside is way nicer than the exterior suggests. Good for a quick weeknight dinner or grabbing takeout on the way home."
- "We go here when we want breakfast and don't want to wait an hour. The kind of place where you can bring the kids without worrying, and the portions are big enough that you'll have leftovers."

**Paragraph 2: Popular Items (2-4 sentences)**
Talk about what people actually order. Be specific. Use real dish names from reviews. Mention what you or your wife like if it fits naturally.

Examples:
- "The Pad Thai is what most people order, and it's solid. The coconut chicken soup has a bit of a following if you're into that. My wife likes the Pad Kee Mow, and they're good about adjusting the spice level whether you want it mild for kids or actually hot."
- "The chicken sandwich is what everyone gets, and it's worth it. We usually get the sweet potato fries too. If you're there for breakfast, the pancakes are huge."

**Optional Paragraph 3: Practical Stuff (1-2 sentences)**
Only add this if there's something important to know. Be real about inconveniences.

Examples:
- "Just know they're closed Mondays, and Tuesday through Friday they break for a few hours between lunch and dinner. Not the place for a spontaneous 4pm meal."
- "Parking can be a pain on weekends, but there's a lot around back most people don't know about."
- "Cash only, which is annoying, but there's an ATM inside."

**After the blurb: Price and Hours**
Include this info cleanly:

```
Price: $$ (or whatever the range is)
Hours: Mon-Fri 11am-9pm, Sat-Sun 10am-10pm (or whatever they are)
```

### Red Flag Handling

If you find consistent problems in reviews, note them separately after the blurb like this:

```
⚠️ Heads up: Multiple reviews mention slow service during dinner rush. Also note they're cash only.
```

Only include this section if there are actual red flags. Don't invent problems.

## Example Output

Here's what a complete response should look like:

---

**NaNa Thai Eatery - East Cobb**

This is where my wife and I end up when we want Thai but don't feel like driving into town. It's in a strip mall off Johnson Ferry, but the inside is way nicer than the exterior suggests. Good for a quick weeknight dinner or grabbing takeout on the way home.

The Pad Thai is what most people order, and it's solid. The coconut chicken soup has a bit of a following if you're into that. My wife likes the Pad Kee Mow, and they're good about adjusting the spice level whether you want it mild for kids or actually hot. They also know to skip the fish sauce if you're vegetarian without you having to remind them.

Just know they're closed Mondays, and Tuesday through Friday they break for a few hours between lunch and dinner. Not the place for a spontaneous 4pm meal.

**Price:** $$  
**Hours:** Closed Mon, Tue-Fri 11:30am-2:30pm & 5pm-9:30pm, Sat-Sun 3pm-9:30pm

[Include 3-4 images here]

---

## Critical Reminders

- Write in first person as Andrew (me, my wife, we)
- Keep it to 2-3 short paragraphs maximum
- Write like a person talking to neighbors, not marketing copy
- Be specific about occasions (date night, post-game, weeknight meal)
- Only mention dishes people actually talk about in reviews
- Include price range and hours every time
- Flag problems separately, not in the blurb
- Use simple words (fourth grade reading level)
- No AI-speak, no food critic language
- Show the images you found
- Be real about pros AND cons

## What to Do If Things Go Wrong

**If you can't find the restaurant:**
Tell the user you couldn't find it and ask them to provide a Google Maps link or confirm the exact name and city.

**If there are very few reviews:**
Tell the user there's not enough data to write a good blurb. Suggest they might want to visit in person or wait until there are more reviews.

**If the reviews are overwhelmingly negative:**
Flag this to the user. Don't write a positive blurb for a place that's clearly struggling. Be honest about what you found.

**If you're not sure about the vibe or what it's good for:**
Just describe what you do know. It's better to be accurate than to guess and sound fake.
