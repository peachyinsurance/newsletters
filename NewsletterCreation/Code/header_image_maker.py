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
TEMPLATE_PATH = REPO_ROOT / "Featured Event" / "Template" / "featured_event_template.png"

# Eyeballed from the 1200x630 template — see header_image_probe output.
BLOB_BBOX  = (650, 78, 1121, 505)   # rough rectangle that contains the photo blob
TITLE_BOX  = (40, 80, 600, 360)     # rectangle the title should fill (left side)
TITLE_FILL = (255, 255, 255)        # white text reads cleanly on red bg

# Acceptable detection colors for the placeholder we want to mask out.
# Sky:  pale blue (~ R<235, G>200, B>220, B>=R)
# Grass: muted green (~ G dominant)
def _build_blob_mask(template_rgb: Image.Image) -> Image.Image:
    """Return an L-mode mask: 255 where the template's placeholder photo
    (sky+grass blob) lives, 0 elsewhere. Pure-Pillow (no numpy) so the
    GitHub Actions runner doesn't need extra wheels.

    Detection:
      sky   ≈ pale blue (B > R, G > 200, R < 235)
      grass ≈ muted green (G > R and G > B and R < 220)
    """
    R, G, B = template_rgb.split()

    # Helper: build a binary L-mask where `op(p)` is True
    def _binmask(channel: Image.Image, lo: int, hi: int) -> Image.Image:
        # point() returns 0/255 per pixel based on threshold
        return channel.point(lambda p, lo=lo, hi=hi: 255 if lo <= p <= hi else 0)

    # Sky candidates
    sky_R = _binmask(R, 150, 234)
    sky_G = _binmask(G, 201, 255)
    sky_B = _binmask(B, 221, 255)
    sky   = ImageChops.multiply(ImageChops.multiply(sky_R, sky_G), sky_B)

    # Grass: G > R and G > B (use ImageChops.subtract; result>0 ⇒ True)
    g_minus_r = ImageChops.subtract(G, R)            # >0 where G>R
    g_minus_b = ImageChops.subtract(G, B)            # >0 where G>B
    g_gt_r    = g_minus_r.point(lambda p: 255 if p > 0 else 0)
    g_gt_b    = g_minus_b.point(lambda p: 255 if p > 0 else 0)
    g_min     = _binmask(G, 171, 255)
    r_low     = _binmask(R, 0, 219)
    grass     = ImageChops.multiply(ImageChops.multiply(g_gt_r, g_gt_b),
                                    ImageChops.multiply(g_min, r_low))

    mask = ImageChops.lighter(sky, grass)            # union (sky OR grass)

    # Restrict to the blob bounding box — kills false positives elsewhere
    # (e.g. the bottom-right "EAST COBB" badge contains light pixels too).
    bbox_mask = Image.new("L", template_rgb.size, 0)
    ImageDraw.Draw(bbox_mask).rectangle(BLOB_BBOX, fill=255)
    mask = ImageChops.multiply(mask, bbox_mask)

    # Smooth a hair so the edge blends with the dark blob outline.
    return mask.filter(ImageFilter.GaussianBlur(radius=1.2))


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
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
    if photo is None and photo_url:
        photo = _fetch_photo(photo_url)
    if photo is not None:
        x1, y1, x2, y2 = BLOB_BBOX
        box_w, box_h = x2 - x1, y2 - y1
        # Scale-and-center-crop with minimal cropping: cover the bbox
        # while preserving aspect ratio (Pillow's fit() does center crop).
        fitted = ImageOps.fit(photo, (box_w, box_h), method=Image.Resampling.LANCZOS)

        # Build a full-canvas RGB image that has the photo at the bbox and
        # transparent everywhere else; we'll composite using the blob mask.
        canvas = Image.new("RGB", base.size, (0, 0, 0))
        canvas.paste(fitted, (x1, y1))

        mask = _build_blob_mask(template)
        base = Image.composite(canvas, base, mask)

    # ---- 2. Title text ---------------------------------------------------
    if title:
        # White out the baked "{title}" placeholder text only — keep the
        # red background by sampling its average color in that area.
        # Simpler: don't white-out; the placeholder is white text on red,
        # so painting our title in white over the same area covers it.
        draw = ImageDraw.Draw(base)
        font, lines = _fit_title(draw, title, TITLE_BOX)
        x1, y1, x2, y2 = TITLE_BOX
        line_h = font.size + 8
        total_h = line_h * len(lines)
        # Vertically center within the title box
        cy = y1 + (y2 - y1 - total_h) // 2
        # First, cover the existing placeholder by drawing a slightly shadowed
        # rectangle in the brand red. We sample the template at a known-red
        # coordinate to match the gradient.
        sampled = template.getpixel((20, 20))   # top-left red wash
        cover = Image.new("RGB", (x2 - x1, y2 - y1), sampled)
        base.paste(cover, (x1, y1))
        draw = ImageDraw.Draw(base)
        # Re-fit now that the cover is laid (text region is the same size).
        for i, line in enumerate(lines):
            tw = draw.textlength(line, font=font)
            tx = x1 + max(0, ((x2 - x1) - int(tw)) // 2)
            ty = cy + i * line_h
            # Soft shadow for legibility
            draw.text((tx + 2, ty + 2), line, fill=(80, 10, 10), font=font)
            draw.text((tx, ty),         line, fill=TITLE_FILL,    font=font)

    buf = io.BytesIO()
    base.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI TEST
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    title = sys.argv[1] if len(sys.argv) > 1 else "Cinco de Mayo Festival on the Square"
    url   = sys.argv[2] if len(sys.argv) > 2 else \
        "https://images.unsplash.com/photo-1530103862676-de8c9debad1d?w=1280"
    out = "/tmp/header_test.png"
    data = build_header_image(title=title, photo_url=url)
    if data:
        Path(out).write_bytes(data)
        print(f"Wrote {out} ({len(data):,} bytes)")
