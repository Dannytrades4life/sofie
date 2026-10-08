"""Uptime and health: a daily "I'm online" DM, a DM after any unexpected restart,
a watchdog that exits (so systemd restarts it) if the Discord connection stays dead,
and /logs to read the latest log lines from Discord.
"""
from __future__ import annotations

import io
import logging
import math
import os
import shutil
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..control import FEATURES, feature_on, identity, owner_only, run_once
from ..safety import dm_owner
from ..util import GREEN, ORANGE, local_now

log = logging.getLogger(__name__)

HEARTBEAT_MIN = 5
WATCHDOG_MIN = 10  # disconnected this long -> exit and let systemd restart us


class ErrorCounter(logging.Handler):
    """Remembers when errors were logged, for the daily status DM."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.times: deque[float] = deque(maxlen=1000)
        self.last: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        self.times.append(record.created)
        self.last = f"{record.name}: {record.getMessage()}"[:300]

    def since(self, t: float) -> int:
        return sum(1 for x in self.times if x >= t)


ERRORS = ErrorCounter()


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} days"


class Health(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.started = time.time()
        self.announced = False
        self.dead_since: float | None = None
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ---- restart detection ----
    async def mark_clean_exit(self) -> None:
        try:
            await self.bot.db.set_setting(0, "health:clean_exit", "1")
        except Exception:  # database may already be closed
            pass

    async def _record_start(self) -> tuple[str, float | None]:
        """Returns ('first' | 'crash' | 'clean', last_seen)."""
        db = self.bot.db
        last_seen = db.get_setting(0, "health:last_seen")
        clean = db.get_setting(0, "health:clean_exit")
        starts = [t for t in (db.get_json(0, "health:starts", []) or []) if t > time.time() - 7 * 86400] + [time.time()]
        await db.set_json(0, "health:starts", starts)
        await db.set_setting(0, "health:clean_exit", "0")
        await db.set_setting(0, "health:last_seen", time.time())
        if last_seen is None:
            return "first", None
        return ("clean" if clean == "1" else "crash"), float(last_seen)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.announced:
            return
        self.announced = True
        kind, last_seen = await self._record_start()
        name = identity(self.bot, self.bot.guilds[0].id)["name"] if self.bot.guilds else self.bot.config.bot_name
        if kind == "first":
            servers = ", ".join(g.name for g in self.bot.guilds) or "no server yet (use the invite link from the README)"
            await dm_owner(self.bot, content=f"✅ **{name} is connected.** Phase 1 check passed: I can see {servers}. "
                                             "Next I'll DM you the server upgrade plan to approve (or run `/rebuild plan`). I'll DM you every morning to say I'm still online.")
        elif kind == "crash":
            down = time.time() - last_seen if last_seen else 0
            await dm_owner(self.bot, content=f"⚠️ I stopped unexpectedly and restarted myself (down about {_ago(down)}). "
                                             "Everything is running again. If this keeps happening, `/logs` shows what went wrong.")
        log.info("startup kind=%s", kind)

    # ---- heartbeat, watchdog, daily status ----
    @tasks.loop(minutes=1)
    async def loop(self):
        now = time.time()
        if int(now // 60) % HEARTBEAT_MIN == 0:
            await self.bot.db.set_setting(0, "health:last_seen", now)
        connected = self.bot.is_ready() and not math.isinf(self.bot.latency) and not math.isnan(self.bot.latency)
        if connected:
            self.dead_since = None
        else:
            self.dead_since = self.dead_since or now
            if now - self.dead_since > WATCHDOG_MIN * 60:
                log.critical("No Discord connection for %d min; exiting so the service manager restarts me.", WATCHDOG_MIN)
                os._exit(1)
        local = local_now(self.bot)
        if connected and 0 <= self.bot.config.status_hour <= local.hour and await run_once(self.bot, 0, "status_dm", local.strftime("%Y-%m-%d")):
            await dm_owner(self.bot, embed=await self.status_embed())

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    async def status_embed(self) -> discord.Embed:
        b = self.bot
        day_ago = time.time() - 86400
        errors = ERRORS.since(day_ago)
        restarts = sum(1 for t in (b.db.get_json(0, "health:starts", []) or []) if t >= day_ago) - (1 if self.started >= day_ago else 0)
        name = identity(b, b.guilds[0].id)["name"] if b.guilds else b.config.bot_name
        ok = not b.paused and errors == 0 and restarts == 0
        e = discord.Embed(title=f"🟢 {name} is online" if not b.paused else f"⏸️ {name} is online but paused",
                          color=GREEN if ok else ORANGE, timestamp=discord.utils.utcnow())
        e.add_field(name="Up for", value=_ago(time.time() - self.started))
        e.add_field(name="Discord latency", value=f"{b.latency * 1000:.0f} ms")
        e.add_field(name="Restarts (24h)", value=str(max(restarts, 0)))
        e.add_field(name="Errors (24h)", value=str(errors) + (f"\nlast: `{ERRORS.last[:150]}`" if errors and ERRORS.last else ""), inline=False)
        e.add_field(name="Servers", value=", ".join(f"{g.name} ({g.member_count:,})" for g in b.guilds) or "none")
        e.add_field(name="Text AI / vision", value=f"{'on' if b.llm.enabled else 'off'} / {'on' if b.vision.enabled else 'off'}")
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(b.config.db_path)), "update-status.txt")) as fh:
                e.add_field(name="Self-update", value=fh.read()[:300], inline=False)
        except OSError:
            pass
        for g in b.guilds[:1]:
            off = [k for k in FEATURES if not feature_on(b, g.id, k)]
            if off:
                e.add_field(name="Switched off", value=", ".join(off)[:1000], inline=False)
            pending = await b.db.fetchone("SELECT COUNT(*) n FROM approvals WHERE status='pending' AND guild_id=?", g.id)
            if pending["n"]:
                e.add_field(name="Waiting on you", value=f"{pending['n']} approval(s) in our DMs")
        try:
            disk = shutil.disk_usage(os.path.dirname(os.path.abspath(b.config.db_path)) or ".")
            db_mb = os.path.getsize(b.config.db_path) / 1e6 if os.path.exists(b.config.db_path) else 0
            e.add_field(name="Storage", value=f"database {db_mb:.1f} MB · {disk.free / 1e9:.1f} GB free")
        except OSError:
            pass
        e.set_footer(text="Daily check-in. Turn it off by setting STATUS_HOUR=-1.")
        return e

    @app_commands.command(name="logs", description="Show the bot's latest log lines (only you see them)")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(lines="How many lines (default 80)", errors_only="Only warnings and errors")
    async def logs_cmd(self, interaction: discord.Interaction, lines: app_commands.Range[int, 10, 2000] = 80, errors_only: bool = False):
        path = os.path.join(self.bot.config.log_dir, "bot.log")
        if not os.path.exists(path):
            await interaction.response.send_message("No log file yet. On the server: `journalctl -u tradingbot -n 100`.", ephemeral=True)
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = deque((l for l in f if not errors_only or any(w in l for w in ("WARNING", "ERROR", "CRITICAL"))), maxlen=lines)
        text = "".join(tail) or "Nothing logged."
        if len(text) < 1800:
            await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)
        else:
            await interaction.response.send_message(f"Last {len(tail)} lines:", ephemeral=True,
                                                    file=discord.File(io.BytesIO(text.encode()), filename="bot-log.txt"))

    @app_commands.command(name="health", description="Get the daily status check-in now")
    @app_commands.guild_only()
    @owner_only()
    async def health_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await self.status_embed(), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Health(bot))
