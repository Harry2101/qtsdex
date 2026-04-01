"""
cogs/help.py  —  /help
Shows all available commands. Incense section only shown in the QT clan server.
"""

import discord
from discord import app_commands
from discord.ext import commands

QT_GUILD_ID = 1477887017034584248   # QT clan server — incense commands shown here only

FOOTER = "QT's Dex  •  powered by PokéAPI"


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands.")
    async def help_cmd(self, interaction: discord.Interaction):
        is_qt_server = interaction.guild_id == QT_GUILD_ID

        embed = discord.Embed(
            title="📖  QT's Dex — Command Reference",
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
                "   ↳ Buttons: 🔢 Numbers · 📊 Bar · ⚔️ Battle Card · 📋 Moves\n"
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
                "   ↳ Buttons: sort A–Z · Dex # · Evo Group · Remaining · Switch list\n"
                "`/checklist add <pokemon>`  — Add Pokémon (bulk CSV, `--evo` chains supported)\n"
                "   ↳ Example: `/checklist add pokemon:charmander, bulbasaur`\n"
                "   ↳ Evo chains: `/checklist add pokemon:--evo charmander`\n"
                "   ↳ Toggle: `include_evolutions:True` to expand every mon to its full chain\n"
                "`/checklist catch <pokemon>`  — Mark as caught ✅ ✨\n"
                "`/checklist uncatch <pokemon>`  — Unmark a catch\n"
                "`/checklist remaining`  — Show only uncaught Pokémon\n"
                "`/checklist remove <pokemon>`  — Remove a Pokémon from the list\n"
                "`/event_setup <name>`  — Name the active event hunt (e.g. Community Day)"
            ),
            inline=False,
        )

        # ── Settings ──────────────────────────────────────────────────────────
        embed.add_field(
            name="⚙️ Settings",
            value=(
                "`/settings`  — Choose your stat display style\n"
                "   ↳ 🔢 Plain numbers · 📊 Bar chart\n"
                "   ↳ Your preference is saved and applies to all future `/pokemon` lookups"
            ),
            inline=False,
        )

        # ── Incense Manager — only shown in QT server ─────────────────────────
        if is_qt_server:
            embed.add_field(
                name="🌿 Incense Manager  *(Organizer role required)*",
                value=(
                    "**Prefix commands:**\n"
                    "`!pause`  — Lock all active incense channels simultaneously\n"
                    "`!resume`  — Unlock all paused incense channels simultaneously\n"
                    "`!incset <id1> <id2> ...`  — Bulk register channels as incense channels\n"
                    "   ↳ Accepts space or comma-separated IDs\n\n"
                    "**Slash commands:**\n"
                    "`/inc_add <channel>`  — Register a single incense channel\n"
                    "`/inc_remove <channel>`  — Unregister a channel\n"
                    "`/inc_lock [channel]`  — Lock a specific channel (defaults to current)\n"
                    "`/inc_unlock [channel]`  — Unlock a specific channel\n"
                    "`/inc_set_recursive`  — Register every channel after this one\n"
                    "   ↳ Optional: `end_channel` to stop at a specific channel\n"
                    "   ↳ Optional: `include_current:True` to include this channel too\n"
                    "`/inc_status`  — See all channels: live / paused / idle\n"
                    "`/inc_clear [channel]`  — Clear incense record after it expires\n\n"
                    "**Auto-behaviour:**\n"
                    "When Operation Dex activates an incense in a registered channel,\n"
                    "the channel is locked automatically and a notification is posted."
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

        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
