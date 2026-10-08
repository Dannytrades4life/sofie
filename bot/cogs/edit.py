"""/edit and /edit-history: change any field on a bot-made post, regenerate the graphic,
update the original message in place, and keep a full edit log.

Owner: every field. Leaders/Mods (if allowed by /perm): text only (title, caption, notes).
The values first posted are kept in trades.original and never overwritten.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from ..control import OWNER, persona, staff, staff_level
from ..safety import log_action

TEXT_FIELDS = {"title": "Title / name", "caption": "Caption", "notes": "Notes"}
BASE_NUMBERS = {"entry": "Entry", "exit": "Exit", "stop": "Stop", "target": "Target", "contracts": "Contracts"}
DERIVED_NUMBERS = {"pnl_usd": "P&L ($)", "pnl_pct": "Percent", "r_multiple": "R multiple", "points": "Points", "ticks": "Ticks"}
OWNER_TEXT = {"symbol": "Symbol", "side": "Direction", "show_usd": "Show $ and contracts (on/off/default)"}
ALL_FIELDS = {**TEXT_FIELDS, **OWNER_TEXT, **BASE_NUMBERS, **DERIVED_NUMBERS}

LINK_RE = re.compile(r"discord\.com/channels/\d+/(\d+)/(\d+)")


class Edit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _resolve(self, guild: discord.Guild, target: str) -> tuple[str, int | None, int | None]:
        """Returns (kind, trade_id, message_id). kind is 'trade' or 'message'."""
        target = target.strip()
        if m := LINK_RE.search(target):
            msg_id = int(m.group(2))
            row = await self.bot.db.fetchone("SELECT kind, ref_id FROM posts WHERE message_id = ? AND guild_id = ?", msg_id, guild.id)
            if row and row["kind"].startswith("trade_") and row["ref_id"]:
                return "trade", row["ref_id"], msg_id
            if row:
                return "message", None, msg_id
            raise ValueError("That message wasn't posted by me, so I can't edit it.")
        if m := re.fullmatch(r"#?(\d{1,9})", target):
            return "trade", int(m.group(1)), None
        raise ValueError("Give a trade number like `#12` or a message link (right-click the post > Copy Message Link).")

    @app_commands.command(name="edit", description="Edit a bot post or trade; the graphic is regenerated")
    @app_commands.guild_only()
    @staff("edit")
    @app_commands.describe(target="Trade number (#12) or message link", field="What to change",
                           value="New value. For caption, type 'auto' to have me rewrite it.")
    @app_commands.choices(field=[app_commands.Choice(name=v, value=k) for k, v in ALL_FIELDS.items()][:25])
    async def edit(self, interaction: discord.Interaction, target: str, field: app_commands.Choice[str], value: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, key = interaction.guild, field.value
        is_owner = staff_level(self.bot, interaction.user) == OWNER
        if key not in TEXT_FIELDS and not is_owner:
            await interaction.followup.send("Only the owner can change numbers, the symbol or the direction. You can fix the title, caption or notes.", ephemeral=True)
            return
        try:
            kind, trade_id, msg_id = await self._resolve(guild, target)
            if kind == "trade":
                result = await self.edit_trade(guild, trade_id, key, value, interaction.user.id)
            else:
                result = await self.edit_message(guild, msg_id, key, value, interaction.user.id)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        await interaction.followup.send(result, ephemeral=True)

    async def edit_trade(self, guild: discord.Guild, trade_id: int, key: str, value: str, editor_id: int) -> str:
        trades = self.bot.get_cog("Trades")
        trade = await self.bot.db.get_trade(guild.id, trade_id)
        if trade is None:
            raise ValueError(f"Trade #{trade_id} not found.")
        d = dict(trade["data"])
        closed = trade["status"] == "closed"
        # Title and caption apply to the latest post: the result once closed, otherwise the entry.
        store = {"caption": "result_caption", "title": "result_title"}.get(key, key) if closed else key
        old = d.get("contract") if key == "symbol" else d.get(store)
        if key in TEXT_FIELDS:
            if key == "caption" and value.strip().lower() == "auto":
                new = await trades.caption(guild, d, "result" if closed else "entry")
            else:
                new = value
            d[store] = new
        elif key == "symbol":
            new = value.upper().lstrip("/")
            d["contract"] = new
            d.pop("symbol", None)
        elif key == "show_usd":
            v = value.strip().lower()
            if v not in ("on", "off", "default", "yes", "no"):
                raise ValueError("Type on, off, or default (follow the /display setting).")
            new = None if v == "default" else v in ("on", "yes")
            old = d.get("show_usd")
            for k in ("show_usd", "show_size"):
                d.pop(k, None) if new is None else d.__setitem__(k, new)
        elif key == "side":
            new = value.lower()
            if new not in ("long", "short"):
                raise ValueError("Direction must be long or short.")
            d["side"] = new
        else:
            try:
                new = float(value.replace(",", "").replace("$", "").replace("%", "").replace("R", ""))
            except ValueError:
                raise ValueError("That needs to be a number.")
            if key == "contracts":
                new = int(new)
            d[key] = new
            if key in DERIVED_NUMBERS:
                d["overrides"] = sorted(set(d.get("overrides", [])) | {key})
        d = await trades.normalize(guild.id, d)
        await self.bot.db.execute("UPDATE trades SET data = ? WHERE id = ?", json.dumps(d), trade_id)
        await self.bot.db.edit_log(guild.id, f"trade:{trade_id}", key, old, new, editor_id)
        updated = []
        link = None
        if trade["entry_message_id"]:
            link = f"https://discord.com/channels/{guild.id}/{trade['entry_channel_id']}/{trade['entry_message_id']}"
            if await self._rewrite(guild, trade["entry_channel_id"], trade["entry_message_id"], *(await trades.render(guild, trade_id, d, "entry")), trade_id):
                updated.append("entry post")
        if trade["status"] == "closed" and trade["result_message_id"]:
            if await self._rewrite(guild, trade["result_channel_id"], trade["result_message_id"], *(await trades.render(guild, trade_id, d, "result", entry_link=link)), trade_id):
                updated.append("result post")
        await log_action(self.bot, guild, "edit", f"trade #{trade_id} {key}: {old} → {new} by <@{editor_id}>")
        return f"Trade #{trade_id}: **{ALL_FIELDS[key]}** `{old}` → `{new}`. Updated: {', '.join(updated) or 'no posted messages found'}."

    async def _rewrite(self, guild, channel_id, message_id, embed, png, trade_id) -> bool:
        ch = guild.get_channel(channel_id)
        if ch is None:
            return False
        try:
            msg = await ch.fetch_message(message_id)
            await msg.edit(embed=embed, attachments=[discord.File(io.BytesIO(png), filename=f"trade-{trade_id}.png")])
            return True
        except discord.HTTPException:
            return False

    async def edit_message(self, guild: discord.Guild, msg_id: int, key: str, value: str, editor_id: int) -> str:
        if key not in ("title", "caption"):
            raise ValueError("For non-trade posts I can change the title or caption.")
        row = await self.bot.db.fetchone("SELECT channel_id FROM posts WHERE message_id = ?", msg_id)
        ch = guild.get_channel(row["channel_id"])
        msg = await ch.fetch_message(msg_id)
        if msg.embeds:
            e = msg.embeds[0]
            old = e.title if key == "title" else e.description
            if key == "caption" and value.strip().lower() == "auto":
                value = await self.bot.llm.chat(persona(self.bot, guild), f"Rewrite this post text in your voice, same meaning, same length:\n{old}") or old
            if key == "title":
                e.title = value
            else:
                e.description = value
            await msg.edit(embed=e)
        else:
            old = msg.content
            await msg.edit(content=value)
        await self.bot.db.edit_log(guild.id, f"msg:{msg_id}", key, old, value, editor_id)
        await log_action(self.bot, guild, "edit", f"message {msg.jump_url} {key} changed by <@{editor_id}>")
        return f"Updated {msg.jump_url}"

    @app_commands.command(name="edit-history", description="Show the edit log for a trade or post")
    @app_commands.guild_only()
    @staff("edit-history")
    async def history(self, interaction: discord.Interaction, target: str):
        try:
            kind, trade_id, msg_id = await self._resolve(interaction.guild, target)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        key = f"trade:{trade_id}" if kind == "trade" else f"msg:{msg_id}"
        rows = await self.bot.db.fetchall("SELECT * FROM edit_log WHERE guild_id=? AND target=? ORDER BY id", interaction.guild.id, key)
        lines = [f"`{datetime.fromtimestamp(r['created_at']):%b %d %H:%M}` <@{r['editor_id']}> **{ALL_FIELDS.get(r['field'], r['field'])}** `{r['old_value']}` → `{r['new_value']}`" for r in rows]
        if kind == "trade":
            t = await self.bot.db.get_trade(interaction.guild.id, trade_id)
            if t:
                o = t["original"]
                lines.insert(0, f"**Originally posted:** {o.get('side')} {o.get('contract')} entry {o.get('entry')} exit {o.get('exit')} "
                                f"points {o.get('points')} R {o.get('r_multiple')}")
        await interaction.response.send_message("\n".join(lines)[:1900] or "No edits.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot) -> None:
    await bot.add_cog(Edit(bot))
