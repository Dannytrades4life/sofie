"""Shared helpers: server layout and branding, channel lookup, time helpers."""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from .config import MARKET_TZ

if TYPE_CHECKING:
    from .core import TradingBot

DISCLAIMER = "Not financial advice. For education only. Futures trading involves substantial risk of loss."
SHORT_NFA = "Not financial advice."
GREEN = 0x2ECC71
RED = 0xE74C3C
GREY = 0x95A5A6
GOLD = 0xF1C40F
ORANGE = 0xE67E22

# ---------------------------------------------------------------- server layout
CATEGORIES = ["📌 START HERE", "📊 MARKETS", "📈 TRADING", "🎓 LEARN", "💬 COMMUNITY", "🛠 STAFF"]

# key -> (category, channel name, topic, members can post?)
CHANNELS: dict[str, tuple[str, str, str, bool]] = {
    "welcome":        ("📌 START HERE", "👋┃welcome", "New members land here. Say hi!", False),
    "rules":          ("📌 START HERE", "📜┃rules", "Read the rules and tap the button to unlock the server.", False),
    "roles":          ("📌 START HERE", "🎭┃pick-roles", "Pick your markets and alerts.", False),
    "announcements":  ("📌 START HERE", "📢┃announcements", "Server news.", False),
    "invite_rewards": ("📌 START HERE", "🚀┃invite-rewards", "Invite traders, earn ranks and bonus giveaway entries.", False),
    "calendar":       ("📊 MARKETS", "🗓┃economic-calendar", "CPI, FOMC, NFP, EIA crude and other market-moving events (New York time).", False),
    "premarket":      ("📊 MARKETS", "☀️┃premarket-plan", "Daily premarket levels and market regime. Not financial advice.", False),
    "recap":          ("📊 MARKETS", "🌙┃daily-recap", "End-of-day recap and weekly performance.", False),
    "trade_entries":  ("📈 TRADING", "📈┃trade-entries", "Trade entries. Not financial advice.", False),
    "results":        ("📈 TRADING", "🏆┃trade-results", "Closed trades: wins and losses. Not financial advice.", False),
    "member_trades":  ("📈 TRADING", "📊┃member-trades", "Share your trades with /share-trade. Self-reported, not financial advice.", True),
    "journal":        ("📈 TRADING", "📓┃trade-journal", "Journal your day: what you saw, what you did, what you learned.", True),
    "challenges":     ("📈 TRADING", "🏁┃weekly-challenge", "This week's challenge and leaderboard.", False),
    "lessons":        ("🎓 LEARN", "🧠┃daily-lessons", "One short futures lesson a day.", False),
    "setups":         ("🎓 LEARN", "🔍┃setup-breakdowns", "Setup breakdowns. Educational only.", False),
    "general":        ("💬 COMMUNITY", "💬┃general", "Hang out and talk markets.", True),
    "chart_talk":     ("💬 COMMUNITY", "📉┃chart-talk", "Share charts, levels and ideas.", True),
    "memes":          ("💬 COMMUNITY", "😂┃memes-and-gifs", "Trading memes and GIFs.", True),
    "level_ups":      ("💬 COMMUNITY", "⭐┃level-ups", "Level-ups and rank-ups.", False),
    "giveaways":      ("💬 COMMUNITY", "🎁┃giveaways", "Sponsor giveaways. No purchase necessary.", False),
    "staff_panel":    ("🛠 STAFF", "🛠┃staff-panel", "Staff commands and bot confirmations.", True),
    "trade_submit":   ("🛠 STAFF", "📝┃trade-submit", "Owner: drop trades or screenshots here.", True),
    "mod_log":        ("🛠 STAFF", "🛡┃mod-log", "Moderation actions.", False),
    "bot_log":        ("🛠 STAFF", "🤖┃bot-log", "Everything the bot does.", False),
}
STAFF_ONLY_CATEGORY = "🛠 STAFF"
PUBLIC_CATEGORY = "📌 START HERE"

# ---------------------------------------------------------------- roles
VERIFIED_ROLE = "Trader"
FOUNDER_ROLE = "👑 Founder"
LEADER_ROLE = "Leaders"
MOD_ROLE = "Mods"
CHAMP_ROLE = "🏁 Challenge Champ"
ALERT_ROLE = "🔔 Trade Alerts"
CALENDAR_ROLE = "🗓 Calendar Alerts"
GIVEAWAY_ROLE = "🎁 Giveaway Pings"

LEVEL_ROLES = {5: "📗 Rookie", 10: "📘 Active Trader", 20: "📙 Consistent", 35: "📕 Pro Trader", 50: "💎 Veteran"}
INVITE_ROLES = {3: "🤝 Recruiter", 10: "🚀 Ambassador", 25: "👑 Legend"}
SELF_ROLES = [("NQ", "📈"), ("ES", "📊"), ("CL", "🛢️"), (ALERT_ROLE, "🔔"), (CALENDAR_ROLE, "🗓️"), (GIVEAWAY_ROLE, "🎁")]

# name -> (color, hoist, mentionable). Order here = order top to bottom in the role list.
ROLE_STYLE: dict[str, tuple[int, bool, bool]] = {
    FOUNDER_ROLE: (0xFFC93C, True, False),
    LEADER_ROLE: (0xF1C40F, True, False),
    MOD_ROLE: (0xE74C3C, True, False),
    CHAMP_ROLE: (0xE67E22, True, False),
    "💎 Veteran": (0x1ABC9C, True, False),
    "📕 Pro Trader": (0xC0392B, True, False),
    "📙 Consistent": (0xE67E22, True, False),
    "📘 Active Trader": (0x3498DB, True, False),
    "📗 Rookie": (0x2ECC71, True, False),
    "👑 Legend": (0x9B59B6, False, False),
    "🚀 Ambassador": (0x8E44AD, False, False),
    "🤝 Recruiter": (0xA569BD, False, False),
    VERIFIED_ROLE: (0x5DADE2, False, False),
    "NQ": (0, False, False), "ES": (0, False, False), "CL": (0, False, False),
    ALERT_ROLE: (0, False, True), CALENDAR_ROLE: (0, False, True), GIVEAWAY_ROLE: (0, False, True),
}


def slug(name: str) -> str:
    """'📈┃trade-entries' -> 'trade-entries'"""
    return re.split(r"[┃・|]", name)[-1].strip().lower()


def get_channel(bot: "TradingBot", guild: discord.Guild, key: str) -> discord.TextChannel | None:
    cid = bot.db.get_setting(guild.id, f"ch:{key}")
    if cid:
        ch = guild.get_channel(int(cid))
        if isinstance(ch, discord.TextChannel):
            return ch
    want = slug(CHANNELS[key][1])
    for ch in guild.text_channels:
        if slug(ch.name) == want:
            return ch
    return None


def brand_color(bot: "TradingBot", guild: discord.Guild | None = None) -> int:
    if guild:
        raw = bot.db.get_setting(guild.id, "brand_color")
        if raw:
            return int(raw)
    return bot.config.brand_color


def local_now(bot: "TradingBot") -> datetime:
    return datetime.now(bot.config.tz)


def market_now() -> datetime:
    return datetime.now(MARKET_TZ)


def today(bot: "TradingBot") -> str:
    return local_now(bot).strftime("%Y-%m-%d")


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)
