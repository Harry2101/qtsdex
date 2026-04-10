"""
cogs/incense.py  —  Incense Management System
Handles Mass Incense operations for clan servers.

Prefix commands (incense manager role or bot owner only):
  !pause [group]      — pause all active incenses (or just a named group)
  !resume [group]     — resume all paused incenses (or just a named group)
  !incset ID1 ID2...  — bulk register incense channels

Slash commands (grouped under /incense):
  /incense add        — register channel(s) as incense channels
  /incense remove     — unregister a channel
  /incense lock       — lock a specific channel (pause its incense)
  /incense unlock     — unlock a specific channel (resume its incense)
  /incense recursive  — set every channel after this one as an inc channel
  /incense status     — show all incense channels and their states
  /incense clear      — clear all incense data for a channel
  /incense resync     — scan history, fix lock state, clean old bot messages
  /incense setup      — configure incense manager role and Operation Dex bot
  /incense log        — view audit log of incense operations
  /incense group create  — create a named channel group (max 3)
  /incense group delete  — delete a group
  /incense group add     — add channels to a group
  /incense group remove  — remove channels from a group
  /incense group list    — list all groups and their channels

Auto-behaviour:
  When Operation Dex bot sends "Incense Activated!" in a registered channel,
  the channel is automatically locked and a notification is posted.

Locking = deny Send Messages permission for Operation Dex bot in that channel.
Unlocking = remove that deny override (restore default).
"""

import asyncio
import logging
import os
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import incense_db, guild_settings_db
from services.guild_settings_db import make_footer

log = logging.getLogger("qtsdex.incense")

# ── Constants ─────────────────────────────────────────────────────────────────

OWNER_ID       = int(os.getenv("OWNER_ID", "145065060568530944"))
DEFAULT_OPDEX  = int(os.getenv("DEFAULT_OPDEX_BOT_ID", "1471263987340410978"))
_QT_GUILD_ID   = "1477887017034584248"

_NOT_QT_MSG = "🚫 Mass Incense management is only available in the QTs server."


def _is_qt_guild(ctx_or_interaction) -> bool:
    """Check if this is the QTs server."""
    if isinstance(ctx_or_interaction, commands.Context):
        return str(getattr(ctx_or_interaction.guild, "id", "")) == _QT_GUILD_ID
    return str(getattr(ctx_or_interaction, "guild_id", "")) == _QT_GUILD_ID


# ── Permission helpers ────────────────────────────────────────────────────────

async def _get_opdex_id(guild_id: str) -> int:
    """Get the Operation Dex bot ID for this guild, or the default."""
    custom = await guild_settings_db.get_opdex_bot_id(guild_id)
    return custom or DEFAULT_OPDEX


async def _is_authorised(ctx_or_interaction) -> bool:
    """Check if user has the incense manager role or is the bot owner."""
    if isinstance(ctx_or_interaction, commands.Context):
        user  = ctx_or_interaction.author
        guild = ctx_or_interaction.guild
    else:
        user  = ctx_or_interaction.user
        guild = ctx_or_interaction.guild
    if user.id == OWNER_ID:
        return True
    if guild is None:
        return False
    # Check for server admin
    if user.guild_permissions.administrator:
        return True
    # Check configured incense manager role
    role_id = await guild_settings_db.get_incense_role(str(guild.id))
    if role_id and any(r.id == role_id for r in getattr(user, "roles", [])):
        return True
    return False


async def _lock_channel(channel: discord.TextChannel, opdex_id: int) -> bool:
    target = channel.guild.get_member(opdex_id) or discord.Object(id=opdex_id)
    try:
        overwrite = channel.overwrites_for(target)
        overwrite.send_messages = False
        await channel.set_permissions(target, overwrite=overwrite, reason="Incense paused")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Failed to lock #{channel.name}: {e}")
        return False


async def _unlock_channel(channel: discord.TextChannel, opdex_id: int) -> bool:
    target = channel.guild.get_member(opdex_id) or discord.Object(id=opdex_id)
    try:
        overwrite = channel.overwrites_for(target)
        overwrite.send_messages = None
        if overwrite.is_empty():
            await channel.set_permissions(target, overwrite=None, reason="Incense resumed")
        else:
            await channel.set_permissions(target, overwrite=overwrite, reason="Incense resumed")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Failed to unlock #{channel.name}: {e}")
        return False


def _is_channel_locked(channel: discord.TextChannel, opdex_id: int) -> bool:
    target    = discord.Object(id=opdex_id)
    overwrite = channel.overwrites_for(target)
    return overwrite.send_messages is False


# ── Incense detection helpers ─────────────────────────────────────────────────

def _parse_incense_activation(message: discord.Message) -> tuple[bool, str, int] | None:
    """
    Check if a message contains an incense activation.
    Returns (incense_type, total_spawns) if activated, else None.
    """
    activated = False
    incense_type = "Standard"
    total_spawns = 0

    for embed in message.embeds:
        title = (embed.title or "").lower()
        desc = (embed.description or "")
        if "incense activated" in title or "incense activated" in desc.lower():
            activated = True
            type_match = re.search(r"(\w+)\s+incense\s+is\s+now\s+burning", desc, re.IGNORECASE)
            if type_match:
                incense_type = type_match.group(1).title()
            spawns_match = re.search(r"(\d+)\s+(?:total\s+)?spawns?", desc, re.IGNORECASE)
            if spawns_match:
                total_spawns = int(spawns_match.group(1))
            for field in embed.fields:
                if "spawn" in field.name.lower():
                    try:
                        total_spawns = int(re.search(r"\d+", field.value).group())
                    except Exception:
                        pass
            break

    if not activated and "incense activated" in message.content.lower():
        activated = True

    return (incense_type, total_spawns) if activated else None


def _parse_incense_active(message: discord.Message) -> tuple[str, int] | None:
    """
    Detect an *already-running* incense from any Operation Dex embed style.
    Returns (incense_type, spawns_remaining) or None.

    Handles all known Op Dex embed formats:
      • "Incense Activated!" title / description
      • Spawn embeds: "X Incense Active" in title or description
      • Footer / field text showing spawns left
      • Any embed mentioning "Incense Active" with a spawn count
    """
    for embed in message.embeds:
        title   = embed.title or ""
        desc    = embed.description or ""
        combined = f"{title}\n{desc}"
        # Also collect field values
        for f in embed.fields:
            combined += f"\n{f.name}\n{f.value}"

        combined_low = combined.lower()

        # --- style 1: activation embed ("Incense Activated!") ---
        if "incense activated" in combined_low:
            inc_type = "Standard"
            spawns   = 0
            m = re.search(r"(\w+)\s+incense\s+is\s+now\s+burning", combined, re.IGNORECASE)
            if m:
                inc_type = m.group(1).title()
            else:
                # Try "a <Type> Incense has been activated"
                m2 = re.search(r"(\w+)\s+incense\s+(?:has been|was)\s+activated", combined, re.IGNORECASE)
                if m2:
                    inc_type = m2.group(1).title()
            sm = re.search(r"(\d+)\s+(?:total\s+)?spawns?", combined, re.IGNORECASE)
            if sm:
                spawns = int(sm.group(1))
            return (inc_type, spawns)

        # --- style 2: "Incense Active" anywhere in title or description ---
        if "incense active" in combined_low or "incense is active" in combined_low:
            inc_type = "Standard"
            spawns   = 0
            # Extract type: "<Type> Incense Active" or "<Type> Incense is Active"
            m = re.search(
                r"([A-Za-z][\w\s]{0,20}?)\s+Incense\s+(?:is\s+)?Active",
                combined, re.IGNORECASE,
            )
            if m:
                raw = m.group(1).strip().split()[-1]  # last word (avoid grabbing long phrases)
                if raw.lower() not in ("an", "a", "the", "is", "was"):
                    inc_type = raw.title()
            # Extract spawns remaining
            sm = re.search(
                r"(\d+)\s+spawns?\s+(?:left|remaining|rem)|"
                r"spawns?\s+(?:left|remaining|rem)[:\s]+(\d+)|"
                r"(\d+)\s+spawns?",
                combined, re.IGNORECASE,
            )
            if sm:
                spawns = int(next(g for g in sm.groups() if g is not None))
            return (inc_type, spawns)

        # --- style 3: footer text "Incense Active" ---
        if embed.footer and embed.footer.text:
            if "incense active" in embed.footer.text.lower():
                inc_type = "Standard"
                spawns   = 0
                m = re.search(
                    r"([A-Za-z]+)\s+Incense\s+Active",
                    embed.footer.text, re.IGNORECASE,
                )
                if m and m.group(1).lower() not in ("an", "a", "the"):
                    inc_type = m.group(1).title()
                sm = re.search(r"(\d+)\s+spawns?", embed.footer.text, re.IGNORECASE)
                if sm:
                    spawns = int(sm.group(1))
                return (inc_type, spawns)

    # Plain content fallback
    if "incense active" in message.content.lower() or "incense activated" in message.content.lower():
        sm = re.search(r"(\d+)\s+spawns?", message.content, re.IGNORECASE)
        spawns = int(sm.group(1)) if sm else 0
        return ("Standard", spawns)

    return None


async def _check_and_lock_active_incense(
    channel: discord.TextChannel,
    guild_id: str,
    opdex_id: int,
    bot_user_id: str,
) -> bool:
    """
    Scan the last 50 messages in a channel for an active incense.
    If found, register it and lock the channel. Returns True if locked.
    """
    try:
        async for msg in channel.history(limit=50):
            if msg.author.id != opdex_id:
                continue
            result = _parse_incense_active(msg)
            if result is None:
                continue
            incense_type, total_spawns = result

            # Found an active incense — register + lock
            await incense_db.register_incense(guild_id, str(channel.id), incense_type, total_spawns)
            await incense_db.set_paused(guild_id, str(channel.id), True)

            success = await _lock_channel(channel, opdex_id)
            if success:
                await channel.send(embed=_auto_lock_embed(channel, incense_type, total_spawns, guild_id))
                await incense_db.log_action(
                    guild_id, bot_user_id, "auto_lock",
                    f"Auto-locked #{channel.name} on registration ({incense_type}, {total_spawns} spawns)"
                )
            else:
                log.warning(f"Auto-lock on registration failed for #{channel.name}")
            return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Couldn't scan #{channel.name} for active incense: {e}")
    return False


async def _resync_unregistered_channels(
    guild: discord.Guild,
    guild_id: str,
    channel_ids: list[str],
    opdex_id: int,
    bot_uid: str,
) -> list[str]:
    """
    For channels that are registered but have NO active_incenses DB record,
    scan history for a running incense and register + lock if found.
    Returns list of channel_ids that were newly registered.
    """
    newly_registered = []
    for cid in channel_ids:
        ch = guild.get_channel(int(cid))
        if ch is None:
            continue
        try:
            async for msg in ch.history(limit=75):
                if msg.author.id != opdex_id:
                    continue
                result = _parse_incense_active(msg)
                if result is None:
                    continue
                inc_type, spawns = result
                await incense_db.register_incense(guild_id, cid, inc_type, spawns)
                await incense_db.set_paused(guild_id, cid, True)
                if not _is_channel_locked(ch, opdex_id):
                    await _lock_channel(ch, opdex_id)
                await incense_db.log_action(
                    guild_id, bot_uid, "resync_auto",
                    f"Auto-registered #{ch.name} on !p/!r ({inc_type}, {spawns} spawns)",
                )
                newly_registered.append(cid)
                break
        except (discord.Forbidden, discord.HTTPException):
            pass
    return newly_registered


# ── Channel list helpers ──────────────────────────────────────────────────────

def _sort_channel_ids(guild: discord.Guild, ids: list[str]) -> list[str]:
    """Return channel IDs sorted by their actual server position (category then channel)."""
    def _pos(cid: str) -> tuple[int, int]:
        ch = guild.get_channel(int(cid))
        if ch is None:
            return (99999, 99999)
        cat_pos = ch.category.position if ch.category else -1
        return (cat_pos, ch.position)
    return sorted(ids, key=_pos)


def _two_col_fields(
    name: str,
    mentions: list[str],
    *,
    per_col: int = 15,
) -> list[tuple[str, str, bool]]:
    """
    Build (field_name, field_value, inline) tuples for a 2-column layout.

    Up to `per_col` entries per column.  Returns at most 2 inline fields
    (left col + right col) — enough for one "row" in Discord's 3-col grid.
    Entries beyond 2×per_col are summarised as "+N more".
    """
    cap   = per_col * 2
    shown = mentions[:cap]
    extra = len(mentions) - cap

    half  = (len(shown) + 1) // 2
    left  = shown[:half]
    right = shown[half:]

    fields: list[tuple[str, str, bool]] = []
    fields.append((name, "\n".join(left), True))
    right_val = "\n".join(right) if right else "​"   # zero-width space keeps column alive
    if extra > 0:
        right_val = ("\n".join(right) + "\n" if right else "") + f"*+{extra} more…*"
    fields.append(("​", right_val, True))           # blank name for right col
    # Discord needs a non-inline spacer after two inline fields to avoid a
    # lonely third column.  We use an invisible zero-width field.
    fields.append(("​", "​", False))
    return fields


def _add_two_col(embed: discord.Embed, name: str, mentions: list[str], per_col: int = 15):
    """Add a 2-column channel listing to an embed."""
    for fname, fval, inline in _two_col_fields(name, mentions, per_col=per_col):
        embed.add_field(name=fname, value=fval, inline=inline)


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _pause_embed(
    locked, already, failed, cleaned,
    guild_id: str = "",
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    colour = 0xED4245 if failed else (0xFEE75C if already else 0xFF6B35)
    embed  = discord.Embed(title="⏸️  Mass Incense Paused", colour=colour)

    parts = []
    if locked:
        parts.append(f"🔒 **{len(locked)}** locked")
    if already:
        parts.append(f"⏭️ **{len(already)}** already paused")
    if failed:
        parts.append(f"⚠️ **{len(failed)}** failed")
    if cleaned:
        parts.append(f"🧹 **{len(cleaned)}** cleaned up")
    embed.description = "  •  ".join(parts) if parts else "Nothing to report."

    if guild:
        locked  = _sort_channel_ids(guild, locked)
        already = _sort_channel_ids(guild, already)
        failed  = _sort_channel_ids(guild, failed)

    if locked:
        _add_two_col(embed, f"🔒 Locked ({len(locked)})",
                     [f"<#{c}>" for c in locked])
    if already:
        _add_two_col(embed, f"⏭️ Already paused ({len(already)})",
                     [f"<#{c}>" for c in already])
    if failed:
        _add_two_col(embed, f"⚠️ Failed ({len(failed)})",
                     [f"<#{c}>" for c in failed])
    if cleaned:
        embed.add_field(
            name=f"🧹 Cleaned up ({len(cleaned)})",
            value=f"{len(cleaned)} deleted channel(s) removed from database.",
            inline=False,
        )
    embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
    return embed


def _resume_embed(
    unlocked, already, failed, cleaned,
    guild_id: str = "",
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    colour = 0xED4245 if failed else 0x57F287
    embed  = discord.Embed(title="▶️  Mass Incense Resumed", colour=colour)

    parts = []
    if unlocked:
        parts.append(f"🔓 **{len(unlocked)}** unlocked")
    if already:
        parts.append(f"⏭️ **{len(already)}** already live")
    if failed:
        parts.append(f"⚠️ **{len(failed)}** failed")
    if cleaned:
        parts.append(f"🧹 **{len(cleaned)}** cleaned up")
    embed.description = "  •  ".join(parts) if parts else "Nothing to report."

    if guild:
        unlocked = _sort_channel_ids(guild, unlocked)
        already  = _sort_channel_ids(guild, already)
        failed   = _sort_channel_ids(guild, failed)

    if unlocked:
        _add_two_col(embed, f"🔓 Resumed ({len(unlocked)})",
                     [f"<#{c}>" for c in unlocked])
    if already:
        _add_two_col(embed, f"⏭️ Already active ({len(already)})",
                     [f"<#{c}>" for c in already])
    if failed:
        _add_two_col(embed, f"⚠️ Failed ({len(failed)})",
                     [f"<#{c}>" for c in failed])
    if cleaned:
        embed.add_field(
            name=f"🧹 Cleaned up ({len(cleaned)})",
            value=f"{len(cleaned)} deleted channel(s) removed from database.",
            inline=False,
        )
    embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
    return embed


def _auto_lock_embed(channel, incense_type, total_spawns, guild_id: str = "") -> discord.Embed:
    embed = discord.Embed(
        title="🔒  Incense Auto-Paused",
        description=(
            f"A **{incense_type} Incense** was detected in {channel.mention}.\n\n"
            f"The channel has been **automatically locked** — "
            f"Pokémon can't spawn here until the incense is resumed.\n\n"
            f"*Waiting for the organizer to resume when all channels are ready.*"
        ),
        colour=0xFF6B35,
    )
    if total_spawns:
        embed.add_field(name="📊 Total Spawns", value=str(total_spawns), inline=True)
    embed.add_field(name="📍 Channel", value=channel.mention, inline=True)
    embed.set_footer(text=make_footer(guild_id, "Use !resume to start all incenses simultaneously"))
    return embed


# ── Confirmation views ───────────────────────────────────────────────────────

class _IncenseAddConfirmView(discord.ui.View):
    """Confirm for bulk incense add."""
    def __init__(self, author: discord.User, channels: list[discord.TextChannel],
                 guild_id: str, user_id: str, bot: commands.Bot):
        super().__init__(timeout=15)
        self.author   = author
        self.channels = channels
        self.guild_id = guild_id
        self.user_id  = user_id
        self.bot      = bot
        self.confirmed = False
        self._message: Optional[discord.InteractionMessage] = None

    async def on_timeout(self):
        if not self.confirmed:
            for item in self.children:
                item.disabled = True
            self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

        cids = [str(ch.id) for ch in self.channels]
        added_ids, already_ids = await incense_db.bulk_add_channels(
            self.guild_id, cids, self.user_id
        )

        added   = [ch for ch in self.channels if str(ch.id) in added_ids]
        already = [ch for ch in self.channels if str(ch.id) in already_ids]

        # Check newly added channels for already-active incenses & send notifications
        opdex_id = await _get_opdex_id(self.guild_id)
        bot_uid = str(self.bot.user.id)
        auto_locked = []
        for ch in added:
            locked = await _check_and_lock_active_incense(ch, self.guild_id, opdex_id, bot_uid)
            if locked:
                auto_locked.append(ch)
            else:
                try:
                    notify = discord.Embed(
                        title="🌿 Incense Channel Activated",
                        description=(
                            "This channel has been set up as a **QTs mass incense channel**.\n\n"
                            "When an incense is activated here, the channel will be "
                            "**automatically locked** until the organiser runs `!resume`."
                        ),
                        colour=0x57F287,
                    )
                    notify.set_footer(text=make_footer(self.guild_id, "Incense Manager"))
                    await ch.send(embed=notify)
                except discord.Forbidden:
                    pass

        embed = discord.Embed(
            title="🌿 Incense Channels Updated",
            colour=0x57F287 if added else 0xFEE75C,
        )
        if added:
            preview = ", ".join(ch.mention for ch in added[:20])
            if len(added) > 20:
                preview += f" *+{len(added) - 20} more*"
            embed.add_field(name=f"✅ Registered ({len(added)})", value=preview, inline=False)
        if auto_locked:
            preview = ", ".join(ch.mention for ch in auto_locked[:20])
            embed.add_field(name=f"🔒 Auto-locked ({len(auto_locked)})", value=preview, inline=False)
        if already:
            preview = ", ".join(ch.mention for ch in already[:15])
            if len(already) > 15:
                preview += f" *+{len(already) - 15} more*"
            embed.add_field(name=f"⏭️ Already registered ({len(already)})", value=preview, inline=False)
        embed.set_footer(text=make_footer(self.guild_id, "Incense Manager"))

        if added:
            await incense_db.log_action(
                self.guild_id, self.user_id, "register",
                f"Registered {len(added)} channel(s): {', '.join(ch.name for ch in added[:10])}"
            )
        # Dismiss the ephemeral prompt and post the result publicly
        await interaction.edit_original_response(
            embed=discord.Embed(title="✅ Done!", description="Result posted below.", colour=0x57F287),
            view=None,
        )
        await interaction.channel.send(embed=embed)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", description="No channels were registered.", colour=0xED4245),
            view=None,
        )


class _StatusPaginatorView(discord.ui.View):
    """
    Paginated viewer for /incense status.

    Each page shows up to CHANNELS_PER_PAGE channels in a 2-column layout,
    grouped by state (live / paused / idle) in server order.
    """

    CHANNELS_PER_PAGE = 30   # 15 per column × 2

    def __init__(
        self,
        author_id: int,
        guild: discord.Guild,
        live: list[str],
        paused: list[str],
        idle: list[str],
        stale_count: int,
        guild_id: str,
    ):
        super().__init__(timeout=120)
        self.author_id   = author_id
        self.guild       = guild
        self.guild_id    = guild_id
        self.stale_count = stale_count

        # All entries in order: (channel_id, state)
        self._entries: list[tuple[str, str]] = (
            [(cid, "live")   for cid in live]
            + [(cid, "paused") for cid in paused]
            + [(cid, "idle")   for cid in idle]
        )
        self._total   = len(self._entries)
        self._n_pages = max(1, -(-self._total // self.CHANNELS_PER_PAGE))  # ceil div
        self._page    = 0

        self._summary = (
            f"▶️ **{len(live)}** live  •  "
            f"⏸️ **{len(paused)}** paused  •  "
            f"💤 **{len(idle)}** idle  •  "
            f"📋 **{self._total}** total"
            + (f"\n> 🧹 *{stale_count} deleted channel(s) removed*" if stale_count else "")
        )
        self._update_buttons()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _update_buttons(self):
        self.btn_prev.disabled = (self._page == 0)
        self.btn_next.disabled = (self._page >= self._n_pages - 1)
        self.btn_page.label    = f"Page {self._page + 1} / {self._n_pages}"

    def _build_embed(self) -> discord.Embed:
        start = self._page * self.CHANNELS_PER_PAGE
        end   = start + self.CHANNELS_PER_PAGE
        slice_ = self._entries[start:end]

        # Group this page's entries by state
        by_state: dict[str, list[str]] = {"live": [], "paused": [], "idle": []}
        for cid, state in slice_:
            by_state[state].append(cid)

        STATE_ICON = {"live": "▶️", "paused": "⏸️", "idle": "💤"}
        STATE_LABEL = {"live": "Live", "paused": "Paused", "idle": "Idle"}

        embed = discord.Embed(title="📊 Incense Channel Status", colour=0x5865F2)
        embed.description = f"> {self._summary}"

        for state in ("live", "paused", "idle"):
            cids = by_state[state]
            if not cids:
                continue
            mentions = [f"<#{c}>" for c in cids]
            icon     = STATE_ICON[state]
            label    = STATE_LABEL[state]
            half     = (len(mentions) + 1) // 2
            left     = mentions[:half]
            right    = mentions[half:]

            embed.add_field(
                name=f"{icon} {label} ({len(cids)})",
                value="\n".join(left),
                inline=True,
            )
            embed.add_field(
                name="​",
                value="\n".join(right) if right else "​",
                inline=True,
            )
            embed.add_field(name="​", value="​", inline=False)  # row break

        embed.set_footer(
            text=make_footer(self.guild_id, f"Incense Manager  •  Page {self._page + 1}/{self._n_pages}")
        )
        return embed

    # ── buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="status_prev")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your panel.", ephemeral=True)
        self._page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Page 1 / 1", style=discord.ButtonStyle.primary, disabled=True, custom_id="status_page")
    async def btn_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="status_next")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("Not your panel.", ephemeral=True)
        self._page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        self.stop()


class _IncenseRemoveConfirmView(discord.ui.View):
    """Confirm for bulk incense remove."""
    def __init__(self, author: discord.User, channels: list[discord.TextChannel],
                 guild_id: str, user_id: str):
        super().__init__(timeout=15)
        self.author   = author
        self.channels = channels
        self.guild_id = guild_id
        self.user_id  = user_id
        self.confirmed = False

    async def on_timeout(self):
        if not self.confirmed:
            for item in self.children:
                item.disabled = True
            self.stop()

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

        cids = [str(ch.id) for ch in self.channels]
        removed_ids, notfound_ids = await incense_db.bulk_remove_channels(self.guild_id, cids)

        removed  = [ch for ch in self.channels if str(ch.id) in removed_ids]
        notfound = [ch for ch in self.channels if str(ch.id) in notfound_ids]

        embed = discord.Embed(
            title="🌿 Incense Channels Removed",
            colour=0x57F287 if removed else 0xFEE75C,
        )
        if removed:
            preview = ", ".join(ch.mention for ch in removed[:20])
            if len(removed) > 20:
                preview += f" *+{len(removed) - 20} more*"
            embed.add_field(name=f"🗑️ Removed ({len(removed)})", value=preview, inline=False)
        if notfound:
            preview = ", ".join(ch.mention for ch in notfound[:15])
            embed.add_field(name=f"⏭️ Not registered ({len(notfound)})", value=preview, inline=False)
        embed.set_footer(text=make_footer(self.guild_id, "Incense Manager"))

        if removed:
            await incense_db.log_action(
                self.guild_id, self.user_id, "bulk_unregister",
                f"Removed {len(removed)} channel(s): {', '.join(ch.name for ch in removed[:10])}"
            )
        # Dismiss the ephemeral prompt and post the result publicly
        await interaction.edit_original_response(
            embed=discord.Embed(title="✅ Done!", description="Result posted below.", colour=0x57F287),
            view=None,
        )
        await interaction.channel.send(embed=embed)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("Not your confirmation.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Cancelled", description="No channels were removed.", colour=0xED4245),
            view=None,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class IncenseCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Auto-lock listener ────────────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _watch_opdex(self, message: discord.Message):
        if not message.guild:
            return

        guild_id = str(message.guild.id)
        opdex_id = await _get_opdex_id(guild_id)

        if message.author.id != opdex_id:
            return

        result = _parse_incense_activation(message)
        if result is None:
            return

        incense_type, total_spawns = result
        channel_id = str(message.channel.id)

        if not await incense_db.is_incense_channel(guild_id, channel_id):
            log.debug(f"Incense in unregistered channel {channel_id}, ignoring")
            return

        log.info(f"🌿 Incense activated in #{message.channel.name} — auto-locking")

        await incense_db.register_incense(guild_id, channel_id, incense_type, total_spawns)
        await incense_db.set_paused(guild_id, channel_id, True)

        success = await _lock_channel(message.channel, opdex_id)
        if success:
            await message.channel.send(embed=_auto_lock_embed(
                message.channel, incense_type, total_spawns, guild_id
            ))
            await incense_db.log_action(
                guild_id, str(self.bot.user.id), "auto_lock",
                f"Auto-locked #{message.channel.name} ({incense_type}, {total_spawns} spawns)"
            )
        else:
            log.warning(f"Auto-lock failed for #{message.channel.name} in {message.guild.name}")
            err_embed = discord.Embed(
                title="⚠️ Auto-Lock Failed",
                description=(
                    f"A **{incense_type} Incense** was detected in {message.channel.mention}, "
                    f"but I couldn't lock the channel.\n\n"
                    f"**Please check my permissions** (Manage Channel / Manage Roles) "
                    f"and manually pause if needed."
                ),
                colour=0xED4245,
            )
            err_embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
            try:
                await message.channel.send(embed=err_embed)
            except discord.Forbidden:
                log.error(f"Cannot send auto-lock failure message in #{message.channel.name}")

    # ── !pause ────────────────────────────────────────────────────────────────

    @commands.command(name="pause", aliases=["p"])
    async def pause_cmd(self, ctx: commands.Context, group: str = ""):
        """Pause all incenses, or just a named group: !pause [group]"""
        if not _is_qt_guild(ctx):
            return
        if not await _is_authorised(ctx):
            return await ctx.send(
                "🚫 You don't have permission. An admin must set up the Incense Manager "
                "role with `/incense setup role`."
            )

        guild_id = str(ctx.guild.id)
        opdex_id = await _get_opdex_id(guild_id)

        # Resolve group filter
        group = group.strip().lower()
        if group:
            if not await incense_db.group_exists(guild_id, group):
                known = await incense_db.get_groups(guild_id)
                known_str = ", ".join(f"**{g}**" for g in known) if known else "*none created yet*"
                return await ctx.send(
                    f"⚠️ No group named **{group}**. Known groups: {known_str}\n"
                    f"Create one with `/incense group create`."
                )
            candidate_ids = await incense_db.get_group_channels(guild_id, group)
        else:
            candidate_ids = await incense_db.get_channels(guild_id)

        # Auto-resync channels that are registered but have no DB record
        # (catches incenses cracked before the bot was set up, or missed activations)
        known_active_ids = {r["channel_id"] for r in await incense_db.get_active_incenses(guild_id)}
        unregistered = [c for c in candidate_ids if c not in known_active_ids]
        if unregistered:
            await _resync_unregistered_channels(
                ctx.guild, guild_id, unregistered, opdex_id, str(self.bot.user.id)
            )

        if group:
            actives = await incense_db.get_active_incenses_for_group(guild_id, group)
        else:
            actives = await incense_db.get_active_incenses(guild_id)

        if not actives:
            scope = f"group **{group}**" if group else "any registered channel"
            return await ctx.send(
                embed=discord.Embed(
                    title="ℹ️ Nothing to Pause",
                    description=(
                        f"No active incenses are currently running in {scope}.\n\n"
                        "Channels are locked automatically when an incense activates.\n"
                        "If incenses were started before the bot was set up, "
                        "run `/incense resync` to scan channel history."
                    ),
                    colour=0x5865F2,
                )
            )

        locked  = []
        already = []
        failed  = []
        cleaned = []

        async def process(record: dict):
            ch = ctx.guild.get_channel(int(record["channel_id"]))
            if not ch:
                await incense_db.remove_channel_and_incense(guild_id, record["channel_id"])
                cleaned.append(record["channel_id"])
                return
            if record["paused"] or _is_channel_locked(ch, opdex_id):
                already.append(record["channel_id"])
                await incense_db.set_paused(guild_id, record["channel_id"], True)
                return
            success = await _lock_channel(ch, opdex_id)
            if success:
                await incense_db.set_paused(guild_id, record["channel_id"], True)
                locked.append(record["channel_id"])
            else:
                failed.append(record["channel_id"])

        await asyncio.gather(*[process(r) for r in actives])

        action = f"mass_pause:{group}" if group else "mass_pause"
        await incense_db.log_action(
            guild_id, str(ctx.author.id), action,
            f"Locked {len(locked)}, already {len(already)}, failed {len(failed)}, cleaned {len(cleaned)}"
            + (f" [group={group}]" if group else "")
        )
        embed = _pause_embed(locked, already, failed, cleaned, guild_id, ctx.guild)
        if group:
            embed.title = f"⏸️  Mass Incense Paused — {group}"
        await ctx.send(embed=embed)

    # ── !resume ───────────────────────────────────────────────────────────────

    @commands.command(name="resume", aliases=["r"])
    async def resume_cmd(self, ctx: commands.Context, group: str = ""):
        """Resume all incenses, or just a named group: !resume [group]"""
        if not _is_qt_guild(ctx):
            return
        if not await _is_authorised(ctx):
            return await ctx.send(
                "🚫 You don't have permission. An admin must set up the Incense Manager "
                "role with `/incense setup role`."
            )

        guild_id = str(ctx.guild.id)
        opdex_id = await _get_opdex_id(guild_id)

        # Resolve group filter
        group = group.strip().lower()
        if group:
            if not await incense_db.group_exists(guild_id, group):
                known = await incense_db.get_groups(guild_id)
                known_str = ", ".join(f"**{g}**" for g in known) if known else "*none created yet*"
                return await ctx.send(
                    f"⚠️ No group named **{group}**. Known groups: {known_str}\n"
                    f"Create one with `/incense group create`."
                )
            candidate_ids = await incense_db.get_group_channels(guild_id, group)
        else:
            candidate_ids = await incense_db.get_channels(guild_id)

        # Auto-resync channels with no DB record (pre-existing / cracked incenses)
        known_active_ids = {r["channel_id"] for r in await incense_db.get_active_incenses(guild_id)}
        unregistered = [c for c in candidate_ids if c not in known_active_ids]
        if unregistered:
            await _resync_unregistered_channels(
                ctx.guild, guild_id, unregistered, opdex_id, str(self.bot.user.id)
            )

        if group:
            actives = await incense_db.get_active_incenses_for_group(guild_id, group)
        else:
            actives = await incense_db.get_active_incenses(guild_id)

        if not actives:
            scope = f"group **{group}**" if group else "any registered channel"
            return await ctx.send(
                embed=discord.Embed(
                    title="ℹ️ Nothing to Resume",
                    description=(
                        f"No active incenses are currently paused in {scope}.\n\n"
                        "Incenses register automatically when activated in a registered channel.\n"
                        "If incenses were started before the bot was set up, "
                        "run `/incense resync` to scan channel history."
                    ),
                    colour=0x5865F2,
                )
            )

        unlocked = []
        already  = []
        failed   = []
        cleaned  = []

        async def process(record: dict):
            ch = ctx.guild.get_channel(int(record["channel_id"]))
            if not ch:
                await incense_db.remove_channel_and_incense(guild_id, record["channel_id"])
                cleaned.append(record["channel_id"])
                return
            if not record["paused"] and not _is_channel_locked(ch, opdex_id):
                already.append(record["channel_id"])
                return
            success = await _unlock_channel(ch, opdex_id)
            if success:
                await incense_db.set_paused(guild_id, record["channel_id"], False)
                unlocked.append(record["channel_id"])
                try:
                    resume_embed = discord.Embed(
                        title="▶️ Incense Live!",
                        description="The incense is now **active** — Pokémon are spawning! Good luck, trainers! 🎉",
                        colour=0x57F287,
                    )
                    footer_hint = f"Use !pause {group} to pause this group" if group else "Use !pause to pause all incenses"
                    resume_embed.set_footer(text=make_footer(guild_id, footer_hint))
                    await ch.send(embed=resume_embed)
                except discord.Forbidden:
                    pass
            else:
                failed.append(record["channel_id"])

        await asyncio.gather(*[process(r) for r in actives])

        action = f"mass_resume:{group}" if group else "mass_resume"
        await incense_db.log_action(
            guild_id, str(ctx.author.id), action,
            f"Unlocked {len(unlocked)}, already {len(already)}, failed {len(failed)}, cleaned {len(cleaned)}"
            + (f" [group={group}]" if group else "")
        )
        embed = _resume_embed(unlocked, already, failed, cleaned, guild_id, ctx.guild)
        if group:
            embed.title = f"▶️  Mass Incense Resumed — {group}"
        await ctx.send(embed=embed)

    # ── !incset ───────────────────────────────────────────────────────────────

    @commands.command(name="incset")
    async def incset_cmd(self, ctx: commands.Context, *, args: str = ""):
        if not _is_qt_guild(ctx):
            return
        if not await _is_authorised(ctx):
            return await ctx.send("🚫 You need the **Incense Manager** role to register incense channels.")

        raw_ids = re.findall(r"\d{17,20}", args)
        if not raw_ids:
            return await ctx.send(
                "❌ No valid channel IDs found.\n"
                "Usage: `!incset 123456789 987654321`"
            )

        guild_id = str(ctx.guild.id)
        added    = []
        already  = []
        invalid  = []

        for cid in raw_ids:
            ch = ctx.guild.get_channel(int(cid))
            if not ch:
                invalid.append(cid)
                continue
            ok = await incense_db.add_channel(guild_id, cid, str(ctx.author.id))
            (added if ok else already).append(ch)

        # Check newly added channels for already-active incenses
        opdex_id = await _get_opdex_id(guild_id)
        bot_uid = str(self.bot.user.id)
        auto_locked = []
        for ch in added:
            if await _check_and_lock_active_incense(ch, guild_id, opdex_id, bot_uid):
                auto_locked.append(ch)

        embed = discord.Embed(
            title="🌿 Incense Channels Updated",
            colour=0x57F287 if added else 0xFEE75C,
        )
        if added:
            embed.add_field(
                name=f"✅ Registered ({len(added)})",
                value="\n".join(ch.mention for ch in added[:20]),
                inline=True,
            )
        if auto_locked:
            embed.add_field(
                name=f"🔒 Auto-locked ({len(auto_locked)})",
                value="\n".join(ch.mention for ch in auto_locked[:20]),
                inline=True,
            )
        if already:
            embed.add_field(
                name=f"⏭️ Already registered ({len(already)})",
                value="\n".join(ch.mention for ch in already[:10]),
                inline=True,
            )
        if invalid:
            embed.add_field(
                name=f"❓ Not found ({len(invalid)})",
                value="\n".join(f"`{i}`" for i in invalid[:10]),
                inline=False,
            )
        embed.set_footer(text=make_footer(guild_id, "Incense Manager"))

        if added:
            await incense_db.log_action(
                guild_id, str(ctx.author.id), "bulk_register",
                f"Registered {len(added)} channels via !incset"
            )
        await ctx.send(embed=embed)

    # ── Slash command group ──────────────────────────────────────────────────

    incense = app_commands.Group(
        name="incense",
        description="Manage incense channels and operations.",
    )

    # ── /incense setup ───────────────────────────────────────────────────────

    setup_group = app_commands.Group(
        name="setup",
        description="Configure incense settings for this server.",
        parent=incense,
    )

    @setup_group.command(name="role", description="Set which role can manage incenses in this server.")
    @app_commands.describe(role="The role that can manage incenses")
    async def setup_role(self, interaction: discord.Interaction, role: discord.Role):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not interaction.user.guild_permissions.administrator and interaction.user.id != OWNER_ID:
            return await interaction.response.send_message(
                "🚫 Only server administrators can configure the incense manager role.", ephemeral=True
            )
        gid = str(interaction.guild_id or "")
        await guild_settings_db.set_incense_role(gid, role.id)
        await incense_db.log_action(
            gid, str(interaction.user.id), "setup_role",
            f"Set incense manager role to {role.name} ({role.id})"
        )
        embed = discord.Embed(
            title="✅ Incense Manager Role Set",
            description=(
                f"Members with the {role.mention} role can now manage incenses.\n\n"
                f"They'll have access to `!pause`, `!resume`, `!incset`, and all `/incense` commands."
            ),
            colour=0x57F287,
        )
        embed.set_footer(text=make_footer(gid, "Incense Manager"))
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @setup_group.command(name="bot", description="Set which bot is the Operation Dex bot for auto-detection.")
    @app_commands.describe(bot_id="The bot's user ID (right-click the bot → Copy User ID)")
    async def setup_bot(self, interaction: discord.Interaction, bot_id: str):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not interaction.user.guild_permissions.administrator and interaction.user.id != OWNER_ID:
            return await interaction.response.send_message(
                "🚫 Only server administrators can configure the Operation Dex bot.", ephemeral=True
            )
        try:
            bid = int(bot_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid bot ID.", ephemeral=True)

        gid = str(interaction.guild_id or "")
        await guild_settings_db.set_opdex_bot_id(gid, bid)
        await incense_db.log_action(
            gid, str(interaction.user.id), "setup_bot",
            f"Set Operation Dex bot ID to {bid}"
        )
        embed = discord.Embed(
            title="✅ Operation Dex Bot Set",
            description=(
                f"I'll now watch for incense activations from bot ID `{bid}`.\n"
                f"Make sure this bot is in the server!"
            ),
            colour=0x57F287,
        )
        embed.set_footer(text=make_footer(gid, "Incense Manager"))
        await interaction.response.send_message(embed=embed)

    @setup_group.command(name="view", description="View the current incense configuration for this server.")
    async def setup_view(self, interaction: discord.Interaction):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        role_id = await guild_settings_db.get_incense_role(guild_id)
        opdex_id = await _get_opdex_id(guild_id)
        num_channels = len(await incense_db.get_channels(guild_id))

        role_str = f"<@&{role_id}>" if role_id else "*Not set — only admins can manage incenses*"
        embed = discord.Embed(
            title="⚙️ Incense Configuration",
            colour=0x5865F2,
        )
        embed.add_field(name="🎭 Manager Role", value=role_str, inline=False)
        embed.add_field(name="🤖 Operation Dex Bot", value=f"`{opdex_id}`", inline=True)
        embed.add_field(name="📍 Registered Channels", value=str(num_channels), inline=True)
        embed.set_footer(text=make_footer(str(interaction.guild_id or ""), "Incense Manager"))
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # ── /incense add ─────────────────────────────────────────────────────────

    @incense.command(name="add", description="Register incense channels — individual, by category, or by range.")
    @app_commands.describe(
        channel="A single channel to add",
        category="Add all channels in this category",
        category2="Add all channels in a second category (optional)",
        category3="Add all channels in a third category (optional)",
        from_channel="Start of a range to add (inclusive)",
        to_channel="End of a range to add (inclusive)",
    )
    async def inc_add(
        self,
        interaction:   discord.Interaction,
        channel:       Optional[discord.TextChannel]       = None,
        category:      Optional[discord.CategoryChannel]   = None,
        category2:     Optional[discord.CategoryChannel]   = None,
        category3:     Optional[discord.CategoryChannel]   = None,
        from_channel:  Optional[discord.TextChannel]       = None,
        to_channel:    Optional[discord.TextChannel]       = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        targets: list[discord.TextChannel] = []

        # Mode 1: single channel
        if channel:
            targets.append(channel)

        # Mode 2: whole categories
        for cat in [category, category2, category3]:
            if cat:
                targets.extend(
                    ch for ch in cat.channels
                    if isinstance(ch, discord.TextChannel) and ch not in targets
                )

        # Mode 3: range
        if from_channel and to_channel:
            cat = from_channel.category
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == cat],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "❌ Could not resolve channel range. Ensure both are in the same category.",
                    ephemeral=True,
                )
            if si > ei:
                si, ei = ei, si
            for ch in pool[si:ei + 1]:
                if ch not in targets:
                    targets.append(ch)
        elif from_channel or to_channel:
            return await interaction.response.send_message(
                "⚠️ Provide **both** `from_channel` and `to_channel` for a range.", ephemeral=True
            )

        if not targets:
            return await interaction.response.send_message(
                "⚠️ No channels specified. Provide `channel`, `category`, or `from_channel`+`to_channel`.",
                ephemeral=True,
            )

        # Deduplicate while preserving order
        seen: set[int] = set()
        unique: list[discord.TextChannel] = []
        for ch in targets:
            if ch.id not in seen:
                seen.add(ch.id)
                unique.append(ch)

        # Build confirmation
        preview = ", ".join(ch.mention for ch in unique[:25])
        if len(unique) > 25:
            preview += f" *+{len(unique) - 25} more*"

        embed = discord.Embed(
            title="⚠️  Confirm Incense Channel Registration",
            description=(
                f"**{len(unique)}** channel{'s' if len(unique) != 1 else ''} will be registered:\n\n"
                f"{preview}\n\n"
                "Each channel will receive a setup notification.\n"
                "*Click Confirm within 15 seconds.*"
            ),
            colour=0xFEE75C,
        )
        embed.set_footer(text=make_footer(guild_id, "Incense Manager"))

        view = _IncenseAddConfirmView(interaction.user, unique, guild_id, str(interaction.user.id), self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /incense remove ──────────────────────────────────────────────────────

    @incense.command(name="remove", description="Unregister incense channels — individual, by category, or by range.")
    @app_commands.describe(
        channel="A single channel to remove",
        category="Remove all channels in this category",
        category2="Remove all channels in a second category (optional)",
        category3="Remove all channels in a third category (optional)",
        from_channel="Start of a range to remove (inclusive)",
        to_channel="End of a range to remove (inclusive)",
    )
    async def inc_remove(
        self,
        interaction:   discord.Interaction,
        channel:       Optional[discord.TextChannel]       = None,
        category:      Optional[discord.CategoryChannel]   = None,
        category2:     Optional[discord.CategoryChannel]   = None,
        category3:     Optional[discord.CategoryChannel]   = None,
        from_channel:  Optional[discord.TextChannel]       = None,
        to_channel:    Optional[discord.TextChannel]       = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        guild_id = str(interaction.guild_id)
        targets: list[discord.TextChannel] = []

        if channel:
            targets.append(channel)

        for cat in [category, category2, category3]:
            if cat:
                targets.extend(
                    ch for ch in cat.channels
                    if isinstance(ch, discord.TextChannel) and ch not in targets
                )

        if from_channel and to_channel:
            cat = from_channel.category
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == cat],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "❌ Could not resolve channel range.", ephemeral=True
                )
            if si > ei:
                si, ei = ei, si
            for ch in pool[si:ei + 1]:
                if ch not in targets:
                    targets.append(ch)
        elif from_channel or to_channel:
            return await interaction.response.send_message(
                "⚠️ Provide **both** `from_channel` and `to_channel` for a range.", ephemeral=True
            )

        if not targets:
            return await interaction.response.send_message(
                "⚠️ No channels specified.", ephemeral=True,
            )

        seen: set[int] = set()
        unique: list[discord.TextChannel] = []
        for ch in targets:
            if ch.id not in seen:
                seen.add(ch.id)
                unique.append(ch)

        preview = ", ".join(ch.mention for ch in unique[:25])
        if len(unique) > 25:
            preview += f" *+{len(unique) - 25} more*"

        embed = discord.Embed(
            title="⚠️  Confirm Incense Channel Removal",
            description=(
                f"**{len(unique)}** channel{'s' if len(unique) != 1 else ''} will be unregistered:\n\n"
                f"{preview}\n\n"
                "*Click Remove within 15 seconds.*"
            ),
            colour=0xED4245,
        )
        embed.set_footer(text=make_footer(guild_id, "Incense Manager"))

        view = _IncenseRemoveConfirmView(interaction.user, unique, guild_id, str(interaction.user.id))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /incense lock ────────────────────────────────────────────────────────

    @incense.command(name="lock", description="Lock a specific incense channel.")
    @app_commands.describe(channel="Channel to lock (defaults to current channel)")
    async def inc_lock(
        self,
        interaction: discord.Interaction,
        channel:     Optional[discord.TextChannel] = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        ch       = channel or interaction.channel
        guild_id = str(interaction.guild_id)
        opdex_id = await _get_opdex_id(guild_id)

        if not await incense_db.is_incense_channel(guild_id, str(ch.id)):
            return await interaction.response.send_message(
                f"⚠️ {ch.mention} is not a registered incense channel.", ephemeral=True
            )
        if _is_channel_locked(ch, opdex_id):
            return await interaction.response.send_message(
                f"ℹ️ {ch.mention} is already locked.", ephemeral=True
            )

        await interaction.response.defer()
        success = await _lock_channel(ch, opdex_id)
        await incense_db.set_paused(guild_id, str(ch.id), True)

        if success:
            await incense_db.log_action(
                guild_id, str(interaction.user.id), "lock",
                f"Locked #{ch.name}"
            )
            embed = discord.Embed(
                title="🔒 Channel Locked",
                description=f"{ch.mention} has been locked. Pokémon cannot spawn here.",
                colour=0xFF6B35,
            )
            embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
            await interaction.followup.send(embed=embed)
        else:
            err_embed = discord.Embed(
                title="❌ Lock Failed",
                description=(
                    f"I couldn't lock {ch.mention}.\n\n"
                    "Please check that I have **Manage Channel** and **Manage Roles** permissions."
                ),
                colour=0xED4245,
            )
            err_embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
            await interaction.followup.send(embed=err_embed, ephemeral=True)

    # ── /incense unlock ──────────────────────────────────────────────────────

    @incense.command(name="unlock", description="Unlock a specific incense channel.")
    @app_commands.describe(channel="Channel to unlock (defaults to current channel)")
    async def inc_unlock(
        self,
        interaction: discord.Interaction,
        channel:     Optional[discord.TextChannel] = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        ch       = channel or interaction.channel
        guild_id = str(interaction.guild_id)
        opdex_id = await _get_opdex_id(guild_id)

        if not await incense_db.is_incense_channel(guild_id, str(ch.id)):
            return await interaction.response.send_message(
                f"⚠️ {ch.mention} is not a registered incense channel.", ephemeral=True
            )
        if not _is_channel_locked(ch, opdex_id):
            return await interaction.response.send_message(
                f"ℹ️ {ch.mention} is already unlocked.", ephemeral=True
            )

        await interaction.response.defer()
        success = await _unlock_channel(ch, opdex_id)
        await incense_db.set_paused(guild_id, str(ch.id), False)

        if success:
            await incense_db.log_action(
                guild_id, str(interaction.user.id), "unlock",
                f"Unlocked #{ch.name}"
            )
            embed = discord.Embed(
                title="🔓 Channel Unlocked",
                description=f"{ch.mention} is now live. Pokémon will start spawning! 🎉",
                colour=0x57F287,
            )
            embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
            await interaction.followup.send(embed=embed)
            if ch.id != interaction.channel_id:
                try:
                    resume_embed = discord.Embed(
                        title="▶️ Incense Live!",
                        description="The incense is now **active** — Pokémon are spawning! Good luck! 🎉",
                        colour=0x57F287,
                    )
                    resume_embed.set_footer(text=make_footer(guild_id, "Use !pause to pause all incenses"))
                    await ch.send(embed=resume_embed)
                except discord.Forbidden:
                    pass
        else:
            err_embed = discord.Embed(
                title="❌ Unlock Failed",
                description=(
                    f"I couldn't unlock {ch.mention}.\n\n"
                    "Please check that I have **Manage Channel** and **Manage Roles** permissions."
                ),
                colour=0xED4245,
            )
            err_embed.set_footer(text=make_footer(guild_id, "Incense Manager"))
            await interaction.followup.send(embed=err_embed, ephemeral=True)

    # ── /incense recursive ───────────────────────────────────────────────────

    @incense.command(
        name="recursive",
        description="Register channels after this one as incense channels.",
    )
    @app_commands.describe(
        include_current="Also include this channel (default: False)",
        end_channel="Optional: last channel to include (by server position)",
    )
    async def inc_set_recursive(
        self,
        interaction:     discord.Interaction,
        include_current: bool                             = False,
        end_channel:     Optional[discord.TextChannel]   = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        await interaction.response.defer(thinking=True)

        guild    = interaction.guild
        guild_id = str(guild.id)
        current  = interaction.channel

        text_channels = sorted(
            [c for c in guild.channels if isinstance(c, discord.TextChannel)],
            key=lambda c: (c.category.position if c.category else -1, c.position),
        )

        try:
            current_idx = next(i for i, c in enumerate(text_channels) if c.id == current.id)
        except StopIteration:
            return await interaction.followup.send(
                "❌ Couldn't find this channel in the server channel list.", ephemeral=True
            )

        start_idx = current_idx if include_current else current_idx + 1

        if end_channel is not None:
            try:
                end_idx = next(i for i, c in enumerate(text_channels) if c.id == end_channel.id)
            except StopIteration:
                return await interaction.followup.send(
                    "❌ Couldn't find the end channel in the server channel list.", ephemeral=True
                )
            if end_idx < start_idx:
                return await interaction.followup.send(
                    "❌ The end channel must come **after** the start channel in server order.",
                    ephemeral=True,
                )
            targets = text_channels[start_idx:end_idx + 1]
        else:
            targets = text_channels[start_idx:]

        if not targets:
            return await interaction.followup.send(
                "ℹ️ No channels found in that range.", ephemeral=True
            )

        added   = []
        already = []

        async def register(ch: discord.TextChannel):
            ok = await incense_db.add_channel(guild_id, str(ch.id), str(interaction.user.id))
            (added if ok else already).append(ch)

        for i in range(0, len(targets), 10):
            await asyncio.gather(*[register(ch) for ch in targets[i:i + 10]])
            if i + 10 < len(targets):
                await asyncio.sleep(1)

        if end_channel:
            range_desc = (
                f"from {current.mention} " + ("(inclusive)" if include_current else "(exclusive)")
                + f" through {end_channel.mention}"
            )
        else:
            range_desc = (
                f"{'from' if include_current else 'after'} {current.mention} to the last channel"
            )

        # Check newly added channels for already-active incenses
        opdex_id = await _get_opdex_id(guild_id)
        bot_uid = str(self.bot.user.id)
        auto_locked = []
        for ch in added:
            if await _check_and_lock_active_incense(ch, guild_id, opdex_id, bot_uid):
                auto_locked.append(ch)

        embed = discord.Embed(
            title="🌿 Recursive Registration Complete",
            description=f"Scanned **{len(targets)}** channel{'s' if len(targets) != 1 else ''} {range_desc}.",
            colour=0x57F287 if added else 0xFEE75C,
        )
        if added:
            preview = ", ".join(ch.mention for ch in added[:10])
            if len(added) > 10:
                preview += f" *+{len(added)-10} more*"
            embed.add_field(name=f"✅ Registered ({len(added)})", value=preview, inline=False)
        if auto_locked:
            preview = ", ".join(ch.mention for ch in auto_locked[:10])
            if len(auto_locked) > 10:
                preview += f" *+{len(auto_locked)-10} more*"
            embed.add_field(name=f"🔒 Auto-locked ({len(auto_locked)})", value=preview, inline=False)
        if already:
            preview = ", ".join(ch.mention for ch in already[:10])
            if len(already) > 10:
                preview += f" *+{len(already)-10} more*"
            embed.add_field(name=f"⏭️ Already registered ({len(already)})", value=preview, inline=False)
        embed.set_footer(text=make_footer(guild_id, "Incense Manager"))

        if added:
            await incense_db.log_action(
                guild_id, str(interaction.user.id), "recursive_register",
                f"Registered {len(added)} channels recursively"
            )
        await interaction.followup.send(embed=embed)

    # ── /incense status ──────────────────────────────────────────────────────

    @incense.command(name="status", description="Show all incense channels and their current state.")
    async def inc_status(self, interaction: discord.Interaction):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        await interaction.response.defer(thinking=True)

        guild_id   = str(interaction.guild_id)
        opdex_id   = await _get_opdex_id(guild_id)
        registered = await incense_db.get_channels(guild_id)
        actives    = {r["channel_id"]: r for r in await incense_db.get_active_incenses(guild_id)}

        # Auto-clean deleted channels silently
        stale = [cid for cid in registered if not interaction.guild.get_channel(int(cid))]
        for cid in stale:
            await incense_db.remove_channel_and_incense(guild_id, cid)
        registered = [cid for cid in registered if cid not in stale]

        if not registered:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title="📊 Incense Status",
                    description="No incense channels registered yet.\nUse `!incset` or `/incense add` to register some.",
                    colour=0x5865F2,
                )
            )

        # Sort registered channels by actual server position
        def _ch_pos(cid: str) -> tuple[int, int]:
            ch = interaction.guild.get_channel(int(cid))
            if ch is None:
                return (99999, 99999)
            return (ch.category.position if ch.category else -1, ch.position)

        registered = sorted(registered, key=_ch_pos)

        live_ids   = []
        paused_ids = []
        idle_ids   = []

        for cid in registered:
            ch = interaction.guild.get_channel(int(cid))
            if cid in actives:
                rec = actives[cid]
                if rec["paused"] or _is_channel_locked(ch, opdex_id):
                    paused_ids.append(cid)
                else:
                    live_ids.append(cid)
            else:
                idle_ids.append(cid)

        view = _StatusPaginatorView(
            author_id   = interaction.user.id,
            guild       = interaction.guild,
            live        = live_ids,
            paused      = paused_ids,
            idle        = idle_ids,
            stale_count = len(stale),
            guild_id    = guild_id,
        )
        # Only attach the view if there's more than one page
        kwargs = {"embed": view._build_embed()}
        if view._n_pages > 1:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    # ── /incense clear ───────────────────────────────────────────────────────

    @incense.command(name="clear", description="Clear the incense record for a channel.")
    @app_commands.describe(channel="Channel to clear (defaults to current channel)")
    async def inc_clear(
        self,
        interaction: discord.Interaction,
        channel:     Optional[discord.TextChannel] = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message("🚫 You need the **Incense Manager** role.", ephemeral=True)

        ch       = channel or interaction.channel
        guild_id = str(interaction.guild_id)

        if not await incense_db.has_active_incense(guild_id, str(ch.id)):
            return await interaction.response.send_message(
                f"ℹ️ No active incense record for {ch.mention}.", ephemeral=True
            )

        await incense_db.clear_incense(guild_id, str(ch.id))
        await incense_db.log_action(
            guild_id, str(interaction.user.id), "clear",
            f"Cleared incense record for #{ch.name}"
        )
        await interaction.response.send_message(
            f"🗑️ Incense record cleared for {ch.mention}. "
            "It will register fresh on next activation.",
        )

    # ── /incense resync ──────────────────────────────────────────────────────

    @incense.command(
        name="resync",
        description="Scan all registered channels for active incenses and fix lock/DB state.",
    )
    async def inc_resync(self, interaction: discord.Interaction):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Incense Manager** role.", ephemeral=True
            )

        await interaction.response.defer(thinking=True)

        guild_id  = str(interaction.guild_id)
        guild     = interaction.guild
        opdex_id  = await _get_opdex_id(guild_id)
        bot_id    = self.bot.user.id
        bot_uid   = str(bot_id)

        registered = await incense_db.get_channels(guild_id)

        # Prune stale channels
        stale = [cid for cid in registered if not guild.get_channel(int(cid))]
        for cid in stale:
            await incense_db.remove_channel_and_incense(guild_id, cid)
        registered = [cid for cid in registered if cid not in stale]

        found_active:    list[str] = []   # had a live incense in history
        now_locked:      list[str] = []   # we locked it during resync
        already_locked:  list[str] = []   # was already locked, DB updated
        cleared_stale:   list[str] = []   # DB said active but no incense found → cleared
        cleaned_msgs:    int = 0          # bot messages deleted

        # BOT_MSG_MARKERS — titles of messages we sent ourselves that we should clean up
        _OWN_TITLES = {
            "🔒  Incense Auto-Paused",
            "🔒 Channel Locked",
            "▶️  Mass Incense Resumed",
            "⏸️  Mass Incense Paused",
            "▶️ Incense Live!",
            "🌿 Incense Channel Activated",
            "⚠️ Auto-Lock Failed",
        }

        for cid in registered:
            ch = guild.get_channel(int(cid))
            if ch is None:
                continue

            # --- Scan last 75 messages for an Operation Dex incense activation ---
            found_inc: tuple[str, int] | None = None
            try:
                async for msg in ch.history(limit=75):
                    # Delete our own housekeeping messages while we're here
                    if msg.author.id == bot_id:
                        for emb in msg.embeds:
                            if (emb.title or "") in _OWN_TITLES:
                                try:
                                    await msg.delete()
                                    cleaned_msgs += 1
                                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                                    pass
                                break
                        continue

                    if msg.author.id != opdex_id:
                        continue

                    result = _parse_incense_active(msg)
                    if result:
                        found_inc = result
                        break

            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"resync: can't read #{ch.name}: {e}")
                continue

            has_db_record = await incense_db.has_active_incense(guild_id, cid)

            if found_inc:
                inc_type, spawns = found_inc
                found_active.append(cid)
                # Upsert DB record
                await incense_db.register_incense(guild_id, cid, inc_type, spawns)
                if _is_channel_locked(ch, opdex_id):
                    await incense_db.set_paused(guild_id, cid, True)
                    already_locked.append(cid)
                else:
                    # Lock it — it was active but wasn't locked (the cracked-incense bug)
                    success = await _lock_channel(ch, opdex_id)
                    if success:
                        await incense_db.set_paused(guild_id, cid, True)
                        now_locked.append(cid)
                        await incense_db.log_action(
                            guild_id, bot_uid, "resync_lock",
                            f"Resync: locked #{ch.name} ({inc_type}, {spawns} spawns remaining)",
                        )
            else:
                # No active incense found in history
                if has_db_record:
                    # DB thought it was active — stale, clear it
                    await incense_db.clear_incense(guild_id, cid)
                    cleared_stale.append(cid)
                # Also unlock if it was left locked with no incense
                if _is_channel_locked(ch, opdex_id):
                    await _unlock_channel(ch, opdex_id)

        await incense_db.log_action(
            guild_id, bot_uid, "resync",
            (
                f"Resync complete — found {len(found_active)} active, "
                f"locked {len(now_locked)}, already_locked {len(already_locked)}, "
                f"cleared {len(cleared_stale)}, stale_channels {len(stale)}, "
                f"cleaned_msgs {cleaned_msgs}"
            ),
        )

        colour = 0x57F287 if (found_active or cleared_stale) else 0x5865F2
        embed = discord.Embed(title="🔄 Incense Resync Complete", colour=colour)

        parts = []
        if found_active:
            parts.append(f"🌿 **{len(found_active)}** active incense(s) found")
        if now_locked:
            parts.append(f"🔒 **{len(now_locked)}** newly locked (were missing locks)")
        if already_locked:
            parts.append(f"✅ **{len(already_locked)}** already locked (DB updated)")
        if cleared_stale:
            parts.append(f"🗑️ **{len(cleared_stale)}** stale DB record(s) cleared")
        if stale:
            parts.append(f"🧹 **{len(stale)}** deleted channel(s) removed")
        if cleaned_msgs:
            parts.append(f"💬 **{cleaned_msgs}** old bot message(s) deleted")
        if not parts:
            parts.append("Everything looks up-to-date — nothing to fix!")

        embed.description = "\n".join(f"• {p}" for p in parts)

        if now_locked:
            _add_two_col(embed, f"🔒 Newly Locked ({len(now_locked)})",
                         [f"<#{c}>" for c in now_locked])
        if cleared_stale:
            _add_two_col(embed, f"🗑️ Stale Records Cleared ({len(cleared_stale)})",
                         [f"<#{c}>" for c in cleared_stale])

        embed.set_footer(text=make_footer(guild_id, "Incense Manager  •  Resync"))
        await interaction.followup.send(embed=embed)

    # ── /incense group ───────────────────────────────────────────────────────

    group_group = app_commands.Group(
        name="group",
        description="Manage named subsets of incense channels.",
        parent=incense,
    )

    @group_group.command(name="create", description="Create a named incense group (max 3 per server).")
    @app_commands.describe(name="Group name (e.g. clan, main, grind)")
    async def group_create(self, interaction: discord.Interaction, name: str):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Incense Manager** role.", ephemeral=True
            )
        gid = str(interaction.guild_id)
        name = name.strip().lower()
        if not name or len(name) > 32:
            return await interaction.response.send_message(
                "❌ Group name must be 1–32 characters.", ephemeral=True
            )
        try:
            created = await incense_db.create_group(gid, name, str(interaction.user.id))
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        if not created:
            return await interaction.response.send_message(
                f"ℹ️ A group named **{name}** already exists.", ephemeral=True
            )
        await incense_db.log_action(gid, str(interaction.user.id), "group_create", f"Created group '{name}'")
        embed = discord.Embed(
            title="✅ Group Created",
            description=(
                f"Group **{name}** is ready.\n\n"
                f"Add channels with `/incense group add name:{name} channel:#channel`\n"
                f"Then use `!p {name}` / `!r {name}` to pause/resume just that group."
            ),
            colour=0x57F287,
        )
        embed.set_footer(text=make_footer(gid, "Incense Manager"))
        await interaction.response.send_message(embed=embed)

    @group_group.command(name="delete", description="Delete an incense group (channels stay registered).")
    @app_commands.describe(name="Group name to delete")
    async def group_delete(self, interaction: discord.Interaction, name: str):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Incense Manager** role.", ephemeral=True
            )
        gid = str(interaction.guild_id)
        deleted = await incense_db.delete_group(gid, name.strip().lower())
        if not deleted:
            return await interaction.response.send_message(
                f"⚠️ No group named **{name}** found.", ephemeral=True
            )
        await incense_db.log_action(gid, str(interaction.user.id), "group_delete", f"Deleted group '{name}'")
        await interaction.response.send_message(
            f"🗑️ Group **{name}** deleted. Channels remain registered."
        )

    @group_group.command(name="add", description="Add channels to an incense group.")
    @app_commands.describe(
        name="Group name",
        channel="A single channel to add",
        category="Add all channels in this Discord category",
        category2="Second Discord category (optional)",
        category3="Third Discord category (optional)",
        from_channel="Start of a channel range (inclusive)",
        to_channel="End of a channel range (inclusive)",
    )
    async def group_add(
        self,
        interaction: discord.Interaction,
        name: str,
        channel:   Optional[discord.TextChannel]     = None,
        category:  Optional[discord.CategoryChannel] = None,
        category2: Optional[discord.CategoryChannel] = None,
        category3: Optional[discord.CategoryChannel] = None,
        from_channel: Optional[discord.TextChannel]  = None,
        to_channel:   Optional[discord.TextChannel]  = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Incense Manager** role.", ephemeral=True
            )
        gid = str(interaction.guild_id)
        gname = name.strip().lower()
        if not await incense_db.group_exists(gid, gname):
            return await interaction.response.send_message(
                f"⚠️ No group named **{gname}**. Create it first with `/incense group create`.",
                ephemeral=True,
            )

        targets: list[discord.TextChannel] = []
        if channel:
            targets.append(channel)
        for cat in [category, category2, category3]:
            if cat:
                targets.extend(ch for ch in cat.channels if isinstance(ch, discord.TextChannel) and ch not in targets)
        if from_channel and to_channel:
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == from_channel.category],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "❌ Could not resolve range — ensure both channels are in the same category.", ephemeral=True
                )
            if si > ei:
                si, ei = ei, si
            for ch in pool[si:ei + 1]:
                if ch not in targets:
                    targets.append(ch)
        elif from_channel or to_channel:
            return await interaction.response.send_message(
                "⚠️ Provide **both** `from_channel` and `to_channel` for a range.", ephemeral=True
            )

        if not targets:
            return await interaction.response.send_message(
                "⚠️ No channels specified. Provide `channel`, `category`, or `from_channel`+`to_channel`.",
                ephemeral=True,
            )

        seen: set[int] = set()
        unique = [ch for ch in targets if not (ch.id in seen or seen.add(ch.id))]
        cids = [str(ch.id) for ch in unique]
        added, already = await incense_db.add_channels_to_group(gid, gname, cids)
        not_registered = [c for c in cids if c not in added and c not in already]

        embed = discord.Embed(title=f"👥 Group **{gname}** Updated", colour=0x57F287)
        if added:
            embed.add_field(name=f"✅ Added ({len(added)})", value="\n".join(f"<#{c}>" for c in added[:20]), inline=True)
        if already:
            embed.add_field(name=f"⏭️ Already in group ({len(already)})", value="\n".join(f"<#{c}>" for c in already[:20]), inline=True)
        if not_registered:
            embed.add_field(
                name=f"⚠️ Not an incense channel ({len(not_registered)})",
                value="\n".join(f"<#{c}>" for c in not_registered[:20]) + "\n*Register first with `/incense add`*",
                inline=False,
            )
        embed.set_footer(text=make_footer(gid, "Incense Manager"))
        await interaction.response.send_message(embed=embed)

    @group_group.command(name="remove", description="Remove channels from an incense group.")
    @app_commands.describe(
        name="Group name",
        channel="A single channel to remove",
        category="Remove all channels in this Discord category",
        category2="Second Discord category (optional)",
        category3="Third Discord category (optional)",
        from_channel="Start of a channel range (inclusive)",
        to_channel="End of a channel range (inclusive)",
    )
    async def group_remove(
        self,
        interaction: discord.Interaction,
        name: str,
        channel:   Optional[discord.TextChannel]     = None,
        category:  Optional[discord.CategoryChannel] = None,
        category2: Optional[discord.CategoryChannel] = None,
        category3: Optional[discord.CategoryChannel] = None,
        from_channel: Optional[discord.TextChannel]  = None,
        to_channel:   Optional[discord.TextChannel]  = None,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Incense Manager** role.", ephemeral=True
            )
        gid = str(interaction.guild_id)
        gname = name.strip().lower()

        targets: list[discord.TextChannel] = []
        if channel:
            targets.append(channel)
        for cat in [category, category2, category3]:
            if cat:
                targets.extend(ch for ch in cat.channels if isinstance(ch, discord.TextChannel) and ch not in targets)
        if from_channel and to_channel:
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == from_channel.category],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "❌ Could not resolve range — ensure both channels are in the same category.", ephemeral=True
                )
            if si > ei:
                si, ei = ei, si
            for ch in pool[si:ei + 1]:
                if ch not in targets:
                    targets.append(ch)
        elif from_channel or to_channel:
            return await interaction.response.send_message(
                "⚠️ Provide **both** `from_channel` and `to_channel` for a range.", ephemeral=True
            )

        if not targets:
            return await interaction.response.send_message(
                "⚠️ No channels specified. Provide `channel`, `category`, or `from_channel`+`to_channel`.",
                ephemeral=True,
            )

        seen: set[int] = set()
        unique = [ch for ch in targets if not (ch.id in seen or seen.add(ch.id))]
        cids = [str(ch.id) for ch in unique]
        removed = await incense_db.remove_channels_from_group(gid, gname, cids)
        not_in_group = [c for c in cids if c not in removed]

        embed = discord.Embed(title=f"👥 Group **{gname}** Updated", colour=0xFEE75C)
        if removed:
            embed.add_field(name=f"✅ Removed ({len(removed)})", value="\n".join(f"<#{c}>" for c in removed[:20]), inline=True)
        if not_in_group:
            embed.add_field(name=f"⏭️ Not in group ({len(not_in_group)})", value="\n".join(f"<#{c}>" for c in not_in_group[:20]), inline=True)
        embed.set_footer(text=make_footer(gid, "Incense Manager"))
        await interaction.response.send_message(embed=embed)

    @group_group.command(name="list", description="List all incense groups and their channels.")
    async def group_list(self, interaction: discord.Interaction):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        if not await _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Incense Manager** role.", ephemeral=True
            )
        gid = str(interaction.guild_id)
        groups = await incense_db.get_groups(gid)
        if not groups:
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title="👥 Incense Groups",
                    description=(
                        "No groups yet.\n\n"
                        "Create one with `/incense group create`, then add channels with `/incense group add`.\n"
                        "Use `!p <group>` / `!r <group>` to pause/resume just that group."
                    ),
                    colour=0x5865F2,
                ),
            )

        await interaction.response.defer(thinking=True)

        opdex_id = await _get_opdex_id(gid)
        # Build a lookup: channel_id → active_incense record
        all_actives = {r["channel_id"]: r for r in await incense_db.get_active_incenses(gid)}

        embed = discord.Embed(
            title="👥 Incense Groups",
            description=(
                f"**{len(groups)}/{incense_db.MAX_GROUPS_PER_GUILD}** groups defined.\n"
                f"▶️ live  •  ⏸️ paused  •  💤 idle\n\n"
                f"Use `!p <group>` / `!r <group>` to target a group."
            ),
            colour=0x5865F2,
        )

        for gname in groups:
            cids = await incense_db.get_group_channels(gid, gname)
            if not cids:
                embed.add_field(
                    name=f"**{gname.upper()}**  —  0 channels",
                    value="*No channels yet — use `/incense group add`*",
                    inline=False,
                )
                # Spacer so next group starts fresh
                embed.add_field(name="​", value="​", inline=False)
                continue

            cids = _sort_channel_ids(interaction.guild, cids)

            # Annotate each channel with its state icon
            live_n = paused_n = idle_n = 0
            annotated: list[str] = []
            for cid in cids:
                ch = interaction.guild.get_channel(int(cid))
                mention = f"<#{cid}>" if ch else f"`{cid}` *(gone)*"
                rec = all_actives.get(cid)
                if rec:
                    if rec["paused"] or (ch and _is_channel_locked(ch, opdex_id)):
                        annotated.append(f"⏸️ {mention}")
                        paused_n += 1
                    else:
                        inc_label = rec.get("incense_type", "")
                        annotated.append(f"▶️ {mention}" + (f" *({inc_label})*" if inc_label else ""))
                        live_n += 1
                else:
                    annotated.append(f"💤 {mention}")
                    idle_n += 1

            # Summary line for the group
            summary_parts = []
            if live_n:
                summary_parts.append(f"▶️ {live_n} live")
            if paused_n:
                summary_parts.append(f"⏸️ {paused_n} paused")
            if idle_n:
                summary_parts.append(f"💤 {idle_n} idle")
            summary = "  •  ".join(summary_parts) if summary_parts else "all idle"

            field_title = f"**{gname.upper()}**  —  {len(cids)} ch  ({summary})"

            # Split into two columns
            half = (len(annotated) + 1) // 2
            left  = annotated[:half]
            right = annotated[half:]

            cap = 15  # max per column
            if len(left) > cap:
                extra = len(left) - cap + (len(right) - cap if len(right) > cap else 0)
                left  = left[:cap]
                right = right[:cap]
                right.append(f"*+{extra} more…*")

            embed.add_field(name=field_title, value="\n".join(left), inline=True)
            embed.add_field(name="​", value="\n".join(right) if right else "​", inline=True)
            embed.add_field(name="​", value="​", inline=False)  # row break

        embed.set_footer(text=make_footer(gid, "Incense Manager  •  Groups"))
        await interaction.followup.send(embed=embed)

    # ── /incense log ─────────────────────────────────────────────────────────

    @incense.command(name="log", description="View the incense audit log for this server.")
    @app_commands.describe(
        user="Filter by user (optional)",
        channel="Filter by channel (optional)",
        limit="Number of entries to show (default: 15)",
    )
    async def inc_log(
        self,
        interaction: discord.Interaction,
        user:    Optional[discord.Member]      = None,
        channel: Optional[discord.TextChannel] = None,
        limit:   int = 15,
    ):
        if not _is_qt_guild(interaction):
            return await interaction.response.send_message(_NOT_QT_MSG, ephemeral=True)
        # Only admins and owner can view the full audit log
        if not interaction.user.guild_permissions.administrator and interaction.user.id != OWNER_ID:
            return await interaction.response.send_message(
                "🚫 Only server administrators can view the audit log.", ephemeral=True
            )

        await interaction.response.defer(thinking=True, ephemeral=True)

        guild_id = str(interaction.guild_id)
        limit    = max(1, min(limit, 50))

        user_id_filter    = str(user.id) if user else None
        channel_id_filter = str(channel.id) if channel else None

        entries = await incense_db.get_audit_log(
            guild_id, limit, user_id=user_id_filter, channel_id=channel_id_filter
        )

        # Build title
        parts = ["📋 Audit Log"]
        if user:
            parts.append(f"— {user.display_name}")
        if channel:
            parts.append(f"— #{channel.name}")
        title = " ".join(parts) if (user or channel) else "📋 Incense Audit Log"

        if not entries:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title=title,
                    description="No audit log entries found.",
                    colour=0x5865F2,
                ),
                ephemeral=True,
            )

        ACTION_EMOJI = {
            "mass_pause":          "⏸️",
            "mass_resume":         "▶️",
            "auto_lock":           "🔒",
            "lock":                "🔒",
            "unlock":              "🔓",
            "register":            "➕",
            "bulk_register":       "➕",
            "recursive_register":  "➕",
            "unregister":          "➖",
            "clear":               "🗑️",
            "setup_role":          "⚙️",
            "setup_bot":           "⚙️",
        }

        lines = []
        for e in entries:
            emoji  = ACTION_EMOJI.get(e["action"], "📝")
            ts     = e["created_at"][:16].replace("T", " ")  # trim seconds
            lines.append(f"`{ts}` {emoji} <@{e['user_id']}> **{e['action']}**\n> {e['details']}")

        # Chunk into embed-friendly size
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n*...truncated*"

        embed = discord.Embed(title=title, description=text, colour=0x5865F2)

        # If no user/channel filter, also show summary
        if not user and not channel:
            summary = await incense_db.get_user_action_summary(guild_id)
            if summary:
                summary_lines = [
                    f"<@{s['user_id']}> — **{s['total']}** actions (last: {s['last_action'][:10]})"
                    for s in summary[:10]
                ]
                embed.add_field(
                    name="👥 User Summary",
                    value="\n".join(summary_lines),
                    inline=False,
                )

        embed.set_footer(text=make_footer(guild_id, "Incense Manager  •  Admin only"))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(IncenseCog(bot))
