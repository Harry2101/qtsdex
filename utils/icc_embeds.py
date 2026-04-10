"""
utils/icc_embeds.py
Embed builders for the ICC interactive panels (draft + published).

These are additive — the original build_org_embed() in cogs/icc_admin.py
stays untouched for slash-command fallback use.
"""

import discord

from services import icc_db
from services.guild_settings_db import make_footer


# ── Shared helpers (same logic as cogs/icc_admin.py) ────────────────────────

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


# ── Draft Control Panel embed ───────────────────────────────────────────────

async def build_draft_embed(org: dict, guild_id: str) -> discord.Embed:
    """
    Embed for the draft control panel shown to the organizer.
    Shows categories, channel counts, reserve slots, and current reserves.
    """
    org_cats = await icc_db.get_org_categories(org["id"])
    categories = await icc_db.get_categories(guild_id)
    reserves = await icc_db.get_reserves_for_org(org["id"])

    label = org.get("label") or f"Org #{org['id']}"

    # Build a channel-count lookup from the category definitions
    ch_counts: dict[int, int] = {}
    for cat in categories:
        channels = await icc_db.get_category_channels(cat["id"])
        ch_counts[cat["id"]] = len(channels)

    lines = []
    for oc in org_cats:
        mapped = ch_counts.get(oc["category_id"], 0)
        req = oc["required_count"]
        reserve_slots = oc.get("reserve_slots", 0)
        reserve_note = f"  •  {reserve_slots} reserve slots" if reserve_slots else ""
        lines.append(
            f"**{oc['name']}** — {mapped}/{req} ch mapped{reserve_note}"
        )

    # Append reserve picks if any
    if reserves:
        lines.append("")
        lines.append("**Reserves picked:**")
        for r in reserves:
            locked = " 🔒" if r["locked"] else ""
            lines.append(f"• {r['pokemon_name']} — <@{r['owner_id']}>{locked}")

    embed = discord.Embed(
        title=f"Org — {label}  (Draft)",
        description="\n".join(lines) if lines else "No categories.",
        colour=0xFEE75C,
    )
    embed.add_field(name="Status", value="Draft", inline=True)
    embed.add_field(name="Organizer", value=f"<@{org['organizer_id']}>", inline=True)
    embed.add_field(name="Categories", value=str(len(org_cats)), inline=True)
    embed.set_footer(text=make_footer(guild_id, "Draft • Use the buttons below to manage"))
    return embed


# ── Published Org Panel embed ───────────────────────────────────────────────

async def build_published_embed(org: dict, guild_id: str) -> discord.Embed:
    """
    Public-facing embed for the published org panel.
    Shows categories with owners, progress bars, and a helper-text prompt
    to claim via the buttons below.
    """
    org_cats = await icc_db.get_org_categories(org["id"])

    label = org.get("label") or f"Org #{org['id']}"

    lines = []
    for oc in org_cats:
        emoji = _status_emoji(oc["status"])
        owner = f"<@{oc['owner_id']}>" if oc["owner_id"] else "Unclaimed"
        bar = _progress_bar(oc["channels_done"], oc["required_count"])
        done = oc["channels_done"]
        req = oc["required_count"]
        lines.append(
            f"{emoji} **{oc['name']}** — {owner}\n"
            f"  {bar} {done}/{req} ch"
        )

    description = "\n\n".join(lines) if lines else "No categories."
    description += "\n\n*Tap a category below to claim it.*"

    # Use green while published, blue if complete
    colour = 0x57F287 if org["status"] == "published" else 0x5865F2

    embed = discord.Embed(
        title=f"Org — {label}",
        description=description,
        colour=colour,
    )
    embed.add_field(name="Status", value=org["status"].title(), inline=True)
    embed.add_field(name="Organizer", value=f"<@{org['organizer_id']}>", inline=True)
    embed.set_footer(text=make_footer(guild_id, "Org"))
    return embed
