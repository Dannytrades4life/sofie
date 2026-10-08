"""XP, levels, level roles, /rank and /leaderboard.

Activity counts are buffered in memory and flushed every 30s, so busy servers
don't write to the database on every message.
"""
from __future__ import annotations

import random
import time
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..control import active, feature_on
from ..util import GOLD, LEVEL_ROLES, get_channel, today

XP_MIN, XP_MAX, XP_COOLDOWN = 15, 25, 60


def xp_for_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level


def total_xp_for(level: int) -> int:
    return sum(xp_for_level(i) for i in range(level))


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.activity: Counter[tuple[int, int, str]] = Counter()
        self.last_xp: dict[tuple[int, int], float] = {}
        self.flush.start()

    async def cog_unload(self):
        self.flush.cancel()
        await self._flush()

    async def _flush(self):
        if not self.activity:
            return
        rows = [(g, u, d, n) for (g, u, d), n in self.activity.items()]
        self.activity.clear()
        await self.bot.db.add_activity(rows)

    @tasks.loop(seconds=30)
    async def flush(self):
        await self._flush()

    @commands.Cog.listener()
    async def on_clean_message(self, message: discord.Message):
        g, u = message.guild.id, message.author.id
        self.activity[(g, u, today(self.bot))] += 1
        if not feature_on(self.bot, g, "levels"):
            return
        now = time.time()
        if now - self.last_xp.get((g, u), 0) < XP_COOLDOWN:
            return
        self.last_xp[(g, u)] = now
        gain = random.randint(XP_MIN, XP_MAX)
        await self.bot.db.execute(
            "INSERT INTO members (guild_id, user_id, xp, last_xp_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp, last_xp_at = excluded.last_xp_at",
            g, u, gain, now,
        )
        row = await self.bot.db.fetchone("SELECT xp, level FROM members WHERE guild_id = ? AND user_id = ?", g, u)
        new_level = level_from_xp(row["xp"])
        if new_level > row["level"]:
            await self.bot.db.execute("UPDATE members SET level = ? WHERE guild_id = ? AND user_id = ?", new_level, g, u)
            if active(self.bot, g, "levels"):
                await self.level_up(message.author, new_level)

    async def add_xp(self, member: discord.Member, amount: int) -> None:
        """Bonus XP (shared trades, journal entries, challenge wins)."""
        g, u = member.guild.id, member.id
        await self.bot.db.execute(
            "INSERT INTO members (guild_id, user_id, xp) VALUES (?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp",
            g, u, amount)
        row = await self.bot.db.fetchone("SELECT xp, level FROM members WHERE guild_id = ? AND user_id = ?", g, u)
        new_level = level_from_xp(row["xp"])
        if new_level > row["level"]:
            await self.bot.db.execute("UPDATE members SET level = ? WHERE guild_id = ? AND user_id = ?", new_level, g, u)
            if active(self.bot, g, "levels"):
                await self.level_up(member, new_level)

    async def level_up(self, member: discord.Member, level: int):
        reward = None
        reached = [lv for lv in LEVEL_ROLES if lv <= level]
        if reached:
            role = discord.utils.get(member.guild.roles, name=LEVEL_ROLES[max(reached)])
            if role:
                try:
                    if role not in member.roles:
                        await member.add_roles(role, reason=f"Reached level {level}")
                        reward = role
                except discord.HTTPException:
                    pass
        ch = get_channel(self.bot, member.guild, "level_ups")
        if ch:
            text = f"⭐ {member.mention} just hit **level {level}**!"
            if reward:
                text += f" Unlocked the **{reward.name}** role 🎉"
            await ch.send(text)

    @app_commands.command(name="rank", description="See your level and XP")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        await self._flush()
        row = await self.bot.db.fetchone("SELECT xp, level FROM members WHERE guild_id = ? AND user_id = ?", interaction.guild.id, member.id)
        xp, level = (row["xp"], row["level"]) if row else (0, 0)
        pos = await self.bot.db.fetchone("SELECT COUNT(*) + 1 AS n FROM members WHERE guild_id = ? AND xp > ?", interaction.guild.id, xp)
        into = xp - total_xp_for(level)
        need = xp_for_level(level)
        bar = "▰" * int(10 * into / need) + "▱" * (10 - int(10 * into / need))
        embed = discord.Embed(title=f"{member.display_name}", color=GOLD)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="Rank", value=f"#{pos['n']}")
        embed.add_field(name="XP", value=f"{into}/{need}\n{bar}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Top members by XP")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT user_id, xp, level FROM members WHERE guild_id = ? ORDER BY xp DESC LIMIT 10", interaction.guild.id)
        if not rows:
            await interaction.response.send_message("No one's on the board yet. Start chatting!")
            return
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = [f"{medals[i]} <@{r['user_id']}> · level {r['level']} · {r['xp']:,} XP" for i, r in enumerate(rows)]
        embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=GOLD)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot) -> None:
    await bot.add_cog(Levels(bot))
