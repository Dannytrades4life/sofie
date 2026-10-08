"""Growth: invite tracking with referral rewards, and social content ideas with tracked invite links.

An invite counts ("valid") when the new member stays 3 days and their account was at least
7 days old when they joined. Valid invites earn rank roles and bonus giveaway entries.
Each social platform gets its own invite link, so the reports show which platform brings people in.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..control import active, persona, staff
from ..safety import log_action
from ..util import INVITE_ROLES, brand_color, find_role, get_channel

VALID_AFTER_DAYS, MIN_ACCOUNT_DAYS = 3, 7
PLATFORMS = ["x", "tiktok", "youtube", "instagram", "reddit"]


class Growth(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache: dict[int, dict[str, int]] = {}
        self.validate.start()

    def cog_unload(self):
        self.validate.cancel()

    async def snapshot_invites(self, guild: discord.Guild) -> dict[str, int]:
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            return {}
        uses = {i.code: i.uses or 0 for i in invites}
        self.cache[guild.id] = uses
        return uses

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            await self.snapshot_invites(g)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            self.cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            self.cache.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        before = dict(self.cache.get(member.guild.id, {}))
        after = await self.snapshot_invites(member.guild)
        used = [c for c, n in after.items() if n > before.get(c, 0)]
        code = used[0] if len(used) == 1 else None
        inviter = None
        if code:
            try:
                inv = next(i for i in await member.guild.invites() if i.code == code)
                inviter = inv.inviter.id if inv.inviter else None
            except (StopIteration, discord.HTTPException):
                pass
        status = "pending"
        if inviter == member.id or (discord.utils.utcnow() - member.created_at) < timedelta(days=MIN_ACCOUNT_DAYS):
            status = "rejected"
        await self.bot.db.execute("INSERT OR REPLACE INTO invites (guild_id, invitee_id, inviter_id, code, joined_at, status) VALUES (?,?,?,?,?,?)",
                                  member.guild.id, member.id, inviter, code, time.time(), status)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.bot.db.execute("UPDATE invites SET status='left' WHERE guild_id=? AND invitee_id=? AND status='pending'", member.guild.id, member.id)

    async def valid_count(self, guild_id: int, user_id: int) -> int:
        row = await self.bot.db.fetchone("SELECT COUNT(*) n FROM invites WHERE guild_id=? AND inviter_id=? AND status='valid'", guild_id, user_id)
        return row["n"]

    @tasks.loop(minutes=30)
    async def validate(self):
        cutoff = time.time() - VALID_AFTER_DAYS * 86400
        for row in await self.bot.db.fetchall("SELECT * FROM invites WHERE status='pending' AND joined_at<=?", cutoff):
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            if guild.get_member(row["invitee_id"]) is None:
                await self.bot.db.execute("UPDATE invites SET status='left' WHERE guild_id=? AND invitee_id=?", guild.id, row["invitee_id"])
                continue
            await self.bot.db.execute("UPDATE invites SET status='valid' WHERE guild_id=? AND invitee_id=?", guild.id, row["invitee_id"])
            if not row["inviter_id"] or not active(self.bot, guild.id, "invites"):
                continue
            inviter = guild.get_member(row["inviter_id"])
            n = await self.valid_count(guild.id, row["inviter_id"])
            if inviter and n in INVITE_ROLES:
                role = find_role(guild, INVITE_ROLES[n])
                if role:
                    try:
                        await inviter.add_roles(role, reason=f"{n} valid invites")
                    except discord.HTTPException:
                        pass
                ch = get_channel(self.bot, guild, "invite_rewards") or get_channel(self.bot, guild, "general")
                if ch:
                    await ch.send(f"🚀 {inviter.mention} has brought in **{n}** traders and earned **{INVITE_ROLES[n]}**! Thank you 🙏")
                await log_action(self.bot, guild, "invites", f"{inviter} reached {n} valid invites")

    @validate.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="invites", description="How many people you (or someone) invited")
    @app_commands.guild_only()
    async def invites_cmd(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        g = interaction.guild.id
        rows = await self.bot.db.fetchall("SELECT status, COUNT(*) n FROM invites WHERE guild_id=? AND inviter_id=? GROUP BY status", g, member.id)
        c = {r["status"]: r["n"] for r in rows}
        nxt = next((f"{k - c.get('valid', 0)} more for {v}" for k, v in sorted(INVITE_ROLES.items()) if k > c.get("valid", 0)), "max rank reached 👑")
        await interaction.response.send_message(f"**{member.display_name}**: {c.get('valid', 0)} counted · {c.get('pending', 0)} pending (count after "
                                                f"{VALID_AFTER_DAYS} days) · {c.get('left', 0)} left. Next: {nxt}.", ephemeral=True)

    @app_commands.command(name="invite-leaderboard", description="Top inviters")
    @app_commands.guild_only()
    async def invite_board(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT inviter_id, COUNT(*) n FROM invites WHERE guild_id=? AND status='valid' AND inviter_id IS NOT NULL "
                                          "GROUP BY inviter_id ORDER BY n DESC LIMIT 10", interaction.guild.id)
        lines = [f"{i + 1}. <@{r['inviter_id']}> · {r['n']}" for i, r in enumerate(rows)]
        e = discord.Embed(title="🚀 Top inviters", description="\n".join(lines) or "No counted invites yet.", color=brand_color(self.bot, interaction.guild))
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    # ---- social content ----
    async def platform_invite(self, guild: discord.Guild, platform: str) -> str | None:
        code = self.bot.db.get_setting(guild.id, f"invite:{platform}")
        if code and code in self.cache.get(guild.id, {}):
            return f"https://discord.gg/{code}"
        ch = get_channel(self.bot, guild, "welcome") or guild.text_channels[0]
        try:
            inv = await ch.create_invite(max_age=0, max_uses=0, unique=True, reason=f"Tracked invite for {platform}")
        except discord.HTTPException:
            return None
        await self.bot.db.set_setting(guild.id, f"invite:{platform}", inv.code)
        await self.bot.db.set_setting(guild.id, f"invite_code:{inv.code}", platform)
        self.cache.setdefault(guild.id, {})[inv.code] = 0
        return inv.url

    async def platform_joins(self, guild_id: int, since: float) -> dict[str, int]:
        rows = await self.bot.db.fetchall("SELECT code, COUNT(*) n FROM invites WHERE guild_id=? AND joined_at>=? AND code IS NOT NULL GROUP BY code", guild_id, since)
        out = {}
        for r in rows:
            p = self.bot.db.get_setting(guild_id, f"invite_code:{r['code']}")
            if p:
                out[p] = out.get(p, 0) + r["n"]
        return out

    @app_commands.command(name="social", description="Social post ideas with a tracked invite link")
    @app_commands.guild_only()
    @staff("social")
    @app_commands.choices(platform=[app_commands.Choice(name=p, value=p) for p in PLATFORMS])
    async def social(self, interaction: discord.Interaction, platform: app_commands.Choice[str], count: app_commands.Range[int, 1, 5] = 3):
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = interaction.guild
        link = await self.platform_invite(g, platform.value)
        week_ago = time.time() - 7 * 86400
        trades = await self.bot.db.fetchall("SELECT data FROM trades WHERE guild_id=? AND status='closed' AND closed_at>=?", g.id, week_ago)
        results = [{k: json.loads(t["data"]).get(k) for k in ("contract", "side", "points", "ticks", "pnl_pct", "r_multiple")} for t in trades]
        members = g.member_count
        ideas = await self.bot.llm.json(
            persona(self.bot, g) + " Right now you're writing social media content to grow the server.",
            f"Write {count} {platform.name} post ideas for a futures day-trading Discord with {members} members. Real trades from this week "
            f"(use only these, include losses honestly if you mention them, never invent results, never mention dollar amounts or money made; use points, ticks, % and R): {json.dumps(results)}. "
            "Mix: education, behind-the-scenes, community, one result recap. Each needs a hook, the post text or a short video script, and a "
            f"call to action with this link: {link}. Every post that mentions trading results must say 'not financial advice'. "
            'Format: {"ideas": [{"hook": "...", "body": "...", "hashtags": "..."}]}', max_tokens=1200)
        items = ideas.get("ideas") if isinstance(ideas, dict) else None
        if not items:
            items = [{"hook": "What 1 point on NQ actually costs you", "body": f"Breaking down tick values for NQ, ES and CL in 30 seconds. Full daily lessons free in our Discord: {link}", "hashtags": "#futures #daytrading #NQ"},
                     {"hook": "Our losses get posted too", "body": f"Every trade we call gets a result card, red or green. Come see the real record: {link} (not financial advice)", "hashtags": "#futurestrading #transparency"}]
        e = discord.Embed(title=f"📣 {platform.name} ideas", color=brand_color(self.bot, g))
        for i, it in enumerate(items[:count], 1):
            e.add_field(name=f"{i}. {str(it.get('hook', ''))[:200]}", value=(f"{it.get('body', '')}\n{it.get('hashtags', '')}")[:1000], inline=False)
        e.set_footer(text=f"Tracked link for {platform.name}: {link or 'could not create (needs Create Invite permission)'}")
        await log_action(self.bot, g, "social", f"{count} {platform.value} ideas")
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Growth(bot))
