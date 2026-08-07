#!/usr/bin/env python3
"""
Split a fully-rendered newsletter email HTML string into a list of Beehiiv
`blocks` for the create-post API — one `html` block per newsletter section.

Why: sending the whole body as `body_content` makes Beehiiv wrap the entire
issue in ONE htmlSnippet block, which is uneditable as a unit in the editor.
Sending an array of html blocks (one per section) keeps the rendered output
the same while letting editors reorder, delete, or edit a single section in
isolation. (Phase 1 of the blocks migration — Phase 2 converts prose
sections to native paragraph/heading/image blocks.)

How the real template is shaped (verified against the rendered ECC body):
the whole email is nested tables; the actual content is ONE table with
~78 <tr> rows, one row per editor widget. Sections are delimited by the
section-title banner images (restaurant-radar.png, local-lowdown-2.png,
meme-corner.png, …), NOT by heading tags — h1/h2/h3 usage is inconsistent
(h1 = weekend day headers, h2 = individual lowdown stories, h3 = various).

Splitting strategy
------------------
1. Parse; descend from <body> through wrapper chains: single-child nodes,
   or nodes where one child dominates (>DOMINANCE of the length) — classic
   email nesting like table>tr>td>table>… Siblings skipped on the way down
   (preheader div, unsubscribe footer row) are folded into the first/last
   chunk. Stop at the first node with several children and no dominant one:
   that's the widget-row container.
2. Group rows into chunks, starting a new chunk at every row that contains
   a section banner image (img src matching SECTION_BANNER_HINTS).
3. If no banner rows were found (different template design), fall back to
   splitting at rows containing h1/h2, then to a single chunk.
4. Re-wrap every chunk in copies of the wrapper chain so container styling
   (widths, backgrounds, cellpadding) survives, and emit
   {"type": "html", "html": …} per chunk.

The module never raises on unexpected structure — worst case it returns a
single block containing the whole body, which is exactly what body_content
does today. Callers should check `len(blocks)` and log.
"""
from __future__ import annotations

from bs4 import BeautifulSoup, Tag

# A row containing an <img> whose src matches one of these (case-insensitive
# substring) STARTS a new section chunk. These are the section-title banner
# assets shared by the newsletter templates; harmless if some never match.
SECTION_BANNER_HINTS = (
    "table-of-contents",
    "section_titles", "section-titles",
    "sponsor-corner",
    "restaurant-radar",
    "business-brief",
    "real-estate-corner",
    "local-lowdown",
    "furry-friends",
    "local-events",
    "family-fun",
    "adult-events",
    "free-activity",
    "insurance-tip",
    "in-search-of",
    "meme-corner",
    "poll-",
)

# Fallback boundary tags when no banner images are found.
SECTION_HEADING_TAGS = ("h1", "h2")
_HEADING_SENTINEL = "\x00heading"

# Descend into a child when it holds more than this share of its parent's
# serialized length (email wrapper nesting), but never descend past a node
# that already has a healthy number of children (that IS the row container).
DOMINANCE = 0.60
MIN_ROWS_TO_STOP = 6


def _element_children(node: Tag) -> list[Tag]:
    return [c for c in node.children if isinstance(c, Tag)]


def _find_row_container(root: Tag) -> tuple[Tag, list[Tag], list[Tag], list[Tag]]:
    """Descend to the widget-row container.

    Returns (container, wrapper_chain, prefix_nodes, suffix_nodes):
    - wrapper_chain (outer→inner): containers below the root down to and
      including the row container; chunks get re-wrapped in copies of these.
    - prefix/suffix_nodes: siblings skipped while descending (in document
      order) — folded into the first/last chunk by the caller.
    """
    chain: list[Tag] = []
    prefix: list[Tag] = []
    suffix: list[Tag] = []
    node = root
    while True:
        kids = _element_children(node)
        if node is not root:
            chain.append(node)
        if not kids:
            return node, chain, prefix, suffix
        if len(kids) == 1:
            node = kids[0]
            continue
        if len(kids) >= MIN_ROWS_TO_STOP:
            return node, chain, prefix, suffix
        sizes = [len(str(k)) for k in kids]
        total = sum(sizes) or 1
        big = max(range(len(kids)), key=lambda i: sizes[i])
        if sizes[big] / total < DOMINANCE:
            return node, chain, prefix, suffix
        prefix.extend(kids[:big])
        # Suffix accumulates inner-first; the caller appends them after the
        # last chunk in the order encountered (outer siblings last).
        suffix[:0] = kids[big + 1:]
        node = kids[big]


def _wrap(chunk_html: str, chain: list[Tag]) -> str:
    """Re-wrap a chunk in copies of the wrapper chain (outer → inner)."""
    out = chunk_html
    for wrapper in reversed(chain):
        attrs = "".join(
            f' {k}="{" ".join(v) if isinstance(v, list) else v}"'
            for k, v in wrapper.attrs.items()
        )
        out = f"<{wrapper.name}{attrs}>{out}</{wrapper.name}>"
    return out


def _banner_hint(row: Tag) -> str | None:
    """Return the matched banner hint if this row holds a section banner."""
    imgs = ([row] if row.name == "img" else []) + row.find_all("img")
    for img in imgs:
        src = (img.get("src") or "").lower()
        for hint in SECTION_BANNER_HINTS:
            if hint in src:
                return hint
    return None


def _contains_heading(row: Tag) -> bool:
    if row.name in SECTION_HEADING_TAGS:
        return True
    return row.find(SECTION_HEADING_TAGS) is not None


def _label(rows: list[Tag], hint: str | None) -> str:
    if hint and hint != _HEADING_SENTINEL:
        return hint
    for r in rows:
        h = r if r.name in ("h1", "h2", "h3") else r.find(["h1", "h2", "h3"])
        if h is not None:
            t = (h.get_text() or "").replace("\xa0", " ").strip()
            if t:
                return t[:70]
    return "(no label)"


def split_email_html(html: str) -> list[dict]:
    """Split rendered email HTML into per-section chunks.

    Returns a list of dicts: [{"label": str, "html": str}, ...].
    `label` is the banner hint or first heading text — logging only.
    """
    soup = BeautifulSoup(html, "html.parser")
    root: Tag = soup.body if soup.body is not None else soup  # type: ignore[assignment]

    container, chain, prefix, suffix = _find_row_container(root)
    rows = _element_children(container)
    if not rows:
        return [{"label": "(whole body)", "html": html}]

    # Pick the boundary test: banners first, headings as fallback.
    # (_HEADING_SENTINEL marks a boundary without naming it — _label falls
    # through to the chunk's first real heading text.)
    boundaries: list[str | None] = [_banner_hint(r) for r in rows]
    if not any(boundaries):
        boundaries = [_HEADING_SENTINEL if _contains_heading(r) else None for r in rows]

    groups: list[tuple[str | None, list[Tag]]] = []
    for row, hint in zip(rows, boundaries):
        if hint or not groups:
            groups.append((hint, []))
        groups[-1][1].append(row)

    chunks: list[dict] = []
    for i, (hint, grp) in enumerate(groups):
        parts = [str(n) for n in grp]
        if i == 0:
            parts = [str(n) for n in prefix] + parts
        if i == len(groups) - 1:
            parts += [str(n) for n in suffix]
        label = "(preamble)" if (i == 0 and not hint) else _label(grp, hint)
        chunks.append({"label": label, "html": _wrap("".join(parts), chain)})
    return chunks


def html_to_section_blocks(html: str, *, verbose: bool = True) -> list[dict]:
    """Convert rendered email HTML into a Beehiiv `blocks` array —
    one {"type": "html", "html": ...} block per newsletter section."""
    chunks = split_email_html(html)
    if verbose:
        print(f"  Split body into {len(chunks)} html block(s):")
        for i, c in enumerate(chunks):
            print(f"    [{i:2d}] {len(c['html']):>7,} chars  {c['label'][:70]}")
        if len(chunks) == 1:
            print("    ⚠ Only 1 chunk — no banner or heading boundaries found.")
            print("      Post will behave like body_content (single uneditable block).")
    return [{"type": "html", "html": c["html"]} for c in chunks]


# ---------------------------------------------------------------------------
# Local smoke test: python html_to_blocks.py [path/to/body.html]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], encoding="utf-8").read()
        blocks = html_to_section_blocks(raw)
        print(f"\n{len(blocks)} block(s) total")
    else:
        print("usage: python html_to_blocks.py path/to/body.html")
