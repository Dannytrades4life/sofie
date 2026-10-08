"""The owner's control layer: feature switches, approval modes, staff permissions,
the bot's identity, the /teach rulebook, and publish(), the one door every
autonomous post goes through.

publish() order of checks:  kill switch -> feature on? -> owner's rulebook -> approval mode -> send.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .util import LEADER_ROLE, MOD_ROLE, get_channel, market_now

if TYPE_CHECKING:
    from .core import TradingBot

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ features
# key -> (label, default mode). Mode "auto" = act alone, "ask" = DM the owner first.
FEATURES: dict[str, tuple[str, str]] = {
    "trade_posts": ("Trade entry/result posts", "auto"),
    "premarket": ("Premarket plan", "auto"),
    "calendar": ("Economic calendar + alerts", "auto"),
    "recap": ("End-of-day recap + weekly performance", "auto"),
    "welcomes": ("Welcome new members", "auto"),
    "chat_replies": ("Reply when people talk to the bot", "auto"),
    "daily_question": ("Daily discussion question / weekly poll", "auto"),
    "quiet_starters": ("Wake up quiet chats", "auto"),
    "lessons": ("Daily lessons + setup breakdowns", "auto"),
    "gifs": ("GIF / meme posts", "auto"),
    "member_trades": ("Member trade sharing", "auto"),
    "challenges": ("Weekly challenges", "auto"),
    "experiments": ("Weekly self-proposed experiments", "auto"),
    "giveaways": ("Giveaways", "ask"),
    "moderation": ("Auto-moderation", "auto"),
    "levels": ("XP / levels / rank roles", "auto"),
    "invites": ("Invite rewards", "auto"),
    "social": ("Social content ideas", "auto"),
}


def feature_on(bot: "TradingBot", guild_id: int, key: str) -> bool:
    return bot.db.get_setting(guild_id, f"feat:{key}:on", "1") == "1"


def feature_mode(bot: "TradingBot", guild_id: int, key: str) -> str:
    return bot.db.get_setting(guild_id, f"feat:{key}:mode", FEATURES[key][1])


def active(bot: "TradingBot", guild_id: int, key: str) -> bool:
    """Should an autonomous behaviour run right now?"""
    return not bot.paused and feature_on(bot, guild_id, key)


# ------------------------------------------------------------------ permissions
OWNER, LEADER, MOD = "owner", "leader", "mod"

# Which staff levels may use each command by default (the owner can always use everything).
# Change live with /perm. Commands not listed are owner-only.
COMMAND_DEFAULTS: dict[str, set[str]] = {
    "edit": {LEADER, MOD},          # mods/leaders: text fields only; numbers are owner-only
    "edit-history": {LEADER, MOD},
    "poll": {LEADER, MOD},
    "spark": {LEADER, MOD},
    "announce": {LEADER},
    "giveaway": {LEADER},           # leaders can draft; the owner approves
    "giveaway-admin": {LEADER, MOD},  # reroll / end / list
    "teach-list": {LEADER, MOD},
    "challenge": {LEADER},
    "social": {LEADER},
    "status": {LEADER, MOD},
    "pause": {LEADER},
}


def staff_level(bot: "TradingBot", member: discord.abc.User) -> str | None:
    if member.id == bot.config.owner_id:
        return OWNER
    if not isinstance(member, discord.Member):
        return None
    ids = bot.db.get_json(member.guild.id, "staff_roles", {}) or {}
    role_ids = {r.id for r in member.roles}
    names = {r.name for r in member.roles}
    if set(ids.get(LEADER, [])) & role_ids or LEADER_ROLE in names:
        return LEADER
    if set(ids.get(MOD, [])) & role_ids or MOD_ROLE in names:
        return MOD
    return None


def allowed_levels(bot: "TradingBot", guild_id: int, command: str) -> set[str]:
    stored = bot.db.get_json(guild_id, f"perm:{command}")
    return set(stored) if stored is not None else set(COMMAND_DEFAULTS.get(command, set()))


def can_use(bot: "TradingBot", member: discord.abc.User, guild_id: int, command: str) -> bool:
    level = staff_level(bot, member)
    return level == OWNER or (level is not None and level in allowed_levels(bot, guild_id, command))


class NotAllowed(app_commands.CheckFailure):
    pass


def staff(command: str):
    """Slash-command check: owner always; staff if /perm allows it, and only in #staff-panel."""
    async def predicate(interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if interaction.user.id == bot.config.owner_id:
            return True
        if interaction.guild is None or not can_use(bot, interaction.user, interaction.guild.id, command):
            raise NotAllowed("You don't have access to this command.")
        panel = get_channel(bot, interaction.guild, "staff_panel")
        if panel and interaction.channel_id != panel.id:
            raise NotAllowed(f"Use staff commands in {panel.mention}.")
        return True

    return app_commands.check(predicate)


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.client.config.owner_id:
            return True
        raise NotAllowed("Only the owner can use this command.")

    return app_commands.check(predicate)


# ------------------------------------------------------------------ identity & persona
DEFAULT_PERSONALITY = (
    "Sofie, a woman (she/her) who day trades futures, mostly NQ, ES and crude, and has been at it for years. Warm, upbeat, "
    "a bit of dry humor, keeps messages short, lowercase is fine, an emoji now and then. Talks like a real person in a group "
    "chat, never corporate. Hypes people's process and discipline, not their money."
)


def identity(bot: "TradingBot", guild_id: int) -> dict:
    data = bot.db.get_json(guild_id, "identity", {}) or {}
    return {"name": data.get("name") or bot.config.bot_name, "personality": data.get("personality") or DEFAULT_PERSONALITY}


def persona(bot: "TradingBot", guild: discord.Guild) -> str:
    ident = identity(bot, guild.id)
    rules = bot.rulebook.get(guild.id, [])
    text = (
        f"You are {ident['name']}, the community host of the trading Discord server '{guild.name}'. "
        f"Personality: {ident['personality']} "
        "Hard rules: never promise profits, never give personalised financial advice, never invent trades, results, "
        "prices, members, testimonials or statistics, never ask anyone to DM you or send money. If you don't know a fact, "
        "don't state it. You show a BOT tag in Discord; if someone sincerely asks whether you're a bot or AI, say yes."
    )
    if rules:
        text += "\nThe owner's rulebook. These ALWAYS override everything else:\n" + "\n".join(f"- {r['text']}" for r in rules)
    return text


# ------------------------------------------------------------------ rulebook
async def load_rulebook(bot: "TradingBot") -> None:
    bot.rulebook.clear()
    for row in await bot.db.fetchall("SELECT id, guild_id, text FROM rulebook ORDER BY id"):
        bot.rulebook.setdefault(row["guild_id"], []).append({"id": row["id"], "text": row["text"]})


_EVENT_WORDS = ["fomc", "fed", "powell", "cpi", "ppi", "nfp", "non-farm", "payroll", "jobs", "eia", "crude", "inventor", "gdp", "pce", "retail sales"]


async def rules_allow(bot: "TradingBot", guild: discord.Guild, action: str) -> tuple[bool, str | None]:
    """Ask: does this autonomous action break any of the owner's rules right now?"""
    rules = bot.rulebook.get(guild.id, [])
    if not rules:
        return True, None
    market = bot.get_cog("Market")
    near = market.events_near(minutes=60) if market else []
    now = market_now()
    context = f"Now: {now:%A %Y-%m-%d %H:%M} New York time. Market events within 60 min: " + (
        ", ".join(f"{e['title']} at {e['time']:%H:%M}" for e in near) or "none")
    verdict = await bot.llm.json(
        "You enforce a Discord server owner's rulebook for their bot. Be strict: if an action plausibly breaks a rule, block it.",
        "Rules:\n" + "\n".join(f"{i + 1}. {r['text']}" for i, r in enumerate(rules)) +
        f"\n\n{context}\n\nProposed action: {action}\n"
        'Answer {"allow": true|false, "rule": "<the rule it breaks, or empty>"}',
        max_tokens=120, temperature=0,
    )
    if isinstance(verdict, dict) and "allow" in verdict:
        return bool(verdict["allow"]), (verdict.get("rule") or None)
    # Offline fallback: block posting rules that name an event happening right now.
    titles = " ".join(e["title"].lower() for e in near)
    for r in rules:
        t = r["text"].lower()
        if re.search(r"\b(never|don'?t|do not|no)\b", t) and "post" in t:
            if any(w in t and w in titles for w in _EVENT_WORDS):
                return False, r["text"]
    return True, None


# ------------------------------------------------------------------ publish
PENDING_DIR = "data/pending"


async def publish(
    bot: "TradingBot", guild: discord.Guild, feature: str, channel: discord.abc.Messageable | str, *,
    content: str | None = None, embed: discord.Embed | None = None, file: tuple[bytes, str] | None = None,
    view: discord.ui.View | None = None, kind: str = "post", ref_id: int | None = None,
    allowed_mentions: discord.AllowedMentions | None = None, owner_initiated: bool = False,
    describe: str | None = None, thread_name: str | None = None,
) -> discord.Message | None:
    """Send a bot post through every safety layer. Returns the message, or None if held/blocked."""
    from .safety import log_action, request_approval  # local import avoids a cycle

    if isinstance(channel, str):
        channel = get_channel(bot, guild, channel)
    if channel is None:
        return None
    if not owner_initiated:
        if bot.paused or not feature_on(bot, guild.id, feature):
            return None
        summary = describe or (embed.title if embed and embed.title else "") + " " + (content or (embed.description if embed else "") or "")
        ok, rule = await rules_allow(bot, guild, f"Post in #{channel.name}: {summary[:600]}")
        if not ok:
            await log_action(bot, guild, "blocked_by_rule", f"{feature} post held back by your rule: {rule}")
            return None
        if feature_mode(bot, guild.id, feature) == "ask" and view is None:
            path = None
            if file:
                os.makedirs(PENDING_DIR, exist_ok=True)
                path = os.path.join(PENDING_DIR, f"{int(time.time() * 1000)}-{file[1]}")
                with open(path, "wb") as fh:
                    fh.write(file[0])
            await request_approval(
                bot, guild, "post", f"**{FEATURES[feature][0]}** wants to post in {channel.mention}:\n>>> {(summary or '')[:1200]}",
                {"channel_id": channel.id, "content": content, "embed": embed.to_dict() if embed else None,
                 "file_path": path, "file_name": file[1] if file else None, "feature": feature, "kind": kind, "ref_id": ref_id},
                preview=embed,
            )
            return None
    msg = await send_post(bot, guild, channel, feature, content=content, embed=embed, file=file, view=view, kind=kind,
                          ref_id=ref_id, allowed_mentions=allowed_mentions)
    if thread_name:
        try:
            await msg.create_thread(name=thread_name[:90], auto_archive_duration=1440)
        except discord.HTTPException:
            pass
    await log_action(bot, guild, feature, f"posted {kind} in #{channel.name}" + (f" (#{ref_id})" if ref_id else ""))
    return msg


async def send_post(bot, guild, channel, feature, *, content=None, embed=None, file=None, view=None, kind="post",
                    ref_id=None, allowed_mentions=None) -> discord.Message:
    kwargs = {}
    if file:
        kwargs["file"] = discord.File(io.BytesIO(file[0]), filename=file[1])
        if embed and not embed.image:
            embed.set_image(url=f"attachment://{file[1]}")
    if view:
        kwargs["view"] = view
    if allowed_mentions:
        kwargs["allowed_mentions"] = allowed_mentions
    msg = await channel.send(content=content, embed=embed, **kwargs)
    await bot.db.execute(
        "INSERT OR REPLACE INTO posts (message_id, guild_id, channel_id, feature, kind, ref_id, created_at) VALUES (?,?,?,?,?,?,?)",
        msg.id, guild.id, channel.id, feature, kind, ref_id, time.time(),
    )
    return msg


def embed_from_payload(data: dict | None) -> discord.Embed | None:
    return discord.Embed.from_dict(data) if data else None


def dumps(obj) -> str:
    return json.dumps(obj, default=str)


async def run_once(bot: "TradingBot", guild_id: int, key: str, day: str) -> bool:
    """True the first time `key` runs on `day` (persists across restarts)."""
    k = f"ran:{key}"
    if bot.db.get_setting(guild_id, k) == day:
        return False
    await bot.db.set_setting(guild_id, k, day)
    return True
