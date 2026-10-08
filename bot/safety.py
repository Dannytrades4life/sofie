"""Action log and owner approvals.

Routine actions happen automatically and are logged (for the reports and #bot-log).
Risky actions are never executed directly: the owner gets a DM with Approve / Deny
buttons. Buttons survive restarts because they're DynamicItems keyed by approval id.
Any cog can register a handler with @approval_handler("kind").
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from .util import ORANGE, get_channel

if TYPE_CHECKING:
    from .core import TradingBot

log = logging.getLogger(__name__)

Handler = Callable[["TradingBot", dict], Awaitable[str]]
HANDLERS: dict[str, Handler] = {}


def approval_handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


async def log_action(bot: "TradingBot", guild: discord.Guild, kind: str, detail: str) -> None:
    await bot.db.log_action(guild.id, kind, detail)
    ch = get_channel(bot, guild, "bot_log")
    if ch:
        try:
            await ch.send(f"`{kind}` {detail}"[:1900], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def dm_owner(bot: "TradingBot", **kwargs) -> discord.Message | None:
    try:
        owner = await bot.fetch_user(bot.config.owner_id)
        return await owner.send(**kwargs)
    except discord.HTTPException:
        log.error("Could not DM the owner. Are DMs from server members allowed?")
        return None


async def request_approval(bot: "TradingBot", guild: discord.Guild, kind: str, summary: str, payload: dict,
                           *, preview: discord.Embed | None = None) -> int:
    if kind not in HANDLERS:
        raise ValueError(f"No handler for approval kind {kind!r}")
    payload = {"guild_id": guild.id, **payload}
    approval_id = await bot.db.create_approval(guild.id, kind, summary, payload)
    embed = discord.Embed(title=f"Approval needed: {kind}", description=summary[:4000], color=ORANGE)
    embed.set_footer(text=f"Request #{approval_id} • {guild.name}")
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton(approval_id, True))
    view.add_item(ApprovalButton(approval_id, False))
    embeds = [embed] + ([preview.copy()] if preview else [])
    await dm_owner(bot, embeds=embeds, view=view)
    await log_action(bot, guild, "approval_requested", f"#{approval_id} {kind}: {summary[:300]}")
    return approval_id


class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"appr:(?P<ok>[yn]):(?P<id>\d+)"):
    def __init__(self, approval_id: int, approve: bool):
        super().__init__(discord.ui.Button(
            label="Approve" if approve else "Deny",
            style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
            custom_id=f"appr:{'y' if approve else 'n'}:{approval_id}",
        ))
        self.approval_id = approval_id
        self.approve = approve

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["id"]), match["ok"] == "y")

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "TradingBot" = interaction.client  # type: ignore[assignment]
        if interaction.user.id != bot.config.owner_id:
            await interaction.response.send_message("Only the owner can decide this.", ephemeral=True)
            return
        row = await bot.db.decide_approval(self.approval_id, "approved" if self.approve else "denied")
        if row is None:
            await interaction.response.send_message("That request no longer exists.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"Already {row['status']}.", ephemeral=True)
            return
        await interaction.response.defer()
        if self.approve:
            try:
                result = await HANDLERS[row["kind"]](bot, json.loads(row["payload"]))
            except Exception as exc:
                log.exception("Approval #%s failed", self.approval_id)
                result = f"Failed: {exc}"
        else:
            result = "Denied. Nothing was done."
        await interaction.edit_original_response(content=f"**#{self.approval_id}:** {result}"[:2000], view=None)
        guild = bot.get_guild(row["guild_id"])
        if guild:
            await log_action(bot, guild, "approval_decided", f"#{self.approval_id} {row['kind']}: {result[:300]}")


# ---------------------------------------------------------------- built-in risky actions

@approval_handler("ban")
async def _ban(bot: "TradingBot", p: dict) -> str:
    guild = bot.get_guild(p["guild_id"])
    await guild.ban(discord.Object(p["user_id"]), reason=p.get("reason", "Approved by owner"), delete_message_seconds=86400)
    return f"Banned <@{p['user_id']}>."


@approval_handler("announce")
async def _announce(bot: "TradingBot", p: dict) -> str:
    from .control import send_post
    guild = bot.get_guild(p["guild_id"])
    ch = guild.get_channel(p["channel_id"])
    embed = discord.Embed.from_dict(p["embed"]) if p.get("embed") else None
    await send_post(bot, guild, ch, "announce", content=f"@everyone {p.get('content') or ''}".strip(), embed=embed,
                    kind="announcement", allowed_mentions=discord.AllowedMentions(everyone=True))
    return f"Announcement posted in {ch.mention}."


@approval_handler("lockdown")
async def _lockdown(bot: "TradingBot", p: dict) -> str:
    guild = bot.get_guild(p["guild_id"])
    await guild.edit(verification_level=discord.VerificationLevel.high, reason="Raid lockdown approved by owner")
    return "Verification level raised to High. Lower it in Server Settings > Safety Setup when the raid is over."


@approval_handler("post")
async def _post(bot: "TradingBot", p: dict) -> str:
    from .control import send_post
    guild = bot.get_guild(p["guild_id"])
    ch = guild.get_channel(p["channel_id"])
    file = None
    if p.get("file_path") and os.path.exists(p["file_path"]):
        with open(p["file_path"], "rb") as fh:
            file = (fh.read(), p["file_name"])
        os.remove(p["file_path"])
    embed = discord.Embed.from_dict(p["embed"]) if p.get("embed") else None
    msg = await send_post(bot, guild, ch, p["feature"], content=p.get("content"), embed=embed, file=file,
                          kind=p.get("kind", "post"), ref_id=p.get("ref_id"))
    return f"Posted: {msg.jump_url}"
