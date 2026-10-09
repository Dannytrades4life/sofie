"""The bot itself: sharding, intents, startup, and the central message pipeline."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .control import load_rulebook
from .db import Database
from .futures import DEFAULT_SYMBOLS
from .llm import LLM
from .safety import ApprovalButton

log = logging.getLogger(__name__)

EXTENSIONS = [
    "bot.cogs.moderation",
    "bot.cogs.trades",
    "bot.cogs.edit",
    "bot.cogs.onboarding",
    "bot.cogs.levels",
    "bot.cogs.engagement",
    "bot.cogs.market",
    "bot.cogs.community",
    "bot.cogs.experiments",
    "bot.cogs.growth",
    "bot.cogs.giveaways",
    "bot.cogs.rebuild",
    "bot.cogs.expressions",
    "bot.cogs.reports",
    "bot.cogs.panel",
    "bot.cogs.health",
]

KILL_WORDS = {"stop", "pause", "kill", "killswitch"}
RESUME_WORDS = {"resume", "start", "unpause"}


class TradingBot(commands.Bot):
    """One gateway connection. Plenty for a single server (Discord only needs shards past 2,500 servers)."""

    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True          # privileged: "Server Members Intent"
        intents.message_content = True  # privileged: "Message Content Intent"
        intents.invites = True          # invite tracking for referral rewards
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # Safe default: no @everyone / role pings unless a call opts in explicitly.
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
            chunk_guilds_at_startup=False,
        )
        self.config = config
        self.db = Database(config.db_path)
        self.llm = LLM(config.llm_provider, config.llm_api_key, config.llm_model, config.llm_base_url)
        self.vision = LLM(config.vision_provider, config.vision_api_key, config.vision_model, vision=True)
        self.rulebook: dict[int, list[dict]] = {}

    # ---- kill switch ----
    @property
    def paused(self) -> bool:
        return self.db.get_setting(0, "paused", "0") == "1"

    async def set_paused(self, value: bool) -> None:
        await self.db.set_setting(0, "paused", "1" if value else "0")
        if self.is_ready():
            await self.change_presence(
                status=discord.Status.idle if value else discord.Status.online,
                activity=discord.CustomActivity("paused by owner" if value else "watching the tape 📈"),
            )

    # ---- startup ----
    async def init(self) -> None:
        """Everything except syncing commands (kept separate so tests can run offline)."""
        await self.db.connect()
        await load_rulebook(self)
        self.add_dynamic_items(ApprovalButton)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.tree.on_error = self.on_app_command_error

    async def seed_guild(self, guild_id: int) -> None:
        row = await self.db.fetchone("SELECT COUNT(*) n FROM symbols WHERE guild_id = ?", guild_id)
        if row["n"] == 0:
            await self.db.executemany(
                "INSERT INTO symbols (guild_id, root, name, tick_size, tick_value, data_ticker, market_posts) VALUES (?,?,?,?,?,?,?)",
                [(guild_id, s.root, s.name, s.tick_size, s.tick_value, s.data_ticker, int(s.market_posts)) for s in DEFAULT_SYMBOLS],
            )

    async def setup_hook(self) -> None:
        await self.init()
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s on %d shard(s), %d guild(s)", self.user, self.shard_count or 1, len(self.guilds))
        for g in self.guilds:
            await self.seed_guild(g.id)
        await self.set_paused(self.paused)

    async def owner_dm_chat(self, message: discord.Message) -> None:
        """The owner chatting with the bot in DMs gets a normal reply, so it's easy to check the AI is working."""
        from .control import persona
        guild = self.get_guild(self.config.guild_id) or self.guilds[0]
        if message.content.strip().startswith("/") and await self.owner_dm_command(message, guild):
            return
        async with message.channel.typing():
            text = await self.llm.chat(
                persona(self, guild),
                f"The server owner DMed you: {message.content[:800]}\n\nReply like a person, 1-3 short sentences. "
                "If they want something done, point them to the right slash command if you know it.",
                max_tokens=220,
            )
        await message.reply(text or "I'm here 👋 My AI didn't answer that one. Type `/health` here and I'll show you why.")

    async def owner_dm_command(self, message: discord.Message, guild: discord.Guild) -> bool:
        """Slash commands typed as plain text in DMs (Discord sends them as a message, not a command): run them anyway."""
        import io
        parts = message.content.strip()[1:].lower().replace(":", " ").split()
        name, args = (parts[0] if parts else ""), parts[1:]
        health, trades = self.get_cog("Health"), self.get_cog("Trades")
        if name in ("health", "status") and health:
            await message.reply(embed=await health.status_embed())
        elif name == "logs" and health:
            text = health.log_text(60, errors_only="true" in args or "errors" in args)
            if len(text) < 1800:
                await message.reply(f"```\n{text}\n```")
            else:
                await message.reply("Latest log lines:", file=discord.File(io.BytesIO(text.encode()), filename="bot-log.txt"))
        elif name in ("journal-link", "journallink", "journal") and trades:
            action = next((a for a in args if a in ("new", "revoke", "adopt")), "new")
            await message.reply(await trades.make_journal_link(guild, action))
        else:
            return False
        return True

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.seed_guild(guild.id)

    async def close(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True
        health = self.get_cog("Health")
        if health:
            await health.mark_clean_exit()
        await self.llm.close()
        await self.vision.close()
        await self.db.close()
        await super().close()

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "You can't use this command."
        else:
            log.exception("Command error", exc_info=error)
            msg = "Something went wrong running that command. It's been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # ---- message pipeline ----
    async def on_message(self, message: discord.Message) -> None:
        if message.webhook_id and message.guild and message.webhook_id in (self.db.get_json(message.guild.id, "journal_webhooks", []) or []):
            # The owner's own journal app posting into #trade-submit through a webhook made with /journal-link.
            self.dispatch("journal_message", message)
            return
        if message.author.bot:
            return
        if message.guild is None:
            if message.author.id == self.config.owner_id:
                word = message.content.strip().lower().strip("!. ")
                if word in KILL_WORDS:
                    await self.set_paused(True)
                    await message.reply("⏸️ Paused. No autonomous actions until you say `resume`.")
                elif word in RESUME_WORDS:
                    await self.set_paused(False)
                    await message.reply("▶️ Back on.")
                elif message.content.strip() and self.guilds:
                    await self.owner_dm_chat(message)
            return
        mod = self.get_cog("Moderation")
        if mod and await mod.inspect(message):
            return
        self.dispatch("clean_message", message)
