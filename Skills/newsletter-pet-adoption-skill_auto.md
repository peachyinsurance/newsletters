---
name: pet-adoption-blurb-writer
description: Write casual, warm, neighbor-style pet adoption blurbs for local newsletters like East Cobb Connect and Perimeter Post. Pull data from a Petfinder listing or similar adoption source and write a short, friendly blurb that makes readers want to go meet the animal. Use when the user provides a pet listing URL and asks for adoption content for a local newsletter.
---

# Pet Adoption Blurb Writer

Write a newsletter blurb about an adoptable pet. The goal is to make readers feel like their neighbor just texted them saying "you have to go meet this dog/cat."

## When to Use This Skill

Use when the user provides a pet listing link (Petfinder, shelter website, rescue org page, etc.) and asks for adoption content for a local newsletter. Triggers include:
- "Write a blurb for this pet"
- "Feature this dog/cat in the newsletter"
- Any request to write pet adoption content for East Cobb Connect, Perimeter Post, or similar local newsletters

## What This Skill Does

1. Fetches the pet listing from the provided URL
2. Extracts all relevant details (name, age, breed, personality, quirks, shelter info)
3. Writes a 200-300 word blurb that sounds like a pet lover recommending an animal to a neighbor
4. Includes shelter name, address, phone, and hours
5. Flags anything the reader needs to know (behavioral quirks, compatibility notes) with a positive spin

---
## Provided URLs

https://www.petfinder.com/search/pets-for-adoption/us/ga/atlanta/?shelterRescue=dc176986-7d9a-45b9-b144-f2df586d9cf4&includeOutOfTown=true&distance=anywhere

https://atlantahumane.org/adopt/dogs/

https://atlantahumane.org/adopt/cats/ 


## Step 1: Fetch the Listing

Use web_fetch on the URL from Provided URLs section. Extract:
- Name, age, breed, sex, size, color
- Personality traits and behaviors listed
- Any quirks, training status, or compatibility notes (kids, dogs, cats)
- Health status (vaccinated, spayed/neutered, microchipped, heartworm)
- Shelter or rescue name, address, phone, hours, email
- Any ID number needed to inquire about the pet
- How to adopt (walk in, inquiry form, appointment required, etc.)

If the listing has very little information, tell the user before writing and ask if they have anything to add.

---

## Step 2: Write the Blurb

### Voice and Tone Rules

Write as a pet lover in his mid-30s who genuinely loves both dogs and cats. Warm, a little playful, never corny. The tone is whimsical but grounded. Think neighbor-over-the-fence, not animal shelter marketing copy.

**DO:**
- Be specific about the animal's personality using details from the listing
- Give quirks and imperfections a positive spin (not housetrained yet = just needs a routine; shy at first = warms up into a loyal companion)
- Use short sentences and simple words
- Let personality come through naturally — it's okay to be a little funny if it fits
- Write like a real person types, not like an AI generating content
- Be direct about what kind of home would be a good fit

**DON'T:**
- Use em dashes as a writing device
- String together compound phrases trying to be clever ("will-stare-at-your-hand-until-you-throw-it")
- Use "classic [animal] behavior/energy/personality" — it sounds like AI filler
- Use "hidden gem," "forever home," "fur baby," "pawfect," or any pet adoption cliche
- Write flowery, emotional copy that sounds like a fundraising appeal
- Say "could you be the one" or similar adoption-brochure phrases
- Use similes that sound unnatural ("like she's known you forever")
- Over-explain personality with adjectives — show it through specific behaviors instead

### Handling Quirks and Imperfections

Every pet has something. Don't hide it, don't bury it, and don't make it scary. Put a real, honest, positive spin on it.

- Not housetrained: "She's not housetrained yet, but she's been on her own for a while so that's expected. Give her a few weeks and a routine and she'll figure it out fast."
- Shy/timid: "He's a little shy when he first meets you. Not standoffish, just cautious. He's not going to be in your face from day one, but once he warms up, he's going to want to be wherever you are."
- Nipping/energy issues: "He's got some energy to burn and does best with a cat buddy to wrestle around with. If you already have a cat at home, this might actually be the perfect excuse to add a second one. Two cats are almost always easier than one anyway."
- Needs a fenced yard: "She'd do best with a fenced yard. If you've got one, she's going to make great use of it."
- Unknown compatibility: "They don't know yet how he does with kids or other pets since he came in as a stray. You can bring your animals in for a meet and greet before committing."

The pattern: acknowledge it plainly, reframe it as manageable or even a positive, move on. Don't dwell.

### Structure

**Paragraph 1: Hook + Who This Animal Is (2-3 sentences)**
Open with something that pulls the reader in. Use a specific detail from the listing — something that makes this animal feel real and individual, not generic. Don't start with the pet's name as the first word. Don't start with "Meet [Name]" as the first sentence.

**Paragraph 2: Personality and What Makes Them Special (2-3 sentences)**
Get into who they are. Use specific behaviors from the listing. If they have a strong personality trait (obsessed with fetch, shy but loyal, playful, lap cat), lead with that. This is where a little warmth and humor can come in naturally if it fits.

**Paragraph 3: Quirks, Compatibility, and What Kind of Home They Need (2-3 sentences)**
Be honest about what the animal needs. Frame imperfections as manageable. If they'd do great in a specific situation (another pet, fenced yard, patient owner), say so. If there's nothing notable here, skip the paragraph and fold the practical detail into paragraph 2.

**Paragraph 4: How to Adopt (2-3 sentences)**
Name the shelter or rescue briefly. Describe how to get the process started (walk in, submit inquiry, call ahead, etc.). Include the address.

**After the blurb: Shelter Info Block**
Always include this after the blurb:

```
[Shelter/Rescue Name]
[Address]
[Phone] | [Email if available]
[Hours]
```

---

## Approved Example Outputs

Use these as voice and tone anchors. Match this register.

---

**Dog Example — Yoda, Pit Bull Mix, Cobb County Animal Services**

She showed up on December 1st just wandering around, doing her own thing. No collar, no plan, but clearly a good girl. Her name is Yoda, she's four years old, and "ball is life" is basically her whole personality. She will not stop thinking about tennis balls and she will not let you forget it either. If you have a backyard and an arm, she's going to love you forever.

She already knows sit and shake, walks great on a leash, and loves being petted. She's not housetrained yet, but she's been on her own for a while so that's expected. Give her a few weeks and a routine and she'll figure it out fast. Dogs like this usually do. The volunteers say she's calm and sweet, not bouncing off the walls. Just a solid, loving dog who had a rough stretch of luck.

Yoda's fully vaccinated, heartworm negative, and gets spayed and microchipped as part of the adoption. Cobb County Animal Services is on Al Bishop Drive in Marietta, open Tuesday through Sunday, 10:30am to 4:30pm. Ask for ID #15403.

**Cobb County Animal Services**
1060 Al Bishop Dr, Marietta, GA 30008
(770) 499-4136 | Tues-Sun 10:30am-4:30pm, closed Mondays

---

**Cat Example — Fred, Domestic Shorthair, Good Mews Animal Foundation**

Fred is an orange tabby who's been around the block once already. He was adopted through Good Mews a few years ago, and his family recently had to bring him back. Not his fault. Just life. Now he's looking for round two.

He's a little shy when he first meets you. Not standoffish, just cautious. He's not going to be in your face from day one, but once he warms up, he's going to want to be wherever you are. He likes toys when the mood hits and eats wet and dry food without complaint.

He's got some energy to burn and does best with a cat buddy to wrestle around with. If you already have a cat at home, this might actually be the perfect excuse to add a second one. Two cats are almost always easier than one anyway.

Good Mews is a no-kill, cage-free shelter on Robinson Road in Marietta. You'll need to submit an inquiry and visit in person to meet Fred. Call or email to get the process started.

**Good Mews Animal Foundation**
3805 Robinson Road, Marietta, GA 30067
(770) 499-2287 | adopt@goodmews.org

---

## What to Do If Things Go Wrong

**If the listing is unavailable or returns no content:**
Tell the user the page didn't load and ask them to paste the pet's description directly.

**If there's almost no information:**
Tell the user before writing. Ask if they have anything to add. Don't invent personality details or make assumptions about the animal's temperament.

**If the pet has a serious behavioral flag (aggression history, bite record):**
Do not spin this positively in the blurb. Flag it to the user separately and ask how they want to handle it. It's their call whether to feature this animal.

**If the shelter info (hours, address, phone) is missing:**
Note it at the end of your response and ask the user to fill it in before publishing.

---

## Critical Reminders

- 200-300 words. No shorter, no longer.
- Write like a real person, not like adoption marketing copy
- Quirks get a positive spin, but are never hidden
- No em dashes, no pet cliches, no flowery language
- Specific beats vague — use the actual details from the listing
- Always include the shelter info block at the end
- Match the register of the approved examples above
