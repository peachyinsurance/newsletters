#!/usr/bin/env python3
"""
Festive Restaurant Swap.

Idea: If a holiday is upcoming (e.g., Cinco de Mayo) and this week's restaurant
winners DON'T include the corresponding cuisine (Mexican), pull the best-rated
historical restaurant matching that cuisine from Notion (status='approved - old')
and swap it in for this week's lowest-scored Tier 2 Winner.

The swapped-out Tier 2 Winner is ARCHIVED (removed from Notion) — not held for
future use. The historical pick gets re-promoted to Tier 2 Winner so it appears
in the current newsletter.

Run weekly after Restaurants pipeline + before assembling/sending.

ENV:
  NOTION_API_KEY
  NOTION_RESTAURANTS_DB_ID
"""
import os
import sys
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'NewsletterCreation', 'Code'))
from notion_helper import (
    query_database, update_page, archive_page, NOTION_RESTAURANTS_DB_ID,
)

# Reuse the canonical festive calendar from the main pipeline so they stay in sync.
sys.path.append(os.path.dirname(__file__))
from Restaurant_Section import get_festive_boosts  # noqa: E402

NEWSLETTERS = ["East_Cobb_Connect", "Perimeter_Post"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _row_field(props: dict, key: str, default: str = "") -> str:
    """Pull a string field out of a Notion page properties dict regardless of
    whether it's title / rich_text / select / number."""
    p = props.get(key, {}) or {}
    if not p:
        return default
    if "title" in p and p["title"]:
        return p["title"][0].get("text", {}).get("content", "") or default
    if "rich_text" in p and p["rich_text"]:
        return p["rich_text"][0].get("text", {}).get("content", "") or default
    if "select" in p and p["select"]:
        return p["select"].get("name", "") or default
    if "number" in p and p["number"] is not None:
        return p["number"]
    return default


def _row_score(props: dict) -> int:
    """Total Score is a number property in restaurant rows."""
    return props.get("Total Score", {}).get("number") or 0


def _row_cuisine(props: dict) -> str:
    return ((props.get("Cuisine", {}) or {}).get("select") or {}).get("name", "") or ""


def cuisine_matches_boost(cuisine: str, boost_cuisines: list[str]) -> bool:
    """Fuzzy match — cuisine 'Mexican Restaurant' matches boost cuisine 'mexican'."""
    cl = (cuisine or "").lower()
    if not cl:
        return False
    return any(c in cl or cl in c for c in boost_cuisines)


def fetch_newsletter_rows(newsletter_name: str) -> list[dict]:
    return query_database(NOTION_RESTAURANTS_DB_ID, filters={
        "property": "Newsletter",
        "select":   {"equals": newsletter_name}
    })


def status_of(page: dict) -> str:
    sel = (page["properties"].get("Status", {}) or {}).get("select") or {}
    return sel.get("name", "")


# ---------------------------------------------------------------------------
# Per-newsletter swap
# ---------------------------------------------------------------------------
def swap_for_newsletter(newsletter_name: str, boosts: list[dict]) -> None:
    if not boosts:
        return

    print(f"\n=== {newsletter_name} ===")
    rows = fetch_newsletter_rows(newsletter_name)
    if not rows:
        print(f"  No rows in Notion for {newsletter_name}; skipping.")
        return

    current_winners = [r for r in rows if status_of(r) in ("Tier 1 Winner", "Tier 2 Winner")]
    historical      = [r for r in rows if status_of(r) == "approved - old"]
    print(f"  Current winners: {len(current_winners)}  |  approved-old pool: {len(historical)}")

    if not current_winners:
        print(f"  No current winners — nothing to swap. Skipping.")
        return

    current_cuisines = [_row_cuisine(r["properties"]) for r in current_winners]
    print(f"  Current cuisines: {current_cuisines}")

    # For each active boost, check if its cuisine is represented
    for boost in boosts:
        bc = boost["cuisines"]
        # Already represented?
        if any(cuisine_matches_boost(c, bc) for c in current_cuisines):
            print(f"  ✓ {boost['name']}: already represented in current winners ({bc}).")
            continue

        # Find historical matches with this cuisine
        candidates = [
            r for r in historical
            if cuisine_matches_boost(_row_cuisine(r["properties"]), bc)
        ]
        if not candidates:
            print(f"  ⚠ {boost['name']}: no historical {bc} restaurant found; skipping swap.")
            continue

        # Highest-rated historical match
        candidates.sort(key=lambda r: _row_score(r["properties"]), reverse=True)
        best_old = candidates[0]
        best_old_name = _row_field(best_old["properties"], "Name")
        best_old_score = _row_score(best_old["properties"])

        # Lowest-scored current Tier 2 Winner (don't touch Tier 1)
        tier2_winners = [r for r in current_winners if status_of(r) == "Tier 2 Winner"]
        if not tier2_winners:
            print(f"  ⚠ {boost['name']}: no Tier 2 Winner to displace (Tier 1 only). Skipping.")
            continue
        tier2_winners.sort(key=lambda r: _row_score(r["properties"]))
        worst_t2 = tier2_winners[0]
        worst_t2_name  = _row_field(worst_t2["properties"], "Name")
        worst_t2_score = _row_score(worst_t2["properties"])

        print(f"  🔁 {boost['name']} swap:")
        print(f"     - Archiving Tier 2 Winner: {worst_t2_name} ({worst_t2_score})")
        print(f"     + Promoting approved-old: {best_old_name} ({best_old_score}) → Tier 2 Winner")

        # Execute: archive the loser, flip the historical pick to Tier 2 Winner
        archive_page(worst_t2["id"])
        update_page(best_old["id"], {"Status": {"select": {"name": "Tier 2 Winner"}}})

        # Update local view so subsequent boost iterations see the change
        # (relevant if multiple festive boosts are active simultaneously).
        worst_t2_idx = next(i for i, r in enumerate(current_winners) if r["id"] == worst_t2["id"])
        # Replace the archived row with the promoted row in the live list
        current_winners[worst_t2_idx] = best_old
        current_cuisines[worst_t2_idx] = _row_cuisine(best_old["properties"])
        # Remove from historical pool so we don't re-pick it for another boost
        historical = [r for r in historical if r["id"] != best_old["id"]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not NOTION_RESTAURANTS_DB_ID:
        sys.exit("NOTION_RESTAURANTS_DB_ID not set")

    boosts = get_festive_boosts()
    if not boosts:
        print("No active festive boosts today — nothing to swap.")
        return
    print(f"Active festive boosts: {[b['name'] for b in boosts]}")

    for nl in NEWSLETTERS:
        try:
            swap_for_newsletter(nl, boosts)
        except Exception as e:
            print(f"  ✗ {nl} failed: {e}")


if __name__ == "__main__":
    main()
    print("\n✓ Festive swap complete")
