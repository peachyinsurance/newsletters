#!/usr/bin/env python3
"""Scout pass for A Voice For All Paws adoptables widget.

avoiceforallpaws.com/adoptables/ embeds a Petstablished iframe (Angular
SPA). The underlying Adoptapet API rejects species-filtered searches
without a paid key, so we have to render the widget in a real browser.

This script uses Apify's `apify~web-scraper` actor (no permission
approval needed). The pageFunction runs IN the rendered page context
so we use `document`/`window` directly — no `page.evaluate` calls.
It loads the widget, scrolls to trigger any lazy loading, then dumps:

  - the rendered HTML body (first 8 KB) so we can see structure
  - counts of pet-card-ish selectors that matched
  - a sample of each matched card's outer HTML (first 400 chars)
  - any visible text containing the words 'cat' / 'dog' / 'breed' / 'age'

Run once, paste the output back — we use it to design the real extractor.

Usage:
    APIFY_API_KEY=... python3 "Pets/Code/scout_avfap.py"
"""
import os
import sys
import json
import time
import requests

APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "")
if not APIFY_API_KEY:
    print("✗ APIFY_API_KEY not set in env.")
    sys.exit(1)

# Petstablished redirects to Wagtopia (their parent platform). Skip the
# redirect hop by pointing Apify at the final URL directly.
WIDGET_URL = ("https://www.wagtopia.com/search/org"
              "?id=499656&iframe=normal&page=1&sort=default"
              "&name=A+Voice+for+All+Paws")

# Apify pageFunction — runs INSIDE the rendered Chromium page (web-scraper
# evaluates pageFunction in the page context, so document/window/etc are
# globals, but Puppeteer-level APIs like page.evaluate() are NOT available).
PAGE_FUNCTION = r"""
async function pageFunction(context) {
    const { request, log } = context;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    // Selectors that plausibly mark a pet card (one of these should hit)
    const CANDIDATE_SELECTORS = [
        '[data-pet-id]',
        '[data-animal-id]',
        '.pet-card',
        '.animal-card',
        '.pet-listing',
        '.pet-tile',
        '.adoptable',
        '.adoptable-pet',
        'article',
        '.card',
        '.animal',
        '[class*="pet"]',
        '[class*="animal"]',
    ];

    // Wait up to 30s for any candidate selector to appear in the DOM.
    let appearedSelector = null;
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
        for (const sel of CANDIDATE_SELECTORS) {
            try {
                if (document.querySelectorAll(sel).length > 0) {
                    appearedSelector = sel;
                    break;
                }
            } catch (e) { /* invalid selector — skip */ }
        }
        if (appearedSelector) break;
        await sleep(500);
    }
    if (!appearedSelector) log.warning('No pet-card selector appeared in 30s');

    // Scroll to trigger lazy loading.
    for (let i = 0; i < 5; i++) {
        window.scrollTo(0, document.body.scrollHeight);
        await sleep(1500);
    }

    // Tally each candidate selector's hit count + grab a sample of the
    // first matched element's outer HTML.
    const selectorReport = CANDIDATE_SELECTORS.map((sel) => {
        let count = 0;
        let sample = '';
        try {
            const els = document.querySelectorAll(sel);
            count = els.length;
            if (count > 0) sample = els[0].outerHTML.substring(0, 400);
        } catch (e) { /* invalid selector — skip */ }
        return { selector: sel, count, sample };
    });

    // Strip scripts/styles for a readable body dump.
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll('script, style').forEach((n) => n.remove());
    const bodyText = clone.innerHTML;

    // The real pet cards live inside `.pets-container`. Pull its direct
    // children (and their HTML) so we can see the actual card markup.
    const containerSelectors = ['.pets-container', '.row.pets-container',
                                '[class*="pets-container"]', '[class*="animal-container"]'];
    let containerEl = null;
    let containerSelector = null;
    for (const sel of containerSelectors) {
        const el = document.querySelector(sel);
        if (el) { containerEl = el; containerSelector = sel; break; }
    }
    const containerReport = {
        selector: containerSelector,
        found: !!containerEl,
        children_count: containerEl ? containerEl.children.length : 0,
        first_3_children_html: [],
        all_descendant_classes: [],
    };
    if (containerEl) {
        for (let i = 0; i < Math.min(3, containerEl.children.length); i++) {
            containerReport.first_3_children_html.push(
                containerEl.children[i].outerHTML.substring(0, 1500)
            );
        }
        // Class-name frequency inside the container (helps spot card class).
        // Use classList not className.split: SVG elements have className as
        // an SVGAnimatedString (not a string), which crashes .split().
        const classCounts = {};
        containerEl.querySelectorAll('[class]').forEach((el) => {
            const list = el.classList ? Array.from(el.classList) : [];
            for (const c of list) {
                if (!c) continue;
                classCounts[c] = (classCounts[c] || 0) + 1;
            }
        });
        containerReport.all_descendant_classes = Object.entries(classCounts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 25)
            .map(([cls, n]) => `${n}\t${cls}`);
    }

    // Look for any element whose text mentions cat/dog/breed/age — gives
    // us hints at card structure if the named selectors all missed.
    const re = /\b(cat|dog|kitten|puppy|breed|age|adopt)\b/i;
    const speciesHints = [];
    const els = document.querySelectorAll('div, span, p, h1, h2, h3, h4, h5, li, article');
    for (const el of els) {
        const t = (el.innerText || '').trim();
        if (!t || t.length > 200) continue;
        if (re.test(t)) {
            speciesHints.push({
                tag: el.tagName,
                class: el.className,
                text: t.substring(0, 150),
            });
            if (speciesHints.length >= 30) break;
        }
    }

    return {
        url: request.url,
        appeared_selector: appearedSelector,
        selector_report: selectorReport,
        container_report: containerReport,
        species_hints: speciesHints,
        body_html_sample: bodyText.substring(0, 30000),
        body_total_len: bodyText.length,
    };
}
"""

def main() -> int:
    print(f"Scouting Petstablished widget at:\n  {WIDGET_URL}\n")
    print("Calling Apify (apify~web-scraper)... this takes 30–90 seconds.\n")

    payload = {
        "startUrls": [{"url": WIDGET_URL}],
        "pageFunction": PAGE_FUNCTION,
        "maxConcurrency": 1,
        "maxRequestsPerCrawl": 1,
        # Generous timeouts — the Petstablished SPA's first paint can be slow.
        "pageLoadTimeoutSecs": 120,
        "useChrome": True,
        "headless": True,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    t0 = time.time()
    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/apify~web-scraper/run-sync-get-dataset-items",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {APIFY_API_KEY}"},
            json=payload,
            timeout=300,
        )
    except Exception as e:
        print(f"✗ Apify request error: {e}")
        return 1
    elapsed = time.time() - t0
    print(f"  Apify replied in {elapsed:.1f}s with HTTP {r.status_code}\n")
    if r.status_code not in (200, 201):
        print(f"  Body: {r.text[:600]}")
        return 1

    items = r.json()
    if not items:
        print("✗ Apify returned no items.")
        return 1
    data = items[0]

    # Surface Apify-level errors inline (otherwise you'd have to read the
    # JSON dump to figure out why all the data fields are None).
    if data.get("#error"):
        print("✗ Apify reported an error item:")
        dbg = data.get("#debug") or {}
        for k, v in dbg.items():
            if k == "errorMessages" and isinstance(v, list):
                for i, m in enumerate(v):
                    print(f"    [{i}] {m.splitlines()[0]}")
            else:
                print(f"    {k}: {v}")
        return 1

    print("=" * 70)
    print(f"  url:               {data.get('url')}")
    print(f"  appeared_selector: {data.get('appeared_selector')!r}")
    print(f"  body_total_len:    {data.get('body_total_len')} chars")
    print("=" * 70)

    print("\nSelector report (sorted by count):")
    rep = sorted(data.get("selector_report", []),
                 key=lambda x: x["count"], reverse=True)
    for r in rep:
        flag = "✓" if r["count"] > 0 else " "
        print(f"  {flag} {r['count']:>4}  {r['selector']}")

    # For each selector that hit anything, print the sample
    print("\nSamples of first match (selectors that hit):")
    for r in rep:
        if r["count"] == 0:
            continue
        print(f"\n  --- {r['selector']} (count={r['count']}) ---")
        print(f"  {r['sample']}")

    cr = data.get("container_report") or {}
    print(f"\n=== .pets-container report ===")
    print(f"  selector matched:  {cr.get('selector')}")
    print(f"  found:             {cr.get('found')}")
    print(f"  direct children:   {cr.get('children_count')}")
    print(f"\n  Most common class names inside container (count\\tclass):")
    for line in cr.get("all_descendant_classes", [])[:25]:
        print(f"    {line}")
    print(f"\n  First 3 child elements (outer HTML, first 1500 chars each):")
    for i, h in enumerate(cr.get("first_3_children_html", []), 1):
        print(f"\n  --- child #{i} ---")
        print(f"  {h}")

    print("\nSpecies/age/breed text hints (first 15):")
    for h in (data.get("species_hints") or [])[:15]:
        cls = (h.get("class") or "")[:40]
        print(f"  <{h['tag']:5} class={cls!r:42}> {h['text'][:100]}")

    # Dump the rendered body to a file for offline inspection
    out_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(out_dir, exist_ok=True)
    body_path = os.path.join(out_dir, "avfap_widget_dom.html")
    with open(body_path, "w", encoding="utf-8") as f:
        f.write(data.get("body_html_sample") or "")
    print(f"\nFull body sample written to: {body_path}")

    # Save full Apify response as JSON too
    json_path = os.path.join(out_dir, "avfap_scout_response.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Full Apify response written to: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
