"""Draw the trading emoji + sticker pack into assets/ (run once on a dev machine, commit the PNGs).

    python tools/make_expressions.py

The bot only reads the finished PNGs, so the server needs no fonts. Artwork is original, built from
the Inter font (OFL) and Noto Emoji glyphs (Apache 2.0); see assets/CREDITS.md.
"""
from __future__ import annotations

import json
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOJI_DIR, STICKER_DIR = os.path.join(ROOT, "assets", "emojis"), os.path.join(ROOT, "assets", "stickers")
INTER = "/usr/share/fonts/opentype/inter/InterDisplay-Black.otf"
NOTO = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"

GREEN, GREEN_D = (34, 197, 94), (21, 128, 61)
RED, RED_D = (239, 68, 68), (153, 27, 27)
GOLD, GOLD_D = (250, 204, 21), (180, 120, 10)
BLUE, BLUE_D = (59, 130, 246), (30, 64, 175)
PURPLE, PURPLE_D = (168, 85, 247), (91, 33, 182)
AMBER, AMBER_D = (251, 146, 60), (194, 65, 12)
GRAY, GRAY_D = (148, 163, 184), (71, 85, 105)
INK, WHITE = (17, 24, 39), (255, 255, 255)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(INTER, size)


def glyph(char: str, size: int) -> Image.Image:
    """A Noto emoji, cropped and scaled to fit a size x size box."""
    f = ImageFont.truetype(NOTO, 109)
    img = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((80, 80), char, font=f, embedded_color=True, anchor="mm")
    img = img.crop(img.getbbox())
    img.thumbnail((size, size), Image.LANCZOS)
    return img


def gradient(w: int, h: int, top, bottom) -> Image.Image:
    g = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(g)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return g


def fit_text(d: ImageDraw.ImageDraw, text: str, max_w: int, start: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > 10 and d.textlength(text, font=font(size)) > max_w:
        size -= 2
    return font(size)


def paste_center(base: Image.Image, img: Image.Image, cx: int, cy: int) -> None:
    base.alpha_composite(img, (cx - img.width // 2, cy - img.height // 2))


# ------------------------------------------------------------------ emojis (128x128)
S = 128


def badge(text: str, color, dark, shape: str = "circle") -> Image.Image:
    """A glossy coin or pill with bold white text; readable on light and dark Discord themes."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    box = [4, 4, 124, 124] if shape == "circle" else [2, 22, 126, 106]
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse(box, fill=255) if shape == "circle" else md.rounded_rectangle(box, radius=42, fill=255)
    img.paste(gradient(S, S, color, dark), (0, 0), mask)
    d = ImageDraw.Draw(img)
    if shape == "circle":
        d.ellipse(box, outline=dark, width=5)
        shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(shine).chord([20, 12, 108, 72], 180, 360, fill=(255, 255, 255, 46))
        img.alpha_composite(shine)
    else:
        d.rounded_rectangle(box, radius=42, outline=dark, width=5)
    f = fit_text(d, text, 98 if shape == "circle" else 108, 60 if len(text) <= 2 else 46)
    d.text((64, 66), text, font=f, fill=WHITE, anchor="mm", stroke_width=3, stroke_fill=dark)
    return img


def candle(up: bool) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, dk = (GREEN, GREEN_D) if up else (RED, RED_D)
    d.rounded_rectangle([58, 6, 70, 122], radius=6, fill=dk)
    body = [36, 30, 92, 98] if up else [36, 26, 92, 96]
    d.rounded_rectangle(body, radius=10, fill=c, outline=dk, width=5)
    shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shine).rounded_rectangle([44, body[1] + 8, 54, body[3] - 10], radius=4, fill=(255, 255, 255, 80))
    img.alpha_composite(shine)
    return img


def chart(up: bool) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([4, 4, 124, 124], radius=26, fill=255)
    img.paste(gradient(S, S, (30, 41, 59), (15, 23, 42)), (0, 0), mask)
    d = ImageDraw.Draw(img)
    for y in (40, 64, 88):
        d.line([(16, y), (112, y)], fill=(255, 255, 255, 30), width=2)
    c = GREEN if up else RED
    pts = [(16, 98), (40, 78), (58, 86), (84, 52), (112, 26)] if up else [(16, 30), (40, 50), (58, 42), (84, 76), (112, 100)]
    d.line(pts, fill=c, width=10, joint="curve")
    tip = pts[-1]
    r = 10
    d.ellipse([tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r], fill=c, outline=WHITE, width=3)
    return img


def with_corner(char: str, corner: Image.Image | None = None, ribbon: tuple[str, tuple, tuple] | None = None) -> Image.Image:
    """A Noto glyph with our own corner badge or ribbon, so it reads as a server emoji, not a stock one."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = glyph(char, 104 if ribbon is None else 96)
    paste_center(img, g, 62, 58 if ribbon is None else 50)
    if corner is not None:
        small = corner.resize((56, 56), Image.LANCZOS)
        ring = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse([0, 0, 63, 63], fill=WHITE)
        img.alpha_composite(ring, (S - 66, S - 66))
        img.alpha_composite(small, (S - 62, S - 62))
    if ribbon is not None:
        text, color, dark = ribbon
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 90, 124, 124], radius=12, fill=color, outline=dark, width=3)
        f = fit_text(d, text, 108, 28)
        d.text((64, 108), text, font=f, fill=WHITE, anchor="mm", stroke_width=2, stroke_fill=dark)
    return img


def arrow(up: bool) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, dk = (GREEN, GREEN_D) if up else (RED, RED_D)
    d.ellipse([4, 4, 124, 124], fill=c, outline=dk, width=6)
    tri = [(64, 26), (102, 74), (26, 74)] if up else [(64, 102), (102, 54), (26, 54)]
    d.polygon(tri, fill=WHITE)
    d.rectangle([50, 70, 78, 104] if up else [50, 24, 78, 58], fill=WHITE)
    return img


def emojis() -> list[tuple[str, Image.Image]]:
    """Ordered by usefulness, so a nearly full server still gets the best ones first."""
    return [
        ("tp", badge("TP", GREEN, GREEN_D)),
        ("sl", badge("SL", RED, RED_D)),
        ("be", badge("BE", GRAY, GRAY_D)),
        ("green_candle", candle(True)),
        ("red_candle", candle(False)),
        ("pump", chart(True)),
        ("dump", chart(False)),
        ("bull", with_corner("🐂", arrow(True))),
        ("bear", with_corner("🐻", arrow(False))),
        ("plus1r", badge("+1R", GREEN, GREEN_D, "pill")),
        ("plus2r", badge("+2R", GREEN, GREEN_D, "pill")),
        ("plus3r", badge("+3R", GOLD, GOLD_D, "pill")),
        ("minus1r", badge("-1R", RED, RED_D, "pill")),
        ("aplus", badge("A+", GOLD, GOLD_D)),
        ("diamond_hands", with_corner("💎", ribbon=("HOLD", BLUE, BLUE_D))),
        ("paper_hands", with_corner("🧻", ribbon=("PAPER", GRAY, GRAY_D))),
        ("moon", with_corner("🚀", ribbon=("MOON", PURPLE, PURPLE_D))),
        ("lfg", badge("LFG", PURPLE, PURPLE_D)),
        ("gm", with_corner("☀️", ribbon=("GM", AMBER, AMBER_D))),
        ("nq", badge("NQ", BLUE, BLUE_D)),
        ("es", badge("ES", BLUE, BLUE_D)),
        ("cl", badge("CL", INK, (0, 0, 0))),
        ("gc", badge("GC", GOLD, GOLD_D)),
        ("btc", badge("BTC", AMBER, AMBER_D)),
        ("fomc", badge("FOMC", AMBER, AMBER_D, "pill")),
        ("cpi", badge("CPI", AMBER, AMBER_D, "pill")),
        ("news", with_corner("📰", ribbon=("NEWS", AMBER, AMBER_D))),
        ("whale", with_corner("🐋", ribbon=("WHALE", BLUE, BLUE_D))),
        ("liquidated", with_corner("💀", ribbon=("REKT", RED, RED_D))),
        ("fomo", with_corner("😱", ribbon=("FOMO", RED, RED_D))),
        ("revenge", with_corner("🤡", ribbon=("REVENGE", RED, RED_D))),
        ("patience", with_corner("⏳", ribbon=("WAIT", BLUE, BLUE_D))),
        ("bigbrain", with_corner("🧠", ribbon=("BIG BRAIN", PURPLE, PURPLE_D))),
        ("money_printer", with_corner("🖨️", ribbon=("BRRR", GREEN, GREEN_D))),
        ("journal", with_corner("📓", ribbon=("LOG IT", GRAY, GRAY_D))),
        ("bell", with_corner("🔔", ribbon=("OPEN", GREEN, GREEN_D))),
    ]


# ------------------------------------------------------------------ stickers (320x320, die-cut look)
T = 320


def sticker(char: str | tuple[str, ...], lines: list[str], dark) -> Image.Image:
    """Big art on top, bold two-tone caption below, white die-cut outline and a soft shadow."""
    art = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    chars = (char,) if isinstance(char, str) else char
    size = 170 if len(chars) == 1 else 130
    xs = [160] if len(chars) == 1 else [100, 220]
    top = 112 if len(lines) == 1 else 98
    for c, x in zip(chars, xs):
        paste_center(art, glyph(c, size), x, top)
    d = ImageDraw.Draw(art)
    y = 236 if len(lines) == 1 else 206
    for i, line in enumerate(lines):
        f = fit_text(d, line, 288, 66 if len(lines) == 1 else 54)
        d.text((160, y + i * 58), line, font=f, fill=WHITE, anchor="mm", stroke_width=9, stroke_fill=dark)
    # die-cut outline: grow the alpha, fill white, then a soft drop shadow under everything
    alpha = art.getchannel("A")
    cut = alpha.filter(ImageFilter.MaxFilter(15)).point(lambda a: 255 if a > 40 else 0).filter(ImageFilter.GaussianBlur(1))
    shadow = cut.filter(ImageFilter.GaussianBlur(6)).point(lambda a: int(a * 0.45))
    out = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    out.paste((0, 0, 0, 255), (3, 6), shadow)
    white = Image.new("RGBA", (T, T), (255, 255, 255, 255))
    out.alpha_composite(Image.composite(white, Image.new("RGBA", (T, T), (0, 0, 0, 0)), cut))
    out.alpha_composite(art)
    return out


def stickers() -> list[tuple[str, str, str, Image.Image]]:
    """(name, related emoji, description, image). Unboosted servers get 5 sticker slots, so best first."""
    return [
        ("LFG", "🚀", "Let's go! Hype for a clean entry", sticker("🚀", ["LFG"], PURPLE_D)),
        ("TP Hit", "🎯", "Take profit hit", sticker("🎯", ["TP HIT"], GREEN_D)),
        ("Stopped Out", "🛑", "Stopped out, on to the next one", sticker("🛑", ["STOPPED", "OUT"], RED_D)),
        ("Bullish", "🐂", "Bullish bias", sticker("🐂", ["BULLISH"], GREEN_D)),
        ("Bearish", "🐻", "Bearish bias", sticker("🐻", ["BEARISH"], RED_D)),
        ("GM Traders", "☀️", "Good morning traders", sticker("☀️", ["GM", "TRADERS"], AMBER_D)),
        ("Green Day", "💰", "Closed the day green", sticker("💰", ["GREEN", "DAY"], GREEN_D)),
        ("Diamond Hands", "💎", "Holding the runner", sticker(("💎", "🙌"), ["DIAMOND", "HANDS"], BLUE_D)),
        ("To The Moon", "🌕", "Straight up", sticker(("🚀", "🌕"), ["TO THE", "MOON"], PURPLE_D)),
        ("A Plus Setup", "⭐", "A+ setup, textbook", sticker("⭐", ["A+", "SETUP"], GOLD_D)),
        ("Patience Pays", "⏳", "Wait for your level", sticker("⏳", ["PATIENCE", "PAYS"], BLUE_D)),
        ("Risk First", "🛡️", "Protect the account first", sticker("🛡️", ["RISK", "FIRST"], BLUE_D)),
        ("No Revenge", "🤡", "No revenge trading", sticker("🤡", ["NO REVENGE", "TRADES"], RED_D)),
        ("Log It", "📓", "Journal the trade", sticker("📓", ["LOG", "IT"], GRAY_D)),
        ("WAGMI", "🤝", "We're all gonna make it", sticker("🤝", ["WAGMI"], GREEN_D)),
    ]


def slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def main() -> None:
    os.makedirs(EMOJI_DIR, exist_ok=True)
    os.makedirs(STICKER_DIR, exist_ok=True)
    for i, (name, img) in enumerate(emojis()):
        img.save(os.path.join(EMOJI_DIR, f"{i:02d}_{name}.png"), optimize=True)
    meta = []
    for i, (name, tag, desc, img) in enumerate(stickers()):
        fn = f"{i:02d}_{slug(name)}.png"
        img.save(os.path.join(STICKER_DIR, fn), optimize=True)
        meta.append({"file": fn, "name": name, "emoji": tag, "description": desc})
    with open(os.path.join(STICKER_DIR, "stickers.json"), "w") as fh:
        json.dump(meta, fh, indent=1, ensure_ascii=False)
    print(f"{len(emojis())} emojis, {len(meta)} stickers", file=sys.stderr)


if __name__ == "__main__":
    main()
