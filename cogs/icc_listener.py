"""
cogs/icc_listener.py
Op Pokemon detection for ICC + scheduler startup.

Listens to the same Op Dex bot messages as cogs/incense.py but for a
different purpose: marking channels as bought in published ICC orgs.

Detection reuses the same bot ID source (DEFAULT_OPDEX env var +
guild_settings_db.get_opdex_bot_id) and the same embed title check
("incense activated") as incense.py.  The two listeners coexist — Discord
dispatches on_message to all registered listeners.

Key rule: only operates on orgs with status='published'.
"""

import logging
import os

import discord
from discord.ext import commands

from services import icc_db, guild_settings_db
from services.icc_progress_service import mark_channel_done
from services.icc_scheduler import start_scheduler, stop_scheduler

log = logging.getLogger("qtsdex.icc_listener")

DEFAULT_OPDEX = int(os.getenv("DEFAULT_OPDEX_BOT_ID", "1471263987340410978"))


async def _get_opdex_id(guild_id: str) -> int:
    """Same logic as incense.py — reads guild override or env default."""
    custom = await guild_settings_db.get_opdex_bot_id(guild_id)
    return custom or DEFAULT_OPDEX


def _is_incense_activation(message: discord.Message) -> bool:
    """
    Lightweight check matching incense.py's detection logic.
    Returns True if the message signals an incense activation.
    Does NOT parse type/spawns — ICC only cares about the channel.
    """
    for embed in message.embeds:
        title = (embed.title or "").lower()
        desc = (embed.description or "").lower()
        if "incense activated" in title or "incense activated" in desc:
            return True
    if "incense activated" in message.content.lower():
        return True
    return False


class ICCListener(commands.Cog):
    """Watches Op Dex bot for incense activations and feeds ICC progress."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        start_scheduler(self.bot)

    async def cog_unload(self):
        stop_scheduler()

    @commands.Cog.listener("on_message")
    async def _watch_opdex_for_icc(self, message: discord.Message):
        if not message.guild:
            return

        guild_id = str(message.guild.id)
        opdex_id = await _get_opdex_id(guild_id)

        if message.author.id != opdex_id:
            return

        if not _is_incense_activation(message):
            return

        channel_id = str(message.channel.id)

        # Only process if there's a published org
        org = await icc_db.get_active_org(guild_id)
        if not org:
            return

        # Only process if this channel is mapped to a category in this org
        oc = await icc_db.resolve_channel_to_org_category(guild_id, org["id"], channel_id)
        if not oc:
            return

        # Mark done — idempotent, safe to call repeatedly
        ok, msg, refreshed = await mark_channel_done(
            guild_id, channel_id, method="auto", completed_by="",
        )
        if ok:
            log.info(f"ICC auto-detect: {msg}")

            # Notify in announcement channel if category just completed
            if refreshed and refreshed["status"] == "complete":
                ann_channel_id = org.get("announcement_channel_id")
                if ann_channel_id:
                    ann_channel = message.guild.get_channel(int(ann_channel_id))
                    if ann_channel:
                        owner_mention = f"<@{refreshed['owner_id']}>" if refreshed.get("owner_id") else "Unowned"
                        try:
                            await ann_channel.send(
                                f"**{refreshed['name']}** is complete! "
                                f"({refreshed['channels_done']}/{refreshed['required_count']}) "
                                f"— {owner_mention}",
                                allowed_mentions=discord.AllowedMentions(users=True),
                            )
                        except discord.HTTPException:
                            pass

                # Check if entire org is now complete — notify organizer
                updated_org = await icc_db.get_org(org["id"])
                if updated_org and updated_org["status"] == "complete":
                    ann_channel_id = updated_org.get("announcement_channel_id")
                    if ann_channel_id:
                        ann_channel = message.guild.get_channel(int(ann_channel_id))
                        if ann_channel:
                            try:
                                await ann_channel.send(
                                    f"All categories complete! Org **{updated_org.get('label') or updated_org['id']}** "
                                    f"is done. <@{updated_org['organizer_id']}>",
                                    allowed_mentions=discord.AllowedMentions(users=True),
                                )
                            except discord.HTTPException:
                                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ICCListener(bot))
