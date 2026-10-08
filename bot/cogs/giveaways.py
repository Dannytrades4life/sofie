"""Sponsor giveaways.

Flow: plain-English request -> bot fills what it can, asks with buttons for the rest -> preview
-> owner approves (leaders' drafts go to the owner's DMs) -> post with an Enter button, live countdown
and entry count -> random draw at the end -> winners announced and DMed a Claim button
-> unclaimed prizes are re-drawn. Every entry, draw, edit and re-draw is logged.
Prizes are delivered manually by the sponsor.
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..control import active, owner_only, staff
from ..safety import approval_handler, dm_owner, log_action, request_approval
from ..util import GIVEAWAY_ROLE, VERIFIED_ROLE, brand_color, find_role, get_channel

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
DEFAULTS = {
    "min_account_days": 14, "min_member_hours": 24, "require_verified": True, "invite_bonus": True, "invite_bonus_cap": 5,
    "eligibility": "Open to 18+ where legal. Void where prohibited.", "claim_hours": 48,
}
DURATIONS = [("24 hours", 24), ("3 days", 72), ("7 days", 168), ("14 days", 336)]
_rng = random.SystemRandom()


def parse_request(text: str, sponsors: list[dict]) -> dict:
    """Offline best-effort parse of 'create a giveaway for two Lucid 50K accounts, 3 days'."""
    t = text.lower()
    out: dict = {}
    for s in sponsors:
        if s["name"].lower() in t:
            out["sponsor"] = s["name"]
            for size in s["sizes"]:
                if re.search(rf"\b{re.escape(size.lower())}\b", t):
                    out["size"] = size
    if m := re.search(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\b\s+(?:\w+\s+){0,3}?(accounts?|prizes?|winners?|evals?|challenges?)", t):
        n = m.group(1)
        out["quantity"] = int(n) if n.isdigit() else NUMBER_WORDS[n]
    if m := re.search(r"(\d+)\s*(h|hours?|d|days?|w|weeks?)\b", t):
        n, unit = int(m.group(1)), m.group(2)[0]
        out["duration_hours"] = n * {"h": 1, "d": 24, "w": 168}[unit]
    return out


def legal_footer(d: dict) -> str:
    sponsor = d.get("sponsor") or "the host"
    return (f"No purchase necessary. {d.get('eligibility', DEFAULTS['eligibility'])} Sponsored by {sponsor}; prizes are delivered by "
            f"{sponsor}. Not affiliated with Discord. Not financial advice.")


class EnterButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gw:enter:(?P<id>\d+)"):
    def __init__(self, gid: int):
        super().__init__(discord.ui.Button(label="Enter", emoji="🎉", style=discord.ButtonStyle.success, custom_id=f"gw:enter:{gid}"))
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Giveaways").enter(interaction, self.gid)


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gw:claim:(?P<id>\d+)"):
    def __init__(self, gid: int):
        super().__init__(discord.ui.Button(label="Claim my prize", emoji="🏆", style=discord.ButtonStyle.success, custom_id=f"gw:claim:{gid}"))
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Giveaways").claim(interaction, self.gid)


class Builder(discord.ui.View):
    """Ephemeral wizard: asks only for what's missing, then shows a preview."""

    def __init__(self, cog: "Giveaways", gid: int, d: dict, sponsor: dict | None, author_id: int):
        super().__init__(timeout=900)
        self.cog, self.gid, self.d, self.sponsor, self.author_id = cog, gid, d, sponsor, author_id
        self._build()

    def _build(self):
        self.clear_items()
        d = self.d
        if self.sponsor and self.sponsor["sizes"] and not d.get("size"):
            sel = discord.ui.Select(placeholder="Which account size?", options=[discord.SelectOption(label=s) for s in self.sponsor["sizes"][:25]])
            sel.callback = self._pick("size", sel)
            self.add_item(sel)
        if not d.get("quantity"):
            sel = discord.ui.Select(placeholder="How many winners?", options=[discord.SelectOption(label=str(n)) for n in (1, 2, 3, 5, 10)])
            sel.callback = self._pick("quantity", sel, int)
            self.add_item(sel)
        if not d.get("duration_hours"):
            sel = discord.ui.Select(placeholder="How long should it run?", options=[discord.SelectOption(label=l, value=str(h)) for l, h in DURATIONS])
            sel.callback = self._pick("duration_hours", sel, int)
            self.add_item(sel)
        if not d.get("channel_key"):
            sel = discord.ui.Select(placeholder="Which channel?", options=[discord.SelectOption(label=f"#{k.replace('_', '-')}", value=k)
                                                                             for k in ("giveaways", "announcements", "general")])
            sel.callback = self._pick("channel_key", sel)
            self.add_item(sel)
        if self.ready:
            post = discord.ui.Button(label="Post it" if self.author_id == self.cog.bot.config.owner_id else "Send to owner for approval",
                                     style=discord.ButtonStyle.success)
            post.callback = self._submit
            self.add_item(post)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    @property
    def ready(self) -> bool:
        d = self.d
        return bool(d.get("quantity") and d.get("duration_hours") and d.get("channel_key") and (d.get("size") or not (self.sponsor and self.sponsor["sizes"])))

    def _pick(self, key, sel, cast=str):
        async def cb(interaction: discord.Interaction):
            self.d[key] = cast(sel.values[0])
            await self.cog.save(self.gid, self.d)
            self._build()
            await interaction.response.edit_message(embed=self.cog.render(interaction.guild, self.d, preview=True), view=self)
        return cb

    async def _submit(self, interaction: discord.Interaction):
        g = interaction.guild
        if interaction.user.id == self.cog.bot.config.owner_id:
            msg = await self.cog.post(g, self.gid, interaction.user.id)
            await interaction.response.edit_message(content=f"🎉 Live: {msg.jump_url}", embed=None, view=None)
        else:
            aid = await request_approval(self.cog.bot, g, "giveaway_post", f"{interaction.user.mention} drafted giveaway #{self.gid}.",
                                         {"giveaway_id": self.gid, "by": interaction.user.id}, preview=self.cog.render(g, self.d, preview=True))
            await interaction.response.edit_message(content=f"Sent to the owner for approval (#{aid}).", embed=None, view=None)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction):
        await self.cog.bot.db.execute("UPDATE giveaways SET status='cancelled' WHERE id=?", self.gid)
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dirty: set[int] = set()  # giveaways whose entry count changed
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ---------------------------------------------------------------- data helpers
    async def sponsors(self, guild_id: int) -> list[dict]:
        rows = await self.bot.db.fetchall("SELECT * FROM sponsors WHERE guild_id=? ORDER BY name", guild_id)
        return [{**dict(r), "sizes": [s.strip() for s in (r["sizes"] or "").split(",") if s.strip()]} for r in rows]

    async def get(self, gid: int) -> tuple[dict, dict] | None:
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", gid)
        return (dict(row), json.loads(row["data"])) if row else None

    async def save(self, gid: int, d: dict) -> None:
        await self.bot.db.execute("UPDATE giveaways SET data=? WHERE id=?", json.dumps(d), gid)

    async def glog(self, gid: int, actor: int | None, event: str, detail: str = "") -> None:
        await self.bot.db.execute("INSERT INTO giveaway_log (giveaway_id, actor_id, event, detail, created_at) VALUES (?,?,?,?,?)",
                                  gid, actor, event, detail[:1000], time.time())

    def title(self, d: dict) -> str:
        q = d.get("quantity") or 1
        size = f" {d['size']}" if d.get("size") else ""
        prize = d.get("prize") or (f"{d['sponsor']}{size} account" if d.get("sponsor") else "Prize")
        return f"{q}x {prize}{'s' if q > 1 and not prize.endswith('s') else ''}" if q > 1 else prize

    def prize_each(self, d: dict) -> str:
        """What one winner gets: 'a Lucid 50K account'."""
        if d.get("sponsor") and not d.get("prize"):
            return f"a {d['sponsor']}{' ' + d['size'] if d.get('size') else ''} account"
        return d.get("prize") or "the prize"

    def render(self, guild: discord.Guild, d: dict, *, preview: bool = False, entries: int = 0, ends_at: float | None = None,
               winners: list[int] | None = None) -> discord.Embed:
        e = discord.Embed(title=f"🎁 GIVEAWAY · {self.title(d)}", color=brand_color(self.bot, guild))
        e.description = d.get("description") or "Tap **Enter** below. Winners are drawn at random when the timer hits zero."
        if winners is not None:
            e.description = "**Ended.** " + (("Winners: " + ", ".join(f"<@{u}>" for u in winners)) if winners else "No eligible entries.")
        e.add_field(name="Winners", value=str(d.get("quantity") or "?"))
        if ends_at:
            e.add_field(name="Ends", value=f"<t:{int(ends_at)}:R>\n<t:{int(ends_at)}:f>")
        elif d.get("duration_hours"):
            e.add_field(name="Runs for", value=f"{d['duration_hours']} hours")
        e.add_field(name="Entries", value=str(entries))
        req = []
        if d.get("min_account_days"):
            req.append(f"Discord account at least {d['min_account_days']} days old")
        if d.get("min_member_hours"):
            req.append(f"In the server at least {d['min_member_hours']} hours")
        if d.get("require_verified"):
            req.append("Accepted the server rules")
        if d.get("required_role"):
            req.append(f"Has the {d['required_role']} role")
        if d.get("invite_bonus"):
            req.append(f"+1 bonus entry per real invite (max {d.get('invite_bonus_cap', 5)})")
        e.add_field(name="Requirements", value="\n".join(f"• {r}" for r in req) or "None", inline=False)
        if d.get("sponsor_rules"):
            e.add_field(name="Sponsor rules", value=d["sponsor_rules"][:1000], inline=False)
        if d.get("sponsor_link"):
            e.add_field(name="Sponsor", value=f"[{d.get('sponsor')}]({d['sponsor_link']})")
        e.set_footer(text=("PREVIEW · " if preview else "") + legal_footer(d))
        return e

    # ---------------------------------------------------------------- creation
    sponsor = app_commands.Group(name="sponsor", description="Manage giveaway sponsors", guild_only=True)

    @sponsor.command(name="add", description="Add or update a sponsor")
    @owner_only()
    @app_commands.describe(sizes="Comma separated, e.g. 25K, 50K, 150K", rules="Sponsor terms shown on giveaways")
    async def sponsor_add(self, interaction: discord.Interaction, name: str, link: str | None = None, sizes: str | None = None,
                          rules: str | None = None, notes: str | None = None):
        await self.bot.db.execute(
            "INSERT INTO sponsors (guild_id, name, link, rules, sizes, notes) VALUES (?,?,?,?,?,?) ON CONFLICT (guild_id, name) DO UPDATE SET "
            "link=COALESCE(excluded.link, link), rules=COALESCE(excluded.rules, rules), sizes=COALESCE(excluded.sizes, sizes), notes=COALESCE(excluded.notes, notes)",
            interaction.guild.id, name, link, rules, sizes, notes)
        await interaction.response.send_message(f"Saved sponsor **{name}**.", ephemeral=True)

    @sponsor.command(name="list", description="List sponsors")
    @owner_only()
    async def sponsor_list(self, interaction: discord.Interaction):
        rows = await self.sponsors(interaction.guild.id)
        text = "\n".join(f"**{s['name']}** · sizes: {', '.join(s['sizes']) or '—'} · {s['link'] or 'no link'}\n  rules: {s['rules'] or '—'}" for s in rows)
        await interaction.response.send_message(text[:1900] or "No sponsors yet. `/sponsor add`.", ephemeral=True)

    @sponsor.command(name="remove", description="Remove a sponsor")
    @owner_only()
    async def sponsor_remove(self, interaction: discord.Interaction, name: str):
        await self.bot.db.execute("DELETE FROM sponsors WHERE guild_id=? AND name=?", interaction.guild.id, name)
        await interaction.response.send_message(f"Removed {name}.", ephemeral=True)

    giveaway = app_commands.Group(name="giveaway", description="Giveaways", guild_only=True)

    @giveaway.command(name="create", description="Describe it in plain English, e.g. 'two Lucid 50K accounts for 3 days'")
    @staff("giveaway")
    async def create(self, interaction: discord.Interaction, request: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = interaction.guild
        sponsors = await self.sponsors(g.id)
        d = {**DEFAULTS}
        ai = await self.bot.llm.json(
            "You turn a Discord server owner's giveaway request into structured fields. Never invent values that weren't stated.",
            f"Request: {request}\nKnown sponsors: {json.dumps([{'name': s['name'], 'sizes': s['sizes']} for s in sponsors])}\n"
            'Format: {"sponsor": str|null, "size": str|null (must be one of that sponsor\'s sizes), "prize": str|null (only if no sponsor), '
            '"quantity": int|null, "duration_hours": int|null, "channel": "giveaways"|"announcements"|"general"|null, '
            '"min_account_days": int|null, "min_member_hours": int|null, "required_role": str|null, "description": str|null}',
        )
        parsed = parse_request(request, sponsors)
        if isinstance(ai, dict):
            for k, v in ai.items():
                if v not in (None, "", []):
                    parsed.setdefault("channel_key" if k == "channel" else k, v)
        d.update(parsed)
        sp = next((s for s in sponsors if s["name"].lower() == str(d.get("sponsor", "")).lower()), None)
        if sp:
            d["sponsor"], d["sponsor_link"], d["sponsor_rules"] = sp["name"], sp["link"], sp["rules"]
            if d.get("size") and d["size"] not in sp["sizes"]:
                d.pop("size")
        elif d.get("sponsor"):
            d.setdefault("prize", f"{d['sponsor']} account")
        gid = await self.bot.db.execute("INSERT INTO giveaways (guild_id, data, created_by, created_at) VALUES (?,?,?,?)",
                                        g.id, json.dumps(d), interaction.user.id, time.time())
        await self.glog(gid, interaction.user.id, "draft", request)
        view = Builder(self, gid, d, sp, interaction.user.id)
        hint = "" if sp or not sponsors else "\nI didn't match a sponsor from your list, so this is a generic prize."
        await interaction.followup.send(f"Draft #{gid}. Pick anything I couldn't tell from your message.{hint}",
                                        embed=self.render(g, d, preview=True), view=view, ephemeral=True)

    async def post(self, guild: discord.Guild, gid: int, actor: int) -> discord.Message:
        from ..control import send_post
        row, d = await self.get(gid)
        ch = get_channel(self.bot, guild, d.get("channel_key", "giveaways")) or get_channel(self.bot, guild, "general")
        if ch is None:
            raise ValueError("No giveaways or general channel found. Run /rebuild first.")
        ends_at = time.time() + d["duration_hours"] * 3600
        view = discord.ui.View(timeout=None)
        view.add_item(EnterButton(gid))
        role = find_role(guild, GIVEAWAY_ROLE)
        msg = await send_post(self.bot, guild, ch, "giveaways", content=role.mention if role else None,
                              embed=self.render(guild, d, ends_at=ends_at), view=view, kind="giveaway", ref_id=gid,
                              allowed_mentions=discord.AllowedMentions(roles=[role] if role else []))
        await self.bot.db.execute("UPDATE giveaways SET status='active', channel_id=?, message_id=?, ends_at=? WHERE id=?", ch.id, msg.id, ends_at, gid)
        await self.glog(gid, actor, "posted", msg.jump_url)
        await log_action(self.bot, guild, "giveaways", f"giveaway #{gid} live: {self.title(d)}")
        return msg

    # ---------------------------------------------------------------- entering
    async def enter(self, interaction: discord.Interaction, gid: int):
        got = await self.get(gid)
        if not got or got[0]["status"] != "active" or got[0]["ends_at"] < time.time():
            await interaction.response.send_message("This giveaway isn't open.", ephemeral=True)
            return
        row, d = got
        m: discord.Member = interaction.user
        now = discord.utils.utcnow()
        problems = []
        if m.bot:
            problems.append("Bots can't enter.")
        if (now - m.created_at) < timedelta(days=d.get("min_account_days", 0)):
            problems.append(f"Your Discord account needs to be {d['min_account_days']}+ days old.")
        if m.joined_at and (now - m.joined_at) < timedelta(hours=d.get("min_member_hours", 0)):
            problems.append(f"You need to be in the server for {d['min_member_hours']}+ hours.")
        if d.get("require_verified") and find_role(interaction.guild, VERIFIED_ROLE) not in m.roles:
            problems.append("Accept the server rules first.")
        if d.get("required_role") and not any(r.name == d["required_role"] for r in m.roles):
            problems.append(f"You need the {d['required_role']} role.")
        if m.is_timed_out():
            problems.append("You're currently timed out.")
        if problems:
            await interaction.response.send_message("Not eligible yet:\n" + "\n".join(f"• {p}" for p in problems), ephemeral=True)
            return
        existing = await self.bot.db.fetchone("SELECT tickets FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", gid, m.id)
        if existing:
            await interaction.response.send_message(f"You're already in with {existing['tickets']} entr{'y' if existing['tickets'] == 1 else 'ies'}. Good luck 🍀", ephemeral=True)
            return
        tickets = 1
        if d.get("invite_bonus"):
            inv = await self.bot.db.fetchone("SELECT COUNT(*) n FROM invites WHERE guild_id=? AND inviter_id=? AND status='valid'", interaction.guild.id, m.id)
            tickets += min(inv["n"], d.get("invite_bonus_cap", 5))
        await self.bot.db.execute("INSERT INTO giveaway_entries (giveaway_id, user_id, tickets, entered_at) VALUES (?,?,?,?)", gid, m.id, tickets, time.time())
        flag = " (no avatar, new-ish account)" if m.avatar is None and (now - m.created_at) < timedelta(days=90) else ""
        await self.glog(gid, m.id, "entry", f"{tickets} ticket(s){flag}")
        self.dirty.add(gid)
        await interaction.response.send_message(f"You're in! {tickets} entr{'y' if tickets == 1 else 'ies'}. Winners get a DM when it ends 🍀", ephemeral=True)

    # ---------------------------------------------------------------- drawing
    async def draw(self, guild: discord.Guild, gid: int, count: int, exclude: set[int], actor: int | None) -> list[int]:
        rows = await self.bot.db.fetchall("SELECT user_id, tickets FROM giveaway_entries WHERE giveaway_id=?", gid)
        pool = []
        for r in rows:
            if r["user_id"] in exclude:
                continue
            member = guild.get_member(r["user_id"])
            if member is None:
                try:
                    member = await guild.fetch_member(r["user_id"])
                except discord.HTTPException:
                    continue  # left the server
            pool.append((r["user_id"], r["tickets"]))
        winners = []
        while pool and len(winners) < count:
            total = sum(t for _, t in pool)
            pick = _rng.uniform(0, total)
            acc = 0
            for i, (uid, t) in enumerate(pool):
                acc += t
                if pick <= acc:
                    winners.append(uid)
                    pool.pop(i)
                    break
        await self.glog(gid, actor, "draw", f"{len(rows)} entries, {len(pool) + len(winners)} eligible, winners: {winners}")
        return winners

    async def award(self, guild: discord.Guild, gid: int, winners: list[int], d: dict) -> None:
        claim_by = time.time() + d.get("claim_hours", 48) * 3600
        for uid in winners:
            await self.bot.db.execute("INSERT OR REPLACE INTO giveaway_winners (giveaway_id, user_id, status, drawn_at, claim_by) VALUES (?,?,?,?,?)",
                                      gid, uid, "pending", time.time(), claim_by)
            view = discord.ui.View(timeout=None)
            view.add_item(ClaimButton(gid))
            try:
                user = await self.bot.fetch_user(uid)
                await user.send(f"🎉 You won **{self.prize_each(d)}** in {guild.name}! Tap below within {d.get('claim_hours', 48)} hours to claim it. "
                                f"The prize is delivered by {d.get('sponsor') or 'the host'}.", view=view)
            except discord.HTTPException:
                await self.glog(gid, None, "dm_failed", str(uid))

    async def finish(self, guild: discord.Guild, gid: int, actor: int | None = None) -> list[int]:
        row, d = await self.get(gid)
        winners = await self.draw(guild, gid, d.get("quantity", 1), set(), actor)
        await self.bot.db.execute("UPDATE giveaways SET status='ended' WHERE id=?", gid)
        ch = guild.get_channel(row["channel_id"])
        n = await self.bot.db.fetchone("SELECT COUNT(*) n FROM giveaway_entries WHERE giveaway_id=?", gid)
        if ch:
            try:
                msg = await ch.fetch_message(row["message_id"])
                await msg.edit(embed=self.render(guild, d, entries=n["n"], ends_at=row["ends_at"], winners=winners), view=None)
                if winners:
                    await msg.reply(f"🎉 Congrats {', '.join(f'<@{u}>' for u in winners)}! You won **{self.title(d)}**. Check your DMs to claim "
                                    f"within {d.get('claim_hours', 48)}h (DMs closed? message a staff member).")
            except discord.HTTPException:
                pass
        await self.award(guild, gid, winners, d)
        await log_action(self.bot, guild, "giveaways", f"giveaway #{gid} ended: {n['n']} entries, winners {winners}")
        await dm_owner(self.bot, content=f"🎁 Giveaway #{gid} **{self.title(d)}** ended with {n['n']} entries. Winners: "
                                          f"{', '.join(f'<@{u}>' for u in winners) or 'none'}. I'll tell you as they claim.")
        return winners

    async def claim(self, interaction: discord.Interaction, gid: int):
        row = await self.bot.db.fetchone("SELECT * FROM giveaway_winners WHERE giveaway_id=? AND user_id=?", gid, interaction.user.id)
        if not row:
            await interaction.response.send_message("I don't have you as a winner for this one.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"Already {row['status']}.", ephemeral=True)
            return
        if row["claim_by"] < time.time():
            await interaction.response.send_message("Sorry, the claim window closed.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE giveaway_winners SET status='claimed' WHERE giveaway_id=? AND user_id=?", gid, interaction.user.id)
        await self.glog(gid, interaction.user.id, "claimed")
        _, d = await self.get(gid)
        await interaction.response.edit_message(content=f"✅ Claimed! Staff will connect you with {d.get('sponsor') or 'the host'} for delivery.", view=None)
        await dm_owner(self.bot, content=f"🏆 <@{interaction.user.id}> ({interaction.user}) claimed **{self.prize_each(d)}** from giveaway #{gid}. "
                                          f"Pass their details to {d.get('sponsor') or 'the sponsor'} for delivery.")

    @tasks.loop(seconds=30)
    async def loop(self):
        now = time.time()
        # live entry counts (edit at most once per 30s per giveaway)
        for gid in list(self.dirty):
            self.dirty.discard(gid)
            got = await self.get(gid)
            if not got or got[0]["status"] != "active":
                continue
            row, d = got
            guild = self.bot.get_guild(row["guild_id"])
            ch = guild and guild.get_channel(row["channel_id"])
            n = await self.bot.db.fetchone("SELECT COUNT(*) n FROM giveaway_entries WHERE giveaway_id=?", gid)
            try:
                msg = await ch.fetch_message(row["message_id"])
                await msg.edit(embed=self.render(guild, d, entries=n["n"], ends_at=row["ends_at"]))
            except (discord.HTTPException, AttributeError):
                pass
        if self.bot.paused:
            return  # kill switch: no draws or re-draws until resumed
        for row in await self.bot.db.fetchall("SELECT id, guild_id FROM giveaways WHERE status='active' AND ends_at <= ?", now):
            guild = self.bot.get_guild(row["guild_id"])
            if guild and active(self.bot, guild.id, "giveaways"):
                await self.finish(guild, row["id"])
        # unclaimed prizes -> re-draw
        for w in await self.bot.db.fetchall("SELECT w.*, g.guild_id FROM giveaway_winners w JOIN giveaways g ON g.id = w.giveaway_id "
                                            "WHERE w.status='pending' AND w.claim_by <= ?", now):
            guild = self.bot.get_guild(w["guild_id"])
            if not guild:
                continue
            await self.bot.db.execute("UPDATE giveaway_winners SET status='expired' WHERE giveaway_id=? AND user_id=?", w["giveaway_id"], w["user_id"])
            await self.glog(w["giveaway_id"], None, "expired", str(w["user_id"]))
            await self.reroll(guild, w["giveaway_id"], 1, None, reason=f"<@{w['user_id']}> didn't claim in time")

    async def reroll(self, guild: discord.Guild, gid: int, count: int, actor: int | None, reason: str) -> list[int]:
        row, d = await self.get(gid)
        past = {r["user_id"] for r in await self.bot.db.fetchall("SELECT user_id FROM giveaway_winners WHERE giveaway_id=?", gid)}
        winners = await self.draw(guild, gid, count, past, actor)
        await self.glog(gid, actor, "reroll", reason)
        ch = guild.get_channel(row["channel_id"])
        if ch:
            try:
                await ch.send(f"🔁 Re-draw for **{self.title(d)}** ({reason}): " + (", ".join(f"<@{u}>" for u in winners) or "no eligible entries left") + ". Check your DMs!")
            except discord.HTTPException:
                pass
        await self.award(guild, gid, winners, d)
        await log_action(self.bot, guild, "giveaways", f"giveaway #{gid} re-draw ({reason}): {winners}")
        return winners

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------- staff controls
    @giveaway.command(name="list", description="Active and recent giveaways")
    @staff("giveaway-admin")
    async def list_cmd(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM giveaways WHERE guild_id=? ORDER BY id DESC LIMIT 15", interaction.guild.id)
        lines = []
        for r in rows:
            d = json.loads(r["data"])
            n = await self.bot.db.fetchone("SELECT COUNT(*) n FROM giveaway_entries WHERE giveaway_id=?", r["id"])
            lines.append(f"`#{r['id']}` **{self.title(d)}** · {r['status']} · {n['n']} entries" + (f" · ends <t:{int(r['ends_at'])}:R>" if r["ends_at"] else ""))
        await interaction.response.send_message("\n".join(lines) or "None yet.", ephemeral=True)

    @giveaway.command(name="end", description="End a giveaway now and draw winners")
    @staff("giveaway-admin")
    async def end_cmd(self, interaction: discord.Interaction, giveaway_id: int):
        got = await self.get(giveaway_id)
        if not got or got[0]["status"] != "active":
            await interaction.response.send_message("Not an active giveaway.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await self.finish(interaction.guild, giveaway_id, interaction.user.id)
        await interaction.followup.send(f"Ended. Winners: {', '.join(f'<@{u}>' for u in winners) or 'none'}", ephemeral=True)

    @giveaway.command(name="reroll", description="Draw replacement winner(s)")
    @staff("giveaway-admin")
    async def reroll_cmd(self, interaction: discord.Interaction, giveaway_id: int, count: app_commands.Range[int, 1, 10] = 1, reason: str = "staff re-draw"):
        got = await self.get(giveaway_id)
        if not got or got[0]["status"] != "ended":
            await interaction.response.send_message("You can only re-draw an ended giveaway.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await self.reroll(interaction.guild, giveaway_id, count, interaction.user.id, f"{reason}, by {interaction.user}")
        await interaction.followup.send(f"Re-drew: {', '.join(f'<@{u}>' for u in winners) or 'nobody eligible'}", ephemeral=True)

    @giveaway.command(name="cancel", description="Cancel a giveaway")
    @owner_only()
    async def cancel_cmd(self, interaction: discord.Interaction, giveaway_id: int, reason: str):
        got = await self.get(giveaway_id)
        if not got:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        row, d = got
        await self.bot.db.execute("UPDATE giveaways SET status='cancelled' WHERE id=?", giveaway_id)
        await self.glog(giveaway_id, interaction.user.id, "cancelled", reason)
        if row["message_id"]:
            try:
                msg = await interaction.guild.get_channel(row["channel_id"]).fetch_message(row["message_id"])
                e = self.render(interaction.guild, d)
                e.description = f"**Cancelled:** {reason}"
                await msg.edit(embed=e, view=None)
            except (discord.HTTPException, AttributeError):
                pass
        await interaction.response.send_message("Cancelled and logged.", ephemeral=True)

    @giveaway.command(name="edit", description="Change a giveaway (logged)")
    @owner_only()
    @app_commands.choices(field=[app_commands.Choice(name=n, value=n) for n in
                                 ("prize", "description", "quantity", "add_hours", "min_account_days", "min_member_hours", "required_role", "eligibility")])
    async def edit_cmd(self, interaction: discord.Interaction, giveaway_id: int, field: app_commands.Choice[str], value: str):
        got = await self.get(giveaway_id)
        if not got or got[0]["status"] not in ("draft", "active"):
            await interaction.response.send_message("Only drafts and active giveaways can be edited.", ephemeral=True)
            return
        row, d = got
        key = field.value
        if key == "add_hours":
            old, new = row["ends_at"], row["ends_at"] + float(value) * 3600 if row["ends_at"] else None
            if new:
                await self.bot.db.execute("UPDATE giveaways SET ends_at=? WHERE id=?", new, giveaway_id)
        else:
            old = d.get(key)
            new = int(value) if key in ("quantity", "min_account_days", "min_member_hours") else value
            d[key] = new
            await self.save(giveaway_id, d)
        await self.glog(giveaway_id, interaction.user.id, "edit", f"{key}: {old} -> {new}")
        await self.bot.db.edit_log(interaction.guild.id, f"giveaway:{giveaway_id}", key, old, new, interaction.user.id)
        self.dirty.add(giveaway_id)
        await interaction.response.send_message(f"Updated {key}.", ephemeral=True)

    @giveaway.command(name="log", description="Full log for a giveaway")
    @staff("giveaway-admin")
    async def log_cmd(self, interaction: discord.Interaction, giveaway_id: int):
        rows = await self.bot.db.fetchall("SELECT * FROM giveaway_log WHERE giveaway_id=? ORDER BY id", giveaway_id)
        entries = [r for r in rows if r["event"] == "entry"]
        other = [f"<t:{int(r['created_at'])}:f> **{r['event']}** {('<@' + str(r['actor_id']) + '>') if r['actor_id'] else ''} {r['detail'] or ''}"
                 for r in rows if r["event"] != "entry"]
        await interaction.response.send_message((f"{len(entries)} entries logged.\n" + "\n".join(other))[:1900], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


@approval_handler("giveaway_post")
async def _approve_post(bot, p: dict) -> str:
    cog: Giveaways = bot.get_cog("Giveaways")
    guild = bot.get_guild(p["guild_id"])
    msg = await cog.post(guild, p["giveaway_id"], bot.config.owner_id)
    return f"Giveaway live: {msg.jump_url}"


async def setup(bot) -> None:
    bot.add_dynamic_items(EnterButton, ClaimButton)
    await bot.add_cog(Giveaways(bot))
