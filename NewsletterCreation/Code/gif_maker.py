#!/usr/bin/env python3
"""
Utility: Create animated GIFs from multiple image URLs.
Used by newsletter sections to combine photos into a single cycling GIF.

Usage:
    from gif_maker import create_gif_from_urls
    gif_bytes = create_gif_from_urls(
        urls=["https://example.com/photo1.jpg", "https://example.com/photo2.jpg"],
        width=600, height=400, duration_ms=2000
    )
    with open("output.gif", "wb") as f:
        f.write(gif_bytes)
"""
import io
import requests
from PIL import Image, ImageDraw

# Brand styling — match the red border used in the Real Estate templates
BRAND_RED   = (224, 30, 34)
CORNER_RAD  = 22
BORDER_PX   = 8


def _apply_rounded_red_border(img: Image.Image) -> Image.Image:
    """Return an RGB image with rounded corners + red rounded border, matching
    the visual treatment used on Real Estate listing images.

    GIF format doesn't support transparency on rounded outer corners, so we
    paint the corners white (matches the email background) and stroke the
    border on top.
    """
    rgba = img.convert("RGBA")
    # Build a rounded mask covering the frame
    mask = Image.new("L", rgba.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, rgba.size[0], rgba.size[1]),
        radius=CORNER_RAD,
        fill=255,
    )
    # White canvas underneath shows through the rounded corners
    canvas = Image.new("RGB", rgba.size, (255, 255, 255))
    canvas.paste(rgba.convert("RGB"), (0, 0), mask)
    # Stroke the red border just inside the frame edges
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, canvas.size[0] - 1, canvas.size[1] - 1),
        radius=CORNER_RAD,
        outline=BRAND_RED,
        width=BORDER_PX,
    )
    return canvas


def create_gif_from_urls(
    urls: list[str],
    width: int = 544,   # 20% smaller than the prior 680
    height: int = 408,  # 20% smaller than the prior 510
    duration_ms: int = 2000,
    labels: list[str] | None = None,
    crop_top: bool = False,
) -> bytes | None:
    """
    Download images from URLs, crop/resize to uniform dimensions, and create an animated GIF.

    Args:
        urls: List of image URLs to include as frames
        width: Output width in pixels
        height: Output height in pixels
        duration_ms: Time each frame is shown in milliseconds
        labels: Optional text labels to overlay on each frame (e.g. tier names)

    Returns:
        GIF file contents as bytes, or None if no valid images
    """
    frames = []

    for i, url in enumerate(urls):
        if not url:
            continue
        try:
            res = requests.get(url, timeout=15)
            if res.status_code != 200:
                print(f"  GIF: skipping {url[:60]}... (HTTP {res.status_code})")
                continue
            img = Image.open(io.BytesIO(res.content))

            # Convert to RGB (GIFs don't support RGBA well)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Smart crop to target aspect ratio, then resize
            img = _crop_to_aspect(img, width, height, keep_top=crop_top)
            img = img.resize((width, height), Image.LANCZOS)

            # Add label overlay if provided
            if labels and i < len(labels) and labels[i]:
                img = _add_label(img, labels[i])

            # Apply brand red rounded border (matches Real Estate listing visual)
            img = _apply_rounded_red_border(img)

            frames.append(img)

        except Exception as e:
            print(f"  GIF: error processing {url[:60]}... ({e})")
            continue

    if not frames:
        return None

    # Create animated GIF (universally supported by email clients).
    # WebP animates great in browsers but Beehiiv flattens it to a static JPG
    # for email delivery because Outlook/Apple Mail/older Gmail don't render
    # animated WebP. GIF is the lowest-common-denominator that animates everywhere.
    # We convert frames to "P" mode with adaptive palette for size + quality balance.
    palette_frames = []
    for f in frames:
        # ADAPTIVE palette per-frame keeps colors faithful while keeping file size sane
        palette_frames.append(f.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256))

    output = io.BytesIO()
    palette_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=palette_frames[1:],
        duration=duration_ms,
        loop=0,
        optimize=False,  # optimize=True is slow with large frames; output is still tight
        disposal=2,
    )
    output.seek(0)
    print(f"  GIF: created {len(frames)} frames, {width}x{height}, {duration_ms}ms per frame, "
          f"{output.getbuffer().nbytes:,} bytes")
    return output.read()


def _crop_to_aspect(img: Image.Image, target_w: int, target_h: int, keep_top: bool = False) -> Image.Image:
    """Crop image to target aspect ratio. Center crop by default, near-top crop if keep_top=True."""
    target_ratio = target_w / target_h
    img_ratio = img.width / img.height

    if img_ratio > target_ratio:
        # Image is wider — crop sides (always center)
        new_width = int(img.height * target_ratio)
        left = (img.width - new_width) // 2
        img = img.crop((left, 0, left + new_width, img.height))
    elif img_ratio < target_ratio:
        new_height = int(img.width / target_ratio)
        if keep_top:
            # Offset 20% from top — keeps head in frame without cutting it off
            max_top = img.height - new_height
            top = int(max_top * 0.20)
            img = img.crop((0, top, img.width, top + new_height))
        else:
            top = (img.height - new_height) // 2
            img = img.crop((0, top, img.width, top + new_height))

    return img


def _add_label(img: Image.Image, text: str) -> Image.Image:
    """Add a text label bar at the bottom of the image."""
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img)
    bar_height = 40
    y = img.height - bar_height

    # Semi-transparent black bar
    draw.rectangle([(0, y), (img.width, img.height)], fill=(0, 0, 0, 200))

    # White text
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except (OSError, IOError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = (img.width - text_w) // 2
    draw.text((text_x, y + 8), text, fill="white", font=font)

    return img


# ---------------------------------------------------------------------------
# CLI: quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python gif_maker.py output.gif url1 url2 [url3 ...]")
        sys.exit(1)

    output_path = sys.argv[1]
    urls = sys.argv[2:]
    gif_bytes = create_gif_from_urls(urls)
    if gif_bytes:
        with open(output_path, "wb") as f:
            f.write(gif_bytes)
        print(f"Saved to {output_path} ({len(gif_bytes)} bytes)")
    else:
        print("No valid images to create GIF")
