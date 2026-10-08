"""Market content on New York time (weekdays):
  07:00  today's economic calendar (week ahead on Mondays) + alerts 15 min before each event
  08:45  premarket plan: prior-day and overnight levels, market regime
  16:15  end-of-day recap
  Fri 16:45  weekly performance summary of the owner's posted trades (wins and losses)

Free data: Yahoo Finance chart API (delayed, unofficial) and the Forex Factory weekly
calendar JSON. If a source is down, the post is skipped and noted in #bot-log; nothing is made up.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timedelta

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

from ..config import MARKET_TZ
from ..control import active, owner_only, persona, publish
from ..futures import fmt_price, fmt_pts
from ..safety import log_action
from ..util import CALENDAR_ROLE, DISCLAIMER, brand_color, find_role

log = logging.getLogger(__name__)

CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; community-bot/1.0)"}
KEYWORDS = ["cpi", "ppi", "fomc", "federal funds", "fed chair", "powell", "non-farm", "unemployment rate", "crude oil inventories",
            "gdp", "pce", "retail sales", "ism ", "jolts", "unemployment claims"]
EVENT_EMOJI = {"cpi": "🔥", "ppi": "🏭", "fomc": "🏛️", "federal funds": "🏛️", "non-farm": "👷", "crude": "🛢️", "gdp": "📊", "pce": "🧾"}


# ------------------------------------------------------------------ pure helpers (tested offline)
def parse_calendar(data: list[dict]) -> list[dict]:
    out = []
    for e in data or []:
        if e.get("country") != "USD":
            continue
        title = e.get("title", "")
        t = title.lower()
        if e.get("impact") != "High" and not any(k in t for k in KEYWORDS):
            continue
        try:
            when = datetime.fromisoformat(e["date"]).astimezone(MARKET_TZ)
        except (KeyError, ValueError):
            continue
        out.append({"title": title, "time": when, "impact": e.get("impact"), "forecast": e.get("forecast") or "",
                    "previous": e.get("previous") or "", "id": f"{title}|{e['date']}"})
    return sorted(out, key=lambda x: x["time"])


def event_emoji(title: str) -> str:
    t = title.lower()
    return next((v for k, v in EVENT_EMOJI.items() if k in t), "📅")


def parse_chart(data: dict) -> list[dict]:
    """Yahoo chart JSON -> list of bars {t, o, h, l, c} (None bars dropped)."""
    try:
        r = data["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        bars = []
        for i, ts in enumerate(r["timestamp"]):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None not in (o, h, l, c):
                bars.append({"t": datetime.fromtimestamp(ts, MARKET_TZ), "o": o, "h": h, "l": l, "c": c})
        return bars
    except (KeyError, IndexError, TypeError):
        return []


def regime(daily: list[dict]) -> dict:
    """Trend / chop / volatility read from daily bars."""
    if len(daily) < 25:
        return {"label": "Not enough data", "er": None, "atr": None, "atr_pct": None}
    trs = []
    for prev, bar in zip(daily, daily[1:]):
        trs.append(max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]), abs(bar["l"] - prev["c"])))
    atrs = [statistics.mean(trs[i - 14:i]) for i in range(14, len(trs) + 1)]
    atr = atrs[-1]
    atr_pct = round(100 * sum(a <= atr for a in atrs) / len(atrs))
    closes = [b["c"] for b in daily[-11:]]
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    er = abs(closes[-1] - closes[0]) / path if path else 0
    if er >= 0.35:
        label = "Trending up" if closes[-1] > closes[0] else "Trending down"
    elif er < 0.2:
        label = "Choppy / range-bound"
    else:
        label = "Mixed"
    if atr_pct >= 80:
        label += " · High volatility"
    elif atr_pct <= 20:
        label += " · Quiet"
    return {"label": label, "er": round(er, 2), "atr": atr, "atr_pct": atr_pct}


def levels(daily: list[dict], intraday: list[dict], now: datetime) -> dict:
    """Prior-day high/low/close and the overnight range since 18:00 NY time."""
    today = now.date()
    prior = [b for b in daily if b["t"].date() < today]
    out = {}
    if prior:
        p = prior[-1]
        out.update(pdh=p["h"], pdl=p["l"], pdc=p["c"])
    start = datetime.combine(today - timedelta(days=1), datetime.min.time(), MARKET_TZ).replace(hour=18)
    if now.weekday() == 0:
        start -= timedelta(days=2)  # Sunday evening open
    on = [b for b in intraday if start <= b["t"] <= now.replace(hour=9, minute=30)]
    if on:
        out.update(onh=max(b["h"] for b in on), onl=min(b["l"] for b in on))
    if intraday:
        out["last"] = intraday[-1]["c"]
    return out


class Market(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.http = httpx.AsyncClient(timeout=20, headers=UA)
        self.events: list[dict] = []
        self.events_fetched = 0.0
        self.alerted: set[str] = set()
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()
        await self.http.aclose()

    # ---- data ----
    async def refresh_calendar(self, force: bool = False) -> None:
        if not force and time.time() - self.events_fetched < 6 * 3600:
            return
        try:
            r = await self.http.get(CAL_URL)
            r.raise_for_status()
            self.events = parse_calendar(r.json())
            self.events_fetched = time.time()
        except Exception as e:
            log.warning("calendar fetch failed: %s", e)

    def events_near(self, minutes: int = 60) -> list[dict]:
        now = datetime.now(MARKET_TZ)
        return [e for e in self.events if abs((e["time"] - now).total_seconds()) <= minutes * 60]

    async def chart(self, ticker: str, rng: str, interval: str) -> list[dict]:
        try:
            r = await self.http.get(CHART_URL.format(ticker=ticker), params={"range": rng, "interval": interval})
            r.raise_for_status()
            return parse_chart(r.json())
        except Exception as e:
            log.warning("chart %s failed: %s", ticker, e)
            return []

    async def market_symbols(self, guild_id: int) -> list[dict]:
        rows = await self.bot.db.fetchall("SELECT root, name, data_ticker FROM symbols WHERE guild_id=? AND market_posts=1 AND data_ticker IS NOT NULL", guild_id)
        return [dict(r) for r in rows]

    # ---- posts ----
    async def post_calendar(self, guild: discord.Guild, *, week: bool = False, owner: bool = False) -> bool:
        await self.refresh_calendar()
        now = datetime.now(MARKET_TZ)
        evs = [e for e in self.events if (e["time"].date() == now.date() or (week and e["time"] >= now))]
        if not evs:
            return False
        lines = []
        for e in evs:
            meta = " · ".join(x for x in (f"fcst {e['forecast']}" if e["forecast"] else "", f"prev {e['previous']}" if e["previous"] else "") if x)
            lines.append(f"{event_emoji(e['title'])} **{e['title']}** · {e['time']:%a %I:%M %p} ET" + (f"  ({meta})" if meta else ""))
        e = discord.Embed(title="🗓 Week ahead: market-moving events" if week else f"🗓 Today's events · {now:%a %b %d}",
                          description="\n".join(lines)[:4000], color=brand_color(self.bot, guild))
        e.set_footer(text="Times in New York (ET). I'll ping Calendar Alerts 15 minutes before each one. " + DISCLAIMER)
        return bool(await publish(self.bot, guild, "calendar", "calendar", embed=e, kind="calendar", owner_initiated=owner))

    async def alert_events(self, guild: discord.Guild) -> None:
        now = datetime.now(MARKET_TZ)
        role = find_role(guild, CALENDAR_ROLE)
        for e in self.events:
            mins = (e["time"] - now).total_seconds() / 60
            key = f"{guild.id}|{e['id']}"
            if 0 < mins <= 15 and key not in self.alerted:
                self.alerted.add(key)
                text = f"{role.mention + ' ' if role else ''}{event_emoji(e['title'])} **{e['title']}** in {round(mins)} min ({e['time']:%I:%M %p} ET). Spreads can widen and price can whip; size accordingly."
                await publish(self.bot, guild, "calendar", "calendar", content=text, kind="calendar_alert",
                              allowed_mentions=discord.AllowedMentions(roles=[role] if role else []))

    async def post_premarket(self, guild: discord.Guild, *, owner: bool = False) -> bool:
        now = datetime.now(MARKET_TZ)
        e = discord.Embed(title=f"☀️ Premarket plan · {now:%a %b %d}", color=brand_color(self.bot, guild))
        facts = []
        for s in await self.market_symbols(guild.id):
            daily = await self.chart(s["data_ticker"], "6mo", "1d")
            intra = await self.chart(s["data_ticker"], "5d", "30m")
            if not daily:
                continue
            lv, rg = levels(daily, intra, now), regime(daily)
            facts.append({"symbol": s["root"], **{k: round(v, 2) for k, v in lv.items()}, "regime": rg["label"], "atr": round(rg["atr"] or 0, 2)})
            rows = [f"Prior day H/L/C: `{fmt_price(lv.get('pdh'))}` / `{fmt_price(lv.get('pdl'))}` / `{fmt_price(lv.get('pdc'))}`"]
            if "onh" in lv:
                rows.append(f"Overnight H/L: `{fmt_price(lv['onh'])}` / `{fmt_price(lv['onl'])}`")
            if "last" in lv:
                rows.append(f"Last (delayed): `{fmt_price(lv['last'])}`")
            rows.append(f"Regime: **{rg['label']}** (daily ATR ≈ {fmt_price(rg['atr'])})")
            e.add_field(name=f"{s['root']} · {s['name']}", value="\n".join(rows), inline=False)
        if not facts:
            await log_action(self.bot, guild, "market", "premarket skipped: no price data")
            return False
        await self.refresh_calendar()
        today = [ev for ev in self.events if ev["time"].date() == now.date()]
        if today:
            e.add_field(name="Events today", value="\n".join(f"{event_emoji(x['title'])} {x['title']} {x['time']:%I:%M %p}" for x in today)[:1024], inline=False)
        plan = await self.bot.llm.chat(
            persona(self.bot, guild),
            "Write a short premarket note (3-5 sentences) for futures day traders using ONLY these numbers and events. Mention which "
            "levels matter and how the regime should affect expectations (e.g. chop = smaller targets). Educational, no trade calls, "
            f"no predictions stated as fact.\nData: {json.dumps(facts)}\nEvents today: {[x['title'] for x in today]}",
            max_tokens=260,
        )
        e.description = plan or "Levels below. Let price show its hand at them; in chop, keep targets modest."
        e.set_footer(text="Data is delayed and from free sources. " + DISCLAIMER)
        return bool(await publish(self.bot, guild, "premarket", "premarket", embed=e, kind="premarket", owner_initiated=owner))

    async def owner_trades(self, guild_id: int, since: float) -> list[dict]:
        rows = await self.bot.db.fetchall("SELECT id, data FROM trades WHERE guild_id=? AND status='closed' AND closed_at>=?", guild_id, since)
        return [{**json.loads(r["data"]), "id": r["id"]} for r in rows]

    def trade_summary(self, trades: list[dict]) -> str:
        if not trades:
            return "No trades posted."
        wins = [t for t in trades if (t.get("points") or 0) > 0]
        rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
        s = f"{len(trades)} trade(s): {len(wins)} win(s), {len(trades) - len(wins)} loss/flat · win rate {round(100 * len(wins) / len(trades))}%"
        if rs:
            s += f" · {sum(rs):+.2f}R total"
        return s

    async def post_recap(self, guild: discord.Guild, *, owner: bool = False) -> bool:
        now = datetime.now(MARKET_TZ)
        e = discord.Embed(title=f"🌙 Daily recap · {now:%a %b %d}", color=brand_color(self.bot, guild))
        facts = []
        for s in await self.market_symbols(guild.id):
            daily = await self.chart(s["data_ticker"], "1mo", "1d")
            if len(daily) < 2 or daily[-1]["t"].date() != now.date():
                continue
            d, p = daily[-1], daily[-2]
            chg = d["c"] - p["c"]
            facts.append({"symbol": s["root"], "change": round(chg, 2), "pct": round(chg / p["c"] * 100, 2), "range": round(d["h"] - d["l"], 2)})
            e.add_field(name=s["root"], value=f"{'🟢' if chg >= 0 else '🔴'} {chg:+,.2f} ({chg / p['c'] * 100:+.2f}%)\nRange {fmt_price(d['h'] - d['l'])} pts", inline=True)
        start = datetime.combine(now.date(), datetime.min.time(), MARKET_TZ).timestamp()
        trades = await self.owner_trades(guild.id, start)
        e.add_field(name="Our trades today", value=self.trade_summary(trades), inline=False)
        if not facts and not trades:
            return False
        text = await self.bot.llm.chat(
            persona(self.bot, guild),
            f"Write a 2-3 sentence end-of-day recap for the chat using only these facts: {json.dumps(facts)}; our trades: {self.trade_summary(trades)}. "
            "Casual, honest about losses, end with a question to get people talking about their day.",
            max_tokens=160,
        )
        e.description = text or "That's the session. How'd everyone do today?"
        e.set_footer(text=DISCLAIMER)
        return bool(await publish(self.bot, guild, "recap", "recap", embed=e, kind="recap", owner_initiated=owner))

    async def post_weekly(self, guild: discord.Guild, *, owner: bool = False) -> bool:
        now = datetime.now(MARKET_TZ)
        start = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time(), MARKET_TZ).timestamp()
        trades = await self.owner_trades(guild.id, start)
        e = discord.Embed(title=f"📊 Weekly performance · week of {datetime.fromtimestamp(start, MARKET_TZ):%b %d}",
                          description=self.trade_summary(trades), color=brand_color(self.bot, guild))
        by = {}
        for t in trades:
            by.setdefault(t["symbol"], []).append(t)
        for sym, ts in by.items():
            e.add_field(name=sym, value=self.trade_summary(ts), inline=False)
        if trades:
            best = max(trades, key=lambda t: t.get("r_multiple") or t.get("pnl_pct") or 0)
            worst = min(trades, key=lambda t: t.get("r_multiple") or t.get("pnl_pct") or 0)
            fmt = lambda t: f"#{t['id']} {t['contract']} {fmt_pts(t.get('points'))}" + (f" ({t['r_multiple']:+.2f}R)" if t.get("r_multiple") is not None else "")  # noqa: E731
            e.add_field(name="Best / worst", value=f"{fmt(best)} · {fmt(worst)}", inline=False)
        e.set_footer(text="Every posted trade counts, wins and losses. " + DISCLAIMER)
        return bool(await publish(self.bot, guild, "recap", "recap", embed=e, kind="weekly_performance", owner_initiated=owner))

    # ---- scheduler (NY time, weekdays) ----
    async def once(self, guild: discord.Guild, key: str) -> bool:
        """True the first time `key` is seen today (survives restarts)."""
        day = datetime.now(MARKET_TZ).strftime("%Y-%m-%d")
        k = f"ran:{key}"
        if self.bot.db.get_setting(guild.id, k) == day:
            return False
        await self.bot.db.set_setting(guild.id, k, day)
        return True

    @tasks.loop(minutes=1)
    async def scheduler(self):
        now = datetime.now(MARKET_TZ)
        await self.refresh_calendar()
        weekday = now.weekday() < 5
        hm = (now.hour, now.minute)
        for guild in self.bot.guilds:
            g = guild.id
            if active(self.bot, g, "calendar"):
                await self.alert_events(guild)
                if weekday and hm >= (7, 0) and hm < (7, 30) and await self.once(guild, "calendar"):
                    await self.post_calendar(guild, week=now.weekday() == 0)
            if weekday and hm >= (8, 45) and hm < (9, 15) and active(self.bot, g, "premarket") and await self.once(guild, "premarket"):
                await self.post_premarket(guild)
            if weekday and hm >= (16, 15) and hm < (16, 45) and active(self.bot, g, "recap") and await self.once(guild, "recap"):
                await self.post_recap(guild)
            if now.weekday() == 4 and hm >= (16, 45) and hm < (17, 15) and active(self.bot, g, "recap") and await self.once(guild, "weekly_perf"):
                await self.post_weekly(guild)

    @scheduler.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    market = app_commands.Group(name="market", description="Post market content now", guild_only=True)

    @market.command(name="post", description="Post a market update now")
    @owner_only()
    @app_commands.choices(kind=[app_commands.Choice(name=n, value=v) for n, v in
                                (("premarket plan", "premarket"), ("today's calendar", "calendar"), ("week-ahead calendar", "week"),
                                 ("daily recap", "recap"), ("weekly performance", "weekly"))])
    async def post_now(self, interaction: discord.Interaction, kind: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = interaction.guild
        fn = {"premarket": lambda: self.post_premarket(g, owner=True), "calendar": lambda: self.post_calendar(g, owner=True),
              "week": lambda: self.post_calendar(g, week=True, owner=True), "recap": lambda: self.post_recap(g, owner=True),
              "weekly": lambda: self.post_weekly(g, owner=True)}[kind.value]
        await self.refresh_calendar(force=True)
        ok = await fn()
        await interaction.followup.send("Posted." if ok else "Nothing to post (no data or no events right now).", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Market(bot))
