"""Automatic moderation: scams, spam, invite links, mention spam, raids.

Routine responses (delete, timeout, slowmode) happen automatically.
Bans and server lockdown are sent to the owner for approval.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from ..safety import log_action, request_approval
from ..control import active, staff_level
from ..util import get_channel

SCAM_PATTERNS = [
    r"free\s*nitro", r"discord\s*nitro\s*(gift|free)", r"steam\s*gift", r"airdrop.*(claim|connect)",
    r"(dm|message|inbox)\s+me\s+(for|to)\s+(signals?|profits?|invest|earn|trading)",
    r"(account|investment|portfolio)\s+manager", r"(guaranteed|daily)\s+(profit|returns?)",
    r"double\s+your\s+(money|btc|crypto)", r"recover\s+(your\s+)?(lost|stolen)\s+(funds|crypto)",
    r"earn\s+\$?\d[\d,]*\s*(k|usd|\$)?\s*(daily|weekly|in\s+\d+\s*(hours?|days?))",
    r"connect\s+(your\s+)?wallet", r"seed\s*phrase", r"whats\s*app\s*\+?\d",
]
SCAM_DOMAINS = r"(disc[o0]rd[-.]?(gift|nitro|app)\.(?!com\b)\w+|dlscord|discrod|disocrd|steamcommunlty|steamcomunity|t\.me/)"
SCAM_RE = re.compile("|".join(SCAM_PATTERNS) + "|" + SCAM_DOMAINS, re.I)
INVITE_RE = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\w+", re.I)

FLOOD_COUNT, FLOOD_WINDOW = 6, 8          # 6 messages in 8 seconds
DUPE_CHANNELS, DUPE_WINDOW = 3, 60        # same text in 3 channels within 60s
MAX_MENTIONS = 5
RAID_JOINS, RAID_WINDOW = 10, 60          # 10 joins in 60s
RAID_MODE_SECONDS = 15 * 60


def is_scam(text: str) -> bool:
    return bool(SCAM_RE.search(text))


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recent: dict[tuple[int, int], deque[float]] = defaultdict(lambda: deque(maxlen=20))
        self.dupes: dict[tuple[int, int], deque[tuple[float, str, discord.Message]]] = defaultdict(lambda: deque(maxlen=10))
        self.joins: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=100))
        self.raid_until: dict[int, float] = {}
        self.raid_check.start()

    def cog_unload(self) -> None:
        self.raid_check.cancel()

    def in_raid_mode(self, guild_id: int) -> bool:
        return self.raid_until.get(guild_id, 0) > time.time()

    # ---- helpers ------------------------------------------------------------
    async def _punish(self, message: discord.Message, reason: str, minutes: int, *, ask_ban: bool = False, extra: list[discord.Message] | None = None) -> None:
        guild, member = message.guild, message.author
        for m in [message, *(extra or [])]:
            try:
                await m.delete()
            except discord.HTTPException:
                pass
        if minutes and isinstance(member, discord.Member):
            try:
                await member.timeout(timedelta(minutes=minutes), reason=reason)
            except discord.HTTPException:
                pass
        detail = f"{member} ({member.id}) in #{message.channel}: {reason}. Timed out {minutes}m."
        await log_action(self.bot, guild, "moderation", detail)
        mod_log = get_channel(self.bot, guild, "mod_log")
        if mod_log:
            embed = discord.Embed(title="🛡️ Auto-mod", description=detail, color=0xE67E22)
            embed.add_field(name="Message", value=(message.content or "(no text)")[:1000], inline=False)
            await mod_log.send(embed=embed)
        if ask_ban:
            await request_approval(
                self.bot, guild, "ban",
                f"Ban **{member}** (`{member.id}`)?\nReason: {reason}\nMessage: {message.content[:500]!r}\n"
                "They're already timed out and the message is deleted.",
                {"user_id": member.id, "reason": reason},
            )

    async def _warn(self, channel: discord.abc.Messageable, member: discord.Member, text: str) -> None:
        try:
            await channel.send(f"{member.mention} {text}", delete_after=10)
        except discord.HTTPException:
            pass

    # ---- called by the bot for every guild message ------------------------------
    async def inspect(self, message: discord.Message) -> bool:
        """Return True if the message was removed and should be ignored by other features."""
        if not active(self.bot, message.guild.id, "moderation") or not isinstance(message.author, discord.Member):
            return False
        if staff_level(self.bot, message.author) or message.author.guild_permissions.manage_messages:
            return False
        text = message.content or ""
        key = (message.guild.id, message.author.id)
        now = time.time()

        if is_scam(text):
            await self._punish(message, "scam/phishing pattern", 24 * 60, ask_ban=True)
            return True

        if "@everyone" in text or "@here" in text or len(message.raw_mentions) > MAX_MENTIONS:
            await self._punish(message, "mass mentions", 10)
            return True

        if INVITE_RE.search(text):
            await self._punish(message, "posted an invite link", 0)
            await self._warn(message.channel, message.author, "no server invites here please 🙏")
            return True

        # Same text in several channels quickly = classic compromised-account spam.
        if len(text) > 15:
            d = self.dupes[key]
            d.append((now, text.lower(), message))
            same = [m for t, c, m in d if now - t < DUPE_WINDOW and c == text.lower()]
            if len({m.channel.id for m in same}) >= DUPE_CHANNELS:
                await self._punish(message, "cross-channel spam (possible hacked account)", 60, ask_ban=True, extra=same[:-1])
                d.clear()
                return True

        r = self.recent[key]
        r.append(now)
        if len([t for t in r if now - t < FLOOD_WINDOW]) >= FLOOD_COUNT:
            r.clear()
            await self._punish(message, "message flooding", 10)
            await self._warn(message.channel, message.author, "slow down a little, you're on a 10 minute timeout")
            return True
        return False

    # ---- raids ----------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not active(self.bot, member.guild.id, "moderation"):
            return
        now = time.time()
        j = self.joins[member.guild.id]
        j.append(now)
        recent = [t for t in j if now - t < RAID_WINDOW]
        if len(recent) >= RAID_JOINS and not self.in_raid_mode(member.guild.id):
            await self.start_raid_mode(member.guild, len(recent))

    async def start_raid_mode(self, guild: discord.Guild, count: int) -> None:
        self.raid_until[guild.id] = time.time() + RAID_MODE_SECONDS
        general = get_channel(self.bot, guild, "general")
        if general:
            try:
                await general.edit(slowmode_delay=30, reason="Raid mode")
            except discord.HTTPException:
                pass
        await log_action(self.bot, guild, "raid_mode", f"{count} joins in {RAID_WINDOW}s. Slowmode on, welcomes paused for 15 min.")
        await request_approval(
            self.bot, guild, "lockdown",
            f"⚠️ Possible raid: **{count} joins in {RAID_WINDOW}s**. I turned on slowmode and paused welcomes.\n"
            "Approve to raise the server verification level to High (new accounts must have a verified phone).",
            {},
        )

    @tasks.loop(seconds=60)
    async def raid_check(self) -> None:
        now = time.time()
        for gid, until in list(self.raid_until.items()):
            if until <= now:
                del self.raid_until[gid]
                guild = self.bot.get_guild(gid)
                general = guild and get_channel(self.bot, guild, "general")
                if general:
                    try:
                        await general.edit(slowmode_delay=0, reason="Raid mode ended")
                    except discord.HTTPException:
                        pass
                if guild:
                    await log_action(self.bot, guild, "raid_mode", "Raid mode ended, slowmode off.")

    @raid_check.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Moderation(bot))
