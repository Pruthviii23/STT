"""Shared PIL icon drawing for Murmur (tray icon + .ico for shortcuts).

make_icon() draws at 4x and downsamples with LANCZOS for clean anti-aliasing.
Enabled = accent blue rounded square with white mic; disabled = grey.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (124, 156, 255, 255)
GREY = (92, 94, 104, 255)


def make_icon(size: int = 64, enabled: bool = True) -> Image.Image:
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = ACCENT if enabled else GREY
    fg = (255, 255, 255, 255) if enabled else (200, 201, 207, 255)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.24), fill=bg)

    cx = s / 2
    # mic capsule
    bw = s * 0.20
    top, bot = s * 0.17, s * 0.52
    d.rounded_rectangle([cx - bw, top, cx + bw, bot], radius=bw, fill=fg)
    # cradle arc
    lw = max(int(s * 0.07), 2)
    ar = s * 0.30
    acy = s * 0.46
    d.arc([cx - ar, acy - ar * 0.9, cx + ar, acy + ar * 1.1], start=0, end=180, fill=fg, width=lw)
    # stem + base
    d.line([cx, acy + ar * 1.1, cx, s * 0.82], fill=fg, width=lw)
    d.rounded_rectangle([cx - s * 0.15, s * 0.80, cx + s * 0.15, s * 0.80 + lw], radius=lw / 2, fill=fg)
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path, enabled: bool = True) -> Path:
    """Write a multi-resolution .ico to `path` (for shortcuts)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    make_icon(256, enabled).save(p, format="ICO",
                                 sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return p
