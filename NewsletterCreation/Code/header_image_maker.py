#!/usr/bin/env python3
"""
Generate a newsletter header thumbnail by compositing the featured event's
photo and title onto the Canva-designed template at
`Featured Event/Template/featured_event_template.png` (1200x630, RGB).

The template ships WITHOUT an alpha channel, so we synthesize a mask at
runtime by detecting the baked-in sky/grass placeholder colors inside the
blob. Pixels matching those colors get replaced by the event photo; the
dark blob outline + everything else is preserved.

Usage:
    from header_image_maker import build_header_image
    png_bytes = build_header_image(title="Cinco de Mayo Festival",
                                   photo_url="https://...")
    open("/tmp/header.png", "wb").write(png_bytes)
"""
import io
import re
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps


REPO_ROOT     = Path(__file__).resolve().parent.parent.parent
TEMPLATE_PATH = REPO_ROOT / "Featured Event" / "Template" / "feature_event_image.png"

# ── Header composite template (used for build_header_image) ──
# Auto-measured from the magenta chroma extents in the current template
# (1200x630). Update if the template changes.
BLOB_BBOX  = (650, 78, 1122, 552)   # rectangle that fully contains the chroma blob
TITLE_BOX  = (40, 80, 600, 360)     # rectangle the title should fill (left side)
TITLE_FILL = (255, 255, 255)        # white text reads cleanly on red bg

# ── Body GIF template (used for build_event_body_gif) ──
# Photo blob is the rounded square on the LEFT, text placeholders fill
# the right column.
#
# Each placeholder is rendered in a UNIQUE chroma color in the template.
# At runtime we scan the template for each color, derive bbox + font
# size + cover color automatically — so future template edits in Canva
# need ZERO code changes.
BODY_TEMPLATE_PATH = REPO_ROOT / "Featured Event" / "Template" / "feature_event_body_template2.png"

# Chroma colors per placeholder (must match what your Canva template uses).
# All edges/anti-aliasing within ±30 per channel of the target counts.
BODY_PLACEHOLDER_COLORS = {
    "photo":    (255, 0, 255),    # magenta — photo blob
    "title":    (255, 255, 0),    # yellow
    "location": (0, 255, 255),    # cyan
    "address":  (0, 255, 0),      # green
    "date":     (255, 136, 0),    # orange
}
BODY_PLACEHOLDER_TOL = 30

# The dark purple/wine container shape's color. We detect its rightmost
# extent in the template and use that as the right boundary for all
# text — so text never runs past the visual container.
BODY_CONTAINER_COLOR = (72, 16, 48)
BODY_CONTAINER_TOL   = 25
BODY_FIELD_BOLD = {
    "title":    True,
    "location": True,
    "address":  True,
    "date":     True,
}
# Per-field text colors. `outline` adds a stroked outline of the given
# color around the text (0 = no outline).
BODY_FIELD_STYLE = {
    "title":    {"fill": (76, 18, 54),   "outline": (255, 255, 255), "outline_width": 3},
    "location": {"fill": (255, 255, 255), "outline": None,             "outline_width": 0},
    "address":  {"fill": (255, 255, 255), "outline": None,             "outline_width": 0},
    "date":     {"fill": (0, 0, 0),       "outline": (255, 255, 255), "outline_width": 3},
}
BODY_FIELD_MAX_LINES = {
    "title":    2,    # event name can wrap to 2 lines
    "location": 1,
    "address":  1,
    "date":     1,
}
# Font size per field is derived from the chroma bbox height at runtime.
# Canva placed the placeholder text at a specific pixel height (the bbox);
# we match that visual size by setting the PIL font size to bbox_height /
# CAP_HEIGHT_RATIO. CAP_HEIGHT_RATIO ≈ 0.72 means "the visible cap height
# of a glyph is ~72% of the font's em-square" — a typical value for
# humanist sans-serifs like Open Sans / DejaVu Sans / Canva Sans.
#
# If text still looks too small, drop CAP_HEIGHT_RATIO toward 0.4.
# If text looks too big, raise toward 0.8.
# Empirically tuned against the current Canva template export.
CAP_HEIGHT_RATIO = 0.47


def _find_chroma_bbox(im: Image.Image, target_rgb: tuple[int, int, int],
                      tol: int = BODY_PLACEHOLDER_TOL
                      ) -> tuple[int, int, int, int] | None:
    """Return (x1, y1, x2, y2) of all pixels within `tol` of `target_rgb`,
    or None if fewer than 30 pixels match."""
    tr, tg, tb = target_rgb
    R, G, B = im.split()

    def _within(channel: Image.Image, target: int) -> Image.Image:
        lo, hi = max(0, target - tol), min(255, target + tol)
        return channel.point(lambda p, lo=lo, hi=hi: 255 if lo <= p <= hi else 0)

    mask = ImageChops.multiply(
        ImageChops.multiply(_within(R, tr), _within(G, tg)),
        _within(B, tb),
    )
    bbox = mask.getbbox()
    if bbox is None:
        return None
    # Reject tiny noise (anti-alias bleed from a different color)
    pixel_count = sum(1 for p in mask.getdata() if p > 0)
    if pixel_count < 30:
        return None
    return bbox


def _sample_cover_color(im: Image.Image, bbox: tuple[int, int, int, int]
                        ) -> tuple[int, int, int]:
    """Sample a pixel just OUTSIDE the bbox to figure out what background
    the placeholder sits on. Tries above, left, right, then below."""
    x1, y1, x2, y2 = bbox
    candidates = [
        (max(0, x1 - 10), (y1 + y2) // 2),   # just left
        ((x1 + x2) // 2, max(0, y1 - 10)),   # just above
        (min(im.width - 1, x2 + 10), (y1 + y2) // 2),  # just right
        ((x1 + x2) // 2, min(im.height - 1, y2 + 10)), # just below
    ]
    for px, py in candidates:
        try:
            r, g, b = im.getpixel((px, py))[:3]
            return (r, g, b)
        except Exception:
            continue
    return (255, 255, 255)

# Acceptable detection colors for the placeholder we want to mask out.
# Sky:  pale blue (~ R<235, G>200, B>220, B>=R)
# Grass: muted green (~ G dominant)
# Chroma-key color used inside the blob in the template.
# Default: pure magenta (#FF00FF) — pick whichever solid you used in Canva.
# If you switch to green-screen green (#00FF00) etc., just change these.
CHROMA_KEY_RGB    = (255, 0, 255)
CHROMA_TOLERANCE  = 30   # per-channel slack to absorb anti-aliased edge pixels


def _build_blob_mask(template_rgb: Image.Image) -> Image.Image:
    """Return an L-mode mask: 255 where the chroma-key color lives inside
    the blob bounding box, 0 elsewhere.

    Pure-Pillow. Works against a solid-fill placeholder color
    (e.g., #FF00FF magenta) — far more reliable than the old multi-color
    sky/cloud/grass detection.
    """
    R, G, B = template_rgb.split()
    kr, kg, kb = CHROMA_KEY_RGB
    tol = CHROMA_TOLERANCE

    def _within(channel: Image.Image, target: int) -> Image.Image:
        lo, hi = max(0, target - tol), min(255, target + tol)
        return channel.point(lambda p, lo=lo, hi=hi: 255 if lo <= p <= hi else 0)

    mask = ImageChops.multiply(
        ImageChops.multiply(_within(R, kr), _within(G, kg)),
        _within(B, kb),
    )

    # Restrict to blob bbox — kills any stray pixels of the key color
    # elsewhere on the template (logo, decorations, etc.).
    bbox_mask = Image.new("L", template_rgb.size, 0)
    ImageDraw.Draw(bbox_mask).rectangle(BLOB_BBOX, fill=255)
    mask = ImageChops.multiply(mask, bbox_mask)

    # Small dilation + blur to swallow any 1-2px anti-aliased edge ring
    # between the chroma fill and the blob outline.
    mask = mask.filter(ImageFilter.MaxFilter(size=3))
    return mask.filter(ImageFilter.GaussianBlur(radius=1.0))


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Load a humanist sans-serif font in preferred order.

    Canva Sans is proprietary, but Open Sans + DejaVu Sans are the
    closest open-source humanist sans-serifs and a reasonable visual
    match. Open Sans is preferred if available."""
    candidates = []
    if bold:
        candidates += [
            # Open Sans (closest free analog to Canva Sans)
            "/usr/share/fonts/truetype/open-sans/OpenSans-Bold.ttf",
            "/usr/share/fonts/opentype/open-sans/OpenSans-Bold.otf",
            "/Library/Fonts/OpenSans-Bold.ttf",
            # Fall back to DejaVu / system bold faces
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/open-sans/OpenSans-Regular.ttf",
            "/usr/share/fonts/opentype/open-sans/OpenSans-Regular.otf",
            "/Library/Fonts/OpenSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _fetch_photo(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.content:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"    [header] photo fetch error: {e}")
    return None


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw,
               max_width: int) -> list[str]:
    """Greedy word-wrap into lines that each fit `max_width` pixels."""
    words = re.split(r"\s+", text.strip())
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_title(draw: ImageDraw.ImageDraw, title: str, box: tuple[int, int, int, int]
              ) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Pick the largest font size that lets `title` wrap inside `box`."""
    x1, y1, x2, y2 = box
    max_w, max_h = x2 - x1, y2 - y1
    for size in (84, 72, 64, 56, 48, 42, 36, 32, 28):
        font = _load_font(size, bold=True)
        lines = _wrap_text(title, font, draw, max_w)
        line_h = font.size + 8
        total_h = line_h * len(lines)
        widest = max((draw.textlength(l, font=font) for l in lines), default=0)
        if total_h <= max_h and widest <= max_w:
            return font, lines
    # Fallback to smallest tried
    font = _load_font(28, bold=True)
    return font, _wrap_text(title, font, draw, max_w)


def build_header_image(title: str, photo_url: str | None = None,
                       photo: Image.Image | None = None) -> bytes:
    """Composite the featured event title + photo onto the template.

    Returns PNG bytes. Caller can write to disk or upload to Beehiiv as the
    post's thumbnail.
    """
    if not TEMPLATE_PATH.exists():
        print(f"    ✗ Template not found: {TEMPLATE_PATH}")
        return b""

    template = Image.open(TEMPLATE_PATH).convert("RGB")
    base     = template.copy()

    # ---- 1. Blob photo ---------------------------------------------------
    # Photo goes into the right-side blob only. The left side of the
    # template (the red-tinted Canva backdrop) is left untouched — the
    # title (drawn next) overlays it with see-through letters + stroke
    # so the Canva design shows behind/between the letters.
    if photo is None and photo_url:
        photo = _fetch_photo(photo_url)
    if photo is not None:
        x1, y1, x2, y2 = BLOB_BBOX
        box_w, box_h = x2 - x1, y2 - y1
        fitted = ImageOps.fit(photo, (box_w, box_h), method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", base.size, (0, 0, 0))
        canvas.paste(fitted, (x1, y1))
        mask = _build_blob_mask(template)
        base = Image.composite(canvas, base, mask)

    # ---- 2. Title text — see-through overlay on the Canva backdrop ------
    # Title sits in its original TITLE_BOX position. White letters with
    # a thick black stroke for legibility against any backdrop. No
    # opaque background; the Canva backdrop shows through between each
    # letter.
    if title:
        draw = ImageDraw.Draw(base)
        x1, y1, x2, y2 = TITLE_BOX
        font, lines = _fit_title(draw, title, TITLE_BOX)
        line_h  = font.size + 8
        total_h = line_h * len(lines)
        cy = y1 + (y2 - y1 - total_h) // 2  # vertically centered in box
        STROKE_PX = max(2, font.size // 18)
        for i, line in enumerate(lines):
            tw = draw.textlength(line, font=font)
            tx = x1 + max(0, ((x2 - x1) - int(tw)) // 2)
            ty = cy + i * line_h
            draw.text(
                (tx, ty), line,
                font=font,
                fill=(255, 255, 255),
                stroke_width=STROKE_PX,
                stroke_fill=(0, 0, 0),
            )

    buf = io.BytesIO()
    base.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _build_body_chroma_mask(template_rgb: Image.Image,
                            bbox: tuple[int, int, int, int] | None = None
                            ) -> Image.Image:
    """Build a chroma-key mask for the magenta photo blob in the body
    template. If `bbox` is provided, constrain the mask to that rect."""
    R, G, B = template_rgb.split()
    kr, kg, kb = CHROMA_KEY_RGB
    tol = CHROMA_TOLERANCE

    def _within(channel: Image.Image, target: int) -> Image.Image:
        lo, hi = max(0, target - tol), min(255, target + tol)
        return channel.point(lambda p, lo=lo, hi=hi: 255 if lo <= p <= hi else 0)

    mask = ImageChops.multiply(
        ImageChops.multiply(_within(R, kr), _within(G, kg)),
        _within(B, kb),
    )
    if bbox is not None:
        bbox_mask = Image.new("L", template_rgb.size, 0)
        ImageDraw.Draw(bbox_mask).rectangle(bbox, fill=255)
        mask = ImageChops.multiply(mask, bbox_mask)
    mask = mask.filter(ImageFilter.MaxFilter(size=3))
    return mask.filter(ImageFilter.GaussianBlur(radius=1.0))


def _draw_body_field(base: Image.Image, text: str,
                     bbox: tuple[int, int, int, int],
                     cover_color: tuple[int, int, int],
                     font_size: int,
                     bold: bool = True,
                     max_lines: int = 1,
                     right_boundary: int | None = None,
                     fill: tuple[int, int, int] | None = None,
                     outline: tuple[int, int, int] | None = None,
                     outline_width: int = 0) -> None:
    """Render one text field at the auto-detected chroma bbox using a
    FIXED font size (matched to the Canva template's design intent).

    If `fill` is None, auto-contrast against the cover color (dark text
    on light bg, light text on dark bg).
    If `outline` is set, stroke the text with that color at
    `outline_width` pixels — produces a halo around the glyph.

    Wrapping: if max_lines > 1, text may wrap up to that many lines
    within `right_boundary`."""
    x1, y1, x2, y2 = bbox
    box_w, box_h = x2 - x1, y2 - y1
    draw = ImageDraw.Draw(base)

    # 1. ALWAYS cover the chroma placeholder with the surrounding bg
    #    color, even when text is empty — otherwise the green/cyan/
    #    orange chroma rectangle (which has the literal "{address}" /
    #    "{location}" / "{date}" placeholder text drawn on it in the
    #    Canva template) bleeds through to the rendered GIF.
    #    Slightly expand to absorb anti-aliased edges.
    pad = 4
    cover_rect = (max(0, x1 - pad), max(0, y1 - pad),
                  min(base.width, x2 + pad), min(base.height, y2 + pad))
    draw.rectangle(cover_rect, fill=cover_color)

    # If there's no text for this field, we're done — leave the
    # covered rectangle blank so the design degrades gracefully.
    if not text:
        return

    # 2. Use the pre-computed font size as-is. The caller already
    #    applied any necessary shrink-to-fit globally (so the design
    #    hierarchy across fields is preserved).
    size = font_size
    font = _load_font(size, bold=bold)

    # 3. Pick text color: explicit `fill` wins, otherwise auto-contrast.
    if fill is not None:
        text_fill = fill
    else:
        r, g, b = cover_color
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        text_fill = (40, 20, 20) if luma > 160 else (245, 240, 235)

    # 4. Wrap to up to max_lines lines using the right boundary.
    allowed_right = max(x1 + 100, right_boundary if right_boundary else base.width - 30)
    allowed_w = allowed_right - x1
    if max_lines > 1:
        lines = _wrap_text(text, font, draw, allowed_w)[:max_lines]
    else:
        lines = [text]

    # 5. Draw left-aligned, top-anchored to the chroma bbox top.
    #    If `outline` is set, also stroke the text with that color.
    line_h = size + 4
    text_kwargs = {"font": font, "fill": text_fill}
    if outline is not None and outline_width > 0:
        text_kwargs["stroke_fill"]  = outline
        text_kwargs["stroke_width"] = outline_width
    for i, line in enumerate(lines):
        draw.text((x1, y1 + i * line_h), line, **text_kwargs)


def build_event_body_gif(*, title: str = "", location_name: str = "",
                         address: str = "", date: str = "",
                         photo_urls: list[str] | None = None,
                         frame_duration_ms: int = 2000) -> bytes:
    """Build an animated GIF that composites multiple event photos
    into the body template's chroma-blob area, with title / location /
    address / date text overlays rendered the same on every frame.

    Returns animated GIF bytes (empty on failure). Use up to ~4 photos
    for a reasonable file size.
    """
    if not BODY_TEMPLATE_PATH.exists():
        print(f"    ✗ Body template not found: {BODY_TEMPLATE_PATH}")
        return b""
    if not photo_urls:
        print(f"    ✗ No photo URLs provided for body GIF")
        return b""

    template = Image.open(BODY_TEMPLATE_PATH).convert("RGB")

    # Detect the visual container's right edge (rightmost dark-purple
    # pixel anywhere in the template). Text never extends past this.
    container_bbox = _find_chroma_bbox(template, BODY_CONTAINER_COLOR,
                                       tol=BODY_CONTAINER_TOL)
    if container_bbox:
        container_right = container_bbox[2]  # rightmost x of purple
    else:
        # Fallback to canvas edge if no purple detected
        container_right = template.width - 30
    print(f"    [body GIF] right boundary for text: x={container_right}")

    # Auto-detect every chroma placeholder's bbox + surrounding bg color.
    field_bboxes: dict[str, tuple[int, int, int, int]] = {}
    field_covers: dict[str, tuple[int, int, int]] = {}
    for name, target_color in BODY_PLACEHOLDER_COLORS.items():
        bbox = _find_chroma_bbox(template, target_color)
        if bbox is None:
            print(f"    ⚠ chroma '{name}' not found in template")
            continue
        field_bboxes[name] = bbox
        field_covers[name] = _sample_cover_color(template, bbox)

    photo_bbox = field_bboxes.get("photo")
    if photo_bbox is None:
        print(f"    ✗ photo chroma (magenta) not found in template — cannot composite")
        return b""

    mask = _build_body_chroma_mask(template, bbox=photo_bbox)
    x1, y1, x2, y2 = photo_bbox
    blob_w, blob_h = x2 - x1, y2 - y1

    # Build an outline-mask: ring around the photo blob (where photo
    # meets template). We draw this in dark purple on every frame so
    # the photo has a defined edge matching the title text color.
    OUTLINE_WIDTH = 6        # pixels of stroke around the photo
    OUTLINE_COLOR = (76, 18, 54)   # #4C1236 brand purple
    dilated = mask.filter(ImageFilter.MaxFilter(size=2 * OUTLINE_WIDTH + 1))
    eroded  = mask.filter(ImageFilter.MinFilter(size=3))
    # Subtract: dilated MINUS eroded → ring of OUTLINE_WIDTH px just
    # outside the original chroma boundary
    outline_mask = ImageChops.subtract(dilated, eroded)

    field_values = {"title": title, "location": location_name,
                    "address": address, "date": date}

    frames: list[Image.Image] = []
    for url in photo_urls[:6]:
        photo = _fetch_photo(url)
        if photo is None:
            continue
        fitted = ImageOps.fit(photo, (blob_w, blob_h),
                              method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", template.size, (0, 0, 0))
        canvas.paste(fitted, (x1, y1))
        frame = Image.composite(canvas, template, mask)

        # Draw the dark-purple outline along the photo edge.
        outline_layer = Image.new("RGB", template.size, OUTLINE_COLOR)
        frame = Image.composite(outline_layer, frame, outline_mask)

        # Text overlays — same on every frame, each one painted over its
        # auto-detected chroma rectangle.
        # Determine a UNIFORM shrink ratio across all fields so the
        # design hierarchy (title > location/date > address) is preserved
        # even when long text would otherwise force a single field to
        # shrink way more than the others.
        #
        # Fields with max_lines > 1 (title) can WRAP to avoid shrinking.
        # They only contribute to the shrink ratio if even wrapping to
        # max_lines doesn't fit within the allowed width.
        right_padding = 8
        shrink_ratio = 1.0
        tmp_draw = ImageDraw.Draw(frame)
        for fname in ("title", "location", "address", "date"):
            bb = field_bboxes.get(fname)
            if bb is None:
                continue
            text = field_values.get(fname, "")
            if not text:
                continue
            bbox_h = bb[3] - bb[1]
            design_size = max(12, int(round(bbox_h / CAP_HEIGHT_RATIO)))
            allowed_w = container_right - bb[0] - right_padding
            max_lines = BODY_FIELD_MAX_LINES.get(fname, 1)
            font = _load_font(design_size, bold=BODY_FIELD_BOLD.get(fname, True))

            if max_lines > 1:
                # Field can wrap — only force shrink if even wrapping fits too few lines.
                lines = _wrap_text(text, font, tmp_draw, allowed_w)
                if len(lines) <= max_lines:
                    continue   # wraps cleanly at design size, no shrink needed
                # Even wrapping isn't enough — find the size where it does fit
                # in max_lines, contribute that ratio to the global shrink.
                trial = design_size
                while trial > 12:
                    trial = max(12, int(trial * 0.95))
                    trial_font = _load_font(trial, bold=BODY_FIELD_BOLD.get(fname, True))
                    if len(_wrap_text(text, trial_font, tmp_draw, allowed_w)) <= max_lines:
                        break
                shrink_ratio = min(shrink_ratio, trial / design_size)
            else:
                # Single-line field — must fit on one line.
                text_w = tmp_draw.textlength(text, font=font)
                if text_w > allowed_w:
                    shrink_ratio = min(shrink_ratio, allowed_w / text_w)

        for fname in ("title", "location", "address", "date"):
            bb = field_bboxes.get(fname)
            if bb is None:
                continue
            bbox_h = bb[3] - bb[1]
            design_size = max(12, int(round(bbox_h / CAP_HEIGHT_RATIO)))
            # Apply the global shrink ratio (≤ 1.0). Floor at 12px.
            final_size = max(12, int(round(design_size * shrink_ratio)))
            style = BODY_FIELD_STYLE.get(fname, {})
            _draw_body_field(
                frame, field_values[fname], bb, field_covers[fname],
                font_size=final_size,
                bold=BODY_FIELD_BOLD.get(fname, True),
                max_lines=BODY_FIELD_MAX_LINES.get(fname, 1),
                right_boundary=container_right - right_padding,
                fill=style.get("fill"),
                outline=style.get("outline"),
                outline_width=style.get("outline_width", 0),
            )

        # Quantize to a palette for smaller GIF
        frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=256))

    if not frames:
        print(f"    ✗ All photo fetches failed for body GIF")
        return b""

    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI TEST
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    # Usage:
    #   python header_image_maker.py "Title" "https://photo.url"            # header composite (PNG)
    #   python header_image_maker.py body "Title" "Venue" "Address" "Date"  # body GIF
    if len(sys.argv) >= 2 and sys.argv[1] == "body":
        title    = sys.argv[2] if len(sys.argv) > 2 else "Marietta Greek Festival"
        venue    = sys.argv[3] if len(sys.argv) > 3 else "Holy Transfiguration Church"
        address  = sys.argv[4] if len(sys.argv) > 4 else "3431 Trickum Rd NE, Marietta"
        date_s   = sys.argv[5] if len(sys.argv) > 5 else "Friday, May 16, 2026"
        photos = [
            "https://images.unsplash.com/photo-1530103862676-de8c9debad1d?w=1280",
            "https://images.unsplash.com/photo-1495121553079-4c61bcce1894?w=1280",
            "https://images.unsplash.com/photo-1517254797898-04edd251bfb3?w=1280",
            "https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=1280",
        ]
        data = build_event_body_gif(
            title=title, location_name=venue, address=address, date=date_s,
            photo_urls=photos,
        )
        out = "/tmp/event_body_test.gif"
        if data:
            Path(out).write_bytes(data)
            print(f"Wrote {out} ({len(data):,} bytes)")
        else:
            print("Body GIF returned empty bytes")
        sys.exit(0)

    title = sys.argv[1] if len(sys.argv) > 1 else "Cinco de Mayo Festival on the Square"
    url   = sys.argv[2] if len(sys.argv) > 2 else \
        "https://images.unsplash.com/photo-1530103862676-de8c9debad1d?w=1280"
    out = "/tmp/header_test.png"
    data = build_header_image(title=title, photo_url=url)
    if data:
        Path(out).write_bytes(data)
        print(f"Wrote {out} ({len(data):,} bytes)")
