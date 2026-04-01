"""
cogs/weakness.py  —  /weakness
Skips empty buckets — cleaner output.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac, type_ac
from utils.embeds import FOOTER, error_embed, type_colour, TYPE_EMOJI
from utils.normalizer import normalize
from utils.type_chart import group_by_multiplier


def _fmt(types: list[str]) -> str:
    return "  ".join(f"{TYPE_EMOJI.get(t, '❓')} {t.title()}" for t in types)


class WeaknessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="weakness",
        description="Type effectiveness chart for a Pokémon or custom typing.",
    )
    @app_commands.describe(
        pokemon="Pokémon name (fills typing automatically)",
        type1="First type (if no Pokémon given)",
        type2="Second type (optional)",
    )
    @app_commands.autocomplete(pokemon=pokemon_ac, type1=type_ac, type2=type_ac)
    async def weakness_cmd(
        self,
        interaction: discord.Interaction,
        pokemon: Optional[str] = None,
        type1: Optional[str] = None,
        type2: Optional[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        if not pokemon and not type1:
            return await interaction.followup.send(
                embed=error_embed("Missing Input", "Provide either **pokemon** or at least **type1**."),
                ephemeral=True,
            )

        sprite_url = None

        if pokemon:
            data = await pokeapi.get_pokemon(normalize(pokemon))
            if not data:
                return await interaction.followup.send(
                    embed=error_embed("Not Found", f"**{pokemon}** wasn't found."),
                    ephemeral=True,
                )
            types      = [t["type"]["name"] for t in data["types"]]
            t1, t2     = types[0], types[1] if len(types) > 1 else None
            label      = data["name"].replace("-", " ").title()
            sprite_url = (
                data.get("sprites", {}).get("other", {})
                .get("official-artwork", {}).get("front_default")
                or data.get("sprites", {}).get("front_default")
            )
        else:
            t1    = type1.lower()
            t2    = type2.lower() if type2 else None
            label = t1.title() + (f" / {t2.title()}" if t2 else "")

        buckets  = group_by_multiplier(t1, t2)
        type_str = f"{TYPE_EMOJI.get(t1, '')} {t1.title()}"
        if t2:
            type_str += f"  {TYPE_EMOJI.get(t2, '')} {t2.title()}"

        embed = discord.Embed(
            title=f"🔍  {label} — Type Matchups",
            description=f"**Typing:** {type_str}",
            colour=type_colour(t1),
        )
        if sprite_url:
            embed.set_thumbnail(url=sprite_url)

        # Only show non-empty buckets
        buckets_display = [
            ("🔴 4× Weakness",   buckets["4x"]),
            ("🟠 2× Weakness",   buckets["2x"]),
            ("🟢 ½× Resistance", buckets["0.5x"]),
            ("🟡 ¼× Resistance", buckets["0.25x"]),
            ("⚫ Immune",        buckets["0x"]),
        ]
        for label_str, types_list in buckets_display:
            if types_list:
                embed.add_field(name=label_str, value=_fmt(types_list), inline=False)

        embed.set_footer(text="King's Dex  •  Gen 6+ rules  •  powered by PokéAPI")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeaknessCog(bot))
