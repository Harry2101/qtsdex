"""
cogs/channels.py  —  /channel create · /channel delete
Admin-only bulk channel creation / deletion with confirmation flows.
"""

import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands

from services.guild_settings_db import make_footer

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))
MAX_CHANNELS = 50
DISCORD_CATEGORY_LIMIT = 50


async def _retry_api(coro_factory, retries: int = 3):
    """Retry a Discord API call with exponential backoff on 503 / connection errors."""
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except discord.HTTPException as e:
            if e.status == 503 and attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise


def _is_admin(interaction: discord.Interaction) -> bool:
    """Check if the user is a server admin or the bot owner."""
    if interaction.user.id == OWNER_ID:
        return True
    return interaction.user.guild_permissions.administrator


# ── Confirmation views ───────────────────────────────────────────────────────

class CreateConfirmView(discord.ui.View):
    """Confirm bulk channel creation."""

    def __init__(
        self,
        interaction: discord.Interaction,
        count:       int,
        prefix:      str,
        start:       int,
        category:    discord.CategoryChannel | None,
        after:       discord.abc.GuildChannel | None,
        guild_id:    str,
    ):
        super().__init__(timeout=60)
        self.author   = interaction.user
        self.count    = count
        self.prefix   = prefix
        self.start    = start
        self.category = category
        self.after    = after
        self.guild_id = guild_id

    def _names(self) -> list[str]:
        return [f"{self.prefix}{self.start + i}" for i in range(self.count)]

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)

        await interaction.response.defer()
        self.stop()

        names   = self._names()
        created: list[discord.TextChannel] = []
        failed:  list[str] = []

        position = self.after.position + 1 if self.after else None

        for i, name in enumerate(names):
            try:
                kwargs = {"name": name, "category": self.category}
                if position is not None:
                    kwargs["position"] = position + i
                ch = await _retry_api(
                    lambda kw=dict(kwargs): interaction.guild.create_text_channel(**kw)
                )
                created.append(ch)
            except Exception as e:
                failed.append(f"{name}: {e}")
            # Small delay to avoid rate limits on bulk operations
            if (i + 1) % 5 == 0 and i + 1 < len(names):
                await asyncio.sleep(1)

        embed = discord.Embed(
            title=f"{'✅' if created else '❌'}  Channels Created",
            colour=0x57F287 if created else 0xED4245,
        )
        if created:
            preview = ", ".join(ch.mention for ch in created[:15])
            if len(created) > 15:
                preview += f" *+{len(created) - 15} more*"
            embed.add_field(name=f"Created ({len(created)})", value=preview, inline=False)
        if failed:
            embed.add_field(
                name=f"Failed ({len(failed)})",
                value="\n".join(failed[:10]),
                inline=False,
            )
        embed.set_footer(text=make_footer(self.guild_id))
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
        self.stop()
        embed = discord.Embed(
            title="❌  Cancelled",
            description="No channels were created.",
            colour=0xED4245,
        )
        embed.set_footer(text=make_footer(self.guild_id))
        await interaction.response.edit_message(embed=embed, view=None)


class DeleteConfirmView(discord.ui.View):
    """Confirm bulk channel deletion."""

    def __init__(
        self,
        interaction:       discord.Interaction,
        channels:          list[discord.abc.GuildChannel],
        guild_id:          str,
        delete_categories: list[discord.CategoryChannel] | None = None,
    ):
        super().__init__(timeout=60)
        self.author            = interaction.user
        self.channels          = channels
        self.guild_id          = guild_id
        self.delete_categories = delete_categories or []

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)

        await interaction.response.defer()
        self.stop()

        deleted: list[str] = []
        failed:  list[str] = []

        all_items: list[discord.abc.GuildChannel] = list(self.channels) + list(self.delete_categories)

        for i, ch in enumerate(all_items):
            try:
                name = ch.name
                await _retry_api(
                    lambda c=ch: c.delete(reason=f"Bulk delete by {self.author}")
                )
                deleted.append(name)
            except Exception as e:
                failed.append(f"#{ch.name}: {e}")
            # Small delay to avoid rate limits on bulk operations
            if (i + 1) % 5 == 0 and i + 1 < len(all_items):
                await asyncio.sleep(1)

        embed = discord.Embed(
            title=f"{'🗑️' if deleted else '❌'}  Channels Deleted",
            colour=0x57F287 if deleted else 0xED4245,
        )
        if deleted:
            preview = ", ".join(f"`#{n}`" for n in deleted[:20])
            if len(deleted) > 20:
                preview += f" *+{len(deleted) - 20} more*"
            embed.add_field(name=f"Deleted ({len(deleted)})", value=preview, inline=False)
        if failed:
            embed.add_field(
                name=f"Failed ({len(failed)})",
                value="\n".join(failed[:10]),
                inline=False,
            )
        embed.set_footer(text=make_footer(self.guild_id))
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
        self.stop()
        embed = discord.Embed(
            title="❌  Cancelled",
            description="No channels were deleted.",
            colour=0xED4245,
        )
        embed.set_footer(text=make_footer(self.guild_id))
        await interaction.response.edit_message(embed=embed, view=None)


# ── Cog ──────────────────────────────────────────────────────────────────────

class ChannelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    channel = app_commands.Group(
        name="channel",
        description="Bulk channel creation and deletion (admin only).",
    )

    # ── /channel create ──────────────────────────────────────────────────────

    @channel.command(name="create", description="Bulk-create text channels with consecutive names.")
    @app_commands.describe(
        count="Number of channels to create (1-50)",
        prefix="Name prefix, e.g. '♡-' to get ♡-1, ♡-2, ...",
        start_number="Starting number (default 1)",
        category="Category to create channels in (optional)",
        after="Place new channels after this channel (optional)",
    )
    async def ch_create(
        self,
        interaction:  discord.Interaction,
        count:        app_commands.Range[int, 1, MAX_CHANNELS],
        prefix:       str,
        start_number: int = 1,
        category:     discord.CategoryChannel | None = None,
        after:        discord.TextChannel | None = None,
    ):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "🚫 Only server administrators can use this command.", ephemeral=True
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        gid   = str(interaction.guild_id or "")
        names = [f"{prefix}{start_number + i}" for i in range(count)]

        current_count = len(interaction.guild.channels)

        # Validate: if category is set, check the 50-channel limit
        if category:
            existing_in_cat = len(category.channels)
            available_slots = DISCORD_CATEGORY_LIMIT - existing_in_cat
            if available_slots <= 0:
                return await interaction.response.send_message(
                    f"❌ **{category.name}** already has {existing_in_cat} channels "
                    f"(Discord limit is {DISCORD_CATEGORY_LIMIT}). No more channels can be added.",
                    ephemeral=True,
                )
            if count > available_slots:
                return await interaction.response.send_message(
                    f"⚠️ **{category.name}** has {existing_in_cat}/{DISCORD_CATEGORY_LIMIT} channels. "
                    f"Only **{available_slots}** more can be added, but you requested **{count}**.\n\n"
                    f"Please reduce the count to **{available_slots}** or fewer.",
                    ephemeral=True,
                )

        # Validate: if after is set with a category, ensure after is in that category
        if category and after and after.category_id != category.id:
            return await interaction.response.send_message(
                f"⚠️ The `after` channel ({after.mention}) is not in the `{category.name}` category. "
                "Please pick a channel from the same category, or remove the `after` parameter.",
                ephemeral=True,
            )

        first_name = names[0]
        last_name  = names[-1]
        name_range = f"`{first_name}` through `{last_name}`" if count > 1 else f"`{first_name}`"

        desc_lines = [
            f"**Channels:** {count}",
            f"**Names:** {name_range}",
        ]
        if category:
            desc_lines.append(f"**Category:** {category.name} ({len(category.channels)}/{DISCORD_CATEGORY_LIMIT} used)")
        if after:
            desc_lines.append(f"**After:** {after.mention}")
        desc_lines.append(f"\nThis server currently has **{current_count}** channels.")

        embed = discord.Embed(
            title="⚠️  Confirm Channel Creation",
            description="\n".join(desc_lines),
            colour=0xFEE75C,
        )
        embed.set_footer(text=make_footer(gid))

        view = CreateConfirmView(
            interaction, count, prefix, start_number, category, after, gid
        )
        await interaction.response.send_message(embed=embed, view=view)

    # ── /channel delete ──────────────────────────────────────────────────────

    @channel.command(name="delete", description="Delete channels: single, range, and/or whole categories.")
    @app_commands.describe(
        channel="Single channel to delete",
        from_channel="Start of range to delete (inclusive)",
        to_channel="End of range to delete (inclusive)",
        category="Delete all channels in this category (also deletes the category itself)",
        category2="Additional category to delete",
        category3="Additional category to delete",
    )
    async def ch_delete(
        self,
        interaction:  discord.Interaction,
        channel:      discord.TextChannel | None = None,
        from_channel: discord.TextChannel | None = None,
        to_channel:   discord.TextChannel | None = None,
        category:     discord.CategoryChannel | None = None,
        category2:    discord.CategoryChannel | None = None,
        category3:    discord.CategoryChannel | None = None,
    ):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "🚫 Only server administrators can use this command.", ephemeral=True
            )
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        gid = str(interaction.guild_id or "")

        targets: list[discord.abc.GuildChannel] = []
        delete_categories: list[discord.CategoryChannel] = []

        # Collect category channels
        for cat in [category, category2, category3]:
            if cat is None:
                continue
            if cat in delete_categories:
                continue
            delete_categories.append(cat)
            for ch in sorted(cat.channels, key=lambda c: c.position):
                if ch not in targets:
                    targets.append(ch)

        # Single channel
        if channel:
            if channel not in targets:
                targets.append(channel)

        # Range mode
        if from_channel and to_channel:
            cat = from_channel.category
            pool = [
                ch for ch in interaction.guild.text_channels
                if ch.category == cat
            ]
            pool.sort(key=lambda c: c.position)

            try:
                start_idx = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                end_idx   = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "Could not find both range channels in the same category.", ephemeral=True
                )

            if start_idx > end_idx:
                start_idx, end_idx = end_idx, start_idx

            for ch in pool[start_idx : end_idx + 1]:
                if ch not in targets:
                    targets.append(ch)

        elif from_channel or to_channel:
            return await interaction.response.send_message(
                "Provide **both** `from_channel` and `to_channel` for a range.",
                ephemeral=True,
            )

        if not targets and not delete_categories:
            return await interaction.response.send_message(
                "Provide at least one of: `channel`, `from_channel`+`to_channel`, or a `category`.",
                ephemeral=True,
            )

        if not targets and delete_categories:
            # Categories with no channels — still valid, just deleting empty categories
            pass

        if len(targets) > MAX_CHANNELS:
            return await interaction.response.send_message(
                f"Cannot delete more than {MAX_CHANNELS} channels at once. "
                f"The selection has {len(targets)} channels.",
                ephemeral=True,
            )

        # Build preview
        desc_lines = []
        if delete_categories:
            cat_names = ", ".join(f"**{c.name}**" for c in delete_categories)
            desc_lines.append(f"**Categories:** {cat_names} *(categories will also be deleted)*")
        if targets:
            preview = ", ".join(ch.mention for ch in targets[:20])
            if len(targets) > 20:
                preview += f" *+{len(targets) - 20} more*"
            desc_lines.append(f"**Channels ({len(targets)}):** {preview}")
        desc_lines.append("\n*This cannot be undone. All messages in these channels will be lost.*")

        embed = discord.Embed(
            title="⚠️  Confirm Deletion",
            description="\n".join(desc_lines),
            colour=0xED4245,
        )
        embed.set_footer(text=make_footer(gid))

        view = DeleteConfirmView(interaction, targets, gid, delete_categories=delete_categories)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelsCog(bot))
