"""
cogs/extract.py  —  !extract / /extract

Extracts Pokémon IDs from an Operation Dex bot message and returns them
as a space-separated list in a copyable code block.

Usage:
  !extract   — reply to an Operation Dex message
  /extract   — scans recent channel history for the latest Operation Dex message
"""

import logging
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

from services import guild_settings_db

log = logging.getLogger("qtsdex.extract")

# Fallback when guild hasn't configured an OpDex bot ID via /incense setup
_DEFAULT_OPDEX = int(os.getenv("DEFAULT_OPDEX_BOT_ID", "1471263987340410978"))

# Matches "ID: 826422" or "ID:826422" (case-insensitive, any whitespace)
_ID_RE = re.compile(r"\bID:\s*(\d+)", re.IGNORECASE)

# How many recent messages to scan for /extract
_HISTORY_LIMIT = 50


def _collect_text(msg: discord.Message) -> str:
    """Return all human-readable text from a message, including embed fields."""
    parts: list[str] = []
    if msg.content:
        parts.append(msg.content)
    for embed in msg.embeds:
        for chunk in (
            embed.title,
            embed.description,
            embed.footer.text if embed.footer else None,
        ):
            if chunk:
                parts.append(chunk)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
    return "\n".join(parts)


def _extract_ids(msg: discord.Message) -> list[str]:
    return _ID_RE.findall(_collect_text(msg))


async def _get_opdex_id(guild_id: int | None) -> int:
    """Resolve the Operation Dex bot ID for this guild (cached via guild_settings_db)."""
    if guild_id is None:
        return _DEFAULT_OPDEX
    configured = await guild_settings_db.get_opdex_bot_id(str(guild_id))
    return configured if configured is not None else _DEFAULT_OPDEX


class ExtractCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix command: !extract ──────────────────────────────────────────────

    @commands.command(name="extract")
    async def extract_prefix(self, ctx: commands.Context):
        """Reply to an Operation Dex message to extract all Pokémon IDs."""
        if not ctx.message.reference:
            return await ctx.reply(
                "❌ Please **reply** to an Operation Dex message when using `!extract`.",
                delete_after=10,
                mention_author=False,
            )

        # Resolve the referenced message (prefer cached, fallback to fetch)
        ref = ctx.message.reference
        try:
            target = ref.cached_message or await ctx.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.HTTPException):
            return await ctx.reply(
                "❌ Could not fetch the replied-to message.",
                delete_after=10,
                mention_author=False,
            )

        # Validate it's from Operation Dex
        opdex_id = await _get_opdex_id(ctx.guild.id if ctx.guild else None)
        if target.author.id != opdex_id:
            return await ctx.reply(
                f"❌ That message isn't from Operation Dex (expected bot ID `{opdex_id}`).",
                delete_after=10,
                mention_author=False,
            )

        ids = _extract_ids(target)
        if not ids:
            return await ctx.reply(
                "❌ No Pokémon IDs found in that message.",
                delete_after=10,
                mention_author=False,
            )

        await ctx.reply(f"`{' '.join(ids)}`", mention_author=False)

    # ── Slash command: /extract ───────────────────────────────────────────────

    @app_commands.command(
        name="extract",
        description="Extract Pokémon IDs from the latest Operation Dex message in this channel.",
    )
    async def extract_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        opdex_id = await _get_opdex_id(interaction.guild_id)

        # Scan recent history for the latest OpDex message that contains IDs
        target: discord.Message | None = None
        try:
            async for msg in interaction.channel.history(limit=_HISTORY_LIMIT):
                if msg.author.id == opdex_id and _ID_RE.search(_collect_text(msg)):
                    target = msg
                    break
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("extract: could not read channel history: %s", e)
            return await interaction.followup.send(
                "❌ I don't have permission to read this channel's message history.",
            )

        if target is None:
            return await interaction.followup.send(
                f"❌ No recent Operation Dex message with Pokémon IDs found "
                f"in the last {_HISTORY_LIMIT} messages.",
            )

        ids = _extract_ids(target)
        if not ids:
            # Shouldn't happen since we checked _ID_RE above, but be safe
            return await interaction.followup.send(
                "❌ No Pokémon IDs could be extracted from that message.",
            )

        await interaction.followup.send(f"`{' '.join(ids)}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(ExtractCog(bot))
