"""/expressions: the server's custom emojis and stickers.

The trading pack lives in assets/emojis and assets/stickers (drawn by tools/make_expressions.py).
Files are uploaded best first and skipped by name if already there, so installing twice is safe
and a server short on slots still gets the most useful ones. Standard emojis (the iPhone/keyboard
set) need nothing: Discord already supports all of them for every member.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from ..control import owner_only
from ..safety import log_action
from ..util import brand_color

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EMOJI_DIR, STICKER_DIR = os.path.join(ROOT, "assets", "emojis"), os.path.join(ROOT, "assets", "stickers")
EMOJI_MAX, STICKER_MAX = 256 * 1024, 512 * 1024
TIER_NAMES = {0: "no boost", 1: "Level 1", 2: "Level 2", 3: "Level 3"}
MAX_STICKERS = 30039  # Discord error code: sticker slots full


def emoji_name(stem: str) -> str:
    """'03_green-candle' -> 'green_candle' (Discord: 2-32 letters, digits or underscores)."""
    name = re.sub(r"\W", "_", re.sub(r"^\d+_", "", stem)).strip("_")[:32]
    return name if len(name) >= 2 else f"{name}_e"


def pack_emojis() -> list[tuple[str, bytes]]:
    from ..graphics_emoji import default_emojis
    files = dict(default_emojis())
    if os.path.isdir(EMOJI_DIR):
        for fn in sorted(os.listdir(EMOJI_DIR)):
            if fn.lower().endswith((".png", ".gif", ".jpg", ".jpeg", ".webp")):
                with open(os.path.join(EMOJI_DIR, fn), "rb") as fh:
                    files[emoji_name(os.path.splitext(fn)[0])] = fh.read()
    return list(files.items())


def pack_stickers() -> list[dict]:
    """[{name, emoji, description, path}] from stickers.json, plus any loose PNG dropped in the folder."""
    if not os.path.isdir(STICKER_DIR):
        return []
    meta = []
    try:
        with open(os.path.join(STICKER_DIR, "stickers.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        pass
    known = {m["file"] for m in meta}
    for fn in sorted(os.listdir(STICKER_DIR)):
        if fn.lower().endswith(".png") and fn not in known:
            meta.append({"file": fn, "name": re.sub(r"^\d+_", "", os.path.splitext(fn)[0]).replace("_", " ").title()})
    return [{"name": m["name"][:30], "emoji": m.get("emoji") or "📈", "description": m.get("description") or f"{m['name']} sticker",
             "path": os.path.join(STICKER_DIR, m["file"])} for m in meta if os.path.exists(os.path.join(STICKER_DIR, m["file"]))]


def free_emoji_slots(g: discord.Guild, animated: bool = False) -> int:
    return g.emoji_limit - sum(1 for e in g.emojis if e.animated == animated)


async def install_emojis(g: discord.Guild, reason: str = "Emoji pack") -> tuple[list[discord.Emoji], list[str]]:
    """Upload pack emojis that aren't on the server yet. Returns (added, names left out for lack of slots)."""
    existing, free = {e.name.lower() for e in g.emojis}, free_emoji_slots(g)  # g.emojis may update mid-loop; count once
    made, no_room = [], []
    for name, data in pack_emojis():
        if name.lower() in existing:
            continue
        if len(made) >= free:
            no_room.append(name)
            continue
        try:
            made.append(await g.create_custom_emoji(name=name, image=data, reason=reason))
        except discord.HTTPException as e:
            log.warning("emoji %s failed: %s", name, e)
    return made, no_room


async def install_stickers(g: discord.Guild, reason: str = "Sticker pack") -> tuple[list[discord.GuildSticker], list[str]]:
    existing, free = {s.name.lower() for s in g.stickers}, g.sticker_limit - len(g.stickers)
    made, no_room = [], []
    for s in pack_stickers():
        if s["name"].lower() in existing:
            continue
        if no_room or len(made) >= free:
            no_room.append(s["name"])
            continue
        try:
            made.append(await g.create_sticker(name=s["name"], description=s["description"], emoji=s["emoji"],
                                               file=discord.File(s["path"]), reason=reason))
        except discord.HTTPException as e:
            if e.code == MAX_STICKERS:
                no_room.append(s["name"])
            else:
                log.warning("sticker %s failed: %s", s["name"], e)
    return made, no_room


def fit_image(raw: bytes, size: int, limit: int) -> bytes:
    """Square-pad and shrink an upload to Discord's size; animated GIFs under the limit pass through untouched."""
    img = Image.open(io.BytesIO(raw))
    if getattr(img, "is_animated", False) and len(raw) <= limit:
        return raw
    img = img.convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
    for colors in (None, 256, 128, 64):
        out = io.BytesIO()
        (canvas if colors is None else canvas.quantize(colors)).save(out, "PNG", optimize=True)
        if out.tell() <= limit:
            return out.getvalue()
    raise ValueError("That image is too detailed to fit Discord's size limit; try a simpler one.")


def sticker_sheet(images: list[bytes], cols: int = 5, cell: int = 160) -> bytes:
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell, rows * cell), (0, 0, 0, 0))
    for i, raw in enumerate(images):
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGBA")
        except Exception:
            continue
        img.thumbnail((cell - 12, cell - 12), Image.LANCZOS)
        sheet.alpha_composite(img, ((i % cols) * cell + (cell - img.width) // 2, (i // cols) * cell + (cell - img.height) // 2))
    out = io.BytesIO()
    sheet.save(out, "PNG", optimize=True)
    return out.getvalue()


class Expressions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    expressions = app_commands.Group(name="expressions", description="Custom emojis and stickers", guild_only=True)

    @expressions.command(name="slots", description="How many emoji and sticker slots the server has left")
    @owner_only()
    async def slots(self, interaction: discord.Interaction):
        g = interaction.guild
        static = sum(1 for e in g.emojis if not e.animated)
        animated = len(g.emojis) - static
        missing_e = [n for n, _ in pack_emojis() if n.lower() not in {e.name.lower() for e in g.emojis}]
        missing_s = [s["name"] for s in pack_stickers() if s["name"].lower() not in {x.name.lower() for x in g.stickers}]
        e = discord.Embed(title=f"{g.name} · {TIER_NAMES.get(g.premium_tier, g.premium_tier)} ({g.premium_subscription_count or 0} boosts)",
                          color=brand_color(self.bot, g))
        e.add_field(name="Emojis", value=f"{static}/{g.emoji_limit} used")
        e.add_field(name="Animated emojis", value=f"{animated}/{g.emoji_limit} used")
        e.add_field(name="Stickers", value=f"{len(g.stickers)}/{g.sticker_limit} used")
        e.add_field(name="Trading pack", inline=False,
                    value=f"{len(missing_e)} emojis and {len(missing_s)} stickers not installed yet. `/expressions install` adds what fits.")
        e.set_footer(text="Sticker slots: 5 unboosted, 15 at Level 1, 30 at Level 2, 60 at Level 3")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @expressions.command(name="install", description="Upload the trading emoji and sticker pack (skips ones already there)")
    @owner_only()
    @app_commands.choices(what=[app_commands.Choice(name="Emojis and stickers", value="both"),
                                app_commands.Choice(name="Emojis only", value="emojis"),
                                app_commands.Choice(name="Stickers only", value="stickers")])
    async def install(self, interaction: discord.Interaction, what: str = "both"):
        g = interaction.guild
        if not g.me.guild_permissions.manage_emojis_and_stickers:
            return await interaction.response.send_message("I need the **Manage Expressions** permission first.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = []
        if what in ("both", "emojis"):
            made, no_room = await install_emojis(g)
            lines.append(f"Emojis: added {len(made)}" + (f" ({' '.join(str(x) for x in made)[:1200]})" if made else "")
                         + (f". No room for {len(no_room)}: {', '.join(no_room[:20])}" if no_room else ""))
        if what in ("both", "stickers"):
            made, no_room = await install_stickers(g)
            lines.append(f"Stickers: added {len(made)}" + (f" ({', '.join(s.name for s in made)})" if made else "")
                         + (f". No room for {len(no_room)}: {', '.join(no_room)}. Boosting the server unlocks more sticker slots." if no_room else ""))
        await log_action(self.bot, g, "expressions_install", "; ".join(lines))
        await interaction.followup.send("\n".join(lines)[:1990] or "Nothing to add.", ephemeral=True)

    @expressions.command(name="add-emoji", description="Upload any image as a custom emoji (resized for you)")
    @owner_only()
    @app_commands.describe(name="Letters, numbers and underscores, e.g. gold_bar", image="PNG, JPG, WEBP or GIF")
    async def add_emoji(self, interaction: discord.Interaction, name: str, image: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        try:
            data = fit_image(await image.read(), 128, EMOJI_MAX)
            emoji = await interaction.guild.create_custom_emoji(name=emoji_name(name), image=data, reason=f"Added by {interaction.user}")
        except (ValueError, OSError, discord.HTTPException) as e:
            return await interaction.followup.send(f"Couldn't add it: {getattr(e, 'text', None) or e}", ephemeral=True)
        await interaction.followup.send(f"Added {emoji} as `:{emoji.name}:`", ephemeral=True)

    @expressions.command(name="add-sticker", description="Upload any image as a sticker (resized to 320x320 for you)")
    @owner_only()
    @app_commands.describe(name="Sticker name, e.g. Green Day", image="PNG or JPG", emoji="A related standard emoji, e.g. 💰")
    async def add_sticker(self, interaction: discord.Interaction, name: str, image: discord.Attachment, emoji: str = "📈",
                          description: str | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            data = fit_image(await image.read(), 320, STICKER_MAX)
            sticker = await interaction.guild.create_sticker(name=name[:30], description=(description or f"{name} sticker")[:100],
                                                             emoji=emoji, file=discord.File(io.BytesIO(data), "sticker.png"),
                                                             reason=f"Added by {interaction.user}")
        except (ValueError, OSError, discord.HTTPException) as e:
            return await interaction.followup.send(f"Couldn't add it: {getattr(e, 'text', None) or e}", ephemeral=True)
        await interaction.followup.send(f"Added sticker **{sticker.name}**.", ephemeral=True)

    @expressions.command(name="gallery", description="Post a showcase of the server's emojis and stickers for members")
    @owner_only()
    async def gallery(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        g, ch = interaction.guild, channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        emojis = [e for e in g.emojis if e.available]
        e = discord.Embed(title=f"{g.name} emojis & stickers", color=brand_color(self.bot, g),
                          description="Type `:` plus a name in any message, or open the emoji and sticker buttons in the chat bar. "
                                      "Every standard emoji works too.")
        chunk, shown = "", 0
        for em in emojis:
            item = f"{em} `:{em.name}:`  "
            if len(chunk) + len(item) > 1000:
                if len(e.fields) == 4:  # stay well inside Discord's 6000-character embed limit
                    break
                e.add_field(name="\u200b", value=chunk, inline=False)
                chunk = ""
            chunk += item
            shown += 1
        if chunk:
            e.add_field(name="\u200b", value=chunk + (f"\n+{len(emojis) - shown} more" if shown < len(emojis) else ""), inline=False)
        if not emojis:
            e.add_field(name="Emojis", value="None yet.", inline=False)
        file = None
        pngs = []
        for s in g.stickers:
            if s.format in (discord.StickerFormatType.png, discord.StickerFormatType.apng):
                try:
                    pngs.append(await s.read())
                except discord.HTTPException:
                    pass
        if pngs:
            file = discord.File(io.BytesIO(sticker_sheet(pngs)), "stickers.png")
            e.set_image(url="attachment://stickers.png")
            e.add_field(name="Stickers", value=", ".join(s.name for s in g.stickers)[:1000], inline=False)
        try:
            await ch.send(embed=e, file=file)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"Couldn't post in {ch.mention}: {exc.text}", ephemeral=True)
        await interaction.followup.send(f"Posted in {ch.mention}.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Expressions(bot))
