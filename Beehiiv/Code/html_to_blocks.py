#!/usr/bin/env python3
"""
Split a fully-rendered newsletter email HTML string into a list of Beehiiv
`blocks` for the create-post API — one `html` block per newsletter section.

Why: sending the whole body as `body_content` makes Beehiiv wrap the entire
issue in ONE htmlSnippet block, which is uneditable as a unit in the editor.
Sending an array of html blocks (one per section) keeps the rendered output
byte-identical per section while letting editors reorder, delete, or edit a
single section in isolation. (Phase 1 of the blocks migration — Phase 2
converts prose sections to native paragraph/heading/image blocks.)

Splitting strategy
------------------
1. Parse the HTML; if a <body> exists, work inside it.
2. Descend through "lone wrappers" (nodes with exactly one element child and
   no meaningful text of their own) to find the level where the actual
   content siblings live. The wrapper chain is REMEMBERED, not discarded:
   every emitted chunk is re-wrapped in the same chain so per-block styling
   (widths, background colors on container tables) is preserved.
3. Walk the siblings; a sibling whose subtree contains an <h1> or <h2>
   starts a new chunk. Everything before the first heading forms the
   "preamble" chunk (header image, intro).
4. Emit `{"type": "html", "html": <chunk>}` per chunk.

The module never raises on unexpected structure — worst case it returns a
single block containing the whole body, which is exactly what body_content
does today. Callers should check `len(blocks)` and log.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

# Headings that mark the start of a newsletter section in the template.
SECTION_HEADING_TAGS = ("h1", "h2")

# A lone wrapper with more text than this isn't a wrapper — it's content.
_WRAPPER_TEXT_BUDGET = 0


def _element_children(node: Tag) -> list[Tag]:
    return [c for c in node.children if isinstance(c, Tag)]


def _own_text(node: Tag) -> str:
    """Text directly inside `node` excluding its element children."""
    return "".join(
        s for s in node.strings
        if s.parent is node
    ).replace("\xa0", " ").strip()


def _descend_wrappers(root: Tag) -> tuple[Tag, list[Tag]]:
    """Walk down through nodes that have exactly one element child and no
    text of their own — classic email wrapper chains like
    <table><tbody><tr><td>. Returns (split_level_node, wrapper_chain).

    The split-level node's children are the siblings we group into
    sections. `wrapper_chain` (outer → inner) is every container BELOW the
    root down to and including the split-level node; each emitted chunk is
    re-wrapped in copies of this chain so container styling survives the
    split. The root itself (<body> / document) is never in the chain.
    """
    chain: list[Tag] = []
    node = root
    while True:
        kids = _element_children(node)
        if len(kids) != 1 or len(_own_text(node)) > _WRAPPER_TEXT_BUDGET:
            # `node` is the split level. It wraps the siblings, so chunks
            # must be re-wrapped in it too — unless it's the root.
            if node is not root:
                chain.append(node)
            return node, chain
        if node is not root:
            chain.append(node)
        node = kids[0]


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


def _contains_section_heading(node: Tag) -> bool:
    if node.name in SECTION_HEADING_TAGS:
        return True
    return node.find(SECTION_HEADING_TAGS) is not None


def _first_heading_text(nodes: list[Tag]) -> str:
    for n in nodes:
        h = n if n.name in SECTION_HEADING_TAGS else n.find(SECTION_HEADING_TAGS)
        if h is not None:
            t = (h.get_text() or "").replace("\xa0", " ").strip()
            if t:
                return t
    return "(no heading)"


def split_email_html(html: str) -> list[dict]:
    """Split rendered email HTML into per-section chunks.

    Returns a list of dicts: [{"label": str, "html": str}, ...].
    `label` is the section's first h1/h2 text ("(preamble)" for content
    before the first heading) — for logging only, not sent to Beehiiv.
    """
    soup = BeautifulSoup(html, "html.parser")
    root: Tag = soup.body if soup.body is not None else soup  # type: ignore[assignment]

    split_node, chain = _descend_wrappers(root)
    siblings = _element_children(split_node)
    if not siblings:
        return [{"label": "(whole body)", "html": html}]

    # Group siblings into chunks at section-heading boundaries.
    groups: list[list[Tag]] = [[]]
    for sib in siblings:
        if _contains_section_heading(sib) and groups[-1]:
            groups.append([])
        groups[-1].append(sib)
    groups = [g for g in groups if g]

    chunks: list[dict] = []
    for i, g in enumerate(groups):
        label = "(preamble)" if (i == 0 and not _contains_section_heading(g[0])) \
                else _first_heading_text(g)
        chunk_html = "".join(str(n) for n in g)
        chunks.append({"label": label, "html": _wrap(chunk_html, chain)})
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
            print("    ⚠ Only 1 chunk — no h1/h2 boundaries found at the split level.")
            print("      Post will behave like body_content (single uneditable block).")
    return [{"type": "html", "html": c["html"]} for c in chunks]


# ---------------------------------------------------------------------------
# Local smoke test: python html_to_blocks.py [path/to/body.html]
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        raw = open(sys.argv[1], encoding="utf-8").read()
    else:
        raw = """
        <div id="content-blocks">
          <table><tr><td><img src="hero.png"></td></tr></table>
          <p>Welcome intro paragraph.</p>
          <h2>Event of the Week</h2>
          <table><tr><td>event card</td></tr></table>
          <h2>Restaurant Radar</h2>
          <p>restaurant blurb</p>
          <table><tr><td><h2>The Local Lowdown</h2><p>news</p></td></tr></table>
          <hr>
          <p>footer-ish text</p>
        </div>
        """
    blocks = html_to_section_blocks(raw)
    print(f"\n{len(blocks)} block(s) total")
