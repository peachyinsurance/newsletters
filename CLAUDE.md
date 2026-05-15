# CLAUDE.md

Project context for Claude / AI assistants working in this repo. Distilled from collaboration sessions — meant to bootstrap new sessions quickly so you don't have to rediscover conventions.

---

## What this repo is

Newsletter automation for **Peachy Insurance**, an independent agency. Currently powers 3 weekly newsletters:

- **East Cobb Connect** (ECC) — East Cobb GA / Marietta / Roswell
- **Perimeter Post** (PP) — Sandy Springs / Dunwoody / Brookhaven
- **Lewisville Lake Lookout** (LLL) — Lewisville TX / Flower Mound / The Colony area

Each newsletter is assembled in Notion (the "parking spot") and published manually to Beehiiv. Beehiiv's send API requires a paid plan they don't have, so Notion-as-draft + manual paste is the workflow.

## Ownership split (don't refactor across this line without coordinating)

- **Victor:** events sections — Featured Event, Free Events, Insurance Tip, Weekend Planner. Plus general orchestration / review-app extensions.
- **Candice:** infrastructure + remaining sections — `notion_helper.py`, `url_validator.py`, `aggregator_drilldown.py`, `archive_stale_rows.py`, Pets, Restaurants, Real Estate, Local Lowdown, Reader Poll, Beehiiv send.

Before touching a section outside your lane, check `git log` for recent activity and confirm with Victor.

## Newsletters

Canonical config lives at [NewsletterCreation/Code/newsletters_config.py](NewsletterCreation/Code/newsletters_config.py). Every section pipeline imports `NEWSLETTERS` and uses `filter_by_env()` to honor the `NEWSLETTER` env var (single newsletter or `"all"`). Adding a 4th newsletter is a one-file edit there.

## The pipeline shape

```
Section workflow (cron + workflow_dispatch)
   ├─ Section pipeline (Python, in <Section>/Code/<Section>.py)
   │    ├─ Brave Search → candidates
   │    ├─ Pre-filter (URL validation, aggregator blocklist, date filter)
   │    ├─ Claude (skill prompt as system, JSON output)
   │    └─ Save rows to section-specific Notion DB
   ├─ (optional) chain trigger → next section's workflow
   └─ Trigger assemble_newsletter.yml at end of chain

Assembler (NewsletterCreation/Code/assemble_newsletter_page.py)
   ├─ Reads every section DB
   ├─ Renders sections into Notion blocks on the per-newsletter landing page
   └─ Sync-back: scans the landing page for manual edits and patches them back
      to the section DBs (so editors can tweak directly in Notion)
```

## Workflow chain pattern

Every section workflow has a `chain: true/false` workflow_dispatch input. When `chain=true` (default), the workflow's last step posts a `workflow_dispatch` to the next section in the sequence and finally to `assemble_newsletter.yml`. End result: one click runs the whole newsletter.

To debug a single section, dispatch with `chain=false`.

## Skill-as-system-prompt pattern

Each section's Claude call uses a Markdown file under `Skills/` as the system prompt. The skill defines voice, format, output schema. The pipeline supplies candidates + context as the user message and parses the JSON response. **Skills are the deliverable** — voice and format live there, not in code.

## House-style rules (enforced)

### No em dashes — HARD RULE
House style bans em dashes (`—`, U+2014). Every skill file under `Skills/` carries a `> **HARD RULE: NO EM DASHES.**` banner. The assembler ([NewsletterCreation/Code/assemble_newsletter_page.py](NewsletterCreation/Code/assemble_newsletter_page.py)) has a `_strip_em_dashes()` defense-in-depth filter in the block builders. En dashes (`–`, U+2013, for ranges like "10am–4pm") are fine.

Substitute em dashes with commas, periods, parens, semicolons, or "and."

### Link display format keeps `www.`
Visible anchor text strips `https://` and the path, but keeps `www.` if present in the original URL. So `https://www.appenmedia.com/event/123` renders as `www.appenmedia.com`, but `https://appenmedia.com/event/123` renders as `appenmedia.com`.

### Primary sources only
Don't link to competitor newsletters or aggregator roundups. Link to the event's / business's / source's own page. `prefer_primary_source()` in Weekend Planner drills Eventbrite/Meetup/AllEvents picks for embedded primary URLs.

### `candidate_index` is the anti-hallucination contract
Claude is fed candidates each tagged with a 1-based `candidate_index`. Claude returns picks referencing only `candidate_index` — never raw URLs. The pipeline attaches the real URL from the matching candidate. **Never return raw URLs from Claude.** If Claude needs to mention a domain in its output (e.g., "Learn more from X"), the URL is still attached pipeline-side.

## Notion row lifecycle

Every section DB row has a `Status` field with this state machine:

- `pending` — fresh candidate from a generator run, awaiting review or auto-promotion
- `approved` (or `Tier 1 Winner` / `Tier 2 Winner` for restaurants) — this week's published pick
- `approved - old` — previous winners; kept around for 8 weeks as anti-repeat history
- `rejected` — manually rejected in the review app
- (archived) — Notion-deleted; falls outside the 8-week exclusion window

The assembler renders rows with `Status` in {`approved`, `pending`, `Tier 1 Winner`, `Tier 2 Winner`} depending on the section's exact pick logic. Rows in `approved - old` or `rejected` are excluded from rendering.

## Cleanup

- **Per-section weekly cleanup** ([Pets/Code/cleanup_pets.py](Pets/Code/cleanup_pets.py), [Restaurants/Code/cleanup_restaurants.py](Restaurants/Code/cleanup_restaurants.py), [Real Estate Corner/Code/cleanup_real_estate.py](Real%20Estate%20Corner/Code/cleanup_real_estate.py), [Free Events/Code/cleanup_free_events.py](Free%20Events/Code/cleanup_free_events.py)) — run Saturdays at 1pm UTC. Flip `approved` → `approved - old`, archive rows older than 8 weeks, archive stale `pending` / `rejected` rows.
- **Unified ad-hoc cleanup** [archive_stale.yml](.github/workflows/archive_stale.yml) + [archive_stale_rows.py](NewsletterCreation/Code/archive_stale_rows.py) — manually dispatched. Sweeps all 11 section DBs by `cutoff_days` for a chosen newsletter. Use this when the landing page is bleeding stale data (e.g., previous-week rows still rendering). Auto-triggers the assembler after.
- **Friday pre-cleanup** [archive_approved.yml](.github/workflows/archive_approved.yml) — flips Friday-evening `approved` → `approved - old` across pets/restaurants/events the day before the Saturday section cleanups.

**When the user asks for "cleanup script per section," point them at `archive_stale.yml` first** — it's the unified answer.

## Review app

[review-app/](review-app/) is a React/Vite app deployed to GitHub Pages at https://peachyinsurance.github.io/newsletters/. Editors use it to approve/reject Pets, Restaurants, Featured Event picks before they render.

The app auto-discovers newsletter names from row data ([App.jsx:199](review-app/src/App.jsx#L199)) — no hardcoded newsletter list. Adding a new section tab follows a 5-step pattern: extend the section registry in `App.jsx`, add a Tile component (e.g., `BusinessBriefTile.jsx`), add the JSON export in `export_notion_data.py`, copy it into `deploy_review_app.yml`, and add styling. Tile components are intentionally separate per section — don't refactor them into a shared component.

## Section-specific notes

### Weekend Planner (Victor)
- 18 buckets per run: 3 newsletters × 2 audiences (Family / Adult) × 3 days (Fri/Sat/Sun)
- Architecture: pipeline pools all 3 days per audience for ONE Claude call (cost-saving). Three-pass query strategy: primary → fallback retry if too few picks → per-day gap-fill if any day has zero coverage.
- **Day determination is pipeline-side, not Claude-side.** Each candidate is tagged with `days: [Friday, Saturday, ...]` in `fetch_and_filter_candidates` before reaching Claude. Candidates that don't pin to a target-weekend day are dropped pre-Claude. Claude reads the `days` field and uses it to balance picks across the weekend.
- The hard rule: every event must be on the target weekend's Fri/Sat/Sun. The pipeline enforces this; the skill trusts the `days` field.
- **NO pre-Claude URL validation** — bot-protected event-calendar pages (visitlewisville.com/events/, llela.org, etc.) return 403/404 to HEAD requests but are legit. Killing them pre-Claude starves the candidate pool.
- Aggregator drilldown: when Claude picks an Eventbrite/Meetup/AllEvents URL, the pipeline drills the page for the official primary-source link and swaps.
- Listicle filter (added 2026-05-14): `AGGREGATOR_BLOCKLIST` includes mommypoppins.com, thrillist.com, timeout.com, 365atlanta.com, accessatlanta.com, 365thingsindallas.com. `LISTICLE_URL_HINTS` tuple catches roundup-pattern paths (`/things-to-do`, `/best-of`, `/weekend-guide`, etc.) on other domains. These prevent the "Claude wrote about Six Flags but the URL is to a listicle that mentioned it" failure mode.

### Insurance Tip (Victor)
- Shared-tip architecture: **one Claude run produces identical Notion rows for every live newsletter** (currently 3). Compliance-driven decision — insurance content is general-purpose, doesn't differ per newsletter.
- Compliance guardrails baked into the skill: no personalized advice, no specific carrier names, no specific dollar figures for premiums/savings, no scare tactics, no legal-requirement claims (unless from a state regulator source), soft Peachy CTA only (never hard sell). **Before loosening any of these, flag as a compliance change.**
- Trusted sources allowlist: iii.org, naic.org, consumerreports.org, nerdwallet.com, forbes.com, policygenius.com, oid.ga.gov, ready.gov, fema.gov.
- Assembler reads from the DB and renders the row where `Default Winner = true`, falling back to most-recent non-rejected.

### Free Events (Victor)
- Picks ONE free activity per week per newsletter (events OR ongoing — parks, libraries, museums, trails all qualify).
- 400-600 word multi-section recommendation format: hook + What it is / Plan it / On the trail / Logistics / (optional) Heads up.
- Pipeline enriches Brave snippets with full article body via `_fetch_article_text` so Claude has enough source material for the substantive sections.
- `body_markdown` field carries the rich recommendation through to the assembler.

### Featured Event (Victor)
- Single event per newsletter per week. Reviewer approves in the app before assembly.
- Brave cache directory (`Featured Event/Code/brave_cache/`) — keeps round-1 search results for the same week to avoid burning Brave quota on retries.
- Header image template at `Featured Event/Template/featured_event_template.png`.

### Pets (Candice)
- ⚠ **Pets pipeline does NOT yet work for Lewisville.** `ORG_PLAN` in [Furry_Friends_Marietta.py](Pets/Code/Furry_Friends_Marietta.py) is hardcoded to Marietta GA rescues (Mostly Mutts, Barkville Dog Rescue). Running for Lewisville produces zero pets. Full wire-up paused — needs TX rescues that syndicate to RescueGroups + refactor of `ORG_PLAN` into per-newsletter config.

### Real Estate Corner (Candice)
- Three price tiers per newsletter: Starter / Sweet Spot / Showcase. Realtor.com via RapidAPI.
- Tier definitions live in `STANDARD_RE_TIERS` in [newsletters_config.py](NewsletterCreation/Code/newsletters_config.py) — shared across newsletters but a newsletter can override.

### Reader Poll (Candice)
- 4-option Beehiiv poll. Each option maps to a sponsorable local-business category — the poll doubles as ad-pitch intel.
- Avoid re-using categories within the past 8 weeks (drives breadth of category coverage).

## Common gotchas

- **PAT_TOKEN expires.** Most workflows use `secrets.PAT_TOKEN` (a GitHub Personal Access Token) for `actions/checkout`. When it expires, every workflow fails with `fatal: could not read Username for 'https://github.com': terminal prompts disabled`. Regenerate at github.com → Settings → Developer settings → Personal access tokens, then update the repo secret.
- **Bash tool mangles git colon-paths on Windows.** Commands like `git show origin/main:path/to/file` get the colon swapped to a semicolon. Use the PowerShell tool with `$env:MSYS_NO_PATHCONV=1` instead, or use `git diff -- path` to avoid colon syntax.
- **Concurrency on assemble_newsletter.yml.** The workflow has a `concurrency:` guard (queues runs per newsletter+section). Multiple chain/redo/archive triggers firing close together used to race past `notion_clear_page` and leave duplicate content on the page. Don't remove this guard.
- **Notion select option colors.** The Notion API rejects PATCH operations that try to update the color of an existing select option (`Cannot update color of select with name: X`). When adding new options to a select-property schema, OR remove colors from the options list entirely — don't try to change colors on existing options.

## When in doubt

- Don't unilaterally refactor shared infrastructure (`notion_helper.py`, `url_validator.py`, `aggregator_drilldown.py`, `archive_stale_rows.py`). Coordinate.
- Don't generate URLs — every URL in published copy must come from a real candidate the pipeline saw.
- Don't add `webhooks_for_X` or `cleanup_for_Y` scripts that duplicate what `archive_stale_rows.py` already does. Extend the unified script if the unified pattern is insufficient.
- Don't add em dashes. Anywhere.
