"""
cogs/icc_admin.py
ICC admin & org commands under the /icc group.

Subgroups:
  /icc setup   — role configuration
  /icc category — category CRUD + channel mapping
  /icc admin   — overrides, timers, audit

Top-level /icc commands:
  /icc start   — create a draft org
  /icc publish — go live (draft → published)
  /icc status  — org dashboard
  /icc cancel  — cancel org

All business logic lives in services/icc_*.py.
"""

import logging

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from services import icc_db, guild_settings_db
from services.guild_settings_db import make_footer
from services.icc_org_service import create_draft_org, publish_org, cancel_org
from services.icc_claim_service import (
    claim_category, unclaim_category, admin_assign, admin_drop,
)
from services.icc_progress_service import (
    admin_force_complete, admin_reset_category, mark_channel_done,
)
from services.icc_reserve_service import (
    pick_reserve, release_reserve, get_user_reserves,
)
from services import pokemon_list_db
from utils.icc_checks import is_icc_admin, is_icc_organizer

log = logging.getLogger("qtsdex.icc_admin")


# ── Shared helpers ───────────────────────────────────────────────────────────

def _progress_bar(done: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "░" * length
    filled = round((done / total) * length)
    return "█" * filled + "░" * (length - filled)


def _status_emoji(status: str) -> str:
    return {
        "unclaimed": "⬜",
        "claimed": "🟡",
        "in_progress": "🟠",
        "complete": "✅",
        "dropped": "❌",
    }.get(status, "❓")


async def build_org_embed(org: dict, guild_id: str) -> discord.Embed:
    """Build the org status / announcement embed.  Exported for use by other ICC cogs."""
    org_cats = await icc_db.get_org_categories(org["id"])

    label = org.get("label") or f"Org #{org['id']}"

    lines = []
    total_coins = 0
    for oc in org_cats:
        emoji = _status_emoji(oc["status"])
        owner = f"<@{oc['owner_id']}>" if oc["owner_id"] else "Unclaimed"
        bar = _progress_bar(oc["channels_done"], oc["required_count"])
        done = oc["channels_done"]
        req = oc["required_count"]
        lines.append(
            f"{emoji} **{oc['name']}** — {owner}\n"
            f"  {bar} {done}/{req} ch  •  {oc['coin_value']:,} coins"
        )
        total_coins += oc["coin_value"]

    colour = {
        "draft": 0xFEE75C,
        "published": 0x57F287,
        "complete": 0x5865F2,
        "cancelled": 0xED4245,
    }.get(org["status"], 0x5865F2)

    embed = discord.Embed(
        title=f"ICC — {label}",
        description="\n\n".join(lines) if lines else "No categories.",
        colour=colour,
    )
    embed.add_field(name="Status", value=org["status"].title(), inline=True)
    embed.add_field(name="Organizer", value=f"<@{org['organizer_id']}>", inline=True)
    embed.add_field(name="Total Coins", value=f"{total_coins:,}", inline=True)

    if org["status"] == "published":
        embed.set_footer(text=make_footer(guild_id, "Use /icc claim <category> to claim"))
    else:
        embed.set_footer(text=make_footer(guild_id, f"ICC • {org['status'].title()}"))
    return embed


# ── Autocomplete ─────────────────────────────────────────────────────────────

async def _category_autocomplete(
    interaction: Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    guild_id = str(interaction.guild_id)
    cats = await icc_db.get_categories(guild_id, include_inactive=True)
    return [
        app_commands.Choice(name=c["name"], value=c["name"])
        for c in cats if current.lower() in c["name"].lower()
    ][:25]


# ── Cog ──────────────────────────────────────────────────────────────────────

class ICCAdmin(commands.Cog):
    """ICC admin, setup, category, and org lifecycle commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Group tree ───────────────────────────────────────────────────────────

    icc = app_commands.Group(name="icc", description="Incense Control Center")
    setup_group = app_commands.Group(name="setup", description="ICC setup", parent=icc)
    cat_group = app_commands.Group(name="category", description="Manage categories", parent=icc)
    adm_group = app_commands.Group(name="admin", description="Admin overrides", parent=icc)
    reserve_group = app_commands.Group(name="reserve", description="Reserve Pokemon picks", parent=icc)

    # ━━ /icc start / publish / status / cancel ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @icc.command(name="start", description="Create a new draft org")
    @app_commands.describe(label="Optional label for this org")
    async def icc_start(self, interaction: Interaction, label: str = ""):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message("You need organizer permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        ok, msg, org_id = await create_draft_org(guild_id, str(interaction.user.id), label)
        if not ok:
            return await interaction.response.send_message(msg, ephemeral=True)
        org = await icc_db.get_org(org_id)
        embed = await build_org_embed(org, guild_id)
        await interaction.response.send_message(
            "Draft org created. Use `/icc publish` when ready to go live.",
            embed=embed, ephemeral=True,
        )

    @icc.command(name="publish", description="Publish draft org — go live")
    async def icc_publish(self, interaction: Interaction):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message("You need organizer permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        draft = await icc_db.get_draft_org(guild_id)
        if not draft:
            return await interaction.response.send_message("No draft org to publish.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        embed = await build_org_embed(draft, guild_id)
        embed.colour = 0x57F287
        announcement = await interaction.channel.send(embed=embed)

        ok, result_msg, org = await publish_org(
            guild_id, str(interaction.user.id),
            str(interaction.channel_id), str(announcement.id),
        )
        if not ok:
            await interaction.followup.send(result_msg, ephemeral=True)
            return
        await interaction.followup.send("Org published! Claims are now open.", ephemeral=True)

    @icc.command(name="status", description="Show org dashboard")
    @app_commands.describe(org_id="Org ID (defaults to active)")
    async def icc_status(self, interaction: Interaction, org_id: int | None = None):
        guild_id = str(interaction.guild_id)
        if org_id:
            org = await icc_db.get_org(org_id)
            if not org or org["guild_id"] != guild_id:
                return await interaction.response.send_message("Org not found.", ephemeral=True)
        else:
            org = await icc_db.get_active_org(guild_id)
            if not org:
                org = await icc_db.get_draft_org(guild_id)
            if not org:
                return await interaction.response.send_message("No active or draft org.", ephemeral=True)
        embed = await build_org_embed(org, guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @icc.command(name="cancel", description="Cancel the active or draft org")
    @app_commands.describe(reason="Optional reason")
    async def icc_cancel(self, interaction: Interaction, reason: str = ""):
        if not await is_icc_organizer(interaction):
            return await interaction.response.send_message("You need organizer permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        ok, msg = await cancel_org(guild_id, str(interaction.user.id), reason=reason)
        await interaction.response.send_message(msg, ephemeral=True)

    # ━━ /icc setup ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @setup_group.command(name="admin_role", description="Set the ICC admin role")
    @app_commands.describe(role="Role that can manage ICC")
    async def setup_admin_role(self, interaction: Interaction, role: discord.Role):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        await guild_settings_db.set_val(str(interaction.guild_id), "icc_admin_role", str(role.id))
        await interaction.response.send_message(f"ICC admin role set to {role.mention}.", ephemeral=True)

    @setup_group.command(name="organizer_role", description="Set the ICC organizer role")
    @app_commands.describe(role="Role that can create/publish orgs")
    async def setup_organizer_role(self, interaction: Interaction, role: discord.Role):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        await guild_settings_db.set_val(str(interaction.guild_id), "icc_organizer_role", str(role.id))
        await interaction.response.send_message(f"ICC organizer role set to {role.mention}.", ephemeral=True)

    @setup_group.command(name="helper_role", description="Set helper escalation role for a category")
    @app_commands.describe(category="Category name", role="Role to ping when unclaimed")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def setup_helper_role(self, interaction: Interaction, category: str, role: discord.Role):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        cat = await icc_db.get_category_by_name(str(interaction.guild_id), category)
        if not cat:
            return await interaction.response.send_message(f"Category **{category}** not found.", ephemeral=True)
        await icc_db.update_category(cat["id"], helper_role_id=str(role.id))
        await interaction.response.send_message(
            f"Helper role for **{cat['name']}** set to {role.mention}.", ephemeral=True,
        )

    # ━━ /icc category ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @cat_group.command(name="list", description="Show all configured categories")
    async def category_list(self, interaction: Interaction):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        cats = await icc_db.get_categories(guild_id, include_inactive=True)
        if not cats:
            return await interaction.response.send_message("No categories configured.", ephemeral=True)

        lines = []
        for c in cats:
            channels = await icc_db.get_category_channels(c["id"])
            active = "" if c["active"] else " **[inactive]**"
            helper = f" helper=<@&{c['helper_role_id']}>" if c["helper_role_id"] else ""
            reserves = f" / {c['reserve_slots']} reserves" if c.get("reserve_slots") else ""
            lines.append(
                f"**{c['name']}**{active} — {c['required_count']} ch / "
                f"{c['coin_value']:,} coins / {len(channels)} mapped{helper}{reserves}"
            )

        embed = discord.Embed(
            title="ICC Categories", description="\n".join(lines), colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, "ICC"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @cat_group.command(name="create", description="Create a new category")
    @app_commands.describe(
        name="Category name", required_count="Number of channels",
        coin_value="Coin value",
    )
    async def category_create(
        self, interaction: Interaction, name: str, required_count: int,
        coin_value: int = 0,
    ):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        cat_id = await icc_db.create_category(
            str(interaction.guild_id), name, required_count, coin_value,
        )
        if not cat_id:
            return await interaction.response.send_message(f"**{name}** already exists.", ephemeral=True)
        await interaction.response.send_message(
            f"**{name}** created (ID {cat_id}, {required_count} ch, {coin_value:,} coins).", ephemeral=True,
        )

    @cat_group.command(name="edit", description="Edit a category")
    @app_commands.describe(
        category="Category name", required_count="Channel count",
        coin_value="Coin value", reserve_slots="Number of reserve slots (0 to disable)",
    )
    @app_commands.autocomplete(category=_category_autocomplete)
    async def category_edit(
        self, interaction: Interaction, category: str,
        required_count: int | None = None, coin_value: int | None = None,
        reserve_slots: int | None = None,
    ):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        cat = await icc_db.get_category_by_name(str(interaction.guild_id), category)
        if not cat:
            return await interaction.response.send_message(f"**{category}** not found.", ephemeral=True)
        updated = await icc_db.update_category(
            cat["id"], required_count=required_count, coin_value=coin_value,
            reserve_slots=reserve_slots,
        )
        if not updated:
            return await interaction.response.send_message("No changes specified.", ephemeral=True)
        await interaction.response.send_message(f"**{cat['name']}** updated.", ephemeral=True)

    @cat_group.command(name="delete", description="Delete a category")
    @app_commands.describe(category="Category name")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def category_delete(self, interaction: Interaction, category: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        cat = await icc_db.get_category_by_name(str(interaction.guild_id), category)
        if not cat:
            return await interaction.response.send_message(f"**{category}** not found.", ephemeral=True)
        await icc_db.delete_category(cat["id"])
        await interaction.response.send_message(f"**{cat['name']}** deleted.", ephemeral=True)

    @cat_group.command(name="add_channels", description="Map channels to a category — single, whole Discord category, or from/to range")
    @app_commands.describe(
        category="ICC category name",
        channel="A single channel to add",
        discord_category="Add all text channels in this Discord category",
        discord_category2="Second Discord category (optional)",
        discord_category3="Third Discord category (optional)",
        from_channel="Start of a channel range (inclusive)",
        to_channel="End of a channel range (inclusive)",
    )
    @app_commands.autocomplete(category=_category_autocomplete)
    async def category_add_channels(
        self,
        interaction: Interaction,
        category: str,
        channel:           discord.TextChannel | None = None,
        discord_category:  discord.CategoryChannel | None = None,
        discord_category2: discord.CategoryChannel | None = None,
        discord_category3: discord.CategoryChannel | None = None,
        from_channel:      discord.TextChannel | None = None,
        to_channel:        discord.TextChannel | None = None,
    ):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        cat = await icc_db.get_category_by_name(guild_id, category)
        if not cat:
            return await interaction.response.send_message(f"**{category}** not found.", ephemeral=True)

        targets: list[discord.TextChannel] = []
        if channel:
            targets.append(channel)
        for dc in [discord_category, discord_category2, discord_category3]:
            if dc:
                targets.extend(ch for ch in dc.channels if isinstance(ch, discord.TextChannel) and ch not in targets)
        if from_channel and to_channel:
            dc = from_channel.category
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == dc],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "❌ Could not resolve range — ensure both channels are in the same Discord category.", ephemeral=True
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
                "⚠️ No channels specified. Provide `channel`, `discord_category`, or `from_channel`+`to_channel`.",
                ephemeral=True,
            )

        seen: set[int] = set()
        unique = [ch for ch in targets if not (ch.id in seen or seen.add(ch.id))]
        raw = [str(ch.id) for ch in unique]
        added, already = await icc_db.add_category_channels(cat["id"], guild_id, raw)
        parts = []
        if added:
            parts.append(f"Added {len(added)}")
        if already:
            parts.append(f"{len(already)} already mapped")
        await interaction.response.send_message(f"**{cat['name']}**: {' / '.join(parts)}.", ephemeral=True)

    @cat_group.command(name="remove_channels", description="Unmap channels from a category — single, whole Discord category, or from/to range")
    @app_commands.describe(
        category="ICC category name",
        channel="A single channel to remove",
        discord_category="Remove all text channels in this Discord category",
        discord_category2="Second Discord category (optional)",
        discord_category3="Third Discord category (optional)",
        from_channel="Start of a channel range (inclusive)",
        to_channel="End of a channel range (inclusive)",
    )
    @app_commands.autocomplete(category=_category_autocomplete)
    async def category_remove_channels(
        self,
        interaction: Interaction,
        category: str,
        channel:           discord.TextChannel | None = None,
        discord_category:  discord.CategoryChannel | None = None,
        discord_category2: discord.CategoryChannel | None = None,
        discord_category3: discord.CategoryChannel | None = None,
        from_channel:      discord.TextChannel | None = None,
        to_channel:        discord.TextChannel | None = None,
    ):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        cat = await icc_db.get_category_by_name(guild_id, category)
        if not cat:
            return await interaction.response.send_message(f"**{category}** not found.", ephemeral=True)

        targets: list[discord.TextChannel] = []
        if channel:
            targets.append(channel)
        for dc in [discord_category, discord_category2, discord_category3]:
            if dc:
                targets.extend(ch for ch in dc.channels if isinstance(ch, discord.TextChannel) and ch not in targets)
        if from_channel and to_channel:
            dc = from_channel.category
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == dc],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                return await interaction.response.send_message(
                    "❌ Could not resolve range — ensure both channels are in the same Discord category.", ephemeral=True
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
                "⚠️ No channels specified. Provide `channel`, `discord_category`, or `from_channel`+`to_channel`.",
                ephemeral=True,
            )

        seen: set[int] = set()
        unique = [ch for ch in targets if not (ch.id in seen or seen.add(ch.id))]
        raw = [str(ch.id) for ch in unique]
        removed, not_found = await icc_db.remove_category_channels(cat["id"], guild_id, raw)
        parts = []
        if removed:
            parts.append(f"Removed {len(removed)}")
        if not_found:
            parts.append(f"{len(not_found)} not mapped")
        await interaction.response.send_message(f"**{cat['name']}**: {' / '.join(parts)}.", ephemeral=True)

    @cat_group.command(name="view", description="View a category's mapped channels")
    @app_commands.describe(category="Category name")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def category_view(self, interaction: Interaction, category: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        cat = await icc_db.get_category_by_name(guild_id, category)
        if not cat:
            return await interaction.response.send_message(f"**{category}** not found.", ephemeral=True)
        ch_ids = await icc_db.get_category_channels(cat["id"])
        helper = f"\nHelper role: <@&{cat['helper_role_id']}>" if cat["helper_role_id"] else ""
        active_str = "" if cat["active"] else "\n**[INACTIVE]**"
        mentions = " ".join(f"<#{c}>" for c in ch_ids) if ch_ids else "No channels mapped."
        embed = discord.Embed(
            title=f"ICC — {cat['name']}",
            description=(
                f"**Required:** {cat['required_count']} channels\n"
                f"**Coins:** {cat['coin_value']:,}{helper}{active_str}\n\n"
                f"**Channels ({len(ch_ids)}):**\n{mentions}"
            ),
            colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, "ICC"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ━━ /icc admin ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @adm_group.command(name="mark_complete", description="Force-complete a category")
    @app_commands.describe(category="Category name")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def admin_mark_complete(self, interaction: Interaction, category: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        ok, msg = await admin_force_complete(str(interaction.guild_id), str(interaction.user.id), category)
        await interaction.response.send_message(msg, ephemeral=True)

    @adm_group.command(name="reset_category", description="Reset a category to unclaimed")
    @app_commands.describe(category="Category name")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def admin_reset_cat(self, interaction: Interaction, category: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        ok, msg = await admin_reset_category(str(interaction.guild_id), str(interaction.user.id), category)
        await interaction.response.send_message(msg, ephemeral=True)

    @adm_group.command(name="timer_status", description="Show pending timers")
    async def admin_timer_status(self, interaction: Interaction):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        org = await icc_db.get_active_org(guild_id)
        if not org:
            return await interaction.response.send_message("No published org.", ephemeral=True)
        timers = await icc_db.get_pending_timers(org["id"])
        if not timers:
            return await interaction.response.send_message("No pending timers.", ephemeral=True)
        lines = []
        for t in timers:
            oc = await icc_db.get_org_category(t["org_category_id"]) if t["org_category_id"] else None
            cat_name = oc["name"] if oc else "?"
            lines.append(f"ID {t['id']} | `{t['timer_type']}` | **{cat_name}** | fires `{t['fire_at']}`")
        embed = discord.Embed(
            title="ICC Pending Timers", description="\n".join(lines[:30]), colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, "ICC"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @adm_group.command(name="cancel_timer", description="Cancel a timer by ID")
    @app_commands.describe(timer_id="Timer ID")
    async def admin_cancel_timer(self, interaction: Interaction, timer_id: int):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        ok = await icc_db.cancel_timer_by_id(timer_id)
        msg = f"Timer {timer_id} cancelled." if ok else f"Timer {timer_id} not found or already handled."
        await interaction.response.send_message(msg, ephemeral=True)

    @adm_group.command(name="audit", description="View ICC audit log")
    @app_commands.describe(user="Filter by user", limit="Entries (default 20)")
    async def admin_audit(
        self, interaction: Interaction,
        user: discord.Member | None = None, limit: int = 20,
    ):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        entries = await icc_db.get_audit_log(guild_id, limit=limit, user_id=str(user.id) if user else None)
        if not entries:
            return await interaction.response.send_message("No audit log entries.", ephemeral=True)
        lines = [
            f"`{e['created_at']}` <@{e['user_id']}> **{e['action']}** {e['details']}"
            for e in entries
        ]
        embed = discord.Embed(
            title="ICC Audit Log", description="\n".join(lines[:25]), colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, "ICC"))
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # ━━ /icc claim / unclaim / assign / drop ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @icc.command(name="claim", description="Claim an unclaimed category (FCFS)")
    @app_commands.describe(category="Category to claim")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def icc_claim(self, interaction: Interaction, category: str):
        guild_id = str(interaction.guild_id)
        ok, msg = await claim_category(guild_id, str(interaction.user.id), category)
        await interaction.response.send_message(msg, ephemeral=True)

    @icc.command(name="unclaim", description="Release your claim (only before any progress)")
    @app_commands.describe(category="Category to release")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def icc_unclaim(self, interaction: Interaction, category: str):
        guild_id = str(interaction.guild_id)
        ok, msg = await unclaim_category(guild_id, str(interaction.user.id), category)
        await interaction.response.send_message(msg, ephemeral=True)

    @icc.command(name="assign", description="Admin: forcibly assign a category to a user")
    @app_commands.describe(category="Category name", user="User to assign")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def icc_assign(self, interaction: Interaction, category: str, user: discord.Member):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        ok, msg = await admin_assign(guild_id, str(interaction.user.id), category, str(user.id))
        await interaction.response.send_message(msg, ephemeral=True)

    @icc.command(name="drop", description="Admin: force a category back to unclaimed")
    @app_commands.describe(category="Category name")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def icc_drop(self, interaction: Interaction, category: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need ICC admin permissions.", ephemeral=True)
        guild_id = str(interaction.guild_id)
        ok, msg = await admin_drop(guild_id, str(interaction.user.id), category)
        await interaction.response.send_message(msg, ephemeral=True)

    # ━━ /icc mark_bought / progress ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @icc.command(name="mark_bought", description="Manually mark a channel as bought")
    @app_commands.describe(channel="Channel to mark complete")
    async def icc_mark_bought(self, interaction: Interaction, channel: discord.TextChannel):
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)

        # Verify user owns the category this channel belongs to (or is admin)
        org = await icc_db.get_active_org(guild_id)
        if not org:
            return await interaction.response.send_message("No published org is active.", ephemeral=True)

        oc = await icc_db.resolve_channel_to_org_category(guild_id, org["id"], str(channel.id))
        if not oc:
            return await interaction.response.send_message("Channel is not mapped to any category.", ephemeral=True)
        if oc["owner_id"] != user_id and not await is_icc_admin(interaction):
            return await interaction.response.send_message(
                f"You don't own **{oc['name']}**. Only the buyer or an admin can mark it.", ephemeral=True,
            )

        ok, msg, _ = await mark_channel_done(guild_id, str(channel.id), method="manual", completed_by=user_id)
        await interaction.response.send_message(msg, ephemeral=True)

    @icc.command(name="progress", description="Show progress for a category or all")
    @app_commands.describe(category="Category name (optional, shows all if omitted)")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def icc_progress(self, interaction: Interaction, category: str | None = None):
        guild_id = str(interaction.guild_id)
        org = await icc_db.get_active_org(guild_id)
        if not org:
            org = await icc_db.get_draft_org(guild_id)
        if not org:
            return await interaction.response.send_message("No active org.", ephemeral=True)

        org_cats = await icc_db.get_org_categories(org["id"])
        if category:
            org_cats = [oc for oc in org_cats if oc["name"].lower() == category.lower()]
            if not org_cats:
                return await interaction.response.send_message(f"**{category}** not found.", ephemeral=True)

        lines = []
        for oc in org_cats:
            emoji = _status_emoji(oc["status"])
            owner = f"<@{oc['owner_id']}>" if oc["owner_id"] else "Unclaimed"
            bar = _progress_bar(oc["channels_done"], oc["required_count"])
            line = f"{emoji} **{oc['name']}** — {owner}\n  {bar} {oc['channels_done']}/{oc['required_count']}"

            # Show missing channels if in progress
            if oc["status"] in ("claimed", "in_progress") and oc["owner_id"]:
                all_ch = await icc_db.get_category_channels(oc["category_id"])
                done_ch = await icc_db.get_completed_channels(oc["id"])
                missing = [c for c in all_ch if c not in done_ch]
                if missing:
                    missing_str = " ".join(f"<#{c}>" for c in missing[:15])
                    if len(missing) > 15:
                        missing_str += f" +{len(missing) - 15} more"
                    line += f"\n  Missing: {missing_str}"
            lines.append(line)

        embed = discord.Embed(
            title="ICC Progress", description="\n\n".join(lines), colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, "ICC"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ━━ /icc reserve pick / release / list ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @reserve_group.command(name="pick", description="Reserve a Pokemon (FCFS across the org)")
    @app_commands.describe(pokemon="Pokemon name to reserve")
    async def reserve_pick(self, interaction: Interaction, pokemon: str):
        guild_id = str(interaction.guild_id)
        ok, msg = await pick_reserve(guild_id, str(interaction.user.id), pokemon)
        await interaction.response.send_message(msg, ephemeral=True)

    @reserve_pick.autocomplete("pokemon")
    async def _reserve_pick_autocomplete(
        self, interaction: Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete from normal + event lists (excluding rare/gmax/eevo/regional)."""
        choices: list[app_commands.Choice[str]] = []
        # Normal Pokemon = all base names NOT in rare/gmax/eevo/regional
        all_bases = await pokemon_list_db.get_all_base_names()
        for name in all_bases:
            if current.lower() not in name.lower():
                continue
            cat = await pokemon_list_db.get_category_type_for_pokemon(name)
            if cat in pokemon_list_db.CATEGORY_TYPES:
                continue
            choices.append(app_commands.Choice(name=name, value=name))
            if len(choices) >= 20:
                break
        # Event Pokemon
        event_names = await pokemon_list_db.get_all_event_pokemon_names()
        for name in event_names:
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=f"{name} (event)", value=name))
                if len(choices) >= 25:
                    break
        return choices[:25]

    @reserve_group.command(name="release", description="Release a reserved Pokemon")
    @app_commands.describe(pokemon="Pokemon name to release")
    async def reserve_release(self, interaction: Interaction, pokemon: str):
        guild_id = str(interaction.guild_id)
        ok, msg = await release_reserve(guild_id, str(interaction.user.id), pokemon)
        await interaction.response.send_message(msg, ephemeral=True)

    @reserve_release.autocomplete("pokemon")
    async def _reserve_release_autocomplete(
        self, interaction: Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete from user's current reserves."""
        guild_id = str(interaction.guild_id)
        reserves = await get_user_reserves(guild_id, str(interaction.user.id))
        return [
            app_commands.Choice(name=r["pokemon_name"], value=r["pokemon_name"])
            for r in reserves if current.lower() in r["pokemon_name"].lower()
        ][:25]

    @reserve_group.command(name="list", description="Show your reserves (or all reserves)")
    @app_commands.describe(user="Show reserves for a specific user (admin only)")
    async def reserve_list(self, interaction: Interaction, user: discord.Member | None = None):
        guild_id = str(interaction.guild_id)
        org = await icc_db.get_active_org(guild_id)
        if not org:
            return await interaction.response.send_message("No published org is active.", ephemeral=True)

        if user and user.id != interaction.user.id:
            if not await is_icc_admin(interaction):
                return await interaction.response.send_message("Only admins can view other users' reserves.", ephemeral=True)
            reserves = await icc_db.get_reserves_for_owner(org["id"], str(user.id))
            label = f"<@{user.id}>'s"
        elif user:
            reserves = await icc_db.get_reserves_for_owner(org["id"], str(user.id))
            label = "Your"
        else:
            reserves = await icc_db.get_reserves_for_owner(org["id"], str(interaction.user.id))
            label = "Your"

        if not reserves:
            return await interaction.response.send_message(f"{label} reserves: none.", ephemeral=True)

        lines = []
        for r in reserves:
            status = ""
            if r["locked"]:
                status = " [LOCKED]"
            if r["released"]:
                status = " [RELEASED]"
            lines.append(f"\u2022 **{r['pokemon_name']}** (base: {r['base_name']}){status}")

        embed = discord.Embed(
            title=f"{label} Reserves",
            description="\n".join(lines),
            colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, f"{len(reserves)} reserve(s)"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @icc.command(name="history", description="Show past org summaries")
    @app_commands.describe(limit="Number of orgs to show (default 5)")
    async def icc_history(self, interaction: Interaction, limit: int = 5):
        guild_id = str(interaction.guild_id)
        # Fetch recent completed/cancelled orgs
        import aiosqlite
        async with aiosqlite.connect(icc_db.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM icc_orgs WHERE guild_id=? AND status IN ('complete','cancelled')
                   ORDER BY started_at DESC LIMIT ?""",
                (guild_id, limit),
            ) as cur:
                orgs = [dict(r) for r in await cur.fetchall()]

        if not orgs:
            return await interaction.response.send_message("No completed or cancelled orgs.", ephemeral=True)

        lines = []
        for o in orgs:
            status = o["status"].title()
            label = o.get("label") or f"#{o['id']}"
            started = o["started_at"] or "?"
            lines.append(f"**{label}** — {status} — started {started}")

        embed = discord.Embed(
            title="ICC History", description="\n".join(lines), colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, "ICC"))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ICCAdmin(bot))
