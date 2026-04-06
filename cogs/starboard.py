"""
cogs/starboard.py  —  Starboard & Shiny Counter

Tracks shiny Pokémon catches posted by the Operation Dex bot in a
designated starboard channel, and keeps the channel name updated
with the running shiny count.

Slash commands (grouped under /starboard, admin-only):
  /starboard init            — set a channel as the starboard; cleans non-bot messages & counts existing shinies
  /starboard format          — set channel name prefix/suffix (e.g. prefix="✨" suffix="✨")
  /starboard count           — view current shiny count
  /starboard setcount        — manually override the shiny count (admin/owner only)
  /starboard remove          — unlink the starboard channel
  /starboard announcechannel — set channel for weekly/monthly top catcher announcements

Auto-behaviour:
  When the Operation Dex bot posts a shiny catch embed in the starboard channel,
  the count is incremented and the channel name is updated (batched every 5 minutes
  to respect Discord rate limits on channel edits — 2 per 10 min).
  Weekly (Monday 00:00 UTC) and monthly (1st of month 00:00 UTC) top catcher
  announcements are posted to the configured announce channel.
"""

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
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


def _is_privileged(interaction: discord.Interaction) -> bool:
    """Bot owner OR server owner only."""
    if interaction.user.id == OWNER_ID:
        return True
    if interaction.guild and interaction.guild.owner_id == interaction.user.id:
        return True
    return False


def _next_reset_dt(period: str, now: datetime) -> datetime:
    """Return the next reset datetime (Monday 00:00 UTC for week, 1st 00:00 UTC for month)."""
    if period == "week":
        days_until_monday = (7 - now.weekday()) % 7 or 7
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_until_monday)
    else:
        if now.month == 12:
            return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_reset_text(period: str, now: datetime) -> str:
    """Human-readable countdown to next reset (used by leaderboard view)."""
    target = _next_reset_dt(period, now)
    delta = target - now
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {(delta.seconds % 3600) // 60}m"


# ── Cog ──────────────────────────────────────────────────────────────────────

class StarboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._rename_loop.start()
        self._announcement_loop.start()

    async def cog_unload(self):
        self._rename_loop.cancel()
        self._announcement_loop.cancel()

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

        try:
            new_count = await starboard_db.increment_shiny_count(guild_id)
            await starboard_db.record_catch(
                guild_id=guild_id,
                user_name=user_name,
                user_id=user_id,
                pokemon_name=pokemon_name,
                message_id=str(message.id),
            )
        except Exception as e:
            log.error(f"Failed to record shiny catch in {message.guild.name}: {e}")
            return

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
        catches: list[tuple] = []  # (user_name, user_id, pokemon_name, message_id, caught_at)
        to_delete: list[discord.Message] = []

        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.id != opdex_id:
                to_delete.append(msg)
                continue
            # It's from the pokemon bot — check for shiny
            if msg.embeds and _is_shiny_embed(msg.embeds[0]):
                shiny_count += 1
                embed = msg.embeds[0]
                uname, uid = _parse_catcher(embed)
                pname = _parse_pokemon(embed)
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                catches.append((uname, uid, pname, str(msg.id), ts))

        # Bulk-delete non-bot messages in batches of 100 (much faster)
        for i in range(0, len(to_delete), 100):
            batch = to_delete[i:i + 100]
            try:
                await channel.delete_messages(batch)
            except discord.HTTPException:
                # Fallback for messages older than 14 days (can't bulk-delete)
                for msg in batch:
                    try:
                        await msg.delete()
                    except (discord.Forbidden, discord.NotFound):
                        pass
            deleted_count += len(batch)

        # Phase 2: Save config
        existing = starboard_db.get_config(guild_id)
        prefix = existing["prefix"] if existing else ""
        suffix = existing["suffix"] if existing else ""
        await starboard_db.set_config(guild_id, str(channel.id), prefix, suffix, shiny_count)

        # Phase 3: Backfill catch records for leaderboard (duplicates are skipped)
        new_records = 0
        for uname, uid, pname, mid, ts in catches:
            row_id = await starboard_db.record_catch(guild_id, uname, uid, pname, mid, caught_at=ts)
            if row_id:
                new_records += 1

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
                f"• Backfilled **{new_records}** new catch record(s) for leaderboard ({len(catches) - new_records} already tracked)\n"
                f"{'• Channel name updated' if (prefix or suffix) else '• Set a format with `/starboard format` to update the channel name'}"
            ),
        )

    @starboard.command(name="resync", description="Re-read the starboard channel and fix catch timestamps from message dates")
    async def sb_resync(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured. Use `/starboard init` first.", ephemeral=True,
            )

        await interaction.response.defer(thinking=True)
        opdex_id = await _get_opdex_id(guild_id)
        channel = interaction.guild.get_channel(int(cfg["channel_id"]))
        if not channel:
            return await interaction.followup.send("⚠️ Starboard channel not found.", ephemeral=True)

        updated = 0
        new_records = 0
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.id != opdex_id or not msg.embeds:
                continue
            if not _is_shiny_embed(msg.embeds[0]):
                continue
            embed = msg.embeds[0]
            uname, uid = _parse_catcher(embed)
            pname = _parse_pokemon(embed)
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")

            # Try to insert (covers any missing records)
            row_id = await starboard_db.record_catch(guild_id, uname, uid, pname, str(msg.id), caught_at=ts)
            if row_id:
                new_records += 1
            else:
                # Already exists — fix the timestamp
                did_update = await starboard_db.update_catch_timestamp(guild_id, str(msg.id), ts)
                if did_update:
                    updated += 1

        await interaction.followup.send(
            f"✅ **Resync complete**\n"
            f"• Fixed **{updated}** timestamp(s)\n"
            f"• Added **{new_records}** missing record(s)",
            ephemeral=True,
        )

    @starboard.command(name="clearchampions", description="Wipe champion history for a period (server owner / bot owner only)")
    @app_commands.describe(period="Which champion history to clear")
    @app_commands.choices(period=[
        app_commands.Choice(name="Weekly", value="week"),
        app_commands.Choice(name="Monthly", value="month"),
    ])
    async def sb_clearchampions(self, interaction: discord.Interaction, period: str):
        if not _is_privileged(interaction):
            return await interaction.response.send_message("🚫 Server owner or bot owner only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        label = "Weekly" if period == "week" else "Monthly"
        count = await starboard_db.get_champion_history_count(guild_id, period)

        if count == 0:
            return await interaction.response.send_message(
                f"⚠️ No {label} champion history to clear.", ephemeral=True,
            )

        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.confirmed = False

            @discord.ui.button(label=f"Clear {label} History", style=discord.ButtonStyle.danger)
            async def confirm(self, btn: discord.Interaction, button: discord.ui.Button):
                if not _is_privileged(btn):
                    return await btn.response.send_message("🚫 Server owner or bot owner only.", ephemeral=True)
                self.confirmed = True
                self.stop()
                await btn.response.defer()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel(self, btn: discord.Interaction, button: discord.ui.Button):
                self.stop()
                await btn.response.send_message("Cancelled.", ephemeral=True)

        view = ConfirmView()
        await interaction.response.send_message(
            f"⚠️ This will permanently delete **{count}** {label} champion record(s). Are you sure?",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if not view.confirmed:
            return

        deleted = await starboard_db.clear_champion_history(guild_id, period)
        await interaction.edit_original_response(
            content=f"✅ Cleared **{deleted}** {label} champion record(s). Starting fresh!",
            view=None,
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
        weekly_icon = "✅" if cfg.get("weekly_enabled", True) else "⏸️"
        monthly_icon = "✅" if cfg.get("monthly_enabled", True) else "⏸️"
        announce = f"<#{cfg['announce_channel']}>" if cfg.get("announce_channel") else "*(not set)*"
        await interaction.response.send_message(
            f"**Starboard Config**\n"
            f"• Channel: <#{cfg['channel_id']}>\n"
            f"• Prefix: `{cfg['prefix'] or '(none)'}`\n"
            f"• Suffix: `{cfg['suffix'] or '(none)'}`\n"
            f"• Shiny count: **{cfg['shiny_count']}**\n"
            f"• Channel name: `{channel_name}`\n"
            f"• Announce channel: {announce}\n"
            f"• Weekly announcements: {weekly_icon}\n"
            f"• Monthly announcements: {monthly_icon}",
            ephemeral=True,
        )

    # ── Leaderboard command (interactive) ───────────────────────────────────

    @starboard.command(name="leaderboard", description="Show top ✨ catchers with interactive navigation")
    @app_commands.describe(period="Time period to view")
    @app_commands.choices(period=[
        app_commands.Choice(name="This week", value="week"),
        app_commands.Choice(name="This month", value="month"),
        app_commands.Choice(name="All time", value="all"),
    ])
    async def sb_leaderboard(self, interaction: discord.Interaction, period: str = "week"):
        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured. Use `/starboard init` first.", ephemeral=True,
            )

        await interaction.response.defer()
        view = LeaderboardView(self, guild_id, interaction.guild, period)
        embed = await view.build_leaderboard_embed(viewer_id=str(interaction.user.id))
        await interaction.followup.send(embed=embed, view=view)

    # ── Post command (admin, on-demand) ──────────────────────────────────────

    @starboard.command(name="post", description="Manually post a top catcher announcement (admin only, for testing)")
    @app_commands.describe(period="Week or month leaderboard to post")
    @app_commands.choices(period=[
        app_commands.Choice(name="Weekly", value="week"),
        app_commands.Choice(name="Monthly", value="month"),
    ])
    async def sb_post(self, interaction: discord.Interaction, period: str = "week"):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg or not cfg.get("announce_channel"):
            return await interaction.response.send_message(
                "⚠️ No announcement channel configured. Use `/starboard announcechannel` first.",
                ephemeral=True,
            )

        announce_ch = interaction.guild.get_channel(int(cfg["announce_channel"]))
        if not announce_ch:
            return await interaction.response.send_message(
                "⚠️ Announcement channel not found.", ephemeral=True,
            )

        label = "Weekly" if period == "week" else "Monthly"
        cog = self

        class PostView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.action: str | None = None  # "post" | "preview" | None

            @discord.ui.button(label="Preview (ephemeral)", emoji="👁️", style=discord.ButtonStyle.primary)
            async def preview(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
                if not _is_admin(btn_interaction):
                    return await btn_interaction.response.send_message("🚫 Admin only.", ephemeral=True)
                result = await cog._build_announcement(btn_interaction.guild, guild_id, period, record=False)
                if result is None:
                    return await btn_interaction.response.send_message(
                        "⚠️ No data to preview — no catches recorded for this period.", ephemeral=True,
                    )
                content, embed = result
                await btn_interaction.response.send_message(
                    f"-# Preview — this is how it will look in {announce_ch.mention}\n{content}",
                    embed=embed,
                    ephemeral=True,
                )

            @discord.ui.button(label=f"Post {label} Announcement", emoji="📢", style=discord.ButtonStyle.green)
            async def post(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
                if not _is_admin(btn_interaction):
                    return await btn_interaction.response.send_message("🚫 Admin only.", ephemeral=True)
                self.action = "post"
                self.stop()
                await btn_interaction.response.defer()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
                self.action = None
                self.stop()
                await btn_interaction.response.send_message("Cancelled.", ephemeral=True)

        view = PostView()
        await interaction.response.send_message(
            f"**{label} announcement** → {announce_ch.mention}\n"
            f"-# Preview first, or post directly.",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if view.action != "post":
            return

        try:
            await self._post_top_catcher(interaction.guild, guild_id, announce_ch, period)
            await interaction.edit_original_response(
                content=f"✅ {label} announcement posted in {announce_ch.mention}.", view=None,
            )
        except Exception as e:
            log.error(f"Manual announcement failed for {interaction.guild.name}: {e}")
            await interaction.edit_original_response(
                content=f"❌ Failed to post announcement: {e}", view=None,
            )

    @starboard.command(name="announcechannel", description="Set the channel for weekly & monthly top catcher announcements")
    @app_commands.describe(channel="Channel to post announcements in (omit to clear)")
    async def sb_announce_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured. Use `/starboard init` first.", ephemeral=True,
            )

        if channel is None:
            ok = await starboard_db.set_announce_channel(guild_id, "")
            if not ok:
                return await interaction.response.send_message("⚠️ No starboard configured.", ephemeral=True)
            return await interaction.response.send_message(
                "✅ Announcement channel cleared. Weekly/monthly announcements disabled.", ephemeral=True,
            )

        ok = await starboard_db.set_announce_channel(guild_id, str(channel.id))
        if not ok:
            return await interaction.response.send_message("⚠️ No starboard configured.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Announcements will now be posted in {channel.mention}!\n"
            f"📅 Weekly top catcher: every **Monday**\n"
            f"🗓️ Monthly top catcher: first day of each **month**",
            ephemeral=True,
        )

    @starboard.command(name="toggleannounce", description="Enable or disable weekly/monthly automatic announcements (admin only)")
    @app_commands.describe(period="Which announcement to toggle")
    @app_commands.choices(period=[
        app_commands.Choice(name="Weekly", value="week"),
        app_commands.Choice(name="Monthly", value="month"),
    ])
    async def sb_toggleannounce(self, interaction: discord.Interaction, period: str):
        if not _is_admin(interaction):
            return await interaction.response.send_message("🚫 Admin only.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        cfg = starboard_db.get_config(guild_id)
        if not cfg:
            return await interaction.response.send_message(
                "⚠️ No starboard configured. Use `/starboard init` first.", ephemeral=True,
            )

        key = "weekly_enabled" if period == "week" else "monthly_enabled"
        label = "Weekly" if period == "week" else "Monthly"
        current = cfg.get(key, True)
        new_state = not current

        ok = await starboard_db.set_announcement_toggle(guild_id, period, new_state)
        if not ok:
            return await interaction.response.send_message("⚠️ No starboard configured.", ephemeral=True)

        state_str = "✅ **Enabled**" if new_state else "⏸️ **Disabled**"
        await interaction.response.send_message(
            f"{state_str} — {label} announcements are now {'on' if new_state else 'off'}.",
            ephemeral=True,
        )

    # ── Weekly / Monthly announcement scheduler ──────────────────────────────

    @tasks.loop(minutes=30)
    async def _announcement_loop(self):
        """Check if it's time to post weekly or monthly top catcher announcements.
        Skips the first iteration (which fires immediately on cog load)."""
        if not hasattr(self, "_announcement_started"):
            self._announcement_started = True
            return
        now = datetime.now(timezone.utc)
        is_weekly = now.weekday() == 0 and now.hour == 0 and now.minute < 30   # Monday 00:00–00:30
        is_monthly = now.day == 1 and now.hour == 0 and now.minute < 30        # 1st of month 00:00–00:30

        if not is_weekly and not is_monthly:
            return

        for guild in self.bot.guilds:
            guild_id = str(guild.id)
            cfg = starboard_db.get_config(guild_id)
            if not cfg or not cfg.get("announce_channel"):
                continue

            announce_ch = guild.get_channel(int(cfg["announce_channel"]))
            if not announce_ch:
                continue

            try:
                if is_weekly and cfg.get("weekly_enabled", True):
                    await self._post_top_catcher(guild, guild_id, announce_ch, "week")
                if is_monthly and cfg.get("monthly_enabled", True):
                    await self._post_top_catcher(guild, guild_id, announce_ch, "month")
            except Exception as e:
                log.error(f"Announcement failed for {guild.name}: {e}")

    @_announcement_loop.before_loop
    async def _before_announcement_loop(self):
        await self.bot.wait_until_ready()

    # ── Announcement builder ──────────────────────────────────────────────────

    async def _build_announcement(
        self,
        guild: discord.Guild,
        guild_id: str,
        period: str,
        record: bool = True,
    ) -> tuple[str, discord.Embed] | None:
        """
        Build the announcement message content and embed.
        Returns (content, embed), or None if there is nothing to announce.
        If record=True, saves the champion to history (only set False for previews).
        """
        rows = await starboard_db.get_leaderboard(guild_id, period, limit=5)
        total = await starboard_db.get_catch_count(guild_id, period)

        if not rows or total == 0:
            return None

        top_uname, top_uid, top_cnt = rows[0]
        top_display = f"<@{top_uid}>" if top_uid else (top_uname or "Unknown Trainer")

        now = datetime.now(timezone.utc)
        if period == "week":
            period_label_key = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        else:
            period_label_key = f"{now.year}-{now.month:02d}"

        if record:
            await starboard_db.record_champion(
                guild_id=guild_id,
                period_type=period,
                period_label=period_label_key,
                user_id=top_uid,
                user_name=top_uname,
                catch_count=top_cnt,
                total_catches=total,
            )

        streak = await starboard_db.get_champion_streak(guild_id, period, top_uid)
        total_wins = await starboard_db.get_total_wins(guild_id, period, top_uid)

        # ── Resolve avatar ──
        avatar_url = None
        if top_uid:
            try:
                member = guild.get_member(int(top_uid)) or await guild.fetch_member(int(top_uid))
                if member:
                    avatar_url = member.display_avatar.with_size(512).url
            except (discord.NotFound, discord.HTTPException):
                pass

        # ── Theme: weekly = gold trophy, monthly = prestige crown ──
        if period == "week":
            colour = 0xFFD700
            trophy = "🏆"
            title_text = "WEEKLY SHINY CHAMPION"
            period_text = "this week"
            trophy_msg = "Earned a **Weekly Trophy** 🏆"
        else:
            colour = 0xFF4500
            trophy = "👑"
            title_text = "MONTHLY SHINY LEGEND"
            period_text = "this month"
            trophy_msg = "Earned a **Monthly Crown** 👑"

        # ══════════════════════════════════════════════════════════════════
        # THE EMBED — strict hierarchy, top to bottom:
        #   1. Author bar  = trophy icon + title (small, sets context)
        #   2. Thumbnail   = champion's avatar (top-right, clean)
        #   3. Description = trophy award → key stat → streak → podium → server stat → hype → reset
        #   4. Footer      = encouragement (no timestamp clutter)
        # ══════════════════════════════════════════════════════════════════

        desc = []

        # ── 1. TROPHY AWARD — the gamification moment ──
        desc.append(trophy_msg)
        if total_wins == 1 and streak <= 1:
            desc.append("Their **first title** — a star is born! 🌟")
        elif streak >= 2:
            flame = "🔥" * min(streak, 5)
            desc.append(f"{flame} **{streak} wins in a row!** Reigning champion!")
        if total_wins >= 2:
            trophies = trophy * min(total_wins, 10)
            desc.append(f"**{total_wins}** career titles: {trophies}")
        desc.append("")

        # ── 2. KEY STAT — big, unmissable ──
        desc.append(f"## ✨ {top_cnt} shinies caught {period_text}")
        desc.append("")

        # ── 3. PODIUM — compact, runners up clearly subordinate ──
        if len(rows) > 1:
            for i, (uname, uid, cnt) in enumerate(rows[1:5], start=2):
                display = f"<@{uid}>" if uid else (uname or "`[unknown]`")
                medal = {2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
                desc.append(f"{medal} {display} — {cnt}")
            desc.append("")

        # ── 4. SERVER STAT ──
        desc.append(f"**{total}** shinies caught across the server {period_text}")
        desc.append("")

        # ── 5. HYPE + RESET ──
        hype = random.choice([
            "The hunt continues — who's next?",
            "Can anyone dethrone the champion?",
            "The competition is heating up!",
            "Trainers, the grind never stops!",
        ])
        next_ts = int(_next_reset_dt(period, now).timestamp())
        desc.append(f"-# *{hype}* · Resets <t:{next_ts}:R>")

        embed = discord.Embed(description="\n".join(desc), color=colour)

        # Author bar — small title with trophy icon
        embed.set_author(name=title_text, icon_url=avatar_url or discord.Embed.Empty)

        # Thumbnail — champion's avatar, top-right, not oversized
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.set_footer(text="✨ Good luck, hunters!")

        # Message content = champion mention (triggers ping + serves as headline)
        content = f"## {trophy} {title_text}\n{top_display}"
        return content, embed

    async def _post_top_catcher(
        self,
        guild: discord.Guild,
        guild_id: str,
        channel: discord.TextChannel,
        period: str,
    ):
        result = await self._build_announcement(guild, guild_id, period, record=True)
        if result is None:
            return
        content, embed = result
        await channel.send(content, embed=embed)
        log.info(f"Posted {period} announcement in #{channel.name} for {guild.name}")


# ── Interactive Leaderboard View ────────────────────────────────────────────

_PERIOD_LABELS = {"week": "This Week", "month": "This Month", "all": "All Time"}
_MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# View modes
_MODE_LEADERBOARD = "lb"
_MODE_HISTORY = "hist"
_MODE_STATS = "stats"
_MODE_HALL_OF_FAME = "hof"

_HISTORY_PAGE_SIZE = 5


class LeaderboardView(discord.ui.View):
    """Interactive leaderboard with buttons for period switching, history, and stats."""

    def __init__(self, cog: StarboardCog, guild_id: str, guild: discord.Guild, period: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.guild = guild
        self.period = period
        self.mode = _MODE_LEADERBOARD
        self.history_page = 0
        self.history_type = "week"  # for history view
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()

        if self.mode == _MODE_LEADERBOARD:
            # Period buttons
            for val, label, emoji in [("week", "Weekly", "📅"), ("month", "Monthly", "🗓️"), ("all", "All Time", "🌟")]:
                btn = discord.ui.Button(
                    label=label, emoji=emoji,
                    style=discord.ButtonStyle.primary if val == self.period else discord.ButtonStyle.secondary,
                    custom_id=f"lb_period_{val}", row=0,
                )
                btn.callback = self._make_period_callback(val)
                self.add_item(btn)

            # Mode buttons
            hist_btn = discord.ui.Button(label="History", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="lb_history", row=1)
            hist_btn.callback = self._switch_to_history
            self.add_item(hist_btn)

            hof_btn = discord.ui.Button(label="Hall of Fame", emoji="🏛️", style=discord.ButtonStyle.secondary, custom_id="lb_hof", row=1)
            hof_btn.callback = self._switch_to_hof
            self.add_item(hof_btn)

            stats_btn = discord.ui.Button(label="My Stats", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="lb_stats", row=1)
            stats_btn.callback = self._switch_to_stats
            self.add_item(stats_btn)

        elif self.mode == _MODE_HISTORY:
            # Period toggle for history
            for val, label in [("week", "Weekly"), ("month", "Monthly")]:
                btn = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary if val == self.history_type else discord.ButtonStyle.secondary,
                    custom_id=f"hist_type_{val}", row=0,
                )
                btn.callback = self._make_hist_type_callback(val)
                self.add_item(btn)

            # Pagination
            prev_btn = discord.ui.Button(label="Newer", emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="hist_prev", row=1)
            prev_btn.callback = self._hist_prev
            prev_btn.disabled = self.history_page == 0
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(label="Older", emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="hist_next", row=1)
            next_btn.callback = self._hist_next
            self.add_item(next_btn)

            back_btn = discord.ui.Button(label="Back", emoji="🔙", style=discord.ButtonStyle.danger, custom_id="hist_back", row=1)
            back_btn.callback = self._switch_to_leaderboard
            self.add_item(back_btn)

        elif self.mode == _MODE_HALL_OF_FAME:
            for val, label in [("week", "Weekly"), ("month", "Monthly")]:
                btn = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary if val == self.history_type else discord.ButtonStyle.secondary,
                    custom_id=f"hof_type_{val}", row=0,
                )
                btn.callback = self._make_hof_type_callback(val)
                self.add_item(btn)

            back_btn = discord.ui.Button(label="Back", emoji="🔙", style=discord.ButtonStyle.danger, custom_id="hof_back", row=1)
            back_btn.callback = self._switch_to_leaderboard
            self.add_item(back_btn)

        elif self.mode == _MODE_STATS:
            back_btn = discord.ui.Button(label="Back", emoji="🔙", style=discord.ButtonStyle.danger, custom_id="stats_back", row=0)
            back_btn.callback = self._switch_to_leaderboard
            self.add_item(back_btn)

    # ── Embed builders ───────────────────────────────────────────────────

    async def build_leaderboard_embed(self, viewer_id: str = "") -> discord.Embed:
        rows = await starboard_db.get_leaderboard(self.guild_id, self.period, limit=10)
        total = await starboard_db.get_catch_count(self.guild_id, self.period)
        label = _PERIOD_LABELS.get(self.period, self.period)
        now = datetime.now(timezone.utc)

        if not rows:
            return discord.Embed(
                title=f"✨ Leaderboard — {label}",
                description=f"No catches recorded for **{label}** yet.\nGet out there and start hunting! 🎯",
                color=0xFFD700,
            )

        # ── Header: server-wide stat ──
        lines = []
        lines.append(f"**{total}** shinies caught {label.lower()} across the server")
        lines.append("")

        # ── Ranked list: clean, no bars ──
        for i, (uname, uid, cnt) in enumerate(rows):
            medal = _MEDALS[i] if i < len(_MEDALS) else f"` {i+1}. `"
            display = f"<@{uid}>" if uid else (uname or "`[unknown — use /inspect]`")
            lines.append(f"{medal} {display} — **{cnt}** ✨")

        # ── Viewer's rank (if not in top 10) ──
        if viewer_id:
            in_top = any(r[1] == viewer_id for r in rows)
            if not in_top:
                all_rows = await starboard_db.get_leaderboard(self.guild_id, self.period, limit=100)
                for i, (uname, uid, cnt) in enumerate(all_rows):
                    if uid == viewer_id:
                        lines.append("")
                        lines.append(f"-# You are **#{i+1}** with **{cnt}** catches")
                        break

        embed = discord.Embed(
            title=f"✨ Top Shiny Hunters — {label}",
            description="\n".join(lines),
            color=0xFFD700,
        )

        # Next reset in footer (only for week/month, not all-time)
        if self.period in ("week", "month"):
            reset = _next_reset_text(self.period, now)
            embed.set_footer(text=f"Resets in {reset}")
        else:
            embed.set_footer(text="All-time standings")
        embed.timestamp = now

        # #1's avatar as thumbnail
        top_uid = rows[0][1]
        if top_uid:
            try:
                member = self.guild.get_member(int(top_uid)) or await self.guild.fetch_member(int(top_uid))
                if member:
                    embed.set_thumbnail(url=member.display_avatar.with_size(256).url)
            except (discord.NotFound, discord.HTTPException):
                pass

        return embed

    async def build_history_embed(self) -> discord.Embed:
        total_records = await starboard_db.get_champion_history_count(self.guild_id, self.history_type)
        records = await starboard_db.get_champion_history(
            self.guild_id, self.history_type,
            limit=_HISTORY_PAGE_SIZE, offset=self.history_page * _HISTORY_PAGE_SIZE,
        )

        type_label = "Weekly" if self.history_type == "week" else "Monthly"
        total_pages = max(1, (total_records + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)

        if not records:
            return discord.Embed(
                title=f"📜 {type_label} Champion History",
                description="No champions recorded yet.\nUse `/starboard post` to start tracking!",
                color=0x9B59B6,
            )

        lines = []
        for period_label, uname, uid, catch_count, total_catches in records:
            display = f"<@{uid}>" if uid else (uname or "`[unknown]`")
            lines.append(
                f"**{period_label}** — 🏆 {display}\n"
                f"` {catch_count} catches ` out of {total_catches} total"
            )

        embed = discord.Embed(
            title=f"📜 {type_label} Champion History",
            description="\n\n".join(lines),
            color=0x9B59B6,
        )
        embed.set_footer(text=f"Page {self.history_page + 1}/{total_pages} • {total_records} records total")
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def build_hof_embed(self) -> discord.Embed:
        type_label = "Weekly" if self.history_type == "week" else "Monthly"
        top = await starboard_db.get_top_champions(self.guild_id, self.history_type, limit=10)

        if not top:
            return discord.Embed(
                title=f"🏛️ {type_label} Hall of Fame",
                description="No champions recorded yet.",
                color=0xD4AF37,
            )

        lines = []
        for i, (uname, uid, wins) in enumerate(top):
            medal = _MEDALS[i] if i < len(_MEDALS) else f"`{i+1}.`"
            display = f"<@{uid}>" if uid else (uname or "`[unknown]`")
            trophy = "🏆" * min(wins, 5)
            lines.append(f"{medal} {display} — **{wins}** title{'s' if wins != 1 else ''} {trophy}")

        embed = discord.Embed(
            title=f"🏛️ {type_label} Hall of Fame",
            description="*Most championship titles won*\n\n" + "\n".join(lines),
            color=0xD4AF37,
        )
        embed.timestamp = datetime.now(timezone.utc)

        # Top champion's avatar
        if top and top[0][1]:
            try:
                member = self.guild.get_member(int(top[0][1])) or await self.guild.fetch_member(int(top[0][1]))
                if member:
                    embed.set_thumbnail(url=member.display_avatar.with_size(256).url)
            except (discord.NotFound, discord.HTTPException):
                pass

        return embed

    async def build_stats_embed(self, user_id: str, user: discord.User | discord.Member) -> discord.Embed:
        stats = await starboard_db.get_user_stats(self.guild_id, user_id)

        lines = []
        lines.append(f"### {user.display_name}'s Shiny Profile")
        lines.append("")

        # Catches
        lines.append("**── Catches ──**")
        lines.append(f"✨ **All time:** {stats['total_catches']}")
        lines.append(f"📅 **This week:** {stats['week_catches']}")
        lines.append(f"🗓️ **This month:** {stats['month_catches']}")
        lines.append(f"🦎 **Unique shinies:** {stats['unique_pokemon']}")
        lines.append("")

        # Championship titles
        lines.append("**── Titles ──**")
        weekly_wins = stats["weekly_wins"]
        monthly_wins = stats["monthly_wins"]
        lines.append(f"🏆 **Weekly titles:** {weekly_wins}")
        lines.append(f"👑 **Monthly titles:** {monthly_wins}")
        lines.append("")

        # Streaks
        if stats["weekly_streak"] >= 2 or stats["monthly_streak"] >= 2:
            lines.append("**── Active Streaks ──**")
            if stats["weekly_streak"] >= 2:
                flame = "🔥" * min(stats["weekly_streak"], 5)
                lines.append(f"{flame} **{stats['weekly_streak']}-week streak!**")
            if stats["monthly_streak"] >= 2:
                flame = "🔥" * min(stats["monthly_streak"], 5)
                lines.append(f"{flame} **{stats['monthly_streak']}-month streak!**")
            lines.append("")

        # Personal bests
        if stats["best_week"] > 0 or stats["best_month"] > 0:
            lines.append("**── Personal Bests ──**")
            if stats["best_week"] > 0:
                lines.append(f"📅 **Best week:** {stats['best_week']} catches")
            if stats["best_month"] > 0:
                lines.append(f"🗓️ **Best month:** {stats['best_month']} catches")
            lines.append("")

        # Fun titles based on achievements
        titles = []
        if weekly_wins + monthly_wins >= 10:
            titles.append("💎 Diamond Hunter")
        elif weekly_wins + monthly_wins >= 5:
            titles.append("🌟 Star Catcher")
        elif weekly_wins + monthly_wins >= 1:
            titles.append("🏅 Champion")
        if stats["weekly_streak"] >= 5 or stats["monthly_streak"] >= 3:
            titles.append("⚡ Unstoppable")
        if stats["unique_pokemon"] >= 50:
            titles.append("📖 Living Dex")
        elif stats["unique_pokemon"] >= 20:
            titles.append("🦎 Collector")
        if stats["total_catches"] >= 100:
            titles.append("🎯 Shiny Master")
        elif stats["total_catches"] >= 50:
            titles.append("✨ Shiny Veteran")

        if titles:
            lines.append(f"**Titles:** {' '.join(titles)}")

        embed = discord.Embed(
            title="📊 Trainer Stats",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_thumbnail(url=user.display_avatar.with_size(256).url)
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    # ── Button callbacks ─────────────────────────────────────────────────

    def _make_period_callback(self, period: str):
        async def callback(interaction: discord.Interaction):
            self.period = period
            self._update_buttons()
            embed = await self.build_leaderboard_embed(viewer_id=str(interaction.user.id))
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def _switch_to_history(self, interaction: discord.Interaction):
        self.mode = _MODE_HISTORY
        self.history_page = 0
        self._update_buttons()
        embed = await self.build_history_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _switch_to_hof(self, interaction: discord.Interaction):
        self.mode = _MODE_HALL_OF_FAME
        self._update_buttons()
        embed = await self.build_hof_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _switch_to_stats(self, interaction: discord.Interaction):
        self.mode = _MODE_STATS
        self._update_buttons()
        embed = await self.build_stats_embed(str(interaction.user.id), interaction.user)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _switch_to_leaderboard(self, interaction: discord.Interaction):
        self.mode = _MODE_LEADERBOARD
        self._update_buttons()
        embed = await self.build_leaderboard_embed(viewer_id=str(interaction.user.id))
        await interaction.response.edit_message(embed=embed, view=self)

    def _make_hist_type_callback(self, hist_type: str):
        async def callback(interaction: discord.Interaction):
            self.history_type = hist_type
            self.history_page = 0
            self._update_buttons()
            embed = await self.build_history_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    def _make_hof_type_callback(self, hist_type: str):
        async def callback(interaction: discord.Interaction):
            self.history_type = hist_type
            self._update_buttons()
            embed = await self.build_hof_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    async def _hist_prev(self, interaction: discord.Interaction):
        self.history_page = max(0, self.history_page - 1)
        self._update_buttons()
        embed = await self.build_history_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _hist_next(self, interaction: discord.Interaction):
        self.history_page += 1
        self._update_buttons()
        embed = await self.build_history_embed()
        await interaction.response.edit_message(embed=embed, view=self)


async def setup(bot: commands.Bot):
    await bot.add_cog(StarboardCog(bot))
