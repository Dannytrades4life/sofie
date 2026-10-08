"""Owner / staff control panel.

/teach      the bot's persistent rulebook (your rules always win)
/feature    per-feature on/off and approval mode (auto or ask me first)
/perm       which staff level (Leaders, Mods) may use which command
/staff-roles map your existing roles to Leaders / Mods
/identity   bot name, avatar, personality, brand name and color
/display    what trade posts show (dollars and contracts are hidden unless you turn them on)
/pause /resume /kill   kill switch (or DM the bot "stop" / "resume")
/status /announce
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from ..control import (COMMAND_DEFAULTS, FEATURES, LEADER, MOD, allowed_levels, feature_mode, feature_on, identity,
                       load_rulebook, owner_only, staff)
from ..safety import log_action, request_approval
from ..util import brand_color, get_channel

FEATURE_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _) in FEATURES.items()][:25]
COMMANDS = sorted(set(COMMAND_DEFAULTS) | {"teach", "feature", "perm", "identity", "rebuild", "symbols", "sponsor", "kill"})


class Panel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------ rulebook
    teach = app_commands.Group(name="teach", description="Teach the bot rules, tone, topics or facts", guild_only=True)

    @teach.command(name="add", description="Add a rule in plain English, e.g. 'never post during FOMC'")
    @owner_only()
    async def teach_add(self, interaction: discord.Interaction, rule: app_commands.Range[str, 3, 500]):
        rid = await self.bot.db.execute("INSERT INTO rulebook (guild_id, text, created_by, created_at) VALUES (?,?,?,?)",
                                        interaction.guild.id, rule, interaction.user.id, time.time())
        await load_rulebook(self.bot)
        await log_action(self.bot, interaction.guild, "rulebook", f"rule {rid} added: {rule}")
        await interaction.response.send_message(f"Learned rule **{rid}**: {rule}\nIt applies to everything I write and every post I make on my own.", ephemeral=True)

    @teach.command(name="list", description="Show the rulebook")
    @staff("teach-list")
    async def teach_list(self, interaction: discord.Interaction):
        rules = self.bot.rulebook.get(interaction.guild.id, [])
        text = "\n".join(f"**{r['id']}.** {r['text']}" for r in rules) or "No rules yet. Add one with `/teach add`."
        await interaction.response.send_message(text[:1900], ephemeral=True)

    @teach.command(name="edit", description="Rewrite a rule")
    @owner_only()
    async def teach_edit(self, interaction: discord.Interaction, rule_id: int, rule: app_commands.Range[str, 3, 500]):
        old = await self.bot.db.fetchone("SELECT text FROM rulebook WHERE id=? AND guild_id=?", rule_id, interaction.guild.id)
        if not old:
            await interaction.response.send_message("No rule with that number.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE rulebook SET text=?, updated_at=? WHERE id=?", rule, time.time(), rule_id)
        await self.bot.db.edit_log(interaction.guild.id, f"rule:{rule_id}", "text", old["text"], rule, interaction.user.id)
        await load_rulebook(self.bot)
        await interaction.response.send_message(f"Rule {rule_id} updated.", ephemeral=True)

    @teach.command(name="remove", description="Delete a rule")
    @owner_only()
    async def teach_remove(self, interaction: discord.Interaction, rule_id: int):
        old = await self.bot.db.fetchone("SELECT text FROM rulebook WHERE id=? AND guild_id=?", rule_id, interaction.guild.id)
        if not old:
            await interaction.response.send_message("No rule with that number.", ephemeral=True)
            return
        await self.bot.db.execute("DELETE FROM rulebook WHERE id=?", rule_id)
        await self.bot.db.edit_log(interaction.guild.id, f"rule:{rule_id}", "text", old["text"], None, interaction.user.id)
        await load_rulebook(self.bot)
        await interaction.response.send_message(f"Forgot rule {rule_id}.", ephemeral=True)

    # ------------------------------------------------------------ features
    feature = app_commands.Group(name="feature", description="Turn features on/off and set approval modes", guild_only=True)

    @feature.command(name="list", description="Show every feature, on/off and mode")
    @owner_only()
    async def feature_list(self, interaction: discord.Interaction):
        g = interaction.guild.id
        lines = [f"{'🟢' if feature_on(self.bot, g, k) else '⚫'} **{label}** (`{k}`) · {'🤖 auto' if feature_mode(self.bot, g, k) == 'auto' else '✋ ask me first'}"
                 for k, (label, _) in FEATURES.items()]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @feature.command(name="set", description="Switch a feature on or off")
    @owner_only()
    @app_commands.choices(feature=FEATURE_CHOICES)
    async def feature_set(self, interaction: discord.Interaction, feature: app_commands.Choice[str], on: bool):
        await self.bot.db.set_setting(interaction.guild.id, f"feat:{feature.value}:on", "1" if on else "0")
        await log_action(self.bot, interaction.guild, "feature", f"{feature.value} {'on' if on else 'off'}")
        await interaction.response.send_message(f"**{feature.name}** is now {'on' if on else 'off'}.", ephemeral=True)

    @feature.command(name="mode", description="Autonomous, or ask me first (Approve/Deny in DMs)")
    @owner_only()
    @app_commands.choices(feature=FEATURE_CHOICES, mode=[app_commands.Choice(name="autonomous", value="auto"),
                                                         app_commands.Choice(name="ask me first", value="ask")])
    async def feature_mode_cmd(self, interaction: discord.Interaction, feature: app_commands.Choice[str], mode: app_commands.Choice[str]):
        await self.bot.db.set_setting(interaction.guild.id, f"feat:{feature.value}:mode", mode.value)
        await log_action(self.bot, interaction.guild, "feature", f"{feature.value} mode {mode.value}")
        await interaction.response.send_message(f"**{feature.name}**: {mode.name}.", ephemeral=True)

    # ------------------------------------------------------------ permissions
    perm = app_commands.Group(name="perm", description="Which staff can use which commands", guild_only=True)

    @perm.command(name="list", description="Show staff permissions")
    @owner_only()
    async def perm_list(self, interaction: discord.Interaction):
        g = interaction.guild.id
        lines = [f"`/{c}`: owner{', leaders' if LEADER in allowed_levels(self.bot, g, c) else ''}{', mods' if MOD in allowed_levels(self.bot, g, c) else ''}" for c in COMMANDS]
        await interaction.response.send_message("\n".join(lines) + "\n\nStaff commands only work in the staff panel channel. Mods and leaders can never change numbers on posted results.", ephemeral=True)

    @perm.command(name="set", description="Allow or block a staff level for a command")
    @owner_only()
    @app_commands.choices(level=[app_commands.Choice(name="Leaders", value=LEADER), app_commands.Choice(name="Mods", value=MOD)],
                          command=[app_commands.Choice(name=c, value=c) for c in COMMANDS if c not in ("perm", "kill", "rebuild")][:25])
    async def perm_set(self, interaction: discord.Interaction, command: app_commands.Choice[str], level: app_commands.Choice[str], allow: bool):
        g = interaction.guild.id
        levels = allowed_levels(self.bot, g, command.value)
        levels = levels | {level.value} if allow else levels - {level.value}
        await self.bot.db.set_json(g, f"perm:{command.value}", sorted(levels))
        await log_action(self.bot, interaction.guild, "perm", f"/{command.value} {level.value} {'allowed' if allow else 'blocked'}")
        await interaction.response.send_message(f"/{command.value}: {level.name} {'allowed' if allow else 'blocked'}.", ephemeral=True)

    @app_commands.command(name="staff-roles", description="Use one of your existing roles as Leaders or Mods")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.choices(level=[app_commands.Choice(name="Leaders", value=LEADER), app_commands.Choice(name="Mods", value=MOD)])
    async def staff_roles(self, interaction: discord.Interaction, level: app_commands.Choice[str], role: discord.Role, add: bool = True):
        g = interaction.guild.id
        data = self.bot.db.get_json(g, "staff_roles", {}) or {}
        ids = set(data.get(level.value, []))
        ids = ids | {role.id} if add else ids - {role.id}
        data[level.value] = sorted(ids)
        await self.bot.db.set_json(g, "staff_roles", data)
        await interaction.response.send_message(f"{role.mention} {'is now' if add else 'is no longer'} {level.name}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ identity & branding
    @app_commands.command(name="identity", description="Set the bot's name, avatar, personality and branding")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(name="Display name", avatar="Square PNG/JPG", personality="How it talks, in plain English",
                           brand_name="Name on graphics", brand_color_hex="Hex like #00C2A8")
    async def identity_cmd(self, interaction: discord.Interaction, name: str | None = None, avatar: discord.Attachment | None = None,
                           personality: str | None = None, brand_name: str | None = None, brand_color_hex: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = interaction.guild
        data = self.bot.db.get_json(g.id, "identity", {}) or {}
        notes = []
        if name:
            data["name"] = name
            try:
                await g.me.edit(nick=name)
                notes.append(f"Server nickname set to **{name}**.")
            except discord.HTTPException:
                notes.append("Couldn't set my server nickname (needs Change Nickname permission).")
            try:
                await self.bot.user.edit(username=name)
                notes.append("Username changed too.")
            except discord.HTTPException as e:
                notes.append(f"Discord didn't allow a username change right now ({e.text or 'rate limit: 2 per hour'}); the server nickname covers it.")
        if avatar:
            try:
                await self.bot.user.edit(avatar=await avatar.read())
                notes.append("Avatar updated.")
            except discord.HTTPException as e:
                notes.append(f"Avatar not updated: {e.text}")
        if personality:
            data["personality"] = personality
            notes.append("Personality saved.")
        if brand_name:
            await self.bot.db.set_setting(g.id, "brand_name", brand_name)
            notes.append(f"Brand name **{brand_name}**.")
        if brand_color_hex:
            try:
                await self.bot.db.set_setting(g.id, "brand_color", int(brand_color_hex.lstrip("#"), 16))
                notes.append(f"Brand color {brand_color_hex}.")
            except ValueError:
                notes.append("Brand color should look like #00C2A8.")
        await self.bot.db.set_json(g.id, "identity", data)
        ident = identity(self.bot, g.id)
        await interaction.followup.send("\n".join(notes) + f"\n\nI'm **{ident['name']}**: {ident['personality']}", ephemeral=True)

    # ------------------------------------------------------------ what trade posts show
    @app_commands.command(name="display", description="What trade posts show: $ amounts, contracts, % of account")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.describe(show_dollars="Show $ P&L and $ risk on posts (default off)", show_contracts="Show position size (default off)",
                           account_size="Your account size in $, so posts can show % of account. Never posted. 0 clears it.")
    async def display_cmd(self, interaction: discord.Interaction, show_dollars: bool | None = None, show_contracts: bool | None = None,
                          account_size: app_commands.Range[float, 0, 100_000_000] | None = None):
        g, db = interaction.guild, self.bot.db
        if show_dollars is not None:
            await db.set_setting(g.id, "display:show_usd", "on" if show_dollars else "off")
        if show_contracts is not None:
            await db.set_setting(g.id, "display:show_size", "on" if show_contracts else "off")
        if account_size is not None:
            await db.set_setting(g.id, "display:account_size", account_size or None)
        show = self.bot.get_cog("Trades").display(g.id)
        await interaction.response.send_message(
            "Trade posts show points, ticks, % price move and R"
            + (", plus % of account" if show.account else "")
            + (", plus $ amounts" if show.usd else ". No dollar amounts")
            + (", with contracts." if show.size else ", no contract count.")
            + "\nOne-off: `/trade show_dollars:True`, or `/edit` field *Show $ and contracts*. Existing posts change when you `/edit` them.",
            ephemeral=True)

    # ------------------------------------------------------------ kill switch
    @app_commands.command(name="pause", description="Pause all autonomous actions")
    @app_commands.guild_only()
    @staff("pause")
    async def pause(self, interaction: discord.Interaction):
        await self.bot.set_paused(True)
        await log_action(self.bot, interaction.guild, "kill_switch", f"paused by {interaction.user}")
        await interaction.response.send_message("⏸️ Paused. Owner commands still work. `/resume` to restart.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume autonomous actions")
    @app_commands.guild_only()
    @owner_only()
    async def resume(self, interaction: discord.Interaction):
        await self.bot.set_paused(False)
        await log_action(self.bot, interaction.guild, "kill_switch", "resumed")
        await interaction.response.send_message("▶️ Resumed.", ephemeral=True)

    @app_commands.command(name="kill", description="Kill switch: stop everything autonomous right now")
    @owner_only()
    async def kill(self, interaction: discord.Interaction):
        await self.bot.set_paused(True)
        await interaction.response.send_message("🛑 Stopped. Nothing autonomous will run (no posts, replies, moderation, giveaways draws) until `/resume` or you DM me `resume`.", ephemeral=True)

    @app_commands.command(name="status", description="Bot health and settings")
    @app_commands.guild_only()
    @staff("status")
    async def status(self, interaction: discord.Interaction):
        b, g = self.bot, interaction.guild
        e = discord.Embed(title=f"🤖 {identity(b, g.id)['name']} status", color=brand_color(b, g))
        e.add_field(name="State", value="⏸️ paused" if b.paused else "▶️ running")
        e.add_field(name="Latency", value=f"{b.latency * 1000:.0f} ms")
        e.add_field(name="Shards", value=str(b.shard_count or 1))
        e.add_field(name="Text AI", value=f"{b.llm.provider} / {b.llm.model}" if b.llm.enabled else "off (templates)")
        e.add_field(name="Vision", value=f"{b.vision.provider} / {b.vision.model}" if b.vision.enabled else "off")
        off = [k for k in FEATURES if not feature_on(b, g.id, k)]
        ask = [k for k in FEATURES if feature_mode(b, g.id, k) == "ask"]
        e.add_field(name="Features off", value=", ".join(off) or "none", inline=False)
        e.add_field(name="Ask-first features", value=", ".join(ask) or "none", inline=False)
        pending = await b.db.fetchone("SELECT COUNT(*) n FROM approvals WHERE status='pending' AND guild_id=?", g.id)
        e.add_field(name="Pending approvals", value=str(pending["n"]))
        e.add_field(name="Rules learned", value=str(len(b.rulebook.get(g.id, []))))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="announce", description="Post an announcement (@everyone needs the owner's DM approval)")
    @app_commands.guild_only()
    @staff("announce")
    async def announce(self, interaction: discord.Interaction, text: str, ping_everyone: bool = False, title: str | None = None):
        from ..control import send_post
        g = interaction.guild
        ch = get_channel(self.bot, g, "announcements")
        if ch is None:
            await interaction.response.send_message("No announcements channel yet.", ephemeral=True)
            return
        embed = discord.Embed(title=title, description=text.replace("\\n", "\n"), color=brand_color(self.bot, g))
        if ping_everyone:
            aid = await request_approval(self.bot, g, "announce", f"{interaction.user.mention} wants to ping **@everyone** in {ch.mention}.",
                                         {"channel_id": ch.id, "embed": embed.to_dict()}, preview=embed)
            await interaction.response.send_message(f"Sent to the owner for approval (#{aid}).", ephemeral=True)
            return
        await send_post(self.bot, g, ch, "announce", embed=embed, kind="announcement")
        await log_action(self.bot, g, "announcement", f"by {interaction.user}: {text[:150]}")
        await interaction.response.send_message("Posted.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Panel(bot))
