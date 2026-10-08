"""Member trade sharing, the trade-journal channel, and weekly challenges.

Member trades are self-reported and labelled that way; a screenshot is required.
Challenges rotate weekly: best single R-multiple, most green days, longest journal streak.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..config import MARKET_TZ
from ..control import active, publish, run_once, staff
from ..futures import fmt_pct, fmt_price, fmt_pts
from ..safety import log_action
from ..util import CHAMP_ROLE, GOLD, GREEN, RED, brand_color, find_role, get_channel, today

CHALLENGES = {
    "best_r": ("🎯 Best R-multiple", "Share your best trade of the week with `/share-trade` (stop and screenshot required). Highest single R wins."),
    "green_days": ("🟢 Most green days", "Share your trades with `/share-trade`. The most days that finish green (net positive R) wins."),
    "journal": ("📓 Journal streak", "Write a real journal entry in the trade-journal channel each day. Most days journaled wins."),
}
ORDER = list(CHALLENGES)
SHARE_XP, JOURNAL_XP, CHAMP_XP = 40, 25, 200


def week_start(now: datetime) -> datetime:
    return datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time(), MARKET_TZ)


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler.start()

    def cog_unload(self):
        self.scheduler.cancel()

    async def bonus_xp(self, member: discord.Member, amount: int, key: str) -> None:
        if await run_once(self.bot, member.guild.id, f"xp:{key}:{member.id}", today(self.bot)):
            levels = self.bot.get_cog("Levels")
            if levels:
                await levels.add_xp(member, amount)

    # ---- member trades ----
    @app_commands.command(name="share-trade", description="Share one of your trades (self-reported, screenshot required)")
    @app_commands.guild_only()
    @app_commands.choices(side=[app_commands.Choice(name="Long", value="long"), app_commands.Choice(name="Short", value="short")])
    async def share_trade(self, interaction: discord.Interaction, symbol: str, side: app_commands.Choice[str], entry: float, exit: float,
                          screenshot: discord.Attachment, contracts: app_commands.Range[int, 1, 500] = 1, stop: float | None = None,
                          notes: app_commands.Range[str, 0, 300] | None = None):
        g = interaction.guild
        if not active(self.bot, g.id, "member_trades"):
            await interaction.response.send_message("Trade sharing is switched off right now.", ephemeral=True)
            return
        if not (screenshot.content_type or "").startswith("image/"):
            await interaction.response.send_message("The screenshot needs to be an image.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        trades = self.bot.get_cog("Trades")
        d = await trades.normalize(g.id, {"contract": symbol, "side": side.value, "entry": entry, "exit": exit, "stop": stop, "contracts": contracts})
        res = "win" if (d.get("points") or 0) > 0 else "loss" if (d.get("points") or 0) < 0 else "flat"
        e = discord.Embed(title=f"{'✅' if res == 'win' else '❌' if res == 'loss' else '➖'} {d['contract']} {side.value.upper()} · {fmt_pts(d.get('points'))}",
                          description=notes or None, color=GREEN if res == "win" else RED if res == "loss" else GOLD)
        e.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        e.add_field(name="Entry → Exit", value=f"`{fmt_price(entry)} → {fmt_price(exit)}`")
        e.add_field(name="Points / Ticks", value=f"`{d.get('points', '—')} / {d.get('ticks', '—')}`")
        e.add_field(name="Price move", value=f"`{fmt_pct(d.get('pnl_pct'))}`")
        if d.get("r_multiple") is not None:
            e.add_field(name="R", value=f"`{d['r_multiple']:+.2f}R`")
        e.set_footer(text="Self-reported by a member. Not verified. Not financial advice.")
        f = await screenshot.to_file()
        e.set_image(url=f"attachment://{f.filename}")
        ch = get_channel(self.bot, g, "member_trades") or interaction.channel
        msg = await ch.send(embed=e, file=f)
        await self.bot.db.execute("INSERT INTO member_trades (guild_id, user_id, data, message_id, created_at) VALUES (?,?,?,?,?)",
                                  g.id, interaction.user.id, json.dumps(d), msg.id, time.time())
        await self.bonus_xp(interaction.user, SHARE_XP, "share")
        await interaction.followup.send(f"Shared: {msg.jump_url}", ephemeral=True)

    @commands.Cog.listener()
    async def on_clean_message(self, message: discord.Message):
        ch = get_channel(self.bot, message.guild, "journal")
        if not ch or message.channel.id != ch.id or len(message.content) < 80:
            return
        day = today(self.bot)
        await self.bot.db.execute("INSERT OR IGNORE INTO journal_entries (guild_id, user_id, day, message_id) VALUES (?,?,?,?)",
                                  message.guild.id, message.author.id, day, message.id)
        if active(self.bot, message.guild.id, "member_trades"):
            try:
                await message.add_reaction("📓")
            except discord.HTTPException:
                pass
            await self.bonus_xp(message.author, JOURNAL_XP, "journal")

    # ---- challenges ----
    def current(self, guild_id: int) -> str:
        return self.bot.db.get_setting(guild_id, "challenge", ORDER[0])

    async def standings(self, guild: discord.Guild, kind: str, since: float) -> list[tuple[int, float, str]]:
        dq = set(self.bot.db.get_json(guild.id, "challenge_dq", []) or [])
        rows: list[tuple[int, float, str]] = []
        if kind == "journal":
            day0 = datetime.fromtimestamp(since, MARKET_TZ).strftime("%Y-%m-%d")
            for r in await self.bot.db.fetchall("SELECT user_id, COUNT(*) n FROM journal_entries WHERE guild_id=? AND day>=? GROUP BY user_id", guild.id, day0):
                rows.append((r["user_id"], r["n"], f"{r['n']} day(s)"))
        else:
            per_user: dict[int, list[dict]] = {}
            for r in await self.bot.db.fetchall("SELECT user_id, data, created_at FROM member_trades WHERE guild_id=? AND created_at>=?", guild.id, since):
                per_user.setdefault(r["user_id"], []).append({**json.loads(r["data"]), "_t": r["created_at"]})
            for uid, ts in per_user.items():
                if kind == "best_r":
                    rs = [t["r_multiple"] for t in ts if t.get("r_multiple") is not None]
                    if rs:
                        rows.append((uid, max(rs), f"{max(rs):+.2f}R"))
                else:
                    days: dict[str, float] = {}
                    for t in ts:
                        d = datetime.fromtimestamp(t["_t"], MARKET_TZ).strftime("%Y-%m-%d")
                        days[d] = days.get(d, 0) + (t.get("r_multiple") if t.get("r_multiple") is not None else (t.get("points") or 0))
                    green = sum(1 for v in days.values() if v > 0)
                    if green:
                        rows.append((uid, green, f"{green} green day(s)"))
        return sorted([r for r in rows if r[0] not in dq], key=lambda r: r[1], reverse=True)

    async def announce(self, guild: discord.Guild, owner: bool = False):
        kind = self.current(guild.id)
        title, rules = CHALLENGES[kind]
        e = discord.Embed(title=f"🏁 This week's challenge: {title}", color=brand_color(self.bot, guild),
                          description=f"{rules}\n\nWinner gets the **{CHAMP_ROLE}** role for a week and {CHAMP_XP} XP. "
                                      "Results are self-reported; fake screenshots get you disqualified.")
        e.set_footer(text="Ends Sunday 6 PM. Not financial advice.")
        return await publish(self.bot, guild, "challenges", "challenges", embed=e, kind="challenge", owner_initiated=owner)

    async def crown(self, guild: discord.Guild):
        now = datetime.now(MARKET_TZ)
        kind = self.current(guild.id)
        board = await self.standings(guild, kind, week_start(now).timestamp())
        role = find_role(guild, CHAMP_ROLE)
        if role:
            for m in list(role.members):
                try:
                    await m.remove_roles(role, reason="Weekly challenge reset")
                except discord.HTTPException:
                    pass
        lines = [f"{['🥇', '🥈', '🥉', '4.', '5.'][i]} <@{u}> · {label}" for i, (u, _, label) in enumerate(board[:5])]
        e = discord.Embed(title=f"🏁 Challenge results: {CHALLENGES[kind][0]}", description="\n".join(lines) or "No entries this week.",
                          color=brand_color(self.bot, guild))
        if board:
            winner = guild.get_member(board[0][0])
            if winner:
                if role:
                    await winner.add_roles(role, reason="Weekly challenge winner")
                levels = self.bot.get_cog("Levels")
                if levels:
                    await levels.add_xp(winner, CHAMP_XP)
            e.add_field(name="Champion", value=f"<@{board[0][0]}> takes the {CHAMP_ROLE} role 🎉")
        await publish(self.bot, guild, "challenges", "challenges", embed=e, kind="challenge_results")
        await self.bot.db.set_setting(guild.id, "challenge", ORDER[(ORDER.index(kind) + 1) % len(ORDER)])
        await self.bot.db.set_json(guild.id, "challenge_dq", [])
        await log_action(self.bot, guild, "challenges", f"{kind} winner: {board[0][0] if board else 'none'}")

    @tasks.loop(minutes=5)
    async def scheduler(self):
        now = datetime.now(MARKET_TZ)
        day = now.strftime("%Y-%m-%d")
        for guild in self.bot.guilds:
            if not active(self.bot, guild.id, "challenges"):
                continue
            if now.weekday() == 0 and now.hour >= 9 and await run_once(self.bot, guild.id, "challenge_start", day):
                await self.announce(guild)
            if now.weekday() == 6 and now.hour >= 18 and await run_once(self.bot, guild.id, "challenge_end", day):
                await self.crown(guild)

    @scheduler.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    challenge = app_commands.Group(name="challenge", description="Weekly challenge", guild_only=True)

    @challenge.command(name="standings", description="Current challenge leaderboard")
    async def standings_cmd(self, interaction: discord.Interaction):
        kind = self.current(interaction.guild.id)
        board = await self.standings(interaction.guild, kind, week_start(datetime.now(MARKET_TZ)).timestamp())
        lines = [f"{i + 1}. <@{u}> · {label}" for i, (u, _, label) in enumerate(board[:10])]
        await interaction.response.send_message(f"**{CHALLENGES[kind][0]}**\n" + ("\n".join(lines) or "No entries yet."),
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @challenge.command(name="dq", description="Disqualify a member from this week's challenge")
    @staff("challenge")
    async def dq(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        dq = set(self.bot.db.get_json(interaction.guild.id, "challenge_dq", []) or [])
        dq.add(member.id)
        await self.bot.db.set_json(interaction.guild.id, "challenge_dq", sorted(dq))
        await log_action(self.bot, interaction.guild, "challenges", f"{member} disqualified by {interaction.user}: {reason}")
        await interaction.response.send_message(f"{member.mention} disqualified this week.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @challenge.command(name="start", description="Announce this week's challenge now")
    @staff("challenge")
    async def start(self, interaction: discord.Interaction):
        msg = await self.announce(interaction.guild, owner=True)
        await interaction.response.send_message(f"Posted: {msg.jump_url}" if msg else "No challenge channel yet.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Community(bot))
