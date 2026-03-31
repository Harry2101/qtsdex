"""
cogs/incense.py  —  Incense Management System
Handles Mass Incense operations for clan servers.

Prefix commands (organizer role or owner only):
  !pause              — pause all active incenses in the server
  !resume             — resume all paused incenses
  !incset ID1 ID2...  — bulk register incense channels

Slash commands:
  /inc_lock           — lock a specific channel (pause its incense)
  /inc_unlock         — unlock a specific channel (resume its incense)
  /inc_add            — add a single channel as an incense channel
  /inc_remove         — remove a channel from incense channel list
  /inc_set_recursive  — set every channel after this one as an inc channel
  /inc_status         — show all incense channels and their states
  /inc_clear          — clear all incense data for a channel

Auto-behaviour:
  When Operation Dex bot sends "Incense Activated!" in a registered channel,
  the channel is automatically locked and a notification is posted.

Locking = deny Send Messages permission for Operation Dex bot in that channel.
Unlocking = remove that deny override (restore default).
"""

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import incense_db

log = logging.getLogger("qtsdex.incense")

# ── Constants ─────────────────────────────────────────────────────────────────

OPDEX_BOT_ID   = 1471263987340410978   # Operation Dex bot
ORGANIZER_ROLE = 1483594894713946212   # Clan organizer role
OWNER_ID       = 145065060568530944    # Always has full access


# ── Permission helpers ────────────────────────────────────────────────────────

def _is_authorised(ctx_or_interaction) -> bool:
    """True if the user is the owner or has the organizer role."""
    if isinstance(ctx_or_interaction, commands.Context):
        user = ctx_or_interaction.author
        guild = ctx_or_interaction.guild
    else:
        user = ctx_or_interaction.user
        guild = ctx_or_interaction.guild

    if user.id == OWNER_ID:
        return True
    if guild is None:
        return False
    return any(r.id == ORGANIZER_ROLE for r in getattr(user, "roles", []))


async def _lock_channel(channel: discord.TextChannel) -> bool:
    """
    Deny Send Messages for Operation Dex bot.
    Returns True on success.
    """
    opdex = channel.guild.get_member(OPDEX_BOT_ID)
    target = opdex or discord.Object(id=OPDEX_BOT_ID)
    try:
        overwrite = channel.overwrites_for(target)
        overwrite.send_messages = False
        await channel.set_permissions(target, overwrite=overwrite, reason="Incense paused")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Failed to lock #{channel.name}: {e}")
        return False


async def _unlock_channel(channel: discord.TextChannel) -> bool:
    """
    Remove the Send Messages deny for Operation Dex bot.
    Returns True on success.
    """
    opdex = channel.guild.get_member(OPDEX_BOT_ID)
    target = opdex or discord.Object(id=OPDEX_BOT_ID)
    try:
        overwrite = channel.overwrites_for(target)
        overwrite.send_messages = None   # None = inherit / no override
        # If the overwrite is now neutral, remove it entirely
        if overwrite.is_empty():
            await channel.set_permissions(target, overwrite=None, reason="Incense resumed")
        else:
            await channel.set_permissions(target, overwrite=overwrite, reason="Incense resumed")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Failed to unlock #{channel.name}: {e}")
        return False


def _is_channel_locked(channel: discord.TextChannel) -> bool:
    """Check if Operation Dex bot has Send Messages denied."""
    target = discord.Object(id=OPDEX_BOT_ID)
    overwrite = channel.overwrites_for(target)
    return overwrite.send_messages is False


# ── Embed helpers ─────────────────────────────────────────────────────────────

def _pause_embed(
    locked:   list[str],
    already:  list[str],
    failed:   list[str],
    no_inc:   list[str],
) -> discord.Embed:
    total = len(locked) + len(already) + len(failed)
    colour = 0xED4245 if failed else (0xFEE75C if already else 0xFF6B35)

    embed = discord.Embed(
        title="⏸️  Mass Incense Paused",
        colour=colour,
    )
    embed.description = (
        f"**{len(locked)}** incense channel{'s' if len(locked) != 1 else ''} "
        f"locked and paused."
    )
    if locked:
        embed.add_field(
            name=f"🔒 Locked ({len(locked)})",
            value="\n".join(f"<#{c}>" for c in locked[:20])
                  + (f"\n*+{len(locked)-20} more*" if len(locked) > 20 else ""),
            inline=True,
        )
    if already:
        embed.add_field(
            name=f"⏭️ Already paused ({len(already)})",
            value="\n".join(f"<#{c}>" for c in already[:10]),
            inline=True,
        )
    if failed:
        embed.add_field(
            name=f"⚠️ Failed ({len(failed)})",
            value="\n".join(f"<#{c}>" for c in failed[:10]),
            inline=False,
        )
    embed.set_footer(text="QT's Dex  •  Incense Manager")
    return embed


def _resume_embed(
    unlocked: list[str],
    already:  list[str],
    failed:   list[str],
) -> discord.Embed:
    colour = 0xED4245 if failed else 0x57F287

    embed = discord.Embed(
        title="▶️  Mass Incense Resumed",
        colour=colour,
    )
    embed.description = (
        f"**{len(unlocked)}** incense channel{'s' if len(unlocked) != 1 else ''} "
        f"unlocked and live."
    )
    if unlocked:
        embed.add_field(
            name=f"🔓 Resumed ({len(unlocked)})",
            value="\n".join(f"<#{c}>" for c in unlocked[:20])
                  + (f"\n*+{len(unlocked)-20} more*" if len(unlocked) > 20 else ""),
            inline=True,
        )
    if already:
        embed.add_field(
            name=f"⏭️ Already active ({len(already)})",
            value="\n".join(f"<#{c}>" for c in already[:10]),
            inline=True,
        )
    if failed:
        embed.add_field(
            name=f"⚠️ Failed ({len(failed)})",
            value="\n".join(f"<#{c}>" for c in failed[:10]),
            inline=False,
        )
    embed.set_footer(text="QT's Dex  •  Incense Manager")
    return embed


def _auto_lock_embed(
    channel:      discord.TextChannel,
    incense_type: str,
    total_spawns: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="🔒  Incense Auto-Paused",
        description=(
            f"A **{incense_type} Incense** was detected in {channel.mention}.\n\n"
            f"The channel has been **automatically locked** — "
            f"Pokémons can't spawn here until the incense is resumed.\n\n"
            f"*Waiting for the organizer to resume when all channels are ready.*"
        ),
        colour=0xFF6B35,
    )
    if total_spawns:
        embed.add_field(name="📊 Total Spawns", value=str(total_spawns), inline=True)
    embed.add_field(name="📍 Channel", value=channel.mention, inline=True)
    embed.set_footer(text="QT's Dex  •  Use !resume to start all incenses simultaneously")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class IncenseCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Auto-lock on incense activation ──────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _watch_opdex(self, message: discord.Message):
        """
        Watch for Operation Dex bot's "Incense Activated!" message.
        When detected in a registered incense channel, auto-lock it.
        """
        if message.author.id != OPDEX_BOT_ID:
            return
        if not message.guild:
            return

        # Check for incense activation — look in embed titles/descriptions
        activated = False
        incense_type = "Standard"
        total_spawns = 0

        for embed in message.embeds:
            title = (embed.title or "").lower()
            desc  = (embed.description or "")
            if "incense activated" in title or "incense activated" in desc.lower():
                activated = True
                # Extract incense type from description
                type_match = re.search(r"(\w+)\s+incense\s+is\s+now\s+burning", desc, re.IGNORECASE)
                if type_match:
                    incense_type = type_match.group(1).title()
                # Extract total spawns
                spawns_match = re.search(r"(\d+)\s+(?:total\s+)?spawns?", desc, re.IGNORECASE)
                if spawns_match:
                    total_spawns = int(spawns_match.group(1))
                # Also check fields
                for field in embed.fields:
                    if "spawn" in field.name.lower():
                        try:
                            total_spawns = int(re.search(r"\d+", field.value).group())
                        except Exception:
                            pass
                break

        # Also check plain text content
        if not activated:
            content = message.content.lower()
            if "incense activated" in content:
                activated = True

        if not activated:
            return

        guild_id   = str(message.guild.id)
        channel_id = str(message.channel.id)

        # Only act on registered incense channels
        if not await incense_db.is_incense_channel(guild_id, channel_id):
            log.debug(f"Incense in unregistered channel {channel_id}, ignoring")
            return

        log.info(f"🌿 Incense activated in #{message.channel.name} — auto-locking")

        # Record in DB
        await incense_db.register_incense(guild_id, channel_id, incense_type, total_spawns)
        await incense_db.set_paused(guild_id, channel_id, True)

        # Lock the channel
        success = await _lock_channel(message.channel)

        if success:
            await message.channel.send(embed=_auto_lock_embed(
                message.channel, incense_type, total_spawns
            ))
        else:
            await message.channel.send(
                "⚠️ Incense detected but I couldn't lock this channel automatically — "
                "please check my permissions."
            )

    # ── !pause ────────────────────────────────────────────────────────────────

    @commands.command(name="pause")
    async def pause_cmd(self, ctx: commands.Context):
        """Pause all active incenses in the server."""
        if not _is_authorised(ctx):
            return await ctx.send(
                "🚫 You don't have permission to do that. "
                "This command requires the **Organizer** role."
            )

        guild_id = str(ctx.guild.id)
        actives  = await incense_db.get_active_incenses(guild_id)

        if not actives:
            return await ctx.send(
                "ℹ️ No active incenses found in this server. "
                "Channels will be locked automatically when incenses are activated."
            )

        locked  = []
        already = []
        failed  = []

        async def process(record: dict):
            ch = ctx.guild.get_channel(int(record["channel_id"]))
            if not ch:
                failed.append(record["channel_id"])
                return
            if record["paused"] or _is_channel_locked(ch):
                already.append(record["channel_id"])
                await incense_db.set_paused(guild_id, record["channel_id"], True)
                return
            success = await _lock_channel(ch)
            if success:
                await incense_db.set_paused(guild_id, record["channel_id"], True)
                locked.append(record["channel_id"])
            else:
                failed.append(record["channel_id"])

        await asyncio.gather(*[process(r) for r in actives])
        await ctx.send(embed=_pause_embed(locked, already, failed, []))

    # ── !resume ───────────────────────────────────────────────────────────────

    @commands.command(name="resume")
    async def resume_cmd(self, ctx: commands.Context):
        """Resume all paused incenses in the server."""
        if not _is_authorised(ctx):
            return await ctx.send(
                "🚫 You don't have permission to do that. "
                "This command requires the **Organizer** role."
            )

        guild_id = str(ctx.guild.id)
        actives  = await incense_db.get_active_incenses(guild_id)

        if not actives:
            return await ctx.send(
                "ℹ️ No active incenses found. "
                "Incenses are registered automatically when they are activated."
            )

        unlocked = []
        already  = []
        failed   = []

        async def process(record: dict):
            ch = ctx.guild.get_channel(int(record["channel_id"]))
            if not ch:
                failed.append(record["channel_id"])
                return
            if not record["paused"] and not _is_channel_locked(ch):
                already.append(record["channel_id"])
                return
            success = await _unlock_channel(ch)
            if success:
                await incense_db.set_paused(guild_id, record["channel_id"], False)
                unlocked.append(record["channel_id"])
                # Notify the channel
                try:
                    await ch.send(
                        "▶️ **Incense Resumed!** Pokémon will start spawning again. "
                        "Good luck, trainers! 🎉"
                    )
                except discord.Forbidden:
                    pass
            else:
                failed.append(record["channel_id"])

        await asyncio.gather(*[process(r) for r in actives])
        await ctx.send(embed=_resume_embed(unlocked, already, failed))

    # ── !incset ───────────────────────────────────────────────────────────────

    @commands.command(name="incset")
    async def incset_cmd(self, ctx: commands.Context, *, args: str = ""):
        """
        Bulk register incense channels.
        Usage: !incset 123456789 987654321
               !incset 123456789, 987654321, 111111111
        """
        if not _is_authorised(ctx):
            return await ctx.send("🚫 You need the **Organizer** role to register incense channels.")

        # Parse IDs — accept space or comma separated
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
            if ok:
                added.append(ch)
            else:
                already.append(ch)

        # Notify each newly added channel
        async def notify(ch: discord.TextChannel):
            try:
                embed = discord.Embed(
                    title="🌿 Incense Channel Registered",
                    description=(
                        f"{ch.mention} has been registered as an **Incense Channel**.\n"
                        "The bot will automatically lock this channel when an incense is activated here."
                    ),
                    colour=0x57F287,
                )
                embed.set_footer(text="QT's Dex  •  Incense Manager")
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass

        await asyncio.gather(*[notify(ch) for ch in added])

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
        embed.set_footer(text=f"QT's Dex  •  Total registered: use /inc_status to see all")
        await ctx.send(embed=embed)

    # ── Slash: /inc_add ───────────────────────────────────────────────────────

    @app_commands.command(
        name="inc_add",
        description="Register a channel as an incense channel.",
    )
    @app_commands.describe(channel="The channel to register")
    async def inc_add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )
        guild_id = str(interaction.guild_id)
        ok       = await incense_db.add_channel(guild_id, str(channel.id), str(interaction.user.id))

        if ok:
            embed = discord.Embed(
                title="🌿 Channel Registered",
                description=f"{channel.mention} is now an incense channel.",
                colour=0x57F287,
            )
            embed.set_footer(text="QT's Dex  •  Incense Manager")
            await interaction.response.send_message(embed=embed)
            # Notify the channel
            try:
                notify = discord.Embed(
                    title="🌿 Incense Channel Registered",
                    description=(
                        f"{channel.mention} has been registered as an **Incense Channel**.\n"
                        "I'll automatically lock this channel when an incense is activated here."
                    ),
                    colour=0x57F287,
                )
                notify.set_footer(text="QT's Dex  •  Incense Manager")
                await channel.send(embed=notify)
            except discord.Forbidden:
                pass
        else:
            await interaction.response.send_message(
                f"ℹ️ {channel.mention} is already registered as an incense channel.",
                ephemeral=True,
            )

    # ── Slash: /inc_remove ────────────────────────────────────────────────────

    @app_commands.command(
        name="inc_remove",
        description="Remove a channel from the incense channel list.",
    )
    @app_commands.describe(channel="The channel to remove")
    async def inc_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )
        guild_id = str(interaction.guild_id)
        ok       = await incense_db.remove_channel(guild_id, str(channel.id))
        if ok:
            await interaction.response.send_message(
                f"✅ {channel.mention} has been removed from the incense channel list.",
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ {channel.mention} wasn't registered as an incense channel.",
                ephemeral=True,
            )

    # ── Slash: /inc_lock ──────────────────────────────────────────────────────

    @app_commands.command(
        name="inc_lock",
        description="Lock a specific incense channel (pause its incense).",
    )
    @app_commands.describe(channel="Channel to lock (defaults to current channel)")
    async def inc_lock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )

        ch       = channel or interaction.channel
        guild_id = str(interaction.guild_id)

        if not await incense_db.is_incense_channel(guild_id, str(ch.id)):
            return await interaction.response.send_message(
                f"⚠️ {ch.mention} is not a registered incense channel.",
                ephemeral=True,
            )

        if _is_channel_locked(ch):
            return await interaction.response.send_message(
                f"ℹ️ {ch.mention} is already locked.", ephemeral=True
            )

        await interaction.response.defer()
        success = await _lock_channel(ch)
        await incense_db.set_paused(guild_id, str(ch.id), True)

        if success:
            embed = discord.Embed(
                title="🔒 Channel Locked",
                description=f"{ch.mention} has been locked. Pokémons can't spawn here.",
                colour=0xFF6B35,
            )
            embed.set_footer(text="QT's Dex  •  Incense Manager")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                f"⚠️ Couldn't lock {ch.mention} — check my permissions.", ephemeral=True
            )

    # ── Slash: /inc_unlock ────────────────────────────────────────────────────

    @app_commands.command(
        name="inc_unlock",
        description="Unlock a specific incense channel (resume its incense).",
    )
    @app_commands.describe(channel="Channel to unlock (defaults to current channel)")
    async def inc_unlock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )

        ch       = channel or interaction.channel
        guild_id = str(interaction.guild_id)

        if not await incense_db.is_incense_channel(guild_id, str(ch.id)):
            return await interaction.response.send_message(
                f"⚠️ {ch.mention} is not a registered incense channel.",
                ephemeral=True,
            )

        if not _is_channel_locked(ch):
            return await interaction.response.send_message(
                f"ℹ️ {ch.mention} is already unlocked.", ephemeral=True
            )

        await interaction.response.defer()
        success = await _unlock_channel(ch)
        await incense_db.set_paused(guild_id, str(ch.id), False)

        if success:
            embed = discord.Embed(
                title="🔓 Channel Unlocked",
                description=f"{ch.mention} is now live. Pokémon will start spawning! 🎉",
                colour=0x57F287,
            )
            embed.set_footer(text="QT's Dex  •  Incense Manager")
            await interaction.followup.send(embed=embed)
            try:
                await ch.send("▶️ **Incense Resumed!** Pokémon will start spawning. Good luck! 🎉")
            except discord.Forbidden:
                pass
        else:
            await interaction.followup.send(
                f"⚠️ Couldn't unlock {ch.mention} — check my permissions.", ephemeral=True
            )

    # ── Slash: /inc_set_recursive ─────────────────────────────────────────────

    @app_commands.command(
        name="inc_set_recursive",
        description="Set every channel AFTER this one (by position) as an incense channel.",
    )
    @app_commands.describe(
        include_current="Also include the channel this command is run in (default: False)"
    )
    async def inc_set_recursive(
        self,
        interaction: discord.Interaction,
        include_current: bool = False,
    ):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )

        await interaction.response.defer(thinking=True)

        guild    = interaction.guild
        guild_id = str(guild.id)
        current  = interaction.channel

        # Get all text channels sorted by position
        text_channels = sorted(
            [c for c in guild.channels if isinstance(c, discord.TextChannel)],
            key=lambda c: (c.category.position if c.category else -1, c.position),
        )

        # Find the current channel's position in the sorted list
        try:
            current_idx = next(i for i, c in enumerate(text_channels) if c.id == current.id)
        except StopIteration:
            return await interaction.followup.send(
                "❌ Couldn't find this channel in the server's channel list.", ephemeral=True
            )

        start_idx = current_idx if include_current else current_idx + 1
        targets   = text_channels[start_idx:]

        if not targets:
            return await interaction.followup.send(
                "ℹ️ No channels found after the current one.", ephemeral=True
            )

        added   = []
        already = []

        async def register_and_notify(ch: discord.TextChannel):
            ok = await incense_db.add_channel(guild_id, str(ch.id), str(interaction.user.id))
            if ok:
                added.append(ch)
                try:
                    embed = discord.Embed(
                        title="🌿 Incense Channel Registered",
                        description=(
                            f"{ch.mention} has been registered as an **Incense Channel**.\n"
                            "I'll automatically lock this channel when an incense is activated here."
                        ),
                        colour=0x57F287,
                    )
                    embed.set_footer(text="QT's Dex  •  Incense Manager")
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass
            else:
                already.append(ch)

        # Process in batches of 10 to avoid rate-limit hammering
        for i in range(0, len(targets), 10):
            batch = targets[i:i + 10]
            await asyncio.gather(*[register_and_notify(ch) for ch in batch])
            if i + 10 < len(targets):
                await asyncio.sleep(1)  # Brief pause between batches

        embed = discord.Embed(
            title="🌿 Recursive Channel Registration Complete",
            colour=0x57F287 if added else 0xFEE75C,
        )
        embed.description = (
            f"Scanned **{len(targets)}** channel{'s' if len(targets) != 1 else ''} "
            f"after {current.mention}."
        )
        if added:
            count_label = f"Registered ({len(added)})"
            preview = ", ".join(ch.mention for ch in added[:10])
            if len(added) > 10:
                preview += f" *+{len(added)-10} more*"
            embed.add_field(name=f"✅ {count_label}", value=preview, inline=False)
        if already:
            embed.add_field(
                name=f"⏭️ Already registered ({len(already)})",
                value=", ".join(ch.mention for ch in already[:10]),
                inline=False,
            )
        embed.set_footer(text="QT's Dex  •  Incense Manager")
        await interaction.followup.send(embed=embed)

    # ── Slash: /inc_status ────────────────────────────────────────────────────

    @app_commands.command(
        name="inc_status",
        description="Show all incense channels and their current state.",
    )
    async def inc_status(self, interaction: discord.Interaction):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )

        await interaction.response.defer(thinking=True)

        guild_id   = str(interaction.guild_id)
        registered = await incense_db.get_channels(guild_id)
        actives    = {r["channel_id"]: r for r in await incense_db.get_active_incenses(guild_id)}

        if not registered:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title="📊 Incense Status",
                    description="No incense channels registered yet.\nUse `!incset` or `/inc_add` to register some.",
                    colour=0x5865F2,
                )
            )

        live    = []
        paused  = []
        idle    = []

        for cid in registered:
            ch = interaction.guild.get_channel(int(cid))
            ch_mention = ch.mention if ch else f"`{cid}`"
            if cid in actives:
                rec = actives[cid]
                label = f"{ch_mention} — {rec['incense_type']} ({rec['total_spawns']} spawns)"
                if rec["paused"] or (ch and _is_channel_locked(ch)):
                    paused.append(label)
                else:
                    live.append(label)
            else:
                idle.append(ch_mention)

        embed = discord.Embed(
            title="📊 Incense Channel Status",
            colour=0x5865F2,
        )
        embed.description = (
            f"**{len(registered)}** registered  •  "
            f"**{len(live)}** live  •  "
            f"**{len(paused)}** paused  •  "
            f"**{len(idle)}** idle"
        )

        if live:
            embed.add_field(
                name=f"▶️ Live ({len(live)})",
                value="\n".join(live[:15]) + (f"\n*+{len(live)-15} more*" if len(live) > 15 else ""),
                inline=False,
            )
        if paused:
            embed.add_field(
                name=f"⏸️ Paused ({len(paused)})",
                value="\n".join(paused[:15]) + (f"\n*+{len(paused)-15} more*" if len(paused) > 15 else ""),
                inline=False,
            )
        if idle:
            embed.add_field(
                name=f"💤 Idle / No active incense ({len(idle)})",
                value="\n".join(idle[:15]) + (f"\n*+{len(idle)-15} more*" if len(idle) > 15 else ""),
                inline=False,
            )

        embed.set_footer(text="QT's Dex  •  Incense Manager")
        await interaction.followup.send(embed=embed)

    # ── Slash: /inc_clear ─────────────────────────────────────────────────────

    @app_commands.command(
        name="inc_clear",
        description="Clear the incense record for a channel (after it has expired).",
    )
    @app_commands.describe(channel="Channel to clear (defaults to current channel)")
    async def inc_clear(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not _is_authorised(interaction):
            return await interaction.response.send_message(
                "🚫 You need the **Organizer** role.", ephemeral=True
            )

        ch       = channel or interaction.channel
        guild_id = str(interaction.guild_id)

        has = await incense_db.has_active_incense(guild_id, str(ch.id))
        if not has:
            return await interaction.response.send_message(
                f"ℹ️ No active incense record for {ch.mention}.", ephemeral=True
            )

        await incense_db.clear_incense(guild_id, str(ch.id))
        await interaction.response.send_message(
            f"🗑️ Incense record cleared for {ch.mention}. "
            f"The channel will be registered fresh on next activation."
        )


async def setup(bot: commands.Bot):
    await incense_db.init_db()
    await bot.add_cog(IncenseCog(bot))
