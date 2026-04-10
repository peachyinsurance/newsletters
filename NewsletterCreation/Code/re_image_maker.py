#!/usr/bin/env python3
"""
Generate Real Estate Corner listing images matching the Canva template design.
Creates 1200x630 images with photo, tier badge, details, price, and address.

Layout alternates: Starter (photo left), Sweet Spot (photo right), Showcase (photo left).
"""
import io
import requests
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# FONTS — use system fonts with fallbacks
# ---------------------------------------------------------------------------
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try to load a good font, fall back gracefully."""
    font_paths = [
        # Linux (GitHub Actions)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# LAYOUT CONFIG
# ---------------------------------------------------------------------------
WIDTH = 1200
HEIGHT = 630
BORDER = 8
BORDER_COLOR = (223, 35, 40)  # Red (#DF2328)
BG_COLOR = (255, 255, 255)
PHOTO_WIDTH = 650
PHOTO_HEIGHT = 420
PHOTO_Y = 63
INFO_PADDING = 40

BADGE_COLORS = {
    "Starter":    (40, 40, 40),      # Dark gray/black
    "Sweet Spot": (40, 40, 40),
    "Showcase":   (40, 40, 40),
}


# ---------------------------------------------------------------------------
# IMAGE GENERATION
# ---------------------------------------------------------------------------
def _build_frame(
    photo: Image.Image,
    tier: str,
    price: int,
    beds: int,
    baths: int,
    sqft: int,
    address: str,
    listing_url: str = "",
    lot_info: str = "",
    photo_right: bool = False,
) -> Image.Image:
    """Build a single template frame with the given photo."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Draw red border
    draw.rectangle([(0, 0), (WIDTH - 1, HEIGHT - 1)], outline=BORDER_COLOR, width=BORDER)

    # Load fonts
    font_tier = _load_font(28, bold=True)
    font_details = _load_font(26)
    font_price = _load_font(48, bold=True)
    font_address = _load_font(20)
    font_link = _load_font(20)

    # Calculate positions
    if photo_right:
        photo_x = WIDTH - PHOTO_WIDTH - 63
        info_x = 63 + INFO_PADDING
    else:
        photo_x = 63
        info_x = 63 + PHOTO_WIDTH + INFO_PADDING

    # Place photo
    cropped = _crop_to_fit(photo, PHOTO_WIDTH, PHOTO_HEIGHT)
    img.paste(cropped, (photo_x, PHOTO_Y))

    # Draw tier badge
    badge_color = BADGE_COLORS.get(tier, (40, 40, 40))
    badge_bbox = draw.textbbox((0, 0), tier, font=font_tier)
    badge_w = badge_bbox[2] - badge_bbox[0] + 30
    badge_h = badge_bbox[3] - badge_bbox[1] + 16

    if photo_right:
        badge_x = info_x
    else:
        badge_x = info_x + (WIDTH - 63 - PHOTO_WIDTH - 63 - INFO_PADDING - badge_w) // 2 + INFO_PADDING

    badge_y = PHOTO_Y
    draw.rounded_rectangle(
        [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
        radius=6, fill=badge_color
    )
    draw.text((badge_x + 15, badge_y + 6), tier, fill="white", font=font_tier)

    # Draw details
    details_y = PHOTO_Y + 100
    bullet = "•  "
    details_lines = []
    if beds:
        details_lines.append(f"{beds} Beds")
    if baths:
        details_lines.append(f"{baths} Baths")
    if sqft:
        details_lines.append(f"{sqft:,} sq ft")
    if lot_info:
        details_lines.append(lot_info)

    for i, line in enumerate(details_lines):
        y = details_y + (i * 42)
        draw.text((info_x, y), f"{bullet}{line}", fill=(30, 30, 30), font=font_details)

    # Draw price
    price_str = f"${price:,}"
    price_y = details_y + len(details_lines) * 42 + 20
    draw.text((info_x, price_y), price_str, fill=BORDER_COLOR, font=font_price)

    # Draw address (bottom)
    addr_y = HEIGHT - 63
    draw.text((63 + 10, addr_y), address, fill=(30, 30, 30), font=font_address)

    # Draw "Click here to view" (bottom right)
    if listing_url:
        link_text = "Click here to view"
        link_bbox = draw.textbbox((0, 0), link_text, font=font_link)
        link_w = link_bbox[2] - link_bbox[0]
        link_x = WIDTH - 63 - link_w - 10
        draw.text((link_x, addr_y), "Click ", fill=(30, 30, 30), font=font_link)
        click_w = draw.textbbox((0, 0), "Click ", font=font_link)[2]
        here_x = link_x + click_w
        draw.text((here_x, addr_y), "here", fill=BORDER_COLOR, font=font_link)
        here_w = draw.textbbox((0, 0), "here", font=font_link)[2]
        draw.line([(here_x, addr_y + 22), (here_x + here_w, addr_y + 22)], fill=BORDER_COLOR, width=2)
        to_view_x = here_x + here_w
        draw.text((to_view_x, addr_y), " to view", fill=(30, 30, 30), font=font_link)

    return img


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
    photo_right: bool = False,
    duration_ms: int = 2000,
) -> bytes | None:
    """
    Generate an animated GIF with the template overlay on each frame.
    If only 1 photo, returns a static PNG.

    Args:
        photo_urls: List of photo URLs (1-3)
        tier, price, beds, baths, sqft, address, listing_url, lot_info: Listing details
        photo_right: If True, photo goes on right side (for Sweet Spot)
        duration_ms: Time per frame in ms (for GIF)

    Returns:
        Image bytes (GIF if multiple photos, PNG if single) or None
    """
    # Download all photos
    photos = []
    for url in photo_urls[:3]:
        try:
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                photo = Image.open(io.BytesIO(res.content))
                if photo.mode != "RGB":
                    photo = photo.convert("RGB")
                photos.append(photo)
        except Exception:
            pass

    if not photos:
        return None

    # Build frames
    frames = []
    for photo in photos:
        frame = _build_frame(
            photo=photo, tier=tier, price=price, beds=beds, baths=baths,
            sqft=sqft, address=address, listing_url=listing_url,
            lot_info=lot_info, photo_right=photo_right,
        )
        frames.append(frame)

    output = io.BytesIO()
    if len(frames) == 1:
        # Static PNG
        frames[0].save(output, format="PNG", quality=95)
    else:
        # Animated GIF
        frames[0].save(
            output, format="GIF", save_all=True,
            append_images=frames[1:],
            duration=duration_ms, loop=0, optimize=True,
        )

    output.seek(0)
    return output.read()


def _crop_to_fit(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop and resize image to exact dimensions."""
    target_ratio = target_w / target_h
    img_ratio = img.width / img.height

    if img_ratio > target_ratio:
        new_width = int(img.height * target_ratio)
        left = (img.width - new_width) // 2
        img = img.crop((left, 0, left + new_width, img.height))
    elif img_ratio < target_ratio:
        new_height = int(img.width / target_ratio)
        top = (img.height - new_height) // 2
        img = img.crop((0, top, img.width, top + new_height))

    return img.resize((target_w, target_h), Image.LANCZOS)


# ---------------------------------------------------------------------------
# BATCH: Generate all 3 tier images for a newsletter
# ---------------------------------------------------------------------------
def generate_re_images(listings: list[dict], newsletter_name: str, output_dir: str) -> list[dict]:
    """
    Generate listing images for all tiers.
    Returns list of dicts with 'tier', 'image_path', 'image_filename'.
    """
    from pathlib import Path
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    results = []
    for i, listing in enumerate(listings):
        tier = listing.get("tier", "")
        photo_right = (tier == "Sweet Spot")

        # Use multiple photos for animated GIF, fall back to single photo
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
            photo_right=photo_right,
        )

        if img_bytes:
            slug = tier.lower().replace(" ", "_")
            ext = "gif" if len(photo_urls) > 1 else "png"
            filename = f"re_{newsletter_name}_{slug}_{__import__('datetime').datetime.today().strftime('%Y%m%d')}.{ext}"
            filepath = out / filename
            filepath.write_bytes(img_bytes)
            results.append({
                "tier": tier,
                "image_path": str(filepath),
                "image_filename": filename,
            })
            print(f"    ✓ {tier} image: {len(img_bytes):,} bytes")
        else:
            print(f"    ✗ {tier} image failed")

    return results


# ---------------------------------------------------------------------------
# CLI TEST
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Quick test with sample data
    img = create_listing_image(
        photo_urls=["https://ap.rdcpix.com/e181e8fa53208c933bdc9ee40cc82df0l-m74703620od.jpg"],
        tier="Starter",
        price=365000,
        beds=3,
        baths=2,
        sqft=1623,
        address="2170 Beaver Shop Rd, Marietta, GA 30066",
        listing_url="https://www.realtor.com/...",
        lot_info="1.1 acre lot",
    )
    if img:
        with open("test_listing.png", "wb") as f:
            f.write(img)
        print(f"Saved test_listing.png ({len(img):,} bytes)")
