"""A small default set of custom emojis drawn with Pillow (128x128 PNG)."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

GREEN, RED, GOLD, WHITE = (46, 204, 113, 255), (231, 76, 60, 255), (241, 196, 15, 255), (255, 255, 255, 255)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _canvas():
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def default_emojis() -> list[tuple[str, bytes]]:
    out = []
    img, d = _canvas()
    d.ellipse([4, 4, 124, 124], fill=GREEN)
    d.polygon([(64, 24), (104, 84), (24, 84)], fill=WHITE)
    out.append(("long", _png(img)))
    img, d = _canvas()
    d.ellipse([4, 4, 124, 124], fill=RED)
    d.polygon([(64, 104), (104, 44), (24, 44)], fill=WHITE)
    out.append(("short", _png(img)))
    img, d = _canvas()
    d.ellipse([4, 4, 124, 124], fill=GREEN)
    d.line([(34, 66), (56, 90), (96, 40)], fill=WHITE, width=14, joint="curve")
    out.append(("win", _png(img)))
    img, d = _canvas()
    d.ellipse([4, 4, 124, 124], fill=RED)
    d.line([(40, 40), (88, 88)], fill=WHITE, width=14)
    d.line([(88, 40), (40, 88)], fill=WHITE, width=14)
    out.append(("loss", _png(img)))
    img, d = _canvas()
    d.ellipse([4, 4, 124, 124], fill=GOLD)
    d.polygon([(64, 18), (76, 52), (112, 52), (83, 73), (94, 108), (64, 87), (34, 108), (45, 73), (16, 52), (52, 52)], fill=WHITE)
    out.append(("levelup", _png(img)))
    return out
