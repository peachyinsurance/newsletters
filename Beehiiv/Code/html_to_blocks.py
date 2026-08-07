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


# ===========================================================================
# NATIVE MODE (Phase 2): convert widget rows to native Beehiiv blocks so the
# COPY itself is click-and-type editable in the editor — not just the
# section chunks. Rows that can't be converted faithfully (weekend-planner
# card grid, meme grid, dividers, share/button rows) stay `html` blocks.
# ===========================================================================

# The content column is 630px wide in the rendered template; image widget
# widths are expressed as a 1-100 percentage of that column.
_CONTENT_COL_PX = 630

_STYLE_TAGS = {
    "strong": "bold", "b": "bold",
    "em": "italic", "i": "italic",
    "u": "underline",
    "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough",
}


def _inline_runs(node: Tag, styling: tuple[str, ...] = (),
                 link: dict | None = None) -> list[dict]:
    """Walk inline content and emit formattedText runs:
    {"text", "styling"?, "link"?}. <br> becomes a newline in the run text —
    VERIFY in the editor that Beehiiv renders it as a line break."""
    runs: list[dict] = []
    for child in node.children:
        if isinstance(child, Tag):
            if child.name == "br":
                runs.append({"text": "\n", "styling": list(styling), "link": link})
                # Malformed `<br>text</br>` parses as text nested INSIDE the
                # br — recurse so that copy isn't silently dropped.
                runs.extend(_inline_runs(child, styling, link))
            elif child.name in _STYLE_TAGS:
                extra = _STYLE_TAGS[child.name]
                runs.extend(_inline_runs(child, styling + ((extra,) if extra not in styling else ()), link))
            elif child.name == "a":
                href = (child.get("href") or "").strip()
                new_link = {"href": href, "target": "_blank"} if href else link
                runs.extend(_inline_runs(child, styling, new_link))
            else:  # span/font/etc. — recurse transparently
                runs.extend(_inline_runs(child, styling, link))
        else:
            text = str(child).replace("\xa0", " ")
            if text:
                runs.append({"text": text, "styling": list(styling), "link": link})
    return runs


def _merge_runs(runs: list[dict]) -> list[dict]:
    """Collapse whitespace-only fragmentation and drop empty runs; merge
    adjacent runs with identical styling+link. Emit schema-clean dicts."""
    merged: list[dict] = []
    for r in runs:
        if not r["text"]:
            continue
        prev = merged[-1] if merged else None
        if prev is not None and prev["styling"] == r["styling"] and prev["link"] == r["link"]:
            prev["text"] += r["text"]
        else:
            merged.append(dict(r))
    out = []
    for r in merged:
        if not r["text"].strip() and not out:
            continue  # leading pure-whitespace run
        d: dict = {"text": r["text"]}
        if r["styling"]:
            d["styling"] = r["styling"]
        if r["link"]:
            d["link"] = r["link"]
        out.append(d)
    # trailing pure-whitespace run
    while out and not out[-1]["text"].strip():
        out.pop()
    return out


def _alignment_of(node: Tag) -> str | None:
    """Extract left/center/right from align attrs or inline text-align,
    checking the node itself then ancestors up to and including its <td>."""
    n: Tag | None = node
    while isinstance(n, Tag):
        align = (n.get("align") or "").lower()
        style = (n.get("style") or "").lower().replace(" ", "")
        for cand in ("center", "right", "left"):
            if f"text-align:{cand}" in style or align == cand:
                return cand
        if n.name in ("td", "tr"):
            break
        n = n.parent  # type: ignore[assignment]
    return None


def _paragraph_block(p: Tag) -> dict | None:
    runs = _merge_runs(_inline_runs(p))
    if not runs:
        return None
    block: dict = {"type": "paragraph", "formattedText": runs}
    align = _alignment_of(p)
    if align and align != "left":
        block["textAlignment"] = align
    return block


def _heading_block(h: Tag) -> dict | None:
    text = (h.get_text() or "").replace("\xa0", " ").strip()
    if not text:
        return None
    block: dict = {"type": "heading", "level": h.name[1], "text": text}
    align = _alignment_of(h)
    if align and align != "left":
        block["textAlignment"] = align
    return block


def _image_block(img: Tag) -> dict | None:
    src = (img.get("src") or "").strip()
    if not src:
        return None
    block: dict = {"type": "image", "imageUrl": src}
    alt = (img.get("alt") or "").strip()
    if alt:
        block["alt_text"] = alt
    a = img.find_parent("a")
    if a is not None and (a.get("href") or "").strip():
        block["url"] = a["href"].strip()
    # width: px → % of the 630px content column
    px = None
    style = (img.get("style") or "").replace(" ", "").lower()
    import re as _re
    m = _re.search(r"max-width:(\d+)px", style)
    if m:
        px = int(m.group(1))
    elif str(img.get("width") or "").isdigit():
        px = int(img["width"])
    if px and px < _CONTENT_COL_PX:
        block["width"] = max(1, min(100, round(px * 100 / _CONTENT_COL_PX)))
    align = _alignment_of(img)
    if align:
        block["imageAlignment"] = align
    return block


def _list_block(lst: Tag) -> dict | None:
    items = []
    for li in lst.find_all("li", recursive=False) or lst.find_all("li"):
        runs = _merge_runs(_inline_runs(li))
        if runs:
            items.append({"formattedText": runs})
    if not items:
        return None
    return {"type": "list", "items": items,
            "listType": "ordered" if lst.name == "ol" else "unordered"}


_SEMANTIC_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "img")
_MAX_PARAS_PER_ROW = 12  # beyond this it's an injected card grid, not prose


def _is_card_box(row: Tag) -> bool:
    """Styled card containers (background color / solid border / rounded
    corners on an inner table cell) can't be reproduced by bare native
    blocks — converting would lose the card look."""
    for el in row.find_all(["td", "table", "div"]):
        style = (el.get("style") or "").lower().replace(" ", "")
        if el.get("bgcolor") or "background-color:" in style \
                or "border-style:solid" in style or "border-radius:" in style:
            # Ignore the plain image-frame borders (border-width:0)
            if "border-width:0" in style and not el.get("bgcolor") \
                    and "background-color:" not in style:
                continue
            return True
    return False


def _convert_row(row: Tag) -> list[dict] | None:
    """Convert one widget row to native block(s) in document order, or
    None → keep the row as an html fallback block."""
    if row.find("hr") is not None:
        return None  # divider: keep exact rendering
    if _is_card_box(row):
        return None  # pink card boxes etc.: keep exact rendering

    # Top-level semantic elements (skip ones nested inside another, e.g.
    # <p>/<img> inside <li>, <img> inside <p>).
    elems = [e for e in row.find_all(_SEMANTIC_TAGS)
             if not any(isinstance(a, Tag) and a is not row and a.name in _SEMANTIC_TAGS
                        for a in e.parents)]
    if not elems:
        return None

    paras = [e for e in elems if e.name == "p"]
    imgs = [e for e in elems if e.name == "img"]
    headings = [e for e in elems if e.name.startswith("h")]
    if len(paras) > _MAX_PARAS_PER_ROW:
        return None  # injected card grid (weekend planner)
    if len(imgs) > 1 and not (paras or headings):
        return None  # image grid (memes): stacking would break the layout
    if any(h.find("a") is not None for h in headings):
        return None  # native heading blocks can't carry links

    blocks: list[dict] = []
    for e in elems:
        if e.name == "p":
            b = _paragraph_block(e)
        elif e.name in ("ul", "ol"):
            b = _list_block(e)
        elif e.name == "img":
            b = _image_block(e)
        else:
            b = _heading_block(e)
        if b is None:
            # An element we can't express natively (e.g. img with no src):
            # bail on the whole row so nothing is dropped or reordered.
            if (e.get_text() or "").strip() or e.name == "img":
                return None
            continue  # empty p/heading: skippable
        blocks.append(b)
    return blocks or None


def html_to_native_blocks(html: str, *, verbose: bool = True) -> list[dict]:
    """Convert rendered email HTML into a Beehiiv `blocks` array using
    NATIVE block types (paragraph/heading/image/list) wherever a widget row
    converts faithfully; consecutive unconvertible rows merge into `html`
    fallback blocks (wrapped in the container chain, like section mode)."""
    soup = BeautifulSoup(html, "html.parser")
    root: Tag = soup.body if soup.body is not None else soup  # type: ignore[assignment]

    container, chain, prefix, suffix = _find_row_container(root)
    rows = _element_children(container)
    if not rows:
        return html_to_section_blocks(html, verbose=verbose)

    out: list[dict] = []
    pending_html: list[str] = list(str(n) for n in prefix)
    stats = {"native": 0, "html_rows": 0}

    def flush():
        if pending_html:
            out.append({"type": "html", "html": _wrap("".join(pending_html), chain)})
            pending_html.clear()

    for row in rows:
        converted = _convert_row(row)
        if converted:
            flush()
            out.extend(converted)
            stats["native"] += len(converted)
        else:
            pending_html.append(str(row))
            stats["html_rows"] += 1
    pending_html.extend(str(n) for n in suffix)
    flush()

    if verbose:
        kinds: dict[str, int] = {}
        for b in out:
            kinds[b["type"]] = kinds.get(b["type"], 0) + 1
        print(f"  Native conversion: {len(out)} blocks total — "
              + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
        print(f"    ({stats['native']} native blocks from converted rows; "
              f"{stats['html_rows']} rows kept as html fallback)")
    return out


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
