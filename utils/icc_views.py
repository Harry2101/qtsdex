"""
utils/icc_views.py
Interactive discord.ui views for ICC org panels (draft + published).

All business logic is delegated to the services layer — these views
only handle UI rendering, button state, and wiring clicks to service calls.

Persistent views (timeout=None, static custom_id) survive bot restarts
when re-attached via bot.add_view().
"""

import logging

import discord
from discord import Interaction

from services import icc_db, guild_settings_db
from services.icc_org_service import publish_org, cancel_org
from services.icc_claim_service import claim_category, unclaim_category
from utils.icc_checks import is_icc_organizer
from utils.icc_embeds import build_draft_embed, build_published_embed, sort_org_cats

log = logging.getLogger("qtsdex.icc_views")


# ── Fixed row assignments ───────────────────────────────────────────────────
# Row 0: Rares, GMax, Regionals
# Row 1: Eevos, Reserve 1, Reserve 2
# Row 2: Ping, Refresh, Cancel Org

_ROW_MAP = {
    "rares": 0, "gmax": 0, "regionals": 0,
    "eevos": 1, "reserve 1": 1, "reserve 2": 1,
}
_CONTROL_ROW = 2


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _is_org_pinged(guild_id: str, org_id: int) -> bool:
    """Check if the announce ping has already been sent for this org."""
    val = await guild_settings_db.get(guild_id, f"icc_org_pinged_{org_id}")
    return val == "1"


async def build_published_panel(org: dict, guild_id: str) -> tuple[discord.Embed, "PublishedOrgPanel"]:
    """Build the embed + view pair for a published org. Used everywhere."""
    org_cats = sort_org_cats(await icc_db.get_org_categories(org["id"]))
    embed = await build_published_embed(org, guild_id)
    pinged = await _is_org_pinged(guild_id, org["id"])
    view = PublishedOrgPanel(org, org_cats, guild_id, pinged=pinged)
    return embed, view


async def _refresh_published_panel(message: discord.Message, org: dict, guild_id: str):
    """Re-build the published embed + view and edit the message in-place."""
    embed, view = await build_published_panel(org, guild_id)
    try:
        await message.edit(embed=embed, view=view)
    except discord.HTTPException:
        log.warning("Failed to edit published panel message %s", message.id)


async def refresh_panel_by_org(bot: discord.Client, org: dict, guild_id: str):
    """
    Look up the announcement message for a published org and refresh it.
    Called externally (e.g. from icc_listener after auto-detection).
    """
    ann_ch_id = org.get("announcement_channel_id")
    ann_msg_id = org.get("announcement_message_id")
    if not ann_ch_id or not ann_msg_id:
        return

    channel = bot.get_channel(int(ann_ch_id))
    if not channel:
        return

    try:
        message = await channel.fetch_message(int(ann_msg_id))
    except discord.HTTPException:
        return

    await _refresh_published_panel(message, org, guild_id)


# ── Draft Control Panel ────────────────────────────────────────────────────

class DraftControlPanel(discord.ui.View):
    """
    Organizer-facing draft panel with Publish / Cancel / Refresh / Configure buttons.
    Shown ephemerally — no persistence needed.
    """

    def __init__(self, org: dict, guild_id: str):
        super().__init__(timeout=600)  # 10 min for ephemeral draft panel
        self.org = org
        self.guild_id = guild_id

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.green, row=0)
    async def publish_btn(self, interaction: Interaction, button: discord.ui.Button):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message(
                "You need organizer permissions.", ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        guild_id = self.guild_id

        draft = await icc_db.get_draft_org(guild_id)
        if not draft:
            return await interaction.followup.send("No draft org to publish.", ephemeral=True)

        placeholder = await interaction.channel.send("Publishing org…")

        ok, result_msg, org = await publish_org(
            guild_id, str(interaction.user.id),
            str(interaction.channel.id), str(placeholder.id),
        )
        if not ok:
            await placeholder.delete()
            return await interaction.followup.send(result_msg, ephemeral=True)

        embed, view = await build_published_panel(org, guild_id)
        await placeholder.edit(content=None, embed=embed, view=view)

        await interaction.followup.send("Org published! Claims are now open.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=0)
    async def cancel_btn(self, interaction: Interaction, button: discord.ui.Button):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message(
                "You need organizer permissions.", ephemeral=True,
            )
        ok, msg = await cancel_org(self.guild_id, str(interaction.user.id))
        if ok:
            self.stop()
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.grey, row=0)
    async def refresh_btn(self, interaction: Interaction, button: discord.ui.Button):
        org = await icc_db.get_draft_org(self.guild_id)
        if not org:
            return await interaction.response.send_message(
                "Draft org no longer exists.", ephemeral=True,
            )
        self.org = org
        embed = await build_draft_embed(org, self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Configure Reserves", style=discord.ButtonStyle.blurple, row=0)
    async def config_reserves_btn(self, interaction: Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Use `/org category edit <category> reserve_slots:<n>` to configure reserve slots per category.",
            ephemeral=True,
        )


# ── Published Org Panel ────────────────────────────────────────────────────

class CategoryClaimButton(discord.ui.Button):
    """
    A single category button.
    - Unclaimed → green, clicking claims it
    - Claimed by you → red, clicking unclaims (opt out)
    - Claimed by someone else or complete → grey + disabled
    """

    def __init__(
        self,
        category_name: str,
        status: str,
        owner_id: str | None,
        guild_id: str,
        org_id: int,
        *,
        row: int = 0,
    ):
        self.category_name = category_name
        self.guild_id = guild_id
        self.org_id = org_id
        self._owner_id = owner_id

        # Determine style and disabled state
        if status == "complete":
            style = discord.ButtonStyle.grey
            disabled = True
        elif owner_id:
            # Claimed — red for the owner (they can click to unclaim),
            # but we can't know who's viewing, so make it red + enabled.
            # The callback checks ownership.
            style = discord.ButtonStyle.red
            disabled = False
        else:
            # Unclaimed — green, claimable
            style = discord.ButtonStyle.green
            disabled = False

        super().__init__(
            label=category_name,
            style=style,
            disabled=disabled,
            custom_id=f"icc_claim:{guild_id}:{org_id}:{category_name}",
            row=row,
        )

    async def callback(self, interaction: Interaction):
        guild_id = self.guild_id
        user_id = str(interaction.user.id)

        if self._owner_id:
            # Button is claimed — only the owner can unclaim
            if self._owner_id != user_id:
                return await interaction.response.send_message(
                    f"**{self.category_name}** is already claimed. Only the owner can opt out.",
                    ephemeral=True,
                )
            # Owner clicked → unclaim
            ok, msg = await unclaim_category(guild_id, user_id, self.category_name)
        else:
            # Unclaimed → claim
            ok, msg = await claim_category(guild_id, user_id, self.category_name)

        if not ok:
            return await interaction.response.send_message(msg, ephemeral=True)

        # Immediate ephemeral feedback to the clicker
        await interaction.response.send_message(msg, ephemeral=True)

        # Refresh the panel in-place
        org = await icc_db.get_active_org(guild_id)
        if org and interaction.message:
            await _refresh_published_panel(interaction.message, org, guild_id)


class AnnouncePingButton(discord.ui.Button):
    """
    Pings the configured announce_ping_role for the org.
    Disappears after being pressed (panel is refreshed without it).
    """

    def __init__(self, guild_id: str, org_id: int, *, row: int = _CONTROL_ROW):
        super().__init__(
            label="\u200b",  # zero-width space — emoji only
            emoji="📢",
            style=discord.ButtonStyle.blurple,
            custom_id=f"icc_ping:{guild_id}:{org_id}",
            row=row,
        )
        self.guild_id = guild_id
        self.org_id = org_id

    async def callback(self, interaction: Interaction):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message(
                "You need organizer permissions.", ephemeral=True,
            )

        role_id_str = await guild_settings_db.get(self.guild_id, "icc_announce_ping_role")
        if not role_id_str:
            return await interaction.response.send_message(
                "No announce ping role configured. Use `/org setup announce_ping_role <role>`.",
                ephemeral=True,
            )

        # Send the ping as a normal message in the channel
        try:
            await interaction.channel.send(
                f"<@&{role_id_str}>",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException:
            return await interaction.response.send_message(
                "Failed to send ping.", ephemeral=True,
            )

        await interaction.response.send_message("Ping sent!", ephemeral=True)

        # Mark as pinged so button doesn't come back on refresh
        await guild_settings_db.set_val(
            self.guild_id, f"icc_org_pinged_{self.org_id}", "1",
        )

        # Refresh panel — ping button will be absent
        org = await icc_db.get_active_org(self.guild_id)
        if org and interaction.message:
            await _refresh_published_panel(interaction.message, org, self.guild_id)


class PublishedOrgPanel(discord.ui.View):
    """
    Public-facing published org panel with category claim buttons
    and Refresh / Cancel Org / Ping controls.

    Button layout:
      Row 0: Rares, GMax, Regionals
      Row 1: Eevos, Reserve 1, Reserve 2
      Row 2: 📢 Ping, Refresh, Cancel Org

    Uses persistent custom_id format so buttons survive bot restarts.
    """

    def __init__(
        self,
        org: dict,
        org_cats: list[dict],
        guild_id: str,
        *,
        pinged: bool = False,
    ):
        super().__init__(timeout=None)  # persistent
        self.org = org
        self.guild_id = guild_id

        # Add category buttons with fixed row assignments
        for oc in org_cats:
            row = _ROW_MAP.get(oc["name"].lower(), 1)
            self.add_item(
                CategoryClaimButton(
                    category_name=oc["name"],
                    status=oc["status"],
                    owner_id=oc.get("owner_id"),
                    guild_id=guild_id,
                    org_id=org["id"],
                    row=row,
                )
            )

        # Ping button — only show if not yet pinged
        if not pinged:
            self.add_item(AnnouncePingButton(guild_id, org["id"], row=_CONTROL_ROW))

        self.add_item(RefreshPublishedButton(guild_id, org["id"], row=_CONTROL_ROW))
        self.add_item(CancelOrgButton(guild_id, org["id"], row=_CONTROL_ROW))


class RefreshPublishedButton(discord.ui.Button):
    """Refresh the published panel embed."""

    def __init__(self, guild_id: str, org_id: int, *, row: int = _CONTROL_ROW):
        super().__init__(
            label="Refresh",
            style=discord.ButtonStyle.grey,
            custom_id=f"icc_refresh:{guild_id}:{org_id}",
            row=row,
        )
        self.guild_id = guild_id
        self.org_id = org_id

    async def callback(self, interaction: Interaction):
        org = await icc_db.get_active_org(self.guild_id)
        if not org:
            return await interaction.response.send_message(
                "No published org found.", ephemeral=True,
            )
        embed, view = await build_published_panel(org, self.guild_id)
        await interaction.response.edit_message(embed=embed, view=view)


class CancelOrgButton(discord.ui.Button):
    """Cancel the org (organizer/admin only)."""

    def __init__(self, guild_id: str, org_id: int, *, row: int = _CONTROL_ROW):
        super().__init__(
            label="Cancel Org",
            style=discord.ButtonStyle.red,
            custom_id=f"icc_cancel:{guild_id}:{org_id}",
            row=row,
        )
        self.guild_id = guild_id
        self.org_id = org_id

    async def callback(self, interaction: Interaction):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message(
                "You need organizer permissions.", ephemeral=True,
            )
        ok, msg = await cancel_org(self.guild_id, str(interaction.user.id))
        await interaction.response.send_message(msg, ephemeral=True)

        if ok and interaction.message:
            org = await icc_db.get_org(self.org_id)
            if org:
                embed = await build_published_embed(org, self.guild_id)
                try:
                    await interaction.message.edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass


# ── Persistent view re-attach on startup ────────────────────────────────────

async def reattach_persistent_views(bot: discord.Client):
    """
    Called once on bot startup (e.g. from cog_load).
    Finds any published org with an announcement_message_id and
    re-registers a PublishedOrgPanel view so buttons keep working.
    """
    import aiosqlite
    async with aiosqlite.connect(icc_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_orgs WHERE status='published' AND announcement_message_id != ''",
        ) as cur:
            orgs = [dict(r) for r in await cur.fetchall()]

    for org in orgs:
        guild_id = org["guild_id"]
        _, view = await build_published_panel(org, guild_id)
        bot.add_view(view, message_id=int(org["announcement_message_id"]))
        log.info(
            "Re-attached published panel for org %s (msg %s)",
            org["id"], org["announcement_message_id"],
        )
