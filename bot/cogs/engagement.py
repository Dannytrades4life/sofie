"""Keeps chat alive: natural replies and answers, daily question, Wednesday poll,
quiet-chat starters, GIFs, daily lessons and setup breakdowns, /poll and /spark."""
from __future__ import annotations

import random
import time
from datetime import timedelta

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

from ..control import active, persona, publish, run_once, staff
from ..util import DISCLAIMER, brand_color, get_channel, local_now, today

QUESTIONS = [
    "what's one rule you broke once and never will again?",
    "what's your max daily loss and do you actually stop when you hit it? 😅",
    "NQ, ES or CL: which one fits your personality and why?",
    "how many trades is too many in a day for you?",
    "do you trade the open or wait for the first 15 minutes to settle?",
    "what's your go-to setup when the market is chopping?",
    "biggest lesson from your worst day?",
    "do you journal every trade? what do you write down?",
    "micros or minis, and when did you size up?",
    "what does your pre-market routine look like?",
]
STARTERS = ["quiet in here 👀 anyone in a trade?", "what's everyone watching into the close?", "how's the tape treating you today?",
            "drop the cleanest chart you've seen this week 📊", "who's done for the day and who's still grinding?"]
POLLS = [("Which market are you trading most this week?", ["NQ / MNQ", "ES / MES", "CL / MCL", "Sitting out"]),
         ("How was your week?", ["Green 🟢", "Red 🔴", "Flat ➖", "Didn't trade"]),
         ("When do you do your best trading?", ["First hour", "Midday", "Last hour", "Overnight"]),
         ("Biggest struggle right now?", ["Entries", "Exits", "Risk / sizing", "Psychology"])]
LESSON_TOPICS = [
    "tick size and tick value: why 1 point on NQ is $20 but on MNQ is $2", "position sizing with a fixed dollar risk per trade",
    "what the R-multiple is and why it beats win rate", "daily loss limits and why pros stop early", "VWAP: what it is and how traders use it",
    "the opening range and why the first 15-30 minutes matters", "prior day high, low and close as reference levels",
    "overnight high/low and the Globex session", "trading around CPI and FOMC: spreads, slippage and staying flat",
    "EIA crude inventories and CL volatility on Wednesdays", "contract rollover and front-month symbols (H, M, U, Z)",
    "trend days vs range days and how to tell early", "stop placement: structure vs fixed ticks", "scaling out vs all-in, all-out",
    "revenge trading and how to break the loop", "journaling: the 5 things to write after every trade",
    "prop firm evaluations: drawdown types (static vs trailing)", "market vs limit vs stop orders in fast markets",
    "correlation between NQ and ES and why it matters for risk", "how to review a losing week without spiraling",
]
SETUPS = ["opening range breakout", "VWAP reclaim / rejection", "failed breakout (trap) at prior day high or low",
          "trend pullback to the 20 EMA", "initial balance break", "overnight high/low sweep and reverse",
          "first pullback after a news spike", "range fade on a choppy day"]
FALLBACK_LESSON = ("**Know your tick value before you click.** NQ moves in 0.25 ticks worth $5 each, so one point is $20 per contract; "
                   "MNQ is a tenth of that. Decide your dollar risk first, divide by (stop in ticks × tick value), and that's your size.")
REPLY_COOLDOWN = 20


class Engagement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.http = httpx.AsyncClient(timeout=15)
        self.last_activity: dict[int, float] = {}
        self.last_reply: dict[int, float] = {}
        self.starters: dict[tuple[int, str], int] = {}
        self.last_starter: dict[int, float] = {}
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()
        await self.http.aclose()

    # ---- conversation ----
    @commands.Cog.listener()
    async def on_clean_message(self, message: discord.Message):
        general = get_channel(self.bot, message.guild, "general")
        if general and message.channel.id == general.id:
            self.last_activity[message.guild.id] = time.time()
        if not active(self.bot, message.guild.id, "chat_replies"):
            return
        addressed = self.bot.user in message.mentions or (
            message.reference and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.bot.user.id)
        if not addressed:
            return
        now = time.time()
        if now - self.last_reply.get(message.channel.id, 0) < REPLY_COOLDOWN:
            return
        self.last_reply[message.channel.id] = now
        history = []
        async for m in message.channel.history(limit=12, before=message):
            history.append(f"{m.author.display_name}: {m.clean_content[:300]}")
        history.reverse()
        async with message.channel.typing():
            text = await self.bot.llm.chat(
                persona(self.bot, message.guild),
                "Recent chat:\n" + "\n".join(history) + f"\n\n{message.author.display_name} said to you: {message.clean_content[:800]}\n\n"
                "Reply like a person in the chat, 1-3 short sentences. If it's a real question about futures (tick values, sessions, "
                "order types, risk, how the server works), answer it clearly. If they want a trade call, share general thinking and "
                "remind them it's not financial advice.",
                max_tokens=220,
            )
        await message.reply(text or random.choice(["haha fair", "facts 💯", "honestly same", "good q, what do you all think?"]), mention_author=False)

    # ---- scheduled content ----
    @tasks.loop(minutes=5)
    async def scheduler(self):
        now = local_now(self.bot)
        day, hm, wd = today(self.bot), (now.hour, now.minute), now.weekday()
        for guild in self.bot.guilds:
            g = guild.id
            try:
                if active(self.bot, g, "daily_question") and hm >= ((12, 30) if wd < 5 else (11, 0)) and await run_once(self.bot, g, "qotd", day):
                    await (self.post_poll(guild) if wd == 2 else self.post_question(guild))
                if active(self.bot, g, "lessons") and wd < 5 and hm >= (17, 30) and await run_once(self.bot, g, "lesson", day):
                    await self.post_lesson(guild)
                if active(self.bot, g, "lessons") and wd in (1, 3) and hm >= (19, 0) and await run_once(self.bot, g, "setup", day):
                    await self.post_setup(guild)
                if active(self.bot, g, "gifs") and self.bot.config.giphy_api_key and hm >= (13, 0) and await run_once(self.bot, g, "gif", day):
                    await self.post_gif(guild, "friday trading" if wd == 4 else None)
                if active(self.bot, g, "quiet_starters"):
                    await self.maybe_starter(guild, now)
            except discord.HTTPException:
                continue

    @scheduler.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    async def post_question(self, guild: discord.Guild, owner: bool = False):
        q = await self.bot.llm.chat(persona(self.bot, guild), "Write one casual discussion question for futures day traders. Under 20 words, no preamble.",
                                    max_tokens=50) or random.choice(QUESTIONS)
        e = discord.Embed(title="💭 Question of the day", description=q, color=brand_color(self.bot, guild))
        return await publish(self.bot, guild, "daily_question", "general", embed=e, kind="question", owner_initiated=owner,
                             thread_name=f"QOTD {local_now(self.bot):%b %d}")

    async def post_poll(self, guild: discord.Guild, owner: bool = False, hours: int = 24):
        data = await self.bot.llm.json(persona(self.bot, guild),
                                       'Create a fun poll for futures day traders. Format: {"question": "...", "options": ["...", "..."]} with 2-5 short options.')
        if isinstance(data, dict) and isinstance(data.get("options"), list) and 2 <= len(data["options"]) <= 10 and data.get("question"):
            question, options = str(data["question"]), [str(o) for o in data["options"]]
        else:
            question, options = random.choice(POLLS)
        ch = get_channel(self.bot, guild, "general")
        if ch is None or (not owner and not active(self.bot, guild.id, "daily_question")):
            return None
        poll = discord.Poll(question=question[:300], duration=timedelta(hours=hours))
        for o in options[:10]:
            poll.add_answer(text=o[:55])
        msg = await ch.send(poll=poll)
        await self.bot.db.execute("INSERT OR REPLACE INTO posts (message_id, guild_id, channel_id, feature, kind, created_at) VALUES (?,?,?,?,?,?)",
                                  msg.id, guild.id, ch.id, "daily_question", "poll", time.time())
        return msg

    async def post_lesson(self, guild: discord.Guild, owner: bool = False):
        i = int(self.bot.db.get_setting(guild.id, "lesson_idx", "0"))
        topic = LESSON_TOPICS[i % len(LESSON_TOPICS)]
        await self.bot.db.set_setting(guild.id, "lesson_idx", i + 1)
        data = await self.bot.llm.json(
            persona(self.bot, guild),
            f"Write a short, accurate daily lesson for futures traders about: {topic}. 120-180 words, plain language, one concrete example "
            'with numbers (use correct CME tick values: ES/MES 0.25=$12.50/$1.25, NQ/MNQ 0.25=$5/$0.50, CL/MCL 0.01=$10/$1). '
            'Format: {"title": "...", "body": "..."}', max_tokens=450)
        title, body = (data.get("title"), data.get("body")) if isinstance(data, dict) else (None, None)
        e = discord.Embed(title=f"🧠 Daily lesson · {title or topic.capitalize()}", description=body or FALLBACK_LESSON, color=brand_color(self.bot, guild))
        e.set_footer(text=DISCLAIMER)
        return await publish(self.bot, guild, "lessons", "lessons", embed=e, kind="lesson", owner_initiated=owner, thread_name="Questions about today's lesson")

    async def post_setup(self, guild: discord.Guild, owner: bool = False):
        i = int(self.bot.db.get_setting(guild.id, "setup_idx", "0"))
        setup = SETUPS[i % len(SETUPS)]
        await self.bot.db.set_setting(guild.id, "setup_idx", i + 1)
        text = await self.bot.llm.chat(
            persona(self.bot, guild),
            f"Break down the '{setup}' setup for futures day traders. Use these bold headers: **Context**, **Trigger**, **Stop / invalidation**, "
            "**Management**, **Common mistakes**. 180-250 words. Educational, no promises.", max_tokens=500)
        if not text:
            return None
        e = discord.Embed(title=f"🔍 Setup breakdown · {setup.title()}", description=text, color=brand_color(self.bot, guild))
        e.set_footer(text=DISCLAIMER)
        return await publish(self.bot, guild, "lessons", "setups", embed=e, kind="setup", owner_initiated=owner, thread_name=f"{setup[:60]} discussion")

    async def post_gif(self, guild: discord.Guild, term: str | None = None, owner: bool = False):
        term = term or await self.bot.llm.chat(persona(self.bot, guild), "Give a 2-4 word GIF search term for a funny trading-day reaction. Just the words.",
                                               max_tokens=12) or random.choice(["stonks", "wall street", "to the moon", "this is fine", "money printer"])
        try:
            r = await self.http.get("https://api.giphy.com/v1/gifs/search", params={"api_key": self.bot.config.giphy_api_key, "q": term[:50], "limit": 20, "rating": "pg"})
            r.raise_for_status()
            items = r.json().get("data", [])
        except Exception:
            return None
        if not items:
            return None
        gif = random.choice(items)
        caption = await self.bot.llm.chat(persona(self.bot, guild), f"Write a 3-10 word caption for a trading meme GIF about '{term}'.", max_tokens=30) or ""
        e = discord.Embed(description=caption, color=brand_color(self.bot, guild))
        e.set_image(url=gif["images"]["original"]["url"])
        e.set_footer(text="Powered by GIPHY")
        return await publish(self.bot, guild, "gifs", "memes", embed=e, kind="gif", owner_initiated=owner)

    async def maybe_starter(self, guild: discord.Guild, now) -> None:
        cfg = self.bot.config
        if not (cfg.active_start_hour <= now.hour < cfg.active_end_hour):
            return
        channel = get_channel(self.bot, guild, "general")
        if channel is None:
            return
        last = self.last_activity.get(guild.id)
        if last is None:
            last = channel.last_message.created_at.timestamp() if channel.last_message else 0
            self.last_activity[guild.id] = last
        key, t = (guild.id, today(self.bot)), time.time()
        if t - last < cfg.quiet_minutes * 60 or self.starters.get(key, 0) >= cfg.max_starters_per_day or t - self.last_starter.get(guild.id, 0) < 3 * 3600:
            return
        text = await self.bot.llm.chat(persona(self.bot, guild),
                                       f"The chat has been quiet. It's {now:%A %I%p}. Write one short natural message to get futures traders talking. Under 25 words.",
                                       max_tokens=60) or random.choice(STARTERS)
        if await publish(self.bot, guild, "quiet_starters", channel, content=text, kind="starter"):
            self.starters[key] = self.starters.get(key, 0) + 1
            self.last_starter[guild.id] = self.last_activity[guild.id] = t

    # ---- commands ----
    @app_commands.command(name="poll", description="Post a poll")
    @app_commands.guild_only()
    @staff("poll")
    @app_commands.describe(options="Separate with ;  e.g. NQ; ES; CL")
    async def poll_cmd(self, interaction: discord.Interaction, question: str, options: str, hours: app_commands.Range[int, 1, 168] = 24):
        opts = [o.strip() for o in options.split(";") if o.strip()]
        if not 2 <= len(opts) <= 10:
            await interaction.response.send_message("Give 2 to 10 options separated by `;`.", ephemeral=True)
            return
        poll = discord.Poll(question=question[:300], duration=timedelta(hours=hours))
        for o in opts:
            poll.add_answer(text=o[:55])
        ch = get_channel(self.bot, interaction.guild, "general") or interaction.channel
        await ch.send(poll=poll)
        await interaction.response.send_message(f"Poll posted in {ch.mention}.", ephemeral=True)

    @app_commands.command(name="spark", description="Post something now: question, poll, lesson, setup or GIF")
    @app_commands.guild_only()
    @staff("spark")
    @app_commands.choices(kind=[app_commands.Choice(name=n, value=n) for n in ("question", "poll", "lesson", "setup", "gif")])
    async def spark(self, interaction: discord.Interaction, kind: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        g = interaction.guild
        fn = {"question": self.post_question, "poll": self.post_poll, "lesson": self.post_lesson, "setup": self.post_setup, "gif": self.post_gif}[kind.value]
        msg = await fn(g, owner=True)
        await interaction.followup.send(f"Posted: {msg.jump_url}" if msg else "Couldn't post that (missing channel, key or AI).", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Engagement(bot))
