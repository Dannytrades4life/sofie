"""Branded trade cards (Pillow, no external services)."""
from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .futures import Display, fmt_dist, fmt_pct, fmt_price, fmt_usd, outcome

W, H = 1200, 675
BG = (13, 16, 22)
PANEL = (23, 27, 36)
LINE = (38, 44, 56)
MUTED = (139, 148, 158)
WHITE = (240, 246, 252)
GREEN = (46, 204, 113)
RED = (231, 76, 60)
GREY = (149, 165, 166)

_BOLD = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"]
_REG = ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf", "Arial.ttf"]


def _font(size: int, bold: bool = True):
    for name in (_BOLD if bold else _REG):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _hex(color: int) -> tuple[int, int, int]:
    return (color >> 16) & 255, (color >> 8) & 255, color & 255


def _stat(d: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, color=WHITE) -> None:
    d.text((x, y), label.upper(), font=_font(20, False), fill=MUTED)
    d.text((x, y + 28), value, font=_font(36), fill=color)


def _grid(d: ImageDraw.ImageDraw, cells: list[tuple[str, str]]) -> None:
    for i, (label, value) in enumerate(cells):
        _stat(d, 100 + (i % 3) * 360, 395 + (i // 3) * 80, label, value)


def _base(brand: str, brand_color: int, logo: bytes | None, tag: str, tag_color) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 8], fill=_hex(brand_color))
    x = 60
    if logo:
        try:
            icon = ImageOps.fit(Image.open(io.BytesIO(logo)).convert("RGBA"), (56, 56))
            mask = Image.new("L", (56, 56), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, 55, 55], fill=255)
            img.paste(icon, (60, 36), mask)
            x = 132
        except Exception:
            pass
    d.text((x, 48), brand.upper(), font=_font(28), fill=WHITE)
    d.rounded_rectangle([W - 60 - 220, 40, W - 60, 92], radius=26, outline=tag_color, width=3)
    d.text((W - 60 - 110, 66), tag, font=_font(26), fill=tag_color, anchor="mm")
    return img, d


def _footer(d: ImageDraw.ImageDraw, when: str) -> None:
    d.text((60, H - 82), when, font=_font(24, False), fill=MUTED)
    d.text((60, H - 46), "Not financial advice. Futures trading involves substantial risk of loss.", font=_font(20, False), fill=MUTED)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _headline(d, t: dict) -> None:
    side = t.get("side", "long")
    contract = t.get("contract") or t.get("symbol", "")
    session = t.get("session")
    head = f"{contract}  ·  {side.upper()}"
    d.text((100, 150), head, font=_font(48), fill=WHITE)
    if session:
        w = d.textlength(head, font=_font(48))
        d.text((100 + w + 30, 162), f"{session} session", font=_font(28, False), fill=MUTED)


def entry_card(t: dict, *, brand: str, brand_color: int, logo: bytes | None = None, show: Display | None = None) -> bytes:
    show = show or Display()
    side = t.get("side", "long")
    accent = GREEN if side == "long" else RED
    img, d = _base(brand, brand_color, logo, "ENTRY", accent)
    d.rounded_rectangle([60, 120, W - 60, 560], radius=28, fill=PANEL)
    _headline(d, t)
    d.text((100, 225), fmt_price(t.get("entry")), font=_font(110), fill=accent)
    d.line([100, 375, W - 100, 375], fill=LINE, width=2)
    cells = [("Stop", fmt_price(t.get("stop"))), ("Target", fmt_price(t.get("target"))),
             ("Planned R:R", f"1 : {t['rr_planned']:.2f}" if t.get("rr_planned") else "—"),
             ("Risk", fmt_dist(t.get("risk_points"), t.get("risk_ticks"))),
             ("Reward", fmt_dist(t.get("reward_points"), t.get("reward_ticks")))]
    if show.size:
        cells.append(("Contracts", str(t.get("contracts") or "—")))
    if show.usd and t.get("risk_usd") is not None:
        cells.insert(5, ("Risk $", f"${t['risk_usd']:,.0f}"))
    _grid(d, cells[:6])
    _footer(d, _when(t))
    return _png(img)


def result_card(t: dict, *, brand: str, brand_color: int, logo: bytes | None = None, show: Display | None = None) -> bytes:
    show = show or Display()
    res = outcome(t)
    accent = {"win": GREEN, "loss": RED}.get(res, GREY)
    img, d = _base(brand, brand_color, logo, {"win": "WIN", "loss": "LOSS"}.get(res, "FLAT"), accent)
    d.rounded_rectangle([60, 120, W - 60, 560], radius=28, fill=PANEL)
    _headline(d, t)
    acct = show.account_pct(t)
    if show.usd and t.get("pnl_usd") is not None:
        big = fmt_usd(t["pnl_usd"])
    elif t.get("points") is not None:
        big = f"{t['points']:+,.2f} pts"
    else:
        big = "—"
    d.text((100, 230), big, font=_font(96 if len(big) <= 12 else 80), fill=accent)
    if t.get("r_multiple") is not None:
        d.text((W - 100, 250), f"{t['r_multiple']:+.2f}R", font=_font(64), fill=WHITE, anchor="ra")
    d.line([100, 375, W - 100, 375], fill=LINE, width=2)
    ticks = f"{t['ticks']:+,.0f}" if t.get("ticks") is not None else "—"
    cells = [("Entry", fmt_price(t.get("entry"))), ("Exit", fmt_price(t.get("exit"))), ("Ticks", ticks),
             ("Price move", fmt_pct(t.get("pnl_pct")))]
    if acct is not None:
        cells.append(("Account", fmt_pct(acct)))
    if show.usd and t.get("points") is not None:
        cells.append(("Points", f"{t['points']:+,.2f}"))
    if show.size:
        cells.append(("Contracts", str(t.get("contracts") or "—")))
    _grid(d, cells[:6])
    _footer(d, _when(t))
    return _png(img)


def _when(t: dict) -> str:
    ts = t.get("exit_time") or t.get("entry_time")
    try:
        dt = datetime.fromisoformat(ts) if ts else datetime.now()
    except ValueError:
        dt = datetime.now()
    s = dt.strftime("%a %b %d, %Y")
    return s


def _fit(d: ImageDraw.ImageDraw, text: str, size: int, width: int, bold: bool = True):
    """Shrink the font (then trim the text) until it fits the given width."""
    while size > 22 and d.textlength(text, font=_font(size, bold)) > width:
        size -= 2
    while len(text) > 3 and d.textlength(text, font=_font(size, bold)) > width:
        text = text[:-2].rstrip() + "…"
    return text, _font(size, bold)


def journal_card(t: dict, *, brand: str, brand_color: int, logo: bytes | None = None, chart: bytes | None = None) -> bytes:
    """Card for a journaled trade: the journal has no prices, so it shows outcome, R, setup and discipline (never money)."""
    res = t.get("journal_outcome") or "closed"
    accent = {"win": GREEN, "loss": RED}.get(res, GREY)
    img, d = _base(brand, brand_color, logo, {"win": "WIN", "loss": "LOSS", "breakeven": "BE"}.get(res, "TRADE"), accent)
    d.rounded_rectangle([60, 120, W - 60, 560], radius=28, fill=PANEL)
    left_w = 560 if chart else W - 200
    side = (t.get("side") or "long").upper()
    head = f"{t['contract']}  ·  {side}" if t.get("contract") else side
    d.text((100, 150), head, font=_font(48), fill=WHITE)
    if t.get("journal_session") and chart:  # no room beside the headline next to the chart
        sess, f = _fit(d, f"{t['journal_session']} session", 26, left_w, False)
        d.text((100, 334), sess, font=f, fill=MUTED)
    elif t.get("journal_session"):
        w = d.textlength(head, font=_font(48))
        sess, f = _fit(d, f"{t['journal_session']} session", 28, max(left_w - w - 30, 120), False)
        d.text((100 + w + 30, 162), sess, font=f, fill=MUTED)
    rr = t.get("journal_rr")
    big = {"win": f"+{rr:g}R" if rr else "WIN", "loss": "LOSS", "breakeven": "BREAKEVEN"}.get(res, "LOGGED")
    big, f = _fit(d, big, 110 if not chart else 100, left_w)
    d.text((100, 215 if chart else 225), big, font=f, fill=accent)
    if chart:
        try:
            box = (W - 100 - 440, 145, W - 100, 355)
            shot = ImageOps.fit(Image.open(io.BytesIO(chart)).convert("RGB"), (box[2] - box[0], box[3] - box[1]))
            mask = Image.new("L", shot.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, shot.size[0] - 1, shot.size[1] - 1], radius=16, fill=255)
            img.paste(shot, box[:2], mask)
            d.rounded_rectangle(box, radius=16, outline=LINE, width=2)
        except Exception:
            pass
    d.line([100, 375, W - 100, 375], fill=LINE, width=2)
    mistake = t.get("mistake") or ""
    plan = "Followed ✓" if not mistake or mistake.lower().startswith("none") else mistake
    disc = t.get("discipline")
    cells = [("Setup", (t.get("setup") or "—").split(" → ")[0]),
             ("Discipline", f"{disc:.0f}%" if disc is not None else "—"),
             ("Plan", plan)]
    for i, (label, value) in enumerate(cells):
        x = 100 + i * 360
        d.text((x, 395), label.upper(), font=_font(20, False), fill=MUTED)
        value, f = _fit(d, value, 36, 330)
        d.text((x, 423), value, font=f, fill=WHITE)
    rules = t.get("rules_followed") or []
    if rules:
        line, f = _fit(d, "✓ " + "   ✓ ".join(rules), 22, W - 200, False)
        d.text((100, 500), line, font=f, fill=MUTED)
    when = t.get("date")
    try:
        when = datetime.fromisoformat(when).strftime("%a %b %d, %Y") if when else _when(t)
    except ValueError:
        when = _when(t)
    _footer(d, when)
    return _png(img)
