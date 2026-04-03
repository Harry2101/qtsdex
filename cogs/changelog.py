"""
cogs/changelog.py  —  /changelog
Post formatted changelogs to ALL servers that have a changelog channel configured.

/changelog setup #channel  — Admin sets the target channel (stored per-server)
/changelog post             — Bot owner opens a modal to write + post a changelog
/changelog channel          — Show which channel is currently configured

Uses a Discord Modal for the post so multi-line formatting is preserved properly.
Posting broadcasts to every guild that has opted in via /changelog setup.
"""

import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services.guild_settings_db import (
    get_changelog_channel,
    get_all_changelog_channels,
    set_changelog_channel,
    get_bot_name,
    make_footer,
)

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))


def _build_changelog_embed(
    version:  str,
    changes:  str,
    author:   discord.Member | discord.User,
    guild_id: str = "",
) -> discord.Embed:
    """Build a richly formatted changelog embed with sections and dividers."""
    sections: list[tuple[str, list[str]]] = []
    current_header = ""
    current_lines: list[str] = []

    for raw in changes.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        # Detect section headers (lines starting with # or all-caps short lines)
        if stripped.startswith("#"):
            if current_lines:
                sections.append((current_header, current_lines))
                current_lines = []
            current_header = stripped.lstrip("# ").strip()
            continue
        # Normalize bullet points
        if stripped.startswith(("-", "•", "*")):
            stripped = stripped.lstrip("-•* ").strip()
        current_lines.append(f"• {stripped}")

    if current_lines:
        sections.append((current_header, current_lines))

    # Build description
    bot_name = get_bot_name(guild_id) if guild_id else "King's Dex"
    ver_clean = version.lstrip("v")

    embed = discord.Embed(
        title=f"📋  {bot_name} — Changelog v{ver_clean}",
        colour=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )

    if len(sections) == 1 and not sections[0][0]:
        # Single section, no header — put in description
        body = "\n".join(sections[0][1])
        embed.description = body
    else:
        # Multiple sections or named sections
        desc_parts = []
        for header, lines in sections:
            if header:
                desc_parts.append(f"**{header}**")
            desc_parts.extend(lines)
            desc_parts.append("")  # spacing between sections

        embed.description = "\n".join(desc_parts).rstrip()

    embed.set_author(
        name=f"Posted by {author.display_name}",
        icon_url=author.display_avatar.url,
    )
    embed.set_footer(
        text=f"{bot_name}  •  v{ver_clean}  •  {datetime.now(timezone.utc).strftime('%B %d, %Y')}",
    )
    return embed


# ── Modal ─────────────────────────────────────────────────────────────────────

class ChangelogModal(discord.ui.Modal, title="Post Changelog"):
    version = discord.ui.TextInput(
        label="Version",
        placeholder="e.g. 2.0, 2025-06-01, June Update",
        max_length=50,
        style=discord.TextStyle.short,
    )
    changes = discord.ui.TextInput(
        label="Changes (use # for section headers)",
        placeholder="# New Features\n- Added something cool\n# Bug Fixes\n- Fixed a bug",
        style=discord.TextStyle.long,
        max_length=3500,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        all_channels = get_all_changelog_channels()
        if not all_channels:
            return await interaction.followup.send(
                "⚠️ No servers have a changelog channel configured.\n"
                "Ask admins to run `/changelog setup` first.",
                ephemeral=True,
            )

        sent_to: list[str] = []
        failed:  list[str] = []

        for guild_id_str, channel_id in all_channels.items():
            guild = self.bot.get_guild(int(guild_id_str))
            if not guild:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                failed.append(f"`{guild.name}` — channel deleted")
                continue

            embed = _build_changelog_embed(
                self.version.value,
                self.changes.value,
                interaction.user,
                guild_id_str,
            )
            try:
                await channel.send(embed=embed)
                sent_to.append(f"{guild.name} → {channel.mention}")
            except discord.Forbidden:
                failed.append(f"`{guild.name}` — missing permissions")
            except discord.HTTPException as e:
                failed.append(f"`{guild.name}` — {e}")

        # Confirmation to the owner
        ver = self.version.value.lstrip("v")
        desc_lines = [f"Posted **v{ver}** to **{len(sent_to)}** server(s)."]
        if sent_to:
            desc_lines.append("\n**Sent to:**")
            desc_lines.extend(f"• {s}" for s in sent_to[:15])
            if len(sent_to) > 15:
                desc_lines.append(f"*+{len(sent_to) - 15} more*")
        if failed:
            desc_lines.append(f"\n**Failed ({len(failed)}):**")
            desc_lines.extend(f"• {f}" for f in failed[:10])

        confirm = discord.Embed(
            title="✅ Changelog Broadcast Complete",
            description="\n".join(desc_lines),
            colour=0x57F287 if sent_to else 0xED4245,
        )
        confirm.set_footer(text=make_footer(str(interaction.guild_id or "")))
        await interaction.followup.send(embed=confirm, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

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
                f"When the bot owner publishes a changelog from **any** server, "
                f"it will automatically appear here too."
            ),
            colour=0x57F287,
        )
        embed.set_footer(text=make_footer(str(interaction.guild_id or "")))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /changelog channel ────────────────────────────────────────────────────

    @changelog.command(name="channel", description="Show the current changelog channel.")
    async def cl_channel(self, interaction: discord.Interaction):
        gid   = str(interaction.guild_id or "")
        ch_id = get_changelog_channel(gid)
        if ch_id:
            ch   = interaction.guild.get_channel(ch_id) if interaction.guild else None
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

    @changelog.command(name="post", description="Open a form to write and broadcast a changelog to all servers.")
    async def cl_post(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message(
                "🚫 Only the bot owner can post changelogs.", ephemeral=True
            )
        await interaction.response.send_modal(ChangelogModal(self.bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(ChangelogCog(bot))
