#!/usr/bin/env python3
"""
Generate Real Estate Corner listing images by compositing listing data onto
pre-designed PNG templates (exported from Canva).

Templates live in: Real Estate Corner/templates/{starter,sweetspot,showcase}.png
Each template is 1200x630. Python overlays the listing photo in the red-bordered
photo box, writes "4 Beds / 3 Baths / 1,500 sq ft / 0.25 acre lot" (value-first,
covering the baked-in labels), writes the price, and writes the address below
the photo box.

If multiple photos are provided, the result is an animated GIF that cycles
through photos in the photo box while keeping the template background static.
"""
import io
import os
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


# ---------------------------------------------------------------------------
# TEMPLATES DIRECTORY
# ---------------------------------------------------------------------------
# re_image_maker.py lives at NewsletterCreation/Code/, templates at
# Real Estate Corner/templates/
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = REPO_ROOT / "Real Estate Corner" / "templates"


# ---------------------------------------------------------------------------
# FONT LOADER
# ---------------------------------------------------------------------------
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a bold/regular TrueType font with sensible fallbacks."""
    font_paths = [
        # Linux (GitHub Actions)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for p in font_paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# LAYOUT CONFIG — pixel coordinates eyeballed from the 1200x630 templates.
# Tweak these if something lands in the wrong spot.
# ---------------------------------------------------------------------------
# Each layout defines:
#   template:       filename in templates dir
#   photo_box:      (x1, y1, x2, y2) — inner rectangle the photo fills
#   bullet_cover:   (x1, y1, x2, y2) — rectangle to white-out over baked labels
#   bullet_x:       left edge x for bullet text
#   bullet_y:       list of 4 y-values, one per bullet line
#   price_cover:    (x1, y1, x2, y2) — rectangle to white-out over the "$"
#   price_xy:       (x, y) anchor for new price text (draw includes "$")
#   address_xy:     (x, y) anchor — center-bottom below the photo box
LAYOUTS = {
    "Starter": {
        "template":     "starter.png",
        "photo_box":    (60, 42, 728, 490),
        "bullet_cover": (775, 160, 1160, 360),
        "bullet_x":     790,
        "bullet_y":     [175, 225, 275, 325],
        "price_cover":  (780, 380, 1160, 485),
        "price_xy":     (790, 410),   # align with bullets
        "address_xy":   (394, 525),
    },
    "Sweet Spot": {
        "template":     "sweetspot.png",
        "photo_box":    (472, 42, 1140, 490),
        "bullet_cover": (55, 160, 470, 360),
        "bullet_x":     90,
        "bullet_y":     [175, 225, 275, 325],
        "price_cover":  (55, 380, 475, 485),
        "price_xy":     (90, 410),    # align with bullets
        "address_xy":   (806, 525),
    },
    "Showcase": {
        "template":     "showcase.png",
        "photo_box":    (60, 42, 728, 490),
        "bullet_cover": (775, 160, 1160, 360),
        "bullet_x":     790,
        "bullet_y":     [175, 225, 275, 325],
        "price_cover":  (780, 380, 1160, 485),
        "price_xy":     (790, 410),   # align with bullets
        "address_xy":   (394, 525),
    },
}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def _fetch_image(url: str) -> Image.Image | None:
    """Download an image URL into a Pillow Image. Returns None on failure."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        return img
    except Exception as e:
        print(f"    Image fetch error: {e}")
        return None


def _fit_into_box(photo: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """Scale-crop a photo to fill (x1, y1, x2, y2) exactly. Preserves aspect ratio."""
    x1, y1, x2, y2 = box
    target_w, target_h = x2 - x1, y2 - y1
    # Pillow's fit() does center-crop to target size
    return ImageOps.fit(photo, (target_w, target_h), method=Image.Resampling.LANCZOS)


BRAND_RED   = (224, 30, 34)
CORNER_RAD  = 22
BORDER_PX   = 8


def _apply_rounded_corners(photo: Image.Image, radius: int = CORNER_RAD) -> Image.Image:
    """Return an RGBA version of `photo` with rounded corners (transparent outside the radius)."""
    mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, photo.size[0], photo.size[1]),
        radius=radius,
        fill=255,
    )
    rgba = photo.convert("RGBA")
    rgba.putalpha(mask)
    return rgba


def _draw_photo_border(base: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Draw a red rounded border around the photo box (in-place)."""
    ImageDraw.Draw(base).rounded_rectangle(
        box,
        radius=CORNER_RAD,
        outline=BRAND_RED,
        width=BORDER_PX,
    )


def _format_lot(lot_info: str, sqft: int) -> str | None:
    """Return a string for the 'acre lot' bullet, or None if we have nothing useful."""
    if lot_info:
        return lot_info
    return None  # skip bullet if we have no lot data


def _draw_listing_overlay(base: Image.Image, cfg: dict, price: int, beds: int,
                          baths: int, sqft: int, address: str, lot_info: str) -> None:
    """Mutate `base` in place: white-out baked labels and draw listing data."""
    draw = ImageDraw.Draw(base)

    # --- 1. White-out the bullet area and redraw "• 4 Beds" lines ---
    draw.rectangle(cfg["bullet_cover"], fill=(255, 255, 255))
    bullet_font = _load_font(34, bold=True)
    lines = []
    if beds:
        lines.append(f"•  {beds} Beds")
    if baths:
        lines.append(f"•  {baths} Baths")
    if sqft:
        lines.append(f"•  {sqft:,} sq ft")
    lot_str = _format_lot(lot_info, sqft)
    if lot_str:
        lines.append(f"•  {lot_str}")
    bx = cfg["bullet_x"]
    for line, by in zip(lines, cfg["bullet_y"]):
        draw.text((bx, by), line, fill=(20, 20, 20), font=bullet_font)

    # --- 2. White-out the $ area and redraw the full price ---
    draw.rectangle(cfg["price_cover"], fill=(255, 255, 255))
    price_font = _load_font(56, bold=True)
    price_str = f"${price:,.0f}" if price else "$--"
    draw.text(cfg["price_xy"], price_str, fill=(224, 30, 34), font=price_font)  # red

    # --- 3. Address below the photo box ---
    if address:
        addr_font = _load_font(22, bold=True)
        ax, ay = cfg["address_xy"]
        # Center the text horizontally on ax
        bbox = draw.textbbox((0, 0), address, font=addr_font)
        text_w = bbox[2] - bbox[0]
        draw.text((ax - text_w // 2, ay), address, fill=(20, 20, 20), font=addr_font)


# ---------------------------------------------------------------------------
# RENDER FUNCTIONS
# ---------------------------------------------------------------------------
def _render_single(template: Image.Image, photo: Image.Image, cfg: dict,
                   price: int, beds: int, baths: int, sqft: int,
                   address: str, lot_info: str) -> bytes:
    """Render a static PNG with the photo and data composited onto the template."""
    base = template.copy().convert("RGBA")
    fitted = _fit_into_box(photo, cfg["photo_box"])
    rounded = _apply_rounded_corners(fitted)
    base.paste(rounded, (cfg["photo_box"][0], cfg["photo_box"][1]), rounded)
    base = base.convert("RGB")
    _draw_photo_border(base, cfg["photo_box"])
    _draw_listing_overlay(base, cfg, price, beds, baths, sqft, address, lot_info)
    buf = io.BytesIO()
    base.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_animated(template: Image.Image, photos: list[Image.Image], cfg: dict,
                     price: int, beds: int, baths: int, sqft: int,
                     address: str, lot_info: str,
                     frame_ms: int = 2000) -> bytes:
    """Render an animated GIF that cycles the photo inside the static template."""
    frames = []
    for photo in photos[:3]:  # cap at 3 frames
        base = template.copy().convert("RGBA")
        fitted = _fit_into_box(photo, cfg["photo_box"])
        rounded = _apply_rounded_corners(fitted)
        base.paste(rounded, (cfg["photo_box"][0], cfg["photo_box"][1]), rounded)
        base = base.convert("RGB")
        _draw_photo_border(base, cfg["photo_box"])
        _draw_listing_overlay(base, cfg, price, beds, baths, sqft, address, lot_info)
        frames.append(base)
    if not frames:
        return b""
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=frame_ms,
        loop=0,
        optimize=True,
    )
    return buf.getvalue()


def create_listing_image(
    photo_urls: list[str],
    tier: str,
    price: int,
    beds: int,
    baths: int,
    sqft: int,
    address: str,
    listing_url: str = "",
    lot_info: str = "",
    photo_right: bool = False,  # kept for backwards compat — now determined by tier
) -> bytes:
    """Return PNG (single photo) or GIF (multiple photos) bytes for the listing."""
    cfg = LAYOUTS.get(tier)
    if not cfg:
        print(f"    ✗ Unknown tier '{tier}' — no template layout defined")
        return b""

    template_path = TEMPLATES_DIR / cfg["template"]
    if not template_path.exists():
        print(f"    ✗ Template not found: {template_path}")
        return b""

    try:
        template = Image.open(template_path)
    except Exception as e:
        print(f"    ✗ Failed to open template: {e}")
        return b""

    # Fetch photos
    photos = []
    for url in photo_urls or []:
        img = _fetch_image(url)
        if img:
            photos.append(img)

    if not photos:
        # Still produce an image with no photo — just template + text overlay
        base = template.copy().convert("RGB")
        _draw_listing_overlay(base, cfg, price, beds, baths, sqft, address, lot_info)
        buf = io.BytesIO()
        base.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    if len(photos) == 1:
        return _render_single(template, photos[0], cfg, price, beds, baths,
                              sqft, address, lot_info)
    return _render_animated(template, photos, cfg, price, beds, baths,
                            sqft, address, lot_info)


# ---------------------------------------------------------------------------
# BATCH INTERFACE (called from Real_Estate_Corner.py)
# ---------------------------------------------------------------------------
def generate_re_images(listings: list[dict], newsletter_name: str, output_dir: str) -> list[dict]:
    """Generate an image file per tier. Returns list of dicts with tier, image_path, image_filename."""
    import datetime
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    results = []
    for listing in listings:
        tier = listing.get("tier", "")
        photo_urls = listing.get("photos", [])
        if not photo_urls and listing.get("photo_url"):
            photo_urls = [listing["photo_url"]]

        img_bytes = create_listing_image(
            photo_urls=photo_urls,
            tier=tier,
            price=listing.get("price", 0),
            beds=listing.get("beds", 0),
            baths=listing.get("baths", 0),
            sqft=listing.get("sqft", 0),
            address=listing.get("address", ""),
            listing_url=listing.get("listing_url", ""),
            lot_info=listing.get("lot_info", ""),
        )

        if not img_bytes:
            print(f"    ✗ {tier} image failed")
            continue

        slug = tier.lower().replace(" ", "_")
        ext = "gif" if len(photo_urls) > 1 else "png"
        filename = f"re_{newsletter_name}_{slug}_template_{datetime.datetime.today().strftime('%Y%m%d')}.{ext}"
        filepath = out / filename
        filepath.write_bytes(img_bytes)
        results.append({
            "tier": tier,
            "image_path": str(filepath),
            "image_filename": filename,
        })
        print(f"    ✓ {tier} image: {len(img_bytes):,} bytes")

    return results


# ---------------------------------------------------------------------------
# CLI TEST
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Test with all three tiers + a sample photo URL
    SAMPLE_PHOTO = "https://ap.rdcpix.com/c7e1bc58e0e9e5b2f1f7b6f3e7a3d0e7l-m0od-w960_h720.jpg"
    for tier in ("Starter", "Sweet Spot", "Showcase"):
        img = create_listing_image(
            photo_urls=[SAMPLE_PHOTO],
            tier=tier,
            price=350000,
            beds=4,
            baths=3,
            sqft=1500,
            address="123 Main St Marietta GA 30062",
            lot_info="0.25 acre",
        )
        if img:
            out = f"/tmp/test_{tier.replace(' ', '_').lower()}.png"
            open(out, "wb").write(img)
            print(f"Wrote {out}")
