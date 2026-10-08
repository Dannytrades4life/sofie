"""Welcome flow: greet new members, rules-acceptance button, self-assign role buttons."""
from __future__ import annotations

import random
import re
from datetime import timedelta

import discord
from discord.ext import commands

from ..control import active, persona
from ..safety import log_action
from ..util import VERIFIED_ROLE, brand_color, get_channel, today

WELCOME_TEMPLATES = [
    "welcome in {name}! 👋 what do you mostly trade?",
    "hey {name}, glad you found us. stocks, crypto, forex, or a bit of everything?",
    "yo {name} 👋 grab your roles and come say what you're watching this week",
    "welcome {name}! first rule of trading club: protect your capital 😄 what markets are you in?",
]


class RulesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rules:accept"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="I've read the rules", emoji="✅", style=discord.ButtonStyle.success, custom_id="rules:accept"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE)
        if role is None:
            await interaction.response.send_message("The server isn't set up yet, ping an admin.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You're already in 👍", ephemeral=True)
            return
        await interaction.user.add_roles(role, reason="Accepted rules")
        roles_ch = get_channel(interaction.client, interaction.guild, "roles")
        hint = f" Pick your markets in {roles_ch.mention}." if roles_ch else ""
        await interaction.response.send_message(f"You're in! Welcome aboard.{hint}", ephemeral=True)


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rr:(?P<role_id>\d+)"):
    def __init__(self, role_id: int, label: str | None = None, emoji: str | None = None):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"rr:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("That role no longer exists.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="Self-role toggle")
            await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
        else:
            await interaction.user.add_roles(role, reason="Self-role toggle")
            await interaction.response.send_message(f"Added **{role.name}**.", ephemeral=True)


class Onboarding(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.bot.db.bump_stat(member.guild.id, today(self.bot), "joins")
        if member.bot or not active(self.bot, member.guild.id, "welcomes"):
            return
        mod = self.bot.get_cog("Moderation")
        new_account = discord.utils.utcnow() - member.created_at < timedelta(days=3)
        if new_account:
            mod_log = get_channel(self.bot, member.guild, "mod_log")
            if mod_log:
                await mod_log.send(f"🆕 {member.mention} joined with an account created {discord.utils.format_dt(member.created_at, 'R')}. Keeping an eye on them.")
        if mod and mod.in_raid_mode(member.guild.id):
            return  # no welcome spam during a raid

        channel = get_channel(self.bot, member.guild, "welcome")
        if channel is None:
            return
        line = await self.bot.llm.chat(
            persona(self.bot, member.guild),
            f"Write a one-line, casual welcome for a new member named {member.display_name}. "
            "Ask them one light question about what they trade. Max 25 words. Don't use their @.",
            max_tokens=60,
        )
        line = line or random.choice(WELCOME_TEMPLATES).format(name=member.display_name)
        rules = get_channel(self.bot, member.guild, "rules")
        embed = discord.Embed(description=line, color=brand_color(self.bot, member.guild))
        embed.set_author(name=f"Welcome, {member.display_name}!", icon_url=member.display_avatar.url)
        if rules:
            embed.add_field(name="Start here", value=f"Read {rules.mention} and tap the button to unlock the server.")
        embed.set_footer(text=f"Member #{member.guild.member_count}")
        from ..control import send_post
        await send_post(self.bot, member.guild, channel, "welcomes", content=member.mention, embed=embed, kind="welcome")
        await log_action(self.bot, member.guild, "welcomed", str(member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.bot.db.bump_stat(member.guild.id, today(self.bot), "leaves")


async def setup(bot) -> None:
    bot.add_dynamic_items(RulesButton, RoleButton)
    await bot.add_cog(Onboarding(bot))
