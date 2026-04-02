"""
cogs/changelog.py  —  /changelog
Post formatted changelogs to a configured channel.

/changelog setup #channel  — Admin sets the target channel (stored per-server)
/changelog post             — Bot owner posts a changelog to that channel
/changelog channel          — Show which channel is currently configured
"""

import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services.guild_settings_db import (
    get_changelog_channel,
    set_changelog_channel,
    make_footer,
)

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))


def _build_changelog_embed(
    version:  str,
    changes:  str,
    author:   discord.Member | discord.User,
    guild_id: str = "",
) -> discord.Embed:
    """Build a richly formatted changelog embed."""
    # Parse change lines — lines starting with - or • become bullet points
    lines = []
    for raw in changes.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "•", "*")):
            stripped = stripped.lstrip("-•* ").strip()
        lines.append(f"• {stripped}")

    body = "\n".join(lines) if lines else changes.strip()

    embed = discord.Embed(
        title=f"📋  Changelog — v{version.lstrip('v')}",
        description=body,
        colour=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(
        name=author.display_name,
        icon_url=author.display_avatar.url,
    )
    embed.set_footer(text=make_footer(guild_id, f"v{version.lstrip('v')}"))
    return embed


class ChangelogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    changelog = app_commands.Group(
        name="changelog",
        description="Post and manage changelogs.",
    )

    # ── /changelog setup ──────────────────────────────────────────────────────

    @changelog.command(name="setup", description="Set the channel where changelogs are posted.")
    @app_commands.describe(channel="The channel to post changelogs in")
    async def cl_setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator and interaction.user.id != OWNER_ID:
            return await interaction.response.send_message(
                "🚫 Only server administrators can set the changelog channel.", ephemeral=True
            )

        await set_changelog_channel(str(interaction.guild_id), channel.id)

        embed = discord.Embed(
            title="✅ Changelog Channel Set",
            description=(
                f"Changelogs will be posted to {channel.mention}.\n\n"
                f"Use `/changelog post` to publish an update."
            ),
            colour=0x57F287,
        )
        embed.set_footer(text=make_footer(str(interaction.guild_id or "")))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /changelog channel ────────────────────────────────────────────────────

    @changelog.command(name="channel", description="Show the current changelog channel.")
    async def cl_channel(self, interaction: discord.Interaction):
        gid     = str(interaction.guild_id or "")
        ch_id   = get_changelog_channel(gid)
        if ch_id:
            ch = interaction.guild.get_channel(ch_id) if interaction.guild else None
            desc = ch.mention if ch else f"<#{ch_id}> *(channel may have been deleted)*"
        else:
            desc = "*Not set. Use `/changelog setup` to configure one.*"

        embed = discord.Embed(
            title="📋 Changelog Channel",
            description=desc,
            colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(gid))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /changelog post ───────────────────────────────────────────────────────

    @changelog.command(name="post", description="Post a changelog to the configured channel.")
    @app_commands.describe(
        version="Version number or tag (e.g. 1.4, 2025-06-01)",
        changes="What changed — use new lines or dashes for bullet points",
    )
    async def cl_post(
        self,
        interaction: discord.Interaction,
        version:     str,
        changes:     str,
    ):
        # Only the bot owner can post changelogs
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message(
                "🚫 Only the bot owner can post changelogs.", ephemeral=True
            )

        gid   = str(interaction.guild_id or "")
        ch_id = get_changelog_channel(gid)

        if not ch_id:
            return await interaction.response.send_message(
                "⚠️ No changelog channel configured for this server.\n"
                "Ask an admin to run `/changelog setup` first.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(ch_id) if interaction.guild else None
        if not channel:
            return await interaction.response.send_message(
                f"⚠️ The configured changelog channel (`{ch_id}`) no longer exists. "
                "Ask an admin to run `/changelog setup` again.",
                ephemeral=True,
            )

        embed = _build_changelog_embed(version, changes, interaction.user, gid)

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"❌ I don't have permission to send messages in {channel.mention}.",
                ephemeral=True,
            )

        confirm = discord.Embed(
            title="✅ Changelog Posted",
            description=f"Posted **v{version.lstrip('v')}** to {channel.mention}.",
            colour=0x57F287,
        )
        confirm.set_footer(text=make_footer(gid))
        await interaction.response.send_message(embed=confirm, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChangelogCog(bot))
