"""
cogs/move_lookup.py  —  /move
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import move_ac
from utils.embeds import FOOTER, error_embed, type_badge, type_colour, DAMAGE_CLASS_EMOJI
from utils.normalizer import normalize


class MoveLookupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="move", description="Look up detailed info about a Pokémon move.")
    @app_commands.describe(move="Move name")
    @app_commands.autocomplete(move=move_ac)
    async def move_cmd(self, interaction: discord.Interaction, move: str):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_move(normalize(move))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{move}** wasn't found."), ephemeral=True
            )

        await interaction.followup.send(embed=self._build(data))

    def _build(self, data: dict) -> discord.Embed:
        name         = data["name"].replace("-", " ").title()
        move_type    = data["type"]["name"]
        dmg_class    = data["damage_class"]["name"]
        power        = data.get("power") or "—"
        accuracy     = data.get("accuracy") or "—"
        pp           = data.get("pp") or "—"
        priority     = data.get("priority", 0)
        eff_chance   = data.get("effect_chance")
        target       = data.get("target", {}).get("name", "unknown").replace("-", " ").title()

        effect_short = effect_long = flavor = ""
        for e in data.get("effect_entries", []):
            if e.get("language", {}).get("name") == "en":
                effect_short = e.get("short_effect", "")
                effect_long  = e.get("effect", "")
                break
        for f in reversed(data.get("flavor_text_entries", [])):
            if f.get("language", {}).get("name") == "en":
                flavor = f.get("flavor_text", "").replace("\n", " ").replace("\x0c", " ")
                break

        if eff_chance:
            effect_short = effect_short.replace("$effect_chance", str(eff_chance))
            effect_long  = effect_long.replace("$effect_chance", str(eff_chance))

        meta     = data.get("meta") or {}
        ailment  = (meta.get("ailment") or {}).get("name", "none")
        category = (meta.get("category") or {}).get("name", "")

        dc_emoji     = DAMAGE_CLASS_EMOJI.get(dmg_class, "")
        priority_str = f"+{priority}" if priority > 0 else str(priority)

        embed = discord.Embed(title=f"⚔️  {name}", colour=type_colour(move_type))

        embed.add_field(name="🏷️ Type",    value=type_badge(move_type),                   inline=True)
        embed.add_field(name="💥 Class",   value=f"{dc_emoji} {dmg_class.title()}",        inline=True)
        embed.add_field(name="🎯 Target",  value=target,                                   inline=True)

        embed.add_field(name="💪 Power",    value=str(power),                               inline=True)
        embed.add_field(name="🎯 Accuracy", value=f"{accuracy}%" if accuracy != "—" else "—", inline=True)
        embed.add_field(name="🔵 PP",       value=str(pp),                                  inline=True)

        embed.add_field(name="⚡ Priority",     value=priority_str,                         inline=True)
        embed.add_field(name="🎲 Effect Chance", value=f"{eff_chance}%" if eff_chance else "—", inline=True)
        embed.add_field(
            name="🤢 Ailment",
            value=ailment.replace("-", " ").title() if ailment and ailment != "none" else "None",
            inline=True,
        )

        if effect_short:
            embed.add_field(name="📖 Effect", value=effect_short[:500], inline=False)
        if flavor:
            embed.add_field(name="📜 Game Description", value=f"*{flavor[:300]}*", inline=False)

        footer = f"QT's Dex  •  Category: {category.title()}" if category else "QT's Dex • powered by PokéAPI"
        embed.set_footer(text=footer)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(MoveLookupCog(bot))
