"""Daily and weekly DM reports to the owner: what the bot did, what it achieved, what it suggests."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..control import FEATURES, feature_on, owner_only, persona, run_once
from ..futures import fmt_usd
from ..safety import dm_owner
from ..util import brand_color, local_now

LABELS = {
    "trade_posts": "trade posts", "trade_closed": "trades closed", "welcomes": "members welcomed", "moderation": "moderation actions",
    "daily_question": "questions/polls", "quiet_starters": "conversation starters", "lessons": "lessons/setups", "gifs": "GIFs",
    "premarket": "premarket plans", "calendar": "calendar posts/alerts", "recap": "recaps", "challenges": "challenge posts",
    "experiments": "experiments", "giveaways": "giveaway events", "invites": "invite milestones", "social": "social idea batches",
    "edit": "edits", "blocked_by_rule": "posts held back by your rules", "raid_mode": "raid alerts", "approval_requested": "approvals asked",
    "rebuild": "rebuild steps", "rulebook": "rule changes",
}


def pct(now: float, before: float) -> str:
    if before == 0:
        return "new" if now else "—"
    return f"{(now - before) / before * 100:+.0f}%"


class Reports(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler.start()

    def cog_unload(self):
        self.scheduler.cancel()

    async def stats(self, guild: discord.Guild, end: datetime, days: int) -> dict:
        db = self.bot.db
        start = end - timedelta(days=days)
        d0, d1, t0, t1 = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), start.timestamp(), end.timestamp()
        jl = await db.fetchone("SELECT COALESCE(SUM(joins),0) j, COALESCE(SUM(leaves),0) l FROM daily_stats WHERE guild_id=? AND day>? AND day<=?", guild.id, d0, d1)
        act = await db.fetchone("SELECT COALESCE(SUM(messages),0) m, COUNT(DISTINCT user_id) u FROM member_activity WHERE guild_id=? AND day>? AND day<=?", guild.id, d0, d1)
        top = await db.fetchall("SELECT user_id, SUM(messages) m FROM member_activity WHERE guild_id=? AND day>? AND day<=? GROUP BY user_id ORDER BY m DESC LIMIT 3", guild.id, d0, d1)
        actions = await db.fetchall("SELECT kind, COUNT(*) n FROM actions WHERE guild_id=? AND created_at>? AND created_at<=? GROUP BY kind", guild.id, t0, t1)
        closed = [json.loads(r["data"]) for r in await db.fetchall("SELECT data FROM trades WHERE guild_id=? AND status='closed' AND closed_at>? AND closed_at<=?", guild.id, t0, t1)]
        member_trades = await db.fetchone("SELECT COUNT(*) n FROM member_trades WHERE guild_id=? AND created_at>? AND created_at<=?", guild.id, t0, t1)
        gw = await db.fetchone("SELECT COUNT(*) n FROM giveaway_entries e JOIN giveaways g ON g.id=e.giveaway_id WHERE g.guild_id=? AND e.entered_at>? AND e.entered_at<=?", guild.id, t0, t1)
        inv = await db.fetchone("SELECT COUNT(*) n FROM invites WHERE guild_id=? AND joined_at>? AND joined_at<=? AND inviter_id IS NOT NULL", guild.id, t0, t1)
        growth = self.bot.get_cog("Growth")
        wins = sum(1 for t in closed if (t.get("pnl_usd") or t.get("points") or 0) > 0)
        rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
        usd = [t["pnl_usd"] for t in closed if t.get("pnl_usd") is not None]
        return {
            "joins": jl["j"], "leaves": jl["l"], "messages": act["m"], "active_members": act["u"],
            "top_chatters": [(r["user_id"], r["m"]) for r in top], "actions": {r["kind"]: r["n"] for r in actions},
            "trades_closed": len(closed), "wins": wins, "win_rate": round(100 * wins / len(closed)) if closed else None,
            "total_r": round(sum(rs), 2) if rs else None, "net_usd": round(sum(usd), 2) if usd else None,
            "member_trades": member_trades["n"], "giveaway_entries": gw["n"], "invited_joins": inv["n"],
            "joins_by_platform": await growth.platform_joins(guild.id, t0) if growth else {},
        }

    async def suggestions(self, guild: discord.Guild, s: dict, prev: dict, experiments: list[dict]) -> list[str]:
        data = await self.bot.llm.json(
            persona(self.bot, guild) + " Right now you're privately advising the server owner.",
            "Given this period's stats, the previous period's, and this week's experiment results, give 3 short, concrete, specific "
            f'suggestions for what to do next. Format: {{"suggestions": ["...", "...", "..."]}}\nNow: {json.dumps(s, default=str)}\n'
            f"Before: {json.dumps(prev, default=str)}\nExperiments: {json.dumps(experiments)}")
        if isinstance(data, dict) and data.get("suggestions"):
            return [str(x)[:220] for x in data["suggestions"][:3]]
        out = []
        if s["messages"] < prev["messages"]:
            out.append("Chat dropped vs. last period. Try `/spark poll` around lunch, when people are sitting out the chop.")
        if s["joins"] == 0:
            out.append("No new members. `/social x` gives you posts with a tracked invite link.")
        if s["trades_closed"] == 0:
            out.append("No closed trades posted. Even one honest result a day keeps the trading channels alive.")
        return out or ["Steady period. Keep the daily rhythm going."]

    async def build(self, guild: discord.Guild, days: int) -> discord.Embed:
        end = local_now(self.bot)
        s = await self.stats(guild, end, days)
        prev = await self.stats(guild, end - timedelta(days=days), days)
        e = discord.Embed(title=f"{'📅 Daily' if days == 1 else '📊 Weekly'} report · {guild.name}", color=brand_color(self.bot, guild),
                          timestamp=discord.utils.utcnow())
        if self.bot.paused:
            e.description = "⏸️ **I'm paused.** DM me `resume` to turn autonomous actions back on."
        e.add_field(name="👥 Members", value=f"**{guild.member_count:,}** total\n+{s['joins']} / -{s['leaves']} (net {s['joins'] - s['leaves']:+})"
                                            + (f"\nvia invites: {s['invited_joins']}" if s["invited_joins"] else ""))
        e.add_field(name="💬 Engagement", value=f"**{s['messages']:,}** messages ({pct(s['messages'], prev['messages'])})\n**{s['active_members']}** active "
                                               f"({pct(s['active_members'], prev['active_members'])})\n{s['member_trades']} member trades shared")
        t = f"{s['trades_closed']} closed"
        if s["win_rate"] is not None:
            t += f" · {s['win_rate']}% wins"
        if s["total_r"] is not None:
            t += f" · {s['total_r']:+}R"
        if s["net_usd"] is not None:
            t += f" · {fmt_usd(s['net_usd'])}"
        e.add_field(name="📈 Your trades", value=t)
        acts = Counter(s["actions"])
        e.add_field(name="🤖 What I did", value="\n".join(f"• {n} {LABELS.get(k, k)}" for k, n in acts.most_common(10)) or "Nothing this period.", inline=False)
        if s["joins_by_platform"]:
            e.add_field(name="📣 Joins by platform link", value=", ".join(f"{p}: {n}" for p, n in s["joins_by_platform"].items()), inline=False)
        if s["giveaway_entries"]:
            e.add_field(name="🎁 Giveaways", value=f"{s['giveaway_entries']} entries")
        exps = []
        if days >= 7 and self.bot.get_cog("Experiments"):
            from .experiments import week_key
            exps = await self.bot.get_cog("Experiments").weekly_results(guild.id, week_key(end))
            if exps:
                e.add_field(name="🧪 My experiments (best first)", value="\n".join(
                    f"{i + 1}. **{x['title']}** · {x['status']}" + (f" · score {x['score']:.0f}" if x["score"] is not None else "") for i, x in enumerate(exps))[:1024], inline=False)
        if s["top_chatters"]:
            e.add_field(name="🔥 Most active", value=", ".join(f"<@{u}> ({m})" for u, m in s["top_chatters"]), inline=False)
        e.add_field(name="💡 Suggestions", value="\n".join(f"• {x}" for x in await self.suggestions(guild, s, prev, exps))[:1024], inline=False)
        pending = await self.bot.db.fetchone("SELECT COUNT(*) n FROM approvals WHERE guild_id=? AND status='pending'", guild.id)
        off = [k for k in FEATURES if not feature_on(self.bot, guild.id, k)]
        notes = []
        if pending["n"]:
            notes.append(f"{pending['n']} approval request(s) waiting in our DMs")
        if off:
            notes.append("Switched off: " + ", ".join(off))
        if notes:
            e.add_field(name="⏳ Heads up", value="\n".join(notes), inline=False)
        return e

    @tasks.loop(minutes=5)
    async def scheduler(self):
        now = local_now(self.bot)
        if now.hour < self.bot.config.report_hour:
            return
        day = now.strftime("%Y-%m-%d")
        for guild in self.bot.guilds:
            if await run_once(self.bot, guild.id, "report_daily", day):
                levels = self.bot.get_cog("Levels")
                if levels:
                    await levels._flush()
                await dm_owner(self.bot, embed=await self.build(guild, 1))
                if now.weekday() == 6:
                    await dm_owner(self.bot, embed=await self.build(guild, 7))

    @scheduler.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="report", description="Get a report by DM now")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.choices(period=[app_commands.Choice(name="daily", value=1), app_commands.Choice(name="weekly", value=7)])
    async def report_cmd(self, interaction: discord.Interaction, period: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        levels = self.bot.get_cog("Levels")
        if levels:
            await levels._flush()
        msg = await dm_owner(self.bot, embed=await self.build(interaction.guild, period.value))
        await interaction.followup.send("Sent to your DMs." if msg else "I can't DM you. Allow DMs from server members.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Reports(bot))
