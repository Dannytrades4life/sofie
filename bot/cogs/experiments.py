"""Creative autonomy: every Monday the bot proposes 3 new engagement ideas of its own,
schedules them through the week, measures how each did after 24 hours (reactions,
replies, thread messages, poll votes), and the weekly report ranks them. Past results
feed the next week's ideas, so it learns what this community likes.
All experiments go through publish(): kill switch, feature switch, your rulebook, approval mode.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..config import MARKET_TZ
from ..control import active, feature_mode, owner_only, persona, publish, run_once
from ..safety import log_action
from ..util import CHANNELS, brand_color

ALLOWED_CHANNELS = ["general", "chart_talk", "memes", "journal", "lessons", "setups", "member_trades"]
FALLBACK_IDEAS = [
    {"title": "Guess the close", "type": "thread", "channel": "general", "day": 1, "hour": 12,
     "content": "guess game 🎯 where does NQ close today? closest guess gets bragging rights. drop your number in the thread"},
    {"title": "Rate my chart Friday", "type": "thread", "channel": "chart_talk", "day": 4, "hour": 17,
     "content": "rate-my-chart friday 📊 post one chart from this week, everyone else rates it 1-10 with one tip"},
    {"title": "One-word week", "type": "poll", "channel": "general", "day": 3, "hour": 13,
     "content": "Describe your trading week in one word", "options": ["Patient", "Chaotic", "Disciplined", "Tilted"]},
]


def week_key(now: datetime) -> str:
    return f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"


class Experiments(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduler.start()

    def cog_unload(self):
        self.scheduler.cancel()

    async def history(self, guild_id: int) -> list[dict]:
        rows = await self.bot.db.fetchall("SELECT idea, score, status FROM experiments WHERE guild_id=? AND status='measured' ORDER BY id DESC LIMIT 15", guild_id)
        return [{"title": json.loads(r["idea"])["title"], "type": json.loads(r["idea"])["type"], "score": r["score"]} for r in rows]

    async def propose(self, guild: discord.Guild) -> list[dict]:
        hist = await self.history(guild.id)
        data = await self.bot.llm.json(
            persona(self.bot, guild) + " Right now you're planning experiments to grow engagement in your futures trading server.",
            "Propose 3 NEW engagement experiments for this week that a typical admin wouldn't think of. Keep them honest (no fake "
            "results, no giveaways, no money), safe, and runnable as a single bot post.\n"
            f"Past experiments and scores (higher = more engagement): {json.dumps(hist)}\n"
            f"Allowed channels: {ALLOWED_CHANNELS}. Types: post (a message), thread (a message with a discussion thread), poll (2-5 options).\n"
            'Format: {"ideas": [{"title": "...", "why": "one line", "type": "post|thread|poll", "channel": "...", "day": 0-6 (Mon=0), '
            '"hour": 9-21 (New York), "content": "the exact message or poll question", "options": ["only for polls"]}]}',
            max_tokens=900, temperature=0.95)
        ideas = data.get("ideas") if isinstance(data, dict) else None
        valid = []
        for i in ideas or []:
            if isinstance(i, dict) and i.get("type") in ("post", "thread", "poll") and i.get("channel") in ALLOWED_CHANNELS and i.get("content"):
                i["day"] = int(i.get("day", 2)) % 7
                i["hour"] = min(max(int(i.get("hour", 12)), 9), 21)
                if i["type"] == "poll" and not (isinstance(i.get("options"), list) and 2 <= len(i["options"]) <= 5):
                    continue
                valid.append(i)
        return valid[:3] or FALLBACK_IDEAS

    async def plan_week(self, guild: discord.Guild) -> list[dict]:
        now = datetime.now(MARKET_TZ)
        monday = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time(), MARKET_TZ)
        ideas = await self.propose(guild)
        for idea in ideas:
            run_at = monday + timedelta(days=idea["day"], hours=idea["hour"])
            if run_at < now:
                run_at = now + timedelta(hours=1)
            await self.bot.db.execute("INSERT INTO experiments (guild_id, week, idea, run_at, created_at) VALUES (?,?,?,?,?)",
                                      guild.id, week_key(now), json.dumps(idea), run_at.timestamp(), time.time())
        await log_action(self.bot, guild, "experiments", "planned: " + "; ".join(i["title"] for i in ideas))
        return ideas

    async def run(self, guild: discord.Guild, row) -> None:
        idea = json.loads(row["idea"])
        ch_key = idea["channel"] if idea["channel"] in CHANNELS else "general"
        msg = None
        if idea["type"] == "poll" and feature_mode(self.bot, guild.id, "experiments") == "ask":
            idea = {**idea, "type": "post", "content": idea["content"] + "\n" + "\n".join(f"• {o}" for o in idea["options"])}
        if idea["type"] == "poll":
            from ..util import get_channel
            ch = get_channel(self.bot, guild, ch_key)
            from ..control import rules_allow
            ok, _ = await rules_allow(self.bot, guild, f"Post a poll in #{ch.name if ch else ch_key}: {idea['content']}")
            if ch and ok and active(self.bot, guild.id, "experiments"):
                poll = discord.Poll(question=idea["content"][:300], duration=timedelta(hours=24))
                for o in idea["options"][:5]:
                    poll.add_answer(text=str(o)[:55])
                msg = await ch.send(poll=poll)
        else:
            msg = await publish(self.bot, guild, "experiments", ch_key, content=idea["content"][:1900], kind="experiment",
                                thread_name=idea["title"][:90] if idea["type"] == "thread" else None, describe=f"experiment '{idea['title']}': {idea['content']}")
        await self.bot.db.execute("UPDATE experiments SET status=?, message_id=? WHERE id=?", "ran" if msg else "skipped", msg.id if msg else None, row["id"])
        if msg:
            await self.bot.db.execute("UPDATE experiments SET idea=? WHERE id=?", json.dumps({**idea, "channel_id": msg.channel.id}), row["id"])

    async def measure(self, guild: discord.Guild, row) -> None:
        idea = json.loads(row["idea"])
        ch = guild.get_channel(idea.get("channel_id") or 0)
        score = 0.0
        if ch:
            try:
                msg = await ch.fetch_message(row["message_id"])
                score += sum(r.count for r in msg.reactions)
                if msg.poll:
                    score += sum(a.vote_count for a in msg.poll.answers)
                thread = msg.thread or guild.get_thread(msg.id)
                if thread:
                    score += 2 * (thread.message_count or 0)
                async for m in ch.history(after=msg, limit=100):
                    if m.reference and m.reference.message_id == msg.id:
                        score += 2
            except discord.HTTPException:
                pass
        await self.bot.db.execute("UPDATE experiments SET status='measured', score=? WHERE id=?", score, row["id"])

    async def weekly_results(self, guild_id: int, week: str) -> list[dict]:
        rows = await self.bot.db.fetchall("SELECT idea, status, score FROM experiments WHERE guild_id=? AND week=? ORDER BY score DESC", guild_id, week)
        return [{"title": json.loads(r["idea"])["title"], "why": json.loads(r["idea"]).get("why", ""), "status": r["status"], "score": r["score"]} for r in rows]

    @tasks.loop(minutes=5)
    async def scheduler(self):
        now = datetime.now(MARKET_TZ)
        for guild in self.bot.guilds:
            if not active(self.bot, guild.id, "experiments"):
                continue
            if now.weekday() == 0 and now.hour >= 8 and await run_once(self.bot, guild.id, "experiments_plan", now.strftime("%Y-%m-%d")):
                await self.plan_week(guild)
            for row in await self.bot.db.fetchall("SELECT * FROM experiments WHERE guild_id=? AND status='planned' AND run_at<=?", guild.id, now.timestamp()):
                await self.run(guild, row)
            for row in await self.bot.db.fetchall("SELECT * FROM experiments WHERE guild_id=? AND status='ran' AND run_at<=?", guild.id, now.timestamp() - 86400):
                await self.measure(guild, row)

    @scheduler.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="experiments", description="This week's self-proposed experiments and how they did")
    @app_commands.guild_only()
    @owner_only()
    async def show(self, interaction: discord.Interaction, plan_now: bool = False):
        await interaction.response.defer(ephemeral=True)
        if plan_now:
            await self.plan_week(interaction.guild)
        res = await self.weekly_results(interaction.guild.id, week_key(datetime.now(MARKET_TZ)))
        e = discord.Embed(title="🧪 This week's experiments", color=brand_color(self.bot, interaction.guild))
        e.description = "\n".join(f"**{r['title']}** · {r['status']}" + (f" · score {r['score']:.0f}" if r["score"] is not None else "") +
                                  (f"\n  _{r['why']}_" if r["why"] else "") for r in res) or "None planned yet. They're planned Monday mornings, or use plan_now."
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Experiments(bot))
