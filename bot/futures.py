"""Futures contract specs, symbol parsing, P&L math and session labels.

Specs live in the database (table `symbols`) so the owner can add or change them
with /symbols. DEFAULT_SYMBOLS seeds a fresh server.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .config import MARKET_TZ


@dataclass
class Spec:
    root: str
    name: str
    tick_size: float
    tick_value: float          # $ per tick per contract
    data_ticker: str | None    # Yahoo Finance ticker for market posts
    market_posts: bool = True  # include in premarket plan / recap

    @property
    def point_value(self) -> float:
        return self.tick_value / self.tick_size


DEFAULT_SYMBOLS = [
    Spec("NQ", "E-mini Nasdaq-100", 0.25, 5.00, "NQ=F"),
    Spec("MNQ", "Micro E-mini Nasdaq-100", 0.25, 0.50, "NQ=F", market_posts=False),
    Spec("ES", "E-mini S&P 500", 0.25, 12.50, "ES=F"),
    Spec("MES", "Micro E-mini S&P 500", 0.25, 1.25, "ES=F", market_posts=False),
    Spec("CL", "Crude Oil", 0.01, 10.00, "CL=F"),
    Spec("MCL", "Micro Crude Oil", 0.01, 1.00, "CL=F", market_posts=False),
]

MONTH_CODES = "FGHJKMNQUVXZ"


def resolve_root(raw: str, roots: list[str]) -> str | None:
    """'MNQZ6', '/NQ', 'NQ1!', 'ES DEC26', 'mcl' -> the matching root, longest match first."""
    s = re.sub(r"[^A-Z0-9]", "", raw.upper())
    for root in sorted(roots, key=len, reverse=True):
        if s == root:
            return root
        if s.startswith(root):
            rest = s[len(root):]
            # contract month/year (Z6, Z26), continuous (1), or a month name (DEC26)
            if re.fullmatch(rf"[{MONTH_CODES}]\d{{1,4}}|\d{{1,4}}|[A-Z]{{3}}\d{{2,4}}|", rest):
                return root
    return None


def session_label(when: datetime) -> str:
    """CME Globex sessions in New York time."""
    t = when.astimezone(MARKET_TZ)
    hm = t.hour * 60 + t.minute
    if 9 * 60 + 30 <= hm < 17 * 60:
        return "NY"
    if 3 * 60 <= hm < 9 * 60 + 30:
        return "London"
    if hm >= 18 * 60 or hm < 3 * 60:
        return "Asia"
    return "After hours"


SESSION_EMOJI = {"NY": "🗽", "London": "💂", "Asia": "🌏", "After hours": "🌙"}


def compute(trade: dict, spec: Spec | None) -> dict:
    """Fill derived fields (points, ticks, $ P&L, R, %) unless the owner overrode them."""
    out = dict(trade)
    overrides = set(out.get("overrides", []))
    entry, exit_, stop = out.get("entry"), out.get("exit"), out.get("stop")
    contracts = out.get("contracts") or 1
    sign = 1 if out.get("side", "long") == "long" else -1

    def put(key, value):
        if key not in overrides:
            out[key] = value

    if entry is not None and stop is not None:
        risk_pts = abs(entry - stop)
        put("risk_points", round(risk_pts, 4))
        if spec:
            put("risk_ticks", round(risk_pts / spec.tick_size, 1))
            put("risk_usd", round(risk_pts / spec.tick_size * spec.tick_value * contracts, 2))
    if entry is not None and out.get("target") is not None:
        reward_pts = abs(out["target"] - entry)
        put("reward_points", round(reward_pts, 4))
        if spec:
            put("reward_ticks", round(reward_pts / spec.tick_size, 1))
    if entry is not None and out.get("target") is not None and stop is not None and entry != stop:
        put("rr_planned", round(abs(out["target"] - entry) / abs(entry - stop), 2))
    if entry is not None and exit_ is not None:
        points = (exit_ - entry) * sign
        put("points", round(points, 4))
        put("pnl_pct", round(points / entry * 100, 3) if entry else None)
        if spec:
            ticks = points / spec.tick_size
            put("ticks", round(ticks, 1))
            put("pnl_usd", round(ticks * spec.tick_value * contracts, 2))
        if stop is not None and stop != entry:
            put("r_multiple", round(points / abs(entry - stop), 2))
    return out


@dataclass
class Display:
    """What a public post may show. By default: no dollar amounts and no position size,
    only points, ticks, % price move, R and (if an account size is set) % of account."""
    usd: bool = False
    size: bool = False
    account: float | None = None

    def account_pct(self, trade: dict) -> float | None:
        if not self.account or trade.get("pnl_usd") is None:
            return None
        return round(trade["pnl_usd"] / self.account * 100, 2)


def outcome(trade: dict) -> str:
    p = trade.get("pnl_usd")
    if p is None:
        p = trade.get("points")
    if p is None:
        return "open"
    return "win" if p > 0 else "loss" if p < 0 else "breakeven"


def fmt_price(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4g}"


def fmt_usd(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def fmt_pts(points: float | None, ticks: float | None = None) -> str:
    if points is None:
        return "—"
    out = f"{points:+,.2f} pts"
    if ticks is not None:
        out += f" · {ticks:+,.0f} ticks"
    return out


def fmt_dist(points: float | None, ticks: float | None = None) -> str:
    """Unsigned distance, for risk and reward."""
    if points is None:
        return "—"
    return f"{points:,.2f} pts" + (f" · {ticks:,.0f}t" if ticks is not None else "")


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}%"
