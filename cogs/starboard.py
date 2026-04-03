"""
cogs/starboard.py  —  Starboard & Shiny Counter

Tracks shiny Pokémon catches posted by the Operation Dex bot in a
designated starboard channel, and keeps the channel name updated
with the running shiny count.

Slash commands (grouped under /starboard, admin-only):
  /starboard init       — set a channel as the starboard; cleans non-bot messages & counts existing shinies
  /starboard format     — set channel name prefix/suffix (e.g. prefix="✨" suffix="✨")
  /starboard count      — view current shiny count
  /starboard setcount   — manually override the shiny count (admin/owner only)
  /starboard remove     — unlink the starboard channel

Auto-behaviour:
  When the Operation Dex bot posts a shiny catch embed in the starboard channel,
  the count is incremented and the channel name is updated (batched every 5 minutes
  to respect Discord rate limits on channel edits — 2 per 10 min).
"""

import asyncio
import logging
import os
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import starboard_db, guild_settings_db

log = logging.getLogger("qtsdex.starboard")

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))
DEFAULT_OPDEX = int(os.getenv("DEFAULT_OPDEX_BOT_ID", "1471263987340410978"))

# Guilds whose channel name needs a refresh on the next tick
_pending_renames: set[str] = set()

# ── Helpers ──────────────────────────────────────────────────────────────────

_SHINY_TITLE_RE = re.compile(r"✨.*shiny\b", re.IGNORECASE)


def _is_shiny_embed(embed: discord.Embed) -> bool:
    """Check if an embed title matches the shiny catch pattern."""
    if not embed.title:
        return False
    return bool(_SHINY_TITLE_RE.search(embed.title))


def _parse_catcher(embed: discord.Embed) -> tuple[str, str]:
    """
    Extract (display_name, user_id) from an embed's description.
    The bot posts lines like "Caught by: @username" or "**Caught by:** <@123>".
    Returns ('', '') if not found.
    """
    text = embed.description or ""
    # Try to find a user mention <@ID> or <@!ID>
    uid_match = re.search(r"<@!?(\d+)>", text)
    user_id = uid_match.group(1) if uid_match else ""
    # Try to extract display name from "Caught by: @name" line
    name_match = re.search(r"[Cc]aught\s+by:\s*@?\s*(.+)", text)
    user_name = name_match.group(1).strip() if name_match else ""
    # Clean up markdown and mentions from the name
    user_name = re.sub(r"<@!?\d+>", "", user_name).strip().strip("*").strip()
    return user_name, user_id


def _parse_pokemon(embed: discord.Embed) -> str:
    """Extract the Pokémon name from the embed description 'Pokemon: ...' line."""
    text = embed.description or ""
    m = re.search(r"[Pp]ok[eé]mon:\s*(?:Dex#\d+\s+)?(.+)", text)
    return m.group(1).strip() if m else ""


def _build_channel_name(cfg: dict) -> str:
    """Build the starboard channel name from config."""
    prefix = cfg.get("prefix", "")
    suffix = cfg.get("suffix", "")
    count = cfg.get("shiny_count", 0)
    return f"{prefix}{count}{suffix}"


async def _get_opdex_id(guild_id: str) -> int:
    custom = await guild_settings_db.get_opdex_bot_id(guild_id)
    return custom or DEFAULT_OPDEX


def _is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id == OWNER_ID:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return perms.administrator if perms else False


# ── Cog ──────────────────────────────────────────────────────────────────────

class StarboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._rename_loop.start()

    async def cog_unload(self):
        self._rename_loop.cancel()

    # ── Batched channel rename (every 5 min) ─────────────────────────────────

    @tasks.loop(minutes=5)
    async def _rename_loop(self):
        """Apply any pending channel renames."""
        global _pending_renames
        if not _pending_renames:
            return
        pending = _pending_renames.copy()
        _pending_renames.clear()

        for guild_id in pending:
            cfg = starboard_db.get_config(guild_id)
            if not cfg:
                continue
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue
            channel = guild.get_channel(int(cfg["channel_id"]))
            if not channel:
                continue
            new_name = _build_channel_name(cfg)
            if channel.name == new_name:
                continue
            try:
                await channel.edit(name=new_name, reason="Starboard shiny count update")
                log.info(f"Renamed #{channel.name} → {new_name} in {guild.name}")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"Failed to rename starboard channel in {guild.name}: {e}")

    @_rename_loop.before_loop
    async def _before_rename_loop(self):
        await self.bot.wait_until_ready()

    # ── Message listener ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        guild_id = str(message.guild.id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return
        # Only care about the starboard channel
        if str(message.channel.id) != cfg["channel_id"]:
            return
        # Only care about the pokemon bot
        opdex_id = await _get_opdex_id(guild_id)
        if message.author.id != opdex_id:
            return
        # Check for shiny embed
        if not message.embeds:
            return
        embed = message.embeds[0]
        if not _is_shiny_embed(embed):
            return

        # It's a shiny catch!
        user_name, user_id = _parse_catcher(embed)
        pokemon_name = _parse_pokemon(embed)

        new_count = await starboard_db.increment_shiny_count(guild_id)
        await starboard_db.record_catch(
            guild_id=guild_id,
            user_name=user_name,
            user_id=user_id,
            pokemon_name=pokemon_name,
            message_id=str(message.id),
        )
        log.info(
            f"Shiny #{new_count} in {message.guild.name}: "
            f"{pokemon_name} caught by {user_name} ({user_id})"
        )
        _pending_renames.add(guild_id)

    # ── Slash command group ──────────────────────────────────────────────────

    starboard = app_commands.Group(
        name="starboard",
        description="Starboard shiny counter management",
    )

    @starboard.command(name="init", description="Set a channel as the server's starboard and count existing shinies")
    @app_commands.describe(channel="The starboard channel to track")
    async def sb_init(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        await interaction.response.defer(thinking=True)
        guild_id = str(interaction.guild_id)
        opdex_id = await _get_opdex_id(guild_id)

        # Phase 1: Clean non-bot messages and count shinies
        status = await interaction.followup.send(
            f"🔄 Scanning <#{channel.id}>… this may take a moment.", wait=True,
        )
        shiny_count = 0
        deleted_count = 0
        catches: list[tuple] = []  # (user_name, user_id, pokemon_name, message_id)

        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.id != opdex_id:
                try:
                    await msg.delete()
                    deleted_count += 1
                    # small delay to avoid rate limits on bulk deletes
                    if deleted_count % 5 == 0:
                        await asyncio.sleep(1)
                except (discord.Forbidden, discord.NotFound):
                    pass
                continue
            # It's from the pokemon bot — check for shiny
            if msg.embeds and _is_shiny_embed(msg.embeds[0]):
                shiny_count += 1
                embed = msg.embeds[0]
                uname, uid = _parse_catcher(embed)
                pname = _parse_pokemon(embed)
                catches.append((uname, uid, pname, str(msg.id)))

        # Phase 2: Save config
        existing = starboard_db.get_config(guild_id)
        prefix = existing["prefix"] if existing else ""
        suffix = existing["suffix"] if existing else ""
        await starboard_db.set_config(guild_id, str(channel.id), prefix, suffix, shiny_count)

        # Phase 3: Backfill catch records for leaderboard
        for uname, uid, pname, mid in catches:
            await starboard_db.record_catch(guild_id, uname, uid, pname, mid)

        # Phase 4: Rename channel
        cfg = starboard_db.get_config(guild_id)
        if prefix or suffix:
            new_name = _build_channel_name(cfg)
            try:
                await channel.edit(name=new_name, reason="Starboard init")
            except (discord.Forbidden, discord.HTTPException):
                pass

        await status.edit(
            content=(
                f"✅ **Starboard initialised** in <#{channel.id}>\n"
                f"• Cleaned **{deleted_count}** non-bot message(s)\n"
                f"• Counted **{shiny_count}** existing shiny catch(es)\n"
                f"• Backfilled **{len(catches)}** catch record(s) for leaderboard\n"
                f"{'• Channel name updated' if (prefix or suffix) else '• Set a format with `/starboard format` to update the channel name'}"
            ),
        )

    @starboard.command(name="format", description="Set channel name prefix and suffix (e.g. ✨ and ✨ → ✨42✨)")
    @app_commands.describe(
        prefix="Text before the shiny count in the channel name",
        suffix="Text after the shiny count in the channel name",
    )
    async def sb_format(self, interaction: discord.Interaction, prefix: str = "", suffix: str = ""):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured. Use `/starboard init` first.", ephemeral=True,
            )

        await starboard_db.update_prefix_suffix(guild_id, prefix, suffix)
        cfg = starboard_db.get_config(guild_id)
        new_name = _build_channel_name(cfg)

        # Apply rename immediately
        channel = interaction.guild.get_channel(int(cfg["channel_id"]))
        renamed = False
        if channel:
            try:
                await channel.edit(name=new_name, reason="Starboard format update")
                renamed = True
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message(
            f"✅ Format updated → `{new_name}`"
            + (" (channel renamed)" if renamed else ""),
            ephemeral=True,
        )

    @starboard.command(name="count", description="Show the current shiny count")
    async def sb_count(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured.", ephemeral=True,
            )
        await interaction.response.send_message(
            f"✨ **Shiny count:** {cfg['shiny_count']}", ephemeral=True,
        )

    @starboard.command(name="setcount", description="Manually set the shiny count (admin/owner only)")
    @app_commands.describe(count="The new shiny count value")
    async def sb_setcount(self, interaction: discord.Interaction, count: int):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        ok = await starboard_db.set_shiny_count(guild_id, count)
        if not ok:
            return await interaction.response.send_message(
                "⚠️ No starboard configured.", ephemeral=True,
            )

        _pending_renames.add(guild_id)
        await interaction.response.send_message(
            f"✅ Shiny count set to **{count}**. Channel name will update shortly.",
            ephemeral=True,
        )

    @starboard.command(name="remove", description="Unlink the starboard channel")
    async def sb_remove(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        removed = await starboard_db.remove_config(guild_id)
        _pending_renames.discard(guild_id)
        await interaction.response.send_message(
            "✅ Starboard unlinked." if removed else "⚠️ No starboard was configured.",
            ephemeral=True,
        )

    @starboard.command(name="status", description="Show starboard configuration")
    async def sb_status(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured. Use `/starboard init` to set one up.",
                ephemeral=True,
            )

        channel_name = _build_channel_name(cfg)
        await interaction.response.send_message(
            f"**Starboard Config**\n"
            f"• Channel: <#{cfg['channel_id']}>\n"
            f"• Prefix: `{cfg['prefix'] or '(none)'}`\n"
            f"• Suffix: `{cfg['suffix'] or '(none)'}`\n"
            f"• Shiny count: **{cfg['shiny_count']}**\n"
            f"• Channel name: `{channel_name}`",
            ephemeral=True,
        )

    # ── Leaderboard command ──────────────────────────────────────────────────

    @starboard.command(name="leaderboard", description="Show top shiny catchers")
    @app_commands.describe(period="Time period: week, month, year, or all")
    @app_commands.choices(period=[
        app_commands.Choice(name="This week", value="week"),
        app_commands.Choice(name="This month", value="month"),
        app_commands.Choice(name="This year", value="year"),
        app_commands.Choice(name="All time", value="all"),
    ])
    async def sb_leaderboard(self, interaction: discord.Interaction, period: str = "all"):
        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured.", ephemeral=True,
            )

        rows = await starboard_db.get_leaderboard(guild_id, period, limit=15)
        if not rows:
            return await interaction.response.send_message(
                f"No shiny catches recorded for **{period}**.", ephemeral=True,
            )

        period_labels = {"week": "This Week", "month": "This Month", "year": "This Year", "all": "All Time"}
        title = f"✨ Shiny Leaderboard — {period_labels.get(period, period)}"

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uname, uid, cnt) in enumerate(rows):
            rank = medals[i] if i < 3 else f"`{i+1}.`"
            display = f"<@{uid}>" if uid else uname or "Unknown"
            lines.append(f"{rank} {display} — **{cnt}** shiny{'s' if cnt != 1 else ''}")

        total = await starboard_db.get_catch_count(guild_id, period)
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=0xFFD700,
        )
        embed.set_footer(text=f"Total: {total} shinies")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(StarboardCog(bot))
