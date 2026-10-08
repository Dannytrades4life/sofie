"""Rebuild an existing server safely.

  bot joins / first start   snapshot the server, compute the full plan, DM it with ONE Approve/Deny
  (Approve)                 apply stages 1-7 in order without asking again, then DM a summary
  /rebuild plan             do the same by hand (mode:staged asks before each stage instead)
  /rebuild polish    run stage 7 on its own (for servers rebuilt before it existed)
  /rebuild undo      reverse one stage or everything, newest first
  /rebuild snapshot  download a JSON snapshot any time
  /rebuild status

Stages 1-5 delete nothing: channels that don't fit the new layout move to a hidden
🗄 ARCHIVE category with their history intact. Stage 6 is the only one that deletes:
archived channels with no messages for CLEANUP_DAYS, after a text backup of each is
DM'd to the owner, with its own approval listing every channel. Every change records
how to undo it (deleted channels come back empty; messages can't be restored).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..control import owner_only
from ..safety import approval_handler, dm_owner, log_action, request_approval
from ..util import (CATEGORIES, CHANNELS, FOUNDER_ROLE, INVITE_ROLES, LEADER_ROLE, MOD_ROLE, PUBLIC_CATEGORY, ROLE_STYLE, SELF_ROLES,
                    STAFF_ONLY_CATEGORY, VERIFIED_ROLE, brand_color, find_role, get_channel, slug)
from .onboarding import RoleButton, RulesButton

log = logging.getLogger(__name__)

ARCHIVE = "🗄 ARCHIVE"
STAGES = {1: "Roles and colors", 2: "Categories and channels", 3: "Permissions", 4: "Archive leftover channels",
          5: "Branding and welcome content", 6: "Clean up: delete unused channels, tidy the order",
          7: "Polish: role order, founder role, channel headers, let existing members in"}
CLEANUP_DAYS = 60
BACKUP_LIMIT = 5000  # messages saved per deleted channel

# Existing channel names we adopt (rename + move) instead of creating duplicates.
ADOPT = {
    "welcome": ["welcome", "welcomes", "start-here", "introductions", "intro"],
    "rules": ["rules", "server-rules", "info"],
    "roles": ["roles", "pick-roles", "self-roles", "reaction-roles", "get-roles"],
    "announcements": ["announcements", "announcement", "news", "updates"],
    "invite_rewards": ["invites", "invite-rewards", "referrals"],
    "calendar": ["economic-calendar", "calendar", "news-events", "econ-calendar"],
    "premarket": ["premarket", "premarket-plan", "game-plan", "daily-plan", "watchlist"],
    "recap": ["recap", "daily-recap", "eod-recap", "market-recap"],
    "trade_entries": ["trade-entries", "trades", "signals", "alerts", "trade-alerts", "entries", "calls"],
    "results": ["trade-results", "results", "wins", "profits", "pnl", "win-posts"],
    "member_trades": ["member-trades", "member-wins", "your-trades", "share-trades"],
    "journal": ["trade-journal", "journal", "journals"],
    "challenges": ["weekly-challenge", "challenges", "challenge"],
    "lessons": ["daily-lessons", "lessons", "education", "learn"],
    "setups": ["setup-breakdowns", "setups", "strategies"],
    "general": ["general", "chat", "general-chat", "lounge", "main"],
    "chart_talk": ["chart-talk", "charts", "chart", "analysis", "ta"],
    "memes": ["memes-and-gifs", "memes", "gifs", "off-topic", "offtopic"],
    "level_ups": ["level-ups", "levels", "level-up", "rank-ups"],
    "giveaways": ["giveaways", "giveaway"],
    "staff_panel": ["staff-panel", "staff", "staff-chat", "admin", "mods"],
    "trade_submit": ["trade-submit", "submit"],
    "mod_log": ["mod-log", "modlog", "mod-logs", "audit"],
    "bot_log": ["bot-log", "bot-logs", "logs"],
}

RULES = [
    "Be respectful. No harassment, hate or personal attacks.",
    "Nothing here is financial advice. You are responsible for your own trades and risk.",
    "No scams, paid-group shilling, referral links or unsolicited DMs to members.",
    "No pump-and-dump talk or market manipulation.",
    "Post in the right channel: your trades in member-trades, charts in chart-talk, memes in memes-and-gifs.",
    "Staff will never DM you first asking for money, wallets, logins or prop-firm credentials. Report anyone who does.",
    "Self-reported results must be real. Fake screenshots get you removed.",
]


def _norm(name: str) -> str:
    s = slug(name)
    s = re.sub(r"[^\w-]", "", s, flags=re.UNICODE).strip("-_")
    return re.sub(r"^[^a-z0-9]+", "", s)


def _ow_dump(channel: discord.abc.GuildChannel) -> list[dict]:
    out = []
    for target, ow in channel.overwrites.items():
        allow, deny = ow.pair()
        is_role = isinstance(target, discord.Role) or (isinstance(target, discord.Object) and target.type is discord.Role)
        out.append({"id": target.id, "role": is_role, "allow": allow.value, "deny": deny.value})
    return out


def _ow_load(data: list[dict]) -> dict:
    return {discord.Object(o["id"], type=discord.Role if o["role"] else discord.Member):
            discord.PermissionOverwrite.from_pair(discord.Permissions(o["allow"]), discord.Permissions(o["deny"])) for o in data}


def snapshot(guild: discord.Guild) -> dict:
    return {
        "guild": {"id": guild.id, "name": guild.name, "verification_level": str(guild.verification_level)},
        "taken_at": time.time(),
        "roles": [{"id": r.id, "name": r.name, "color": r.color.value, "hoist": r.hoist, "mentionable": r.mentionable,
                   "permissions": r.permissions.value, "position": r.position, "managed": r.managed} for r in guild.roles],
        "channels": [{"id": c.id, "name": c.name, "type": str(c.type), "category_id": c.category_id, "position": c.position,
                      "topic": getattr(c, "topic", None), "slowmode": getattr(c, "slowmode_delay", 0), "nsfw": getattr(c, "nsfw", False),
                      "overwrites": _ow_dump(c)} for c in guild.channels],
    }


# Channels that already get their own content in stage 5; every other layout channel gets a pinned header embed.
HAS_CONTENT = {"welcome", "rules", "roles", "invite_rewards", "staff_panel"}
HEADER_GUIDE = {
    "announcements": "Server news and updates from the team. Turn on notifications so you don't miss anything.",
    "calendar": "Every morning: the day's market-moving events with times in New York time. High-impact ones get a heads-up before they hit.",
    "premarket": "Before the open: key levels, overnight range and the plan for the day.",
    "recap": "After the close: how the day played out, plus a weekly scorecard every Friday.",
    "trade_entries": "Live trade entries with stop, target and planned R:R.",
    "results": "Every closed trade, wins and losses, in points, ticks and R.",
    "member_trades": "Share your own trades with `/share-trade`. Process over P&L: what was the setup, the risk and the lesson?",
    "journal": "Write a few lines about your day. What you saw, what you did, what you'd do differently.",
    "challenges": "A new challenge every week, with a leaderboard and a champion role for the winner.",
    "lessons": "One short futures lesson every day. Questions welcome in chart-talk.",
    "setups": "Setup breakdowns: what the chart showed, why it worked or didn't.",
    "general": "Hang out, say hi and talk markets. Keep it friendly.",
    "chart_talk": "Post charts, levels and ideas. Explain your reasoning so others can learn from it.",
    "memes": "Trading memes and GIFs. Keep it clean.",
    "level_ups": "Chat to earn XP. Level-ups and new ranks are announced here.",
    "giveaways": "Sponsor giveaways. Enter with the button on each post. No purchase necessary.",
    "trade_submit": "Owner only: drop trades, CSVs or screenshots here and I'll draft the posts for you to confirm.",
    "mod_log": "Every moderation action, logged automatically.",
    "bot_log": "Everything I do, logged automatically.",
}
NFA_KEYS = {"premarket", "recap", "trade_entries", "results", "member_trades", "setups", "calendar"}


def role_ops(guild: discord.Guild) -> list[dict]:
    ops = []
    for name, (color, hoist, mentionable) in ROLE_STYLE.items():
        role = find_role(guild, name)
        perms = 0
        if name == LEADER_ROLE:
            perms = discord.Permissions(manage_messages=True, moderate_members=True, kick_members=True, manage_threads=True, mention_everyone=False).value
        elif name == MOD_ROLE:
            perms = discord.Permissions(manage_messages=True, moderate_members=True, manage_threads=True).value
        if role is None:
            ops.append({"op": "create_role", "name": name, "color": color, "hoist": hoist, "mentionable": mentionable, "perms": perms})
        elif role.color.value != color or role.hoist != hoist:
            ops.append({"op": "style_role", "role_id": role.id, "name": name, "color": color, "hoist": hoist})
    return ops


def compute_plan(guild: discord.Guild) -> dict:
    """Pure function of the current server -> list of operations per stage."""
    stages: dict[int, list[dict]] = {n: [] for n in STAGES}
    # Stage 1: roles
    stages[1] += role_ops(guild)
    # Stage 2: structure
    taken: set[int] = set()
    by_norm = {}
    for ch in guild.text_channels:
        by_norm.setdefault(_norm(ch.name), ch)
    for name in CATEGORIES:
        if not discord.utils.get(guild.categories, name=name):
            stages[2].append({"op": "create_category", "name": name})
    for key, (cat, name, topic, _) in CHANNELS.items():
        current = None
        for alias in ADOPT.get(key, [slug(name)]):
            ch = by_norm.get(alias)
            if ch and ch.id not in taken:
                current = ch
                break
        if current:
            taken.add(current.id)
            if current.name != name or (current.category.name if current.category else None) != cat or current.topic != topic:
                stages[2].append({"op": "adopt_channel", "key": key, "channel_id": current.id, "old_name": current.name, "name": name, "category": cat, "topic": topic})
            else:
                stages[2].append({"op": "map_channel", "key": key, "channel_id": current.id, "name": name})
        else:
            stages[2].append({"op": "create_channel", "key": key, "name": name, "category": cat, "topic": topic})
    # Stage 3: permissions on every layout category and channel
    for name in CATEGORIES:
        stages[3].append({"op": "category_perms", "category": name})
    for key in CHANNELS:
        stages[3].append({"op": "channel_perms", "key": key})
    # Stage 4: archive leftovers
    protected = {c.id for c in (guild.rules_channel, guild.public_updates_channel, guild.system_channel) if c}
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel) or ch.id in taken:
            continue
        if ch.category and ch.category.name in CATEGORIES + [ARCHIVE]:
            continue
        if ch.id in protected:
            stages[4].append({"op": "skip_protected", "channel_id": ch.id, "name": ch.name})
            continue
        stages[4].append({"op": "archive", "channel_id": ch.id, "name": ch.name})
    # Stage 5: content
    stages[5] += [{"op": "post_rules"}, {"op": "post_welcome"}, {"op": "post_roles"}, {"op": "post_invite_info"},
                  {"op": "post_staff_guide"}, {"op": "emojis"}, {"op": "stickers"}]
    # Stage 6: worked out after stage 5 from the archive's real activity (see Rebuild.cleanup_ops)
    stages[6].append({"op": "cleanup_pending"})
    # Stage 7: worked out at apply time, once the roles and channels exist (see Rebuild.polish_ops)
    stages[7].append({"op": "polish_pending"})
    return {"stages": {str(k): v for k, v in stages.items()}}


def describe_op(guild: discord.Guild, op: dict) -> str:
    o = op["op"]
    return {
        "create_role": lambda: f"Create role **{op['name']}** (#{op['color']:06X}{', shown separately' if op['hoist'] else ''})",
        "style_role": lambda: f"Recolor role **{op['name']}** to #{op['color']:06X}",
        "create_category": lambda: f"Create category **{op['name']}**",
        "adopt_channel": lambda: f"Rename #{op['old_name']} → **#{op['name']}**, move to {op['category']}, set topic",
        "map_channel": lambda: f"Keep #{op['name']} as-is",
        "create_channel": lambda: f"Create **#{op['name']}** in {op['category']}",
        "category_perms": lambda: f"Set permissions on {op['category']}",
        "channel_perms": lambda: f"Set permissions on #{CHANNELS[op['key']][1]}",
        "archive": lambda: f"Move #{op['name']} to {ARCHIVE} (hidden, history kept)",
        "skip_protected": lambda: f"Leave #{op['name']} alone (Discord community/system channel)",
        "post_rules": lambda: "Post the rules embed with the 'I've read the rules' button",
        "post_welcome": lambda: "Post the welcome / start-here embed",
        "post_roles": lambda: "Post the role picker (NQ, ES, CL, alerts, giveaway pings)",
        "post_invite_info": lambda: "Post how invite rewards work",
        "post_staff_guide": lambda: "Post a command cheat sheet in the staff panel",
        "emojis": lambda: "Upload custom emojis (bundled set + anything in assets/emojis)",
        "stickers": lambda: "Upload stickers from assets/stickers (if any)",
        "cleanup_pending": lambda: f"After stage 5: list archived channels with no messages in {CLEANUP_DAYS} days for deletion "
                                   "(you approve the exact list; each one's history is DM'd to you first), then tidy the channel order",
        "delete_channel": lambda: f"🗑 **Delete** #{op['name']} ({'never used' if not op.get('last') else 'last message ' + op['last'][:10]}), backup DM'd first",
        "keep_archived": lambda: f"Keep #{op['name']} in {ARCHIVE} (active on {op['last'][:10]})",
        "delete_category": lambda: f"🗑 Delete the empty category **{op['name']}**",
        "delete_archive_if_empty": lambda: f"Remove the {ARCHIVE} category if it ends up empty",
        "tidy_order": lambda: "Put categories and channels in the layout order (archive last)",
        "polish_pending": lambda: "After stage 6: order the roles so colors show, give you the 👑 Founder role, "
                                  "pin a styled header in every channel, and give existing members the Trader role so nobody is locked out",
        "role_order": lambda: "Order the roles top to bottom (Founder, staff, ranks, Trader) so the right color shows on each name",
        "give_role": lambda: f"Give **{op['role']}** to {len(op['user_ids'])} member(s)" + (f" ({op['why']})" if op.get("why") else ""),
        "post_header": lambda: f"Pin a styled header in #{CHANNELS[op['key']][1]}",
    }[o]()


def plan_text(guild: discord.Guild, plan: dict) -> str:
    out = [f"# Rebuild plan for {guild.name}", "",
           "Stages 1-5 delete nothing. Stage 6 deletes only the channels listed under it (no messages for "
           f"{CLEANUP_DAYS} days), each after a text backup is DM'd to you; a channel that gets a new message before then is kept. "
           "Every change can be undone with /rebuild undo.", ""]
    for n, title in STAGES.items():
        ops = plan["stages"].get(str(n), [])
        out.append(f"## Stage {n}: {title} ({len(ops)} change{'s' if len(ops) != 1 else ''})")
        out += [f"- {describe_op(guild, op)}" for op in ops] or ["- nothing to do"]
        out.append("")
    return "\n".join(out)


class Rebuild(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.running: set[int] = set()

    def get_plan(self, guild_id: int) -> dict | None:
        return self.bot.db.get_json(guild_id, "rebuild_plan")

    async def record(self, guild: discord.Guild, plan_id: int, stage: int, op: str, undo: dict) -> None:
        await self.bot.db.execute("INSERT INTO rebuild_ops (guild_id, plan_id, stage, op, undo, created_at) VALUES (?,?,?,?,?,?)",
                                  guild.id, plan_id, stage, op, json.dumps(undo), time.time())

    # ------------------------------------------------------------------ commands
    rebuild = app_commands.Group(name="rebuild", description="Snapshot, plan, approve and apply a server rebuild", guild_only=True)

    @rebuild.command(name="snapshot", description="Save and download a snapshot of channels, roles and permissions")
    @owner_only()
    async def snapshot_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        snap = snapshot(interaction.guild)
        sid = await self.bot.db.execute("INSERT INTO snapshots (guild_id, data, created_at) VALUES (?,?,?)", interaction.guild.id, json.dumps(snap), time.time())
        await interaction.followup.send(f"Snapshot #{sid}: {len(snap['roles'])} roles, {len(snap['channels'])} channels.",
                                        file=discord.File(io.BytesIO(json.dumps(snap, indent=1).encode()), filename=f"snapshot-{sid}.json"), ephemeral=True)

    @rebuild.command(name="plan", description="Snapshot the server and DM you the full rebuild plan for approval")
    @owner_only()
    @app_commands.describe(mode="all: one Approve applies every stage (default) · staged: approve each stage")
    @app_commands.choices(mode=[app_commands.Choice(name="all stages with one approval", value="all"),
                                app_commands.Choice(name="approve each stage", value="staged")])
    async def plan_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        problem = await self.start_plan(interaction.guild, mode.value if mode else "all")
        await interaction.followup.send(problem or "Plan and snapshot sent to your DMs. Approve there when you're ready.", ephemeral=True)

    def missing_perms(self, g: discord.Guild) -> list[str]:
        p = g.me.guild_permissions
        if p.administrator:
            return []
        need = {"Manage Roles": p.manage_roles, "Manage Channels": p.manage_channels, "Manage Messages": p.manage_messages,
                "Manage Expressions": p.manage_emojis_and_stickers, "Read Message History": p.read_message_history}
        return [k for k, ok in need.items() if not ok]

    async def start_plan(self, g: discord.Guild, mode: str = "all") -> str | None:
        """Snapshot, compute the plan, DM it. Returns a problem message, or None if the plan was sent."""
        missing = self.missing_perms(g)
        if missing:
            msg = (f"I'm in **{g.name}** but I'm missing: {', '.join(missing)}. Re-open the invite link from the README "
                   "(it asks for the right permissions) or tick Administrator, then run `/rebuild plan`.")
            await dm_owner(self.bot, content=msg)
            return msg
        snap = snapshot(g)
        sid = await self.bot.db.execute("INSERT INTO snapshots (guild_id, data, created_at) VALUES (?,?,?)", g.id, json.dumps(snap), time.time())
        plan = compute_plan(g)
        plan["stages"]["6"] = await self.cleanup_preview(g, plan)
        plan.update(id=int(time.time()), snapshot_id=sid, next_stage=1, applied=[], mode=mode)
        await self.bot.db.set_json(g.id, "rebuild_plan", plan)
        text = plan_text(g, plan)
        await dm_owner(self.bot, content=f"Here's the full upgrade plan for **{g.name}** and a snapshot of the server as it is now. Nothing has changed yet.",
                       files=[discord.File(io.BytesIO(text.encode()), filename="rebuild-plan.md"),
                              discord.File(io.BytesIO(json.dumps(snap, indent=1).encode()), filename=f"snapshot-{sid}.json")])
        if mode == "staged":
            await self.ask_stage(g, plan, 1)
        else:
            await self.ask_all(g, plan)
        await log_action(self.bot, g, "rebuild", f"plan {plan['id']} ({mode}) created from snapshot #{sid}")
        return None

    async def ask_all(self, g: discord.Guild, plan: dict) -> None:
        counts = []
        for n, title in STAGES.items():
            ops = [o for o in plan["stages"].get(str(n), []) if o["op"] not in ("map_channel", "skip_protected", "keep_archived")]
            counts.append(f"{n}. {title}: {len(ops)} change(s)")
        deletes = [describe_op(g, o) for o in plan["stages"]["6"] if o["op"] in ("delete_channel", "delete_category")]
        body = "\n".join(counts)
        body += ("\n\n**Will be deleted in stage 6** (a text backup of each is DM'd to you first):\n" + "\n".join(f"• {d}" for d in deletes[:30])
                 + (f"\n…and {len(deletes) - 30} more (see rebuild-plan.md)" if len(deletes) > 30 else "")) if deletes else "\n\nNothing will be deleted."
        body += ("\n\nEverything else that doesn't fit is moved to a hidden 🗄 ARCHIVE with its history. **Approve** applies every stage "
                 "in order without asking again and DMs you a summary. Undo any time with `/rebuild undo`.\n"
                 "Before approving: Server Settings → Roles → drag my role to the top, so I can recolor your existing roles.")
        await request_approval(self.bot, g, "rebuild_all", f"**Server upgrade for {g.name}**\n{body}", {"plan_id": plan["id"]})

    async def apply_all(self, g: discord.Guild, plan: dict) -> None:
        results = []
        for n in STAGES:
            if n in plan["applied"]:
                continue
            try:
                results.append(await self.apply_stage(g, plan, n))
                await asyncio.sleep(1)
            except Exception as e:  # report and stop; earlier stages stay applied and undoable
                log.exception("rebuild stage %s failed", n)
                results.append(f"Stage {n} ({STAGES[n]}) stopped: {e}. `/rebuild continue` retries from here.")
                break
        done = all(n in plan["applied"] for n in STAGES)
        head = (f"🎉 **{g.name} upgrade done.**" if done else f"⚠️ **{g.name} upgrade stopped partway.**")
        tail = (("\n\nWhat's left in 🗄 ARCHIVE is still in use, so I kept it." if done else "")
                + " Changed your mind on anything? `/rebuild undo stage:N` reverts one stage, `/rebuild undo stage:0` reverts everything.")
        text = head + "\n\n" + "\n\n".join(results) + tail
        for i in range(0, len(text), 1900):
            await dm_owner(self.bot, content=text[i:i + 1900])

    # ---- start by itself when the bot joins the owner's server ----
    async def maybe_auto_start(self, g: discord.Guild) -> None:
        cfg = self.bot.config
        if not cfg.auto_rebuild or (cfg.guild_id and g.id != cfg.guild_id):
            return
        if self.bot.db.get_setting(g.id, "rebuild:auto_started") or self.get_plan(g.id):
            return
        if g.owner_id != cfg.owner_id:
            try:
                await g.fetch_member(cfg.owner_id)
            except discord.HTTPException:
                return  # not the owner's server
        await self.bot.db.set_setting(g.id, "rebuild:auto_started", time.time())
        await dm_owner(self.bot, content=f"👋 I'm in **{g.name}**. Taking a snapshot and working out the upgrade plan now…")
        await self.start_plan(g, "all")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.maybe_auto_start(guild)

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:  # covers being invited while the bot was offline
            await self.maybe_auto_start(g)

    async def cleanup_preview(self, g: discord.Guild, plan: dict) -> list[dict]:
        """Stage 6 worked out up front: channels that will be archived (or already are) with no recent messages."""
        archive = discord.utils.get(g.categories, name=ARCHIVE)
        ids = {o["channel_id"] for o in plan["stages"]["4"] if o["op"] == "archive"} | {c.id for c in (archive.channels if archive else [])}
        cutoff = discord.utils.utcnow().timestamp() - CLEANUP_DAYS * 86400
        ops, kept = [], 0
        for ch in [c for c in g.channels if c.id in ids]:
            if isinstance(ch, discord.CategoryChannel):
                continue
            last = await self._last_activity(ch)
            if last == "unreadable" or (last is not None and last.timestamp() >= cutoff):
                kept += 1
                ops.append({"op": "keep_archived", "channel_id": ch.id, "name": ch.name, "last": last if last == "unreadable" else last.isoformat()})
            else:
                ops.append({"op": "delete_channel", "channel_id": ch.id, "name": ch.name, "last": last.isoformat() if last else None})
        moving = ids | {o["channel_id"] for o in plan["stages"]["2"] if o["op"] in ("adopt_channel", "map_channel")}
        for cat in g.categories:
            if cat.name not in CATEGORIES + [ARCHIVE] and all(c.id in moving for c in cat.channels):
                ops.append({"op": "delete_category", "channel_id": cat.id, "name": cat.name})
        if not kept:
            ops.append({"op": "delete_archive_if_empty"})
        ops.append({"op": "tidy_order"})
        return ops

    async def _last_activity(self, ch):
        """Datetime of the newest message, None if never used, or 'unreadable'."""
        if hasattr(ch, "history"):
            try:
                async for m in ch.history(limit=1):
                    return m.created_at
            except discord.HTTPException:
                return "unreadable"
            return None
        if isinstance(ch, discord.ForumChannel):
            return max((t.created_at for t in ch.threads if t.created_at), default=None)
        return None

    async def polish_ops(self, g: discord.Guild) -> list[dict]:
        """Stage 7, worked out from the server as it is after stages 1-6."""
        ops = role_ops(g)  # covers roles added to the layout after the server was first rebuilt (e.g. Founder)
        ops.append({"op": "role_order"})
        ops.append({"op": "give_role", "role": FOUNDER_ROLE, "user_ids": [self.bot.config.owner_id], "why": "server owner"})
        if not g.chunked:
            await g.chunk()
        trader = find_role(g, VERIFIED_ROLE)
        old = [m.id for m in g.members if not m.bot and (trader is None or trader not in m.roles)]
        if old:
            ops.append({"op": "give_role", "role": VERIFIED_ROLE, "user_ids": old,
                        "why": "existing members, so the new layout doesn't lock them out"})
        for key in CHANNELS:
            if key in HAS_CONTENT or self.bot.db.get_setting(g.id, f"header:{key}"):
                continue
            ops.append({"op": "post_header", "key": key})
        return ops

    def header_embed(self, g: discord.Guild, key: str) -> discord.Embed:
        cat, name, topic, members_post = CHANNELS[key]
        emoji, _, title = name.partition("┃")
        e = discord.Embed(title=f"{emoji} {title.replace('-', ' ').title()}", color=brand_color(self.bot, g),
                          description=HEADER_GUIDE.get(key, topic))
        e.add_field(name="Who can post", value="Everyone" if members_post else "Read only")
        e.add_field(name="Section", value=cat)
        e.set_footer(text=f"{g.name}" + (" · Not financial advice" if key in NFA_KEYS else ""),
                     icon_url=g.icon.url if g.icon else None)
        return e

    @rebuild.command(name="polish", description="Stage 7: role order and colors, founder role, channel headers, unlock existing members")
    @owner_only()
    async def polish_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = interaction.guild
        plan = self.get_plan(g.id)
        if not plan:
            await interaction.followup.send("Run `/rebuild plan` first.", ephemeral=True)
            return
        plan["stages"]["7"] = await self.polish_ops(g)
        plan["applied"] = [s_ for s_ in plan["applied"] if s_ != 7]
        plan["next_stage"] = 7
        await self.bot.db.set_json(g.id, "rebuild_plan", plan)
        await self.ask_stage(g, plan, 7)
        await interaction.followup.send("Stage 7 sent to your DMs. Approve there.", ephemeral=True)

    async def ask_stage(self, guild: discord.Guild, plan: dict, n: int) -> None:
        if n == 6 and not any(o["op"] != "cleanup_pending" for o in plan["stages"]["6"]):
            plan["stages"]["6"] = await self.cleanup_ops(guild)
            await self.bot.db.set_json(guild.id, "rebuild_plan", plan)
        if n == 7 and not any(o["op"] != "polish_pending" for o in plan["stages"].get("7", [])):
            plan["stages"]["7"] = await self.polish_ops(guild)
            await self.bot.db.set_json(guild.id, "rebuild_plan", plan)
        ops = plan["stages"].get(str(n), [])
        lines = [describe_op(guild, o) for o in ops]
        body = "\n".join(f"• {l}" for l in lines[:25]) + (f"\n…and {len(lines) - 25} more" if len(lines) > 25 else "")
        if n == 6 and any(o["op"] in ("delete_channel", "delete_category") for o in ops):
            body = ("⚠️ **This stage deletes channels for good.** Before each one goes, I DM you a text backup of its messages. "
                    "Undo brings the channel back empty, not its messages. Deny to keep everything archived.\n\n") + body
        await request_approval(self.bot, guild, "rebuild_stage", f"**Stage {n}/{len(STAGES)}: {STAGES[n]}**\n{body or 'Nothing to change.'}",
                               {"plan_id": plan["id"], "stage": n})

    async def cleanup_ops(self, g: discord.Guild) -> list[dict]:
        """Archived channels: delete if unused for CLEANUP_DAYS, otherwise keep archived. Then tidy the order."""
        ops: list[dict] = []
        cutoff = discord.utils.utcnow().timestamp() - CLEANUP_DAYS * 86400
        archive = discord.utils.get(g.categories, name=ARCHIVE)
        kept = 0
        for ch in (archive.channels if archive else []):
            last = None
            if hasattr(ch, "history"):
                try:
                    async for m in ch.history(limit=1):
                        last = m.created_at
                except discord.HTTPException:
                    kept += 1
                    ops.append({"op": "keep_archived", "channel_id": ch.id, "name": ch.name, "last": "unreadable"})
                    continue
            elif isinstance(ch, discord.ForumChannel):
                threads = list(ch.threads)
                last = max((t.created_at for t in threads if t.created_at), default=None)
            if last is not None and last.timestamp() >= cutoff:
                kept += 1
                ops.append({"op": "keep_archived", "channel_id": ch.id, "name": ch.name, "last": last.isoformat()})
            else:
                ops.append({"op": "delete_channel", "channel_id": ch.id, "name": ch.name, "last": last.isoformat() if last else None})
        for cat in g.categories:
            if cat.name not in CATEGORIES + [ARCHIVE] and not cat.channels:
                ops.append({"op": "delete_category", "channel_id": cat.id, "name": cat.name})
        if archive and not kept:
            ops.append({"op": "delete_archive_if_empty"})
        ops.append({"op": "tidy_order"})
        return ops

    async def backup_channel(self, ch: discord.abc.GuildChannel) -> None:
        lines = [f"# #{ch.name} ({ch.guild.name}), backed up {discord.utils.utcnow():%Y-%m-%d %H:%M} UTC before deletion", ""]
        if hasattr(ch, "history"):
            async for m in ch.history(limit=BACKUP_LIMIT, oldest_first=True):
                extra = " ".join(a.url for a in m.attachments)
                lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} : {m.content} {extra}".rstrip())
        if len(lines) > 2:
            await dm_owner(self.bot, content=f"Backup of #{ch.name} before I delete it ({len(lines) - 2} messages; attachment links expire).",
                           files=[discord.File(io.BytesIO("\n".join(lines).encode()), filename=f"backup-{slug(ch.name) or ch.id}.txt")])

    @rebuild.command(name="status", description="Where the rebuild is")
    @owner_only()
    async def status_cmd(self, interaction: discord.Interaction):
        plan = self.get_plan(interaction.guild.id)
        if not plan:
            await interaction.response.send_message("No rebuild planned. `/rebuild plan` to start.", ephemeral=True)
            return
        lines = [f"{'✅' if n in plan['applied'] else '⏳' if n == plan['next_stage'] else '○'} Stage {n}: {t}" for n, t in STAGES.items()]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @rebuild.command(name="continue", description="Re-send the approval for the next stage")
    @owner_only()
    async def continue_cmd(self, interaction: discord.Interaction):
        plan = self.get_plan(interaction.guild.id)
        if not plan or plan["next_stage"] > len(STAGES):
            await interaction.response.send_message("Nothing left to apply.", ephemeral=True)
            return
        await self.ask_stage(interaction.guild, plan, plan["next_stage"])
        await interaction.response.send_message(f"Stage {plan['next_stage']} approval sent to your DMs.", ephemeral=True)

    @rebuild.command(name="undo", description="Undo a stage (or 0 for everything), newest changes first")
    @owner_only()
    async def undo_cmd(self, interaction: discord.Interaction, stage: app_commands.Range[int, 0, 7]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = interaction.guild
        plan = self.get_plan(g.id)
        if not plan:
            await interaction.followup.send("Nothing to undo.", ephemeral=True)
            return
        sql = "SELECT * FROM rebuild_ops WHERE guild_id=? AND plan_id=? AND undone=0" + (" AND stage=?" if stage else "") + " ORDER BY id DESC"
        rows = await self.bot.db.fetchall(sql, g.id, plan["id"], *([stage] if stage else []))
        done, failed = 0, []
        for r in rows:
            try:
                await self.undo_one(g, json.loads(r["undo"]))
                await self.bot.db.execute("UPDATE rebuild_ops SET undone=1 WHERE id=?", r["id"])
                done += 1
            except Exception as e:  # keep going; report what couldn't be reverted
                failed.append(f"{r['op']}: {e}")
        undone = set(STAGES) if stage == 0 else {stage}
        plan["applied"] = [s for s in plan["applied"] if s not in undone]
        plan["next_stage"] = min(set(STAGES) - set(plan["applied"]), default=len(STAGES) + 1)
        await self.bot.db.set_json(g.id, "rebuild_plan", plan)
        await log_action(self.bot, g, "rebuild", f"undo stage {stage or 'all'}: {done} reverted, {len(failed)} failed")
        msg = (f"Reverted {done} change(s). Created channels were deleted; adopted and archived channels are back where they were. "
               "`/rebuild continue` re-asks for the next stage, or `/rebuild plan` starts fresh.")
        msg += ("\nCouldn't revert:\n" + "\n".join(failed[:10])) if failed else ""
        await interaction.followup.send(msg[:1900], ephemeral=True)

    # ------------------------------------------------------------------ applying
    async def apply_stage(self, guild: discord.Guild, plan: dict, n: int) -> str:
        if n == 7 and not any(o["op"] != "polish_pending" for o in plan["stages"].get("7", [])):
            plan["stages"]["7"] = await self.polish_ops(guild)
            await self.bot.db.set_json(guild.id, "rebuild_plan", plan)
        ops = plan["stages"].get(str(n), [])
        done, notes = 0, []
        for op in ops:
            try:
                if await self.apply_op(guild, plan, n, op):
                    done += 1
            except discord.HTTPException as e:
                notes.append(f"{describe_op(guild, op)}: {e.text or e}")
        plan["applied"] = sorted(set(plan["applied"]) | {n})
        plan["next_stage"] = n + 1
        await self.bot.db.set_json(guild.id, "rebuild_plan", plan)
        await log_action(self.bot, guild, "rebuild", f"stage {n} applied: {done} changes, {len(notes)} problems")
        out = f"Stage {n} ({STAGES[n]}) done: {done} change(s)."
        if notes:
            out += "\nProblems:\n" + "\n".join(f"• {x}" for x in notes[:10])
        if n == 1:
            out += "\n\n**Check the role order:** in Server Settings > Roles, my role must sit above every role I manage."
        return out

    async def apply_op(self, g: discord.Guild, plan: dict, n: int, op: dict) -> bool:
        o, pid = op["op"], plan["id"]
        if o == "create_role":
            if find_role(g, op["name"]):
                return False
            role = await g.create_role(name=op["name"], colour=discord.Colour(op["color"]), hoist=op["hoist"],
                                       mentionable=op["mentionable"], permissions=discord.Permissions(op["perms"]), reason="Rebuild")
            await self.record(g, pid, n, o, {"do": "delete_role", "id": role.id})
        elif o == "style_role":
            role = g.get_role(op["role_id"])
            if not role:
                return False
            await self.record(g, pid, n, o, {"do": "style_role", "id": role.id, "color": role.color.value, "hoist": role.hoist})
            await role.edit(colour=discord.Colour(op["color"]), hoist=op["hoist"], reason="Rebuild")
        elif o == "create_category":
            if discord.utils.get(g.categories, name=op["name"]):
                return False
            cat = await g.create_category(op["name"], position=CATEGORIES.index(op["name"]), reason="Rebuild")
            await self.record(g, pid, n, o, {"do": "delete_channel", "id": cat.id})
        elif o in ("adopt_channel", "map_channel", "create_channel"):
            key = op["key"]
            old_setting = self.bot.db.get_setting(g.id, f"ch:{key}")
            if o == "create_channel":
                cat = discord.utils.get(g.categories, name=op["category"])
                ch = await g.create_text_channel(op["name"], category=cat, topic=op["topic"], reason="Rebuild")
                await self.record(g, pid, n, o, {"do": "delete_channel", "id": ch.id})
            else:
                ch = g.get_channel(op["channel_id"])
                if ch is None:
                    return False
                if o == "adopt_channel":
                    await self.record(g, pid, n, o, {"do": "restore_channel", "id": ch.id, "name": ch.name, "category_id": ch.category_id,
                                                     "topic": ch.topic, "position": ch.position})
                    cat = discord.utils.get(g.categories, name=op["category"])
                    await ch.edit(name=op["name"], category=cat, topic=op["topic"], sync_permissions=False, reason="Rebuild")
            await self.bot.db.set_setting(g.id, f"ch:{key}", ch.id)
            await self.record(g, pid, n, "setting", {"do": "setting", "key": f"ch:{key}", "value": old_setting})
        elif o == "category_perms":
            cat = discord.utils.get(g.categories, name=op["category"])
            if not cat:
                return False
            await self.record(g, pid, n, o, {"do": "overwrites", "id": cat.id, "data": _ow_dump(cat)})
            await cat.edit(overwrites=self.category_overwrites(g, op["category"]), reason="Rebuild")
        elif o == "channel_perms":
            ch = get_channel(self.bot, g, op["key"])
            if not ch:
                return False
            cat_name, _, _, members_post = CHANNELS[op["key"]]
            ows = self.category_overwrites(g, cat_name)
            trader = find_role(g, VERIFIED_ROLE)
            if not members_post and cat_name not in (STAFF_ONLY_CATEGORY, PUBLIC_CATEGORY) and trader:
                ows[trader] = discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=True,
                                                          create_public_threads=False, send_messages_in_threads=True)
            await self.record(g, pid, n, o, {"do": "overwrites", "id": ch.id, "data": _ow_dump(ch)})
            await ch.edit(overwrites=ows, reason="Rebuild")
        elif o == "archive":
            ch = g.get_channel(op["channel_id"])
            if ch is None:
                return False
            archive = discord.utils.get(g.categories, name=ARCHIVE)
            if archive is None:
                archive = await g.create_category(ARCHIVE, overwrites={g.default_role: discord.PermissionOverwrite(view_channel=False),
                                                                       g.me: discord.PermissionOverwrite(view_channel=True)}, reason="Rebuild archive")
                await self.record(g, pid, n, "create_archive", {"do": "delete_channel", "id": archive.id})
            await self.record(g, pid, n, o, {"do": "restore_channel", "id": ch.id, "name": ch.name, "category_id": ch.category_id,
                                             "position": ch.position, "overwrites": _ow_dump(ch)})
            await ch.edit(category=archive, sync_permissions=True, reason="Rebuild: archived")
        elif o in ("skip_protected", "keep_archived", "cleanup_pending", "polish_pending"):
            return False
        elif o in ("delete_channel", "delete_category"):
            ch = g.get_channel(op["channel_id"])
            if ch is None or (o == "delete_category" and ch.channels):
                return False
            if o == "delete_channel":
                last = await self._last_activity(ch)
                if last == "unreadable" or (last is not None and last.timestamp() >= discord.utils.utcnow().timestamp() - CLEANUP_DAYS * 86400):
                    return False  # someone used it since the plan was approved: keep it archived
                await self.backup_channel(ch)
            await self.record(g, pid, n, o, {"do": "recreate_channel", "name": ch.name, "type": str(ch.type), "topic": getattr(ch, "topic", None),
                                             "category_id": ch.category_id, "position": ch.position, "overwrites": _ow_dump(ch)})
            await ch.delete(reason="Rebuild cleanup (approved by the owner)")
        elif o == "delete_archive_if_empty":
            archive = discord.utils.get(g.categories, name=ARCHIVE)
            if archive is None or archive.channels:
                return False
            await self.record(g, pid, n, o, {"do": "recreate_channel", "name": archive.name, "type": "category", "position": archive.position,
                                             "overwrites": _ow_dump(archive)})
            await archive.delete(reason="Rebuild cleanup: archive empty")
        elif o == "tidy_order":
            before = [{"id": c.id, "position": c.position} for c in g.channels]
            cats = [discord.utils.get(g.categories, name=n_) for n_ in CATEGORIES]
            order = [c for c in cats if c] + [c for c in g.categories if c not in cats]
            payload = [{"id": c.id, "position": i} for i, c in enumerate(order)]
            keys = list(CHANNELS)
            for cat in cats:
                if cat is None:
                    continue
                inside = sorted(cat.text_channels, key=lambda c: next((keys.index(k) for k in keys if get_channel(self.bot, g, k) == c), 99))
                payload += [{"id": c.id, "position": i} for i, c in enumerate(inside)]
            if all(next(b["position"] for b in before if b["id"] == p_["id"]) == p_["position"] for p_ in payload):
                return False
            await self.record(g, pid, n, o, {"do": "positions", "items": before})
            await self.bot.http.bulk_channel_update(g.id, payload, reason="Rebuild: tidy order")
        elif o == "role_order":
            top = g.me.top_role.position
            wanted = [r for r in (find_role(g, name) for name in ROLE_STYLE) if r and not r.managed and r.position < top]
            positions, pos = {}, top - 1
            for r in wanted:
                if pos < 1:
                    break
                positions[r] = pos
                pos -= 1
            if all(r.position == p_ for r, p_ in positions.items()):
                return False
            await self.record(g, pid, n, o, {"do": "role_positions", "items": [[r.id, r.position] for r in g.roles if not r.is_default()]})
            await g.edit_role_positions(positions=positions, reason="Rebuild polish: role order")
        elif o == "give_role":
            role = find_role(g, op["role"])
            if role is None:
                return False
            given = []
            for uid in op["user_ids"]:
                m = g.get_member(uid)
                if m and role not in m.roles:
                    await m.add_roles(role, reason="Rebuild polish" + (f": {op['why']}" if op.get("why") else ""))
                    given.append(uid)
            if not given:
                return False
            await self.record(g, pid, n, o, {"do": "remove_role", "role_id": role.id, "user_ids": given})
        elif o == "post_header":
            ch = get_channel(self.bot, g, op["key"])
            if ch is None:
                return False
            msg = await ch.send(embed=self.header_embed(g, op["key"]))
            try:
                await msg.pin(reason="Channel header")
            except discord.HTTPException:
                pass
            await self.bot.db.set_setting(g.id, f"header:{op['key']}", msg.id)
            await self.record(g, pid, n, o, {"do": "delete_message", "channel_id": ch.id, "id": msg.id})
        elif o.startswith("post_"):
            msg = await self.post_content(g, o)
            if msg is None:
                return False
            await self.record(g, pid, n, o, {"do": "delete_message", "channel_id": msg.channel.id, "id": msg.id})
        elif o == "emojis":
            for emoji in await self.upload_emojis(g):
                await self.record(g, pid, n, o, {"do": "delete_emoji", "id": emoji.id})
        elif o == "stickers":
            for sticker in await self.upload_stickers(g):
                await self.record(g, pid, n, o, {"do": "delete_sticker", "id": sticker.id})
        return True

    def category_overwrites(self, g: discord.Guild, cat_name: str) -> dict:
        everyone, me = g.default_role, g.me
        trader, leaders, mods = find_role(g, VERIFIED_ROLE), find_role(g, LEADER_ROLE), find_role(g, MOD_ROLE)
        bot = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True, manage_messages=True,
                                          manage_channels=True, manage_threads=True, create_public_threads=True, read_message_history=True,
                                          mention_everyone=True)
        staff = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if cat_name == PUBLIC_CATEGORY:
            ows = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False)}
        elif cat_name == STAFF_ONLY_CATEGORY:
            ows = {everyone: discord.PermissionOverwrite(view_channel=False)}
        else:
            ows = {everyone: discord.PermissionOverwrite(view_channel=False)}
            if trader:
                ows[trader] = discord.PermissionOverwrite(view_channel=True)
        for r in (leaders, mods):
            if r:
                ows[r] = staff
        ows[me] = bot
        return ows

    async def post_content(self, g: discord.Guild, op: str) -> discord.Message | None:
        from ..control import identity
        color = brand_color(self.bot, g)
        name = identity(self.bot, g.id)["name"]
        if op == "post_rules":
            ch = get_channel(self.bot, g, "rules")
            e = discord.Embed(title=f"📜 {g.name} · Rules", color=color, description="\n".join(f"**{i}.** {r}" for i, r in enumerate(RULES, 1)))
            e.set_footer(text="Tap the button to unlock the server. Not financial advice.")
            view = discord.ui.View(timeout=None)
            view.add_item(RulesButton())
            return await ch.send(embed=e, view=view) if ch else None
        if op == "post_welcome":
            ch = get_channel(self.bot, g, "welcome")
            rules, roles = get_channel(self.bot, g, "rules"), get_channel(self.bot, g, "roles")
            e = discord.Embed(title=f"👋 Welcome to {g.name}", color=color, description=(
                "A futures day-trading community: NQ, ES, CL and their micros.\n\n"
                f"**1.** Read {rules.mention if rules else 'the rules'} and tap the button.\n"
                f"**2.** Pick your markets and alerts in {roles.mention if roles else 'pick-roles'}.\n"
                "**3.** Say hi in general and share what you trade.\n\n"
                f"I'm **{name}**, the server's bot. I post trades, levels, the economic calendar and daily lessons. "
                "Nothing here is financial advice."))
            if g.icon:
                e.set_thumbnail(url=g.icon.url)
            return await ch.send(embed=e) if ch else None
        if op == "post_roles":
            ch = get_channel(self.bot, g, "roles")
            e = discord.Embed(title="🎭 Pick your roles", color=color, description=(
                "Tap to add or remove.\n**NQ / ES / CL**: what you trade\n**🔔 Trade Alerts**: ping on new trade entries\n"
                "**🗓 Calendar Alerts**: ping before CPI, FOMC, NFP, EIA\n**🎁 Giveaway Pings**: ping when a giveaway starts"))
            view = discord.ui.View(timeout=None)
            for role_name, emoji in SELF_ROLES:
                role = find_role(g, role_name)
                if role:
                    view.add_item(RoleButton(role.id, label=re.sub(r"^\W+\s*", "", role_name), emoji=emoji))
            return await ch.send(embed=e, view=view) if ch else None
        if op == "post_invite_info":
            ch = get_channel(self.bot, g, "invite_rewards")
            ranks = "\n".join(f"**{n} invites** → {r}" for n, r in INVITE_ROLES.items())
            e = discord.Embed(title="🚀 Invite rewards", color=color, description=(
                "Create an invite (right-click the server > Invite People) and share it.\n"
                "An invite counts once your friend has stayed 3 days and their account is at least 7 days old.\n\n"
                f"{ranks}\n\nEvery counted invite also gives you **+1 bonus entry** in giveaways (up to 5). Check yours with `/invites`."))
            return await ch.send(embed=e) if ch else None
        if op == "post_staff_guide":
            ch = get_channel(self.bot, g, "staff_panel")
            e = discord.Embed(title="🛠 Staff panel", color=color, description=(
                "**Owner**: everything. `/feature`, `/perm`, `/teach`, `/identity`, `/rebuild`, `/symbols`, `/sponsor`.\n"
                "**Trades**: `/trade`, `/close`, `/submit`, `/import-journal`, or drop text/screenshots in trade-submit.\n"
                "**Edits**: `/edit #12 caption ...` (mods: text only), `/edit-history`.\n"
                "**Giveaways**: `/giveaway create two Lucid 50K accounts`, `/giveaway list|end|reroll|log`.\n"
                "**Kill switch**: `/pause`, `/kill`, `/resume`, or DM the bot `stop`.\n"
                "Staff commands only work in this channel. Use `/perm list` to see who can do what."))
            return await ch.send(embed=e) if ch else None
        return None

    async def upload_emojis(self, g: discord.Guild) -> list[discord.Emoji]:
        from ..graphics_emoji import default_emojis
        files = dict(default_emojis())
        import os
        folder = "assets/emojis"
        if os.path.isdir(folder):
            for fn in sorted(os.listdir(folder)):
                if fn.lower().endswith((".png", ".gif", ".jpg")):
                    with open(os.path.join(folder, fn), "rb") as fh:
                        files[re.sub(r"\W", "_", os.path.splitext(fn)[0])[:32]] = fh.read()
        made = []
        existing = {e.name for e in g.emojis}
        for name, data in files.items():
            if name in existing or len(g.emojis) + len(made) >= g.emoji_limit:
                continue
            try:
                made.append(await g.create_custom_emoji(name=name, image=data, reason="Rebuild branding"))
            except discord.HTTPException as e:
                log.warning("emoji %s failed: %s", name, e)
        return made

    async def upload_stickers(self, g: discord.Guild) -> list[discord.GuildSticker]:
        import os
        folder, made = "assets/stickers", []
        if not os.path.isdir(folder):
            return made
        existing = {s.name for s in g.stickers}
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(".png") or len(g.stickers) + len(made) >= g.sticker_limit:
                continue
            name = os.path.splitext(fn)[0][:30]
            if name in existing:
                continue
            try:
                made.append(await g.create_sticker(name=name, description=f"{g.name} sticker", emoji="📈",
                                                   file=discord.File(os.path.join(folder, fn)), reason="Rebuild branding"))
            except discord.HTTPException as e:
                log.warning("sticker %s failed: %s", name, e)
        return made

    async def undo_one(self, g: discord.Guild, u: dict) -> None:
        d = u["do"]
        if d == "delete_role":
            role = g.get_role(u["id"])
            if role:
                await role.delete(reason="Rebuild undo")
        elif d == "style_role":
            role = g.get_role(u["id"])
            if role:
                await role.edit(colour=discord.Colour(u["color"]), hoist=u["hoist"], reason="Rebuild undo")
        elif d == "delete_channel":
            ch = g.get_channel(u["id"])
            if ch:
                await ch.delete(reason="Rebuild undo")
        elif d == "restore_channel":
            ch = g.get_channel(u["id"])
            if ch:
                kwargs = {"name": u["name"], "category": g.get_channel(u["category_id"]) if u.get("category_id") else None, "position": u["position"]}
                if "topic" in u:
                    kwargs["topic"] = u["topic"]
                if "overwrites" in u:
                    kwargs["overwrites"] = _ow_load(u["overwrites"])
                await ch.edit(reason="Rebuild undo", **kwargs)
        elif d == "recreate_channel":
            ows = _ow_load(u.get("overwrites") or [])
            if u["type"] == "category":
                await g.create_category(u["name"], overwrites=ows, position=u.get("position"), reason="Rebuild undo")
                return
            cat = g.get_channel(u["category_id"]) if u.get("category_id") else None
            cat = cat or discord.utils.get(g.categories, name=ARCHIVE)
            if u["type"] == "voice":
                await g.create_voice_channel(u["name"], category=cat, overwrites=ows, reason="Rebuild undo")
            else:
                await g.create_text_channel(u["name"], category=cat, topic=u.get("topic"), overwrites=ows, reason="Rebuild undo")
        elif d == "positions":
            await self.bot.http.bulk_channel_update(g.id, u["items"], reason="Rebuild undo")
        elif d == "overwrites":
            ch = g.get_channel(u["id"])
            if ch:
                await ch.edit(overwrites=_ow_load(u["data"]), reason="Rebuild undo")
        elif d == "setting":
            await self.bot.db.set_setting(g.id, u["key"], u["value"])
        elif d == "delete_message":
            ch = g.get_channel(u["channel_id"])
            if ch:
                try:
                    await (await ch.fetch_message(u["id"])).delete()
                except discord.NotFound:
                    pass
        elif d == "role_positions":
            positions = {g.get_role(rid): pos for rid, pos in u["items"]}
            positions = {r: p_ for r, p_ in positions.items() if r and r.position < g.me.top_role.position and not r.managed}
            if positions:
                await g.edit_role_positions(positions=positions, reason="Rebuild undo")
        elif d == "remove_role":
            role = g.get_role(u["role_id"])
            for uid in u["user_ids"] if role else []:
                m = g.get_member(uid)
                if m and role in m.roles:
                    await m.remove_roles(role, reason="Rebuild undo")
        elif d == "delete_emoji":
            emoji = discord.utils.get(g.emojis, id=u["id"])
            if emoji:
                await emoji.delete(reason="Rebuild undo")
        elif d == "delete_sticker":
            sticker = discord.utils.get(g.stickers, id=u["id"])
            if sticker:
                await sticker.delete(reason="Rebuild undo")


@approval_handler("rebuild_all")
async def _approve_all(bot, p: dict) -> str:
    cog: Rebuild = bot.get_cog("Rebuild")
    guild = bot.get_guild(p["guild_id"])
    plan = cog.get_plan(guild.id)
    if not plan or plan["id"] != p["plan_id"]:
        return "That plan has been replaced by a newer one. Use the latest DM."
    if guild.id in cog.running:
        return "Already applying."
    cog.running.add(guild.id)
    task = asyncio.create_task(cog.apply_all(guild, plan))
    task.add_done_callback(lambda _: cog.running.discard(guild.id))
    return "Approved. Applying every stage now; I'll DM you a summary when it's done."


@approval_handler("rebuild_stage")
async def _approve_stage(bot, p: dict) -> str:
    cog: Rebuild = bot.get_cog("Rebuild")
    guild = bot.get_guild(p["guild_id"])
    plan = cog.get_plan(guild.id)
    if not plan or plan["id"] != p["plan_id"]:
        return "That plan has been replaced by a newer one. Use the latest DM."
    n = p["stage"]
    if n in plan["applied"]:
        return f"Stage {n} was already applied."
    if n != plan["next_stage"]:
        return f"Stage {plan['next_stage']} has to go first."
    result = await cog.apply_stage(guild, plan, n)
    if n < len(STAGES):
        await cog.ask_stage(guild, plan, n + 1)
        result += f"\nStage {n + 1} is waiting for your approval below."
    else:
        result += "\n\n🎉 Rebuild complete. Anything still in use stays in 🗄 ARCHIVE (hidden). `/rebuild undo 0` reverts everything."
    return result


async def setup(bot) -> None:
    await bot.add_cog(Rebuild(bot))
