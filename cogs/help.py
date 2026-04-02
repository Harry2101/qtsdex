"""
cogs/help.py  —  /help
Shows all available commands. Incense section shown to users
who have the configured Incense Manager role (or server admins).
"""

import os

import discord
from discord import app_commands
from discord.ext import commands

from services import guild_settings_db
from services.guild_settings_db import make_footer

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))


async def _can_see_incense(interaction: discord.Interaction) -> bool:
    """Check if the user should see incense commands in help."""
    if interaction.user.id == OWNER_ID:
        return True
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_id = await guild_settings_db.get_incense_role(str(interaction.guild_id))
    if role_id and any(r.id == role_id for r in getattr(interaction.user, "roles", [])):
        return True
    return False


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands.")
    async def help_cmd(self, interaction: discord.Interaction):
        show_incense = await _can_see_incense(interaction)

        embed = discord.Embed(
            title="📖  King's Dex — Command Reference",
            description=(
                "Your all-in-one Pokémon companion for PvP, dex lookups and clan operations.\n"
                "All slash commands support autocomplete — just start typing!"
            ),
            colour=0x5865F2,
        )

        # ── Pokédex ───────────────────────────────────────────────────────────
        embed.add_field(
            name="🔍 Pokédex",
            value=(
                "`/pokemon <name>`  — Stats, types, abilities, sprite\n"
                "   ↳ Buttons: ⚔️ Battle Card · 📋 Moves\n"
                "`/pokemon_moves <name>`  — Full paginated move list\n"
                "   ↳ Filter by type · 🔍 Move Details · 📢 Share\n"
                "`/move <name>`  — Power, accuracy, PP, type, effect\n"
                "`/ability <name>`  — Effect description + Pokémon that have it\n"
                "`/item <name>`  — Held item or battle item effect\n"
                "`/sprite <name>`  — Full-size sprite, normal or shiny"
            ),
            inline=False,
        )

        # ── Type tools ────────────────────────────────────────────────────────
        embed.add_field(
            name="🏷️ Type Tools",
            value=(
                "`/type <type>`  — Offensive & defensive matchup chart\n"
                "`/weakness <pokemon>`  — 4× / 2× / ½× / 0× breakdown by Pokémon\n"
                "`/weakness type1:<type> type2:<type>`  — Custom dual-type matchup\n"
                "`/typechart`  — Single matchup, full attacking row, or help"
            ),
            inline=False,
        )

        # ── Shiny hunt checklists ─────────────────────────────────────────────
        embed.add_field(
            name="✨ Shiny Hunt Checklists",
            value=(
                "`/checklist view`  — Your checklist with live catch/uncatch dropdowns\n"
                "   ↳ Buttons: sort A–Z · Dex # · Evo Group · Remaining · Switch list · 👥 Friends\n"
                "`/checklist add <pokemon>`  — Add Pokémon (bulk CSV, `--evo` chains supported)\n"
                "   ↳ Example: `/checklist add pokemon:charmander, bulbasaur`\n"
                "   ↳ Evo chains: `/checklist add pokemon:--evo charmander, --evo bulbasaur`\n"
                "   ↳ Toggle: `include_evolutions:True` to expand every mon to its full chain\n"
                "`/checklist catch <pokemon>`  — Mark as caught ✅ ✨\n"
                "`/checklist uncatch <pokemon>`  — Unmark a catch\n"
                "`/checklist remaining`  — Show only uncaught Pokémon\n"
                "`/checklist remove <pokemon>`  — Remove Pokémon (bulk CSV + `--evo` supported)\n"
                "`/event_setup <name>`  — Name the active event hunt (e.g. Community Day)"
            ),
            inline=False,
        )

        # ── Friends ──────────────────────────────────────────────────────────
        embed.add_field(
            name="👥 Checklist Friends",
            value=(
                "`/checklist friend add @user`  — Send a friend request\n"
                "`/checklist friend accept @user`  — Accept a request\n"
                "`/checklist friend decline @user`  — Decline a request\n"
                "`/checklist friend remove @user`  — Remove a friend\n"
                "`/checklist friend list`  — See your friends\n"
                "`/checklist friend requests`  — View pending requests\n"
                "   ↳ Or use the 👥 Friends button on your checklist!"
            ),
            inline=False,
        )

        # ── Channel Management ────────────────────────────────────────────────
        if interaction.user.guild_permissions.administrator or interaction.user.id == OWNER_ID:
            embed.add_field(
                name="🔧 Channel Management  *(admin only)*",
                value=(
                    "`/channel create count: prefix: start_number:`  — Bulk-create channels\n"
                    "   ↳ e.g. `/channel create count:20 prefix:♡- start_number:30`\n"
                    "   ↳ Optional: `category:` and `after:` for positioning\n"
                    "`/channel delete channel:`  — Delete a single channel\n"
                    "`/channel delete from_channel: to_channel:`  — Delete a range of channels\n"
                    "   ↳ Both modes require confirmation before deletion"
                ),
                inline=False,
            )

        # ── Incense Manager — shown to authorised users ──────────────────────
        if show_incense:
            embed.add_field(
                name="🌿 Incense Manager  *(Incense Manager role required)*",
                value=(
                    "**Setup (admin only):**\n"
                    "`/incense setup role <role>`  — Set the incense manager role\n"
                    "`/incense setup bot <id>`  — Set the Operation Dex bot ID\n"
                    "`/incense setup view`  — View current configuration\n\n"
                    "**Prefix commands:**\n"
                    "`!pause`  — Lock all active incense channels simultaneously\n"
                    "`!resume`  — Unlock all paused incense channels simultaneously\n"
                    "`!incset <id1> <id2> ...`  — Bulk register channels by ID\n\n"
                    "**Slash commands:**\n"
                    "`/incense add <channel>`  — Register up to 5 channels at once\n"
                    "`/incense remove <channel>`  — Unregister a channel\n"
                    "`/incense lock [channel]`  — Lock a specific channel\n"
                    "`/incense unlock [channel]`  — Unlock a specific channel\n"
                    "`/incense recursive`  — Register a range of consecutive channels\n"
                    "`/incense status`  — See all channels: live / paused / idle\n"
                    "`/incense clear [channel]`  — Clear incense record\n"
                    "`/incense log [user]`  — View audit log (admin only)\n\n"
                    "**Auto-behaviour:**\n"
                    "When Operation Dex activates an incense in a registered channel,\n"
                    "the channel is locked automatically and a notification is posted."
                ),
                inline=False,
            )

        # ── Changelog ─────────────────────────────────────────────────────────
        embed.add_field(
            name="📋 Changelog",
            value=(
                "`/changelog channel`  — See where changelogs are posted\n"
                "`/changelog setup #channel`  — Set the changelog channel *(admin)*\n"
                "`/changelog post version: changes:`  — Post a new changelog *(bot owner)*"
            ),
            inline=False,
        )

        # ── Tips ──────────────────────────────────────────────────────────────
        embed.add_field(
            name="💡 Tips",
            value=(
                "• `/pokemon` Battle Card → weaknesses, key stats, strongest moves at a glance\n"
                "• `/pokemon_moves` type filter cycles through move types in one click\n"
                "• `/sprite shiny:True` shows the shiny variant with a toggle button\n"
                "• Checklists are **global** — your progress syncs across all servers"
            ),
            inline=False,
        )

        embed.set_footer(text=make_footer(str(interaction.guild_id or "")))
        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
