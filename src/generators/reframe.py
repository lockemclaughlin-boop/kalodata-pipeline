"""
Aspect-ratio reframer.

Nano Banana / Gemini 2.5 Flash Image inherits the input photo's aspect ratio,
so its output is typically square (~1:1). Veo with aspect_ratio="9:16" then
letterboxes the square frame to fit portrait, leaving hard black bars top
and bottom.

reframe_to_aspect() takes the staged image and pads it to the target
aspect ratio with a Gaussian-blurred version of the image itself as the
background fill — the same trick Instagram Stories uses. Result: full-bleed
9:16 (or any ratio) frame with the original product centered crisply and
the edges filled by a soft, color-matched blur.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFilter


def reframe_to_aspect(
    src: Path,
    dest: Path,
    target_w: int = 9,
    target_h: int = 16,
    blur_radius: int = 60,
) -> Path:
    """
    Pad `src` to a target_w:target_h canvas with a blurred-background fill,
    keep the original image centered and intact. Returns dest.
    """
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    img = Image.open(src).convert("RGB")
    w, h = img.size

    src_ratio = w / h
    tgt_ratio = target_w / target_h

    if abs(src_ratio - tgt_ratio) < 0.01:
        # Already at the target ratio. Skip work, just copy through.
        img.save(dest, "PNG")
        return dest

    # Canvas: keep the long dimension, extend the short one. For square →
    # 9:16, we keep width and make the canvas taller than the source.
    if src_ratio > tgt_ratio:
        # Source is wider than target (e.g. 4:3 → 9:16): keep height, pad width.
        canvas_h = h
        canvas_w = int(round(h * tgt_ratio))
    else:
        # Source is taller-or-equal to target ratio numerically; we still need
        # to grow height since target 9:16 is "taller" than 1:1. Compare in
        # "height per unit width" terms instead.
        canvas_w = w
        canvas_h = int(round(w / tgt_ratio))

    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(0, 0, 0))

    # Background fill: a scaled-up + heavily-blurred version of the image,
    # cover-style (fills the canvas, may crop). Then composite the original
    # crisp image centered on top.
    bg_scale = max(canvas_w / w, canvas_h / h) * 1.25
    bg_w = max(canvas_w, int(round(w * bg_scale)))
    bg_h = max(canvas_h, int(round(h * bg_scale)))
    bg = img.resize((bg_w, bg_h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=blur_radius)
    )
    bg_x = (canvas_w - bg_w) // 2
    bg_y = (canvas_h - bg_h) // 2
    canvas.paste(bg, (bg_x, bg_y))

    # Crisp foreground.
    fg_x = (canvas_w - w) // 2
    fg_y = (canvas_h - h) // 2
    canvas.paste(img, (fg_x, fg_y))

    canvas.save(dest, "PNG")
    return dest
