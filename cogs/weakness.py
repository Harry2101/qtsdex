"""
cogs/weakness.py  —  /weakness
Skips empty buckets — cleaner output.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac
from utils.embeds import error_embed, type_colour, TYPE_EMOJI, make_footer
from utils.normalizer import normalize
from utils.type_chart import group_by_multiplier_with_ability


def _fmt(types: list[str]) -> str:
    return "  ".join(f"{TYPE_EMOJI.get(t, '❓')} {t.title()}" for t in types)


class WeaknessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="weakness",
        description="Type effectiveness chart for a Pokémon (includes ability effects).",
    )
    @app_commands.describe(
        pokemon="Pokémon name",
        ability="Override which ability to use (defaults to first listed).",
    )
    @app_commands.autocomplete(pokemon=pokemon_ac)
    async def weakness_cmd(
        self,
        interaction: discord.Interaction,
        pokemon: str,
        ability: Optional[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_pokemon(normalize(pokemon))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{pokemon}** wasn't found."),
                ephemeral=True,
            )

        types = [t["type"]["name"] for t in data["types"]]
        t1, t2 = types[0], types[1] if len(types) > 1 else None
        label = data["name"].replace("-", " ").title()
        sprite_url = (
            data.get("sprites", {}).get("other", {})
            .get("official-artwork", {}).get("front_default")
            or data.get("sprites", {}).get("front_default")
        )

        abilities = [a["ability"]["name"] for a in data.get("abilities", [])]
        chosen_ability = None
        if ability:
            norm = ability.lower().replace(" ", "-").replace("_", "-")
            if norm in abilities:
                chosen_ability = norm
            else:
                return await interaction.followup.send(
                    embed=error_embed(
                        "Invalid Ability",
                        f"**{label}** doesn't have ability `{ability}`.\nAvailable: "
                        + ", ".join(a.replace("-", " ").title() for a in abilities),
                    ),
                    ephemeral=True,
                )
        elif abilities:
            chosen_ability = abilities[0]

        buckets, ability_note = group_by_multiplier_with_ability(t1, t2, chosen_ability)

        type_str = f"{TYPE_EMOJI.get(t1, '')} {t1.title()}"
        if t2:
            type_str += f"  {TYPE_EMOJI.get(t2, '')} {t2.title()}"

        description = f"**Typing:** {type_str}"
        if chosen_ability:
            description += f"\n**Ability:** {chosen_ability.replace('-', ' ').title()}"
        if ability_note:
            description += f"\n> ⚠️  *{ability_note}*"
        description += "\n\u200b"

        embed = discord.Embed(
            title=f"🔍  {label} — Type Matchups",
            description=description,
            colour=type_colour(t1),
        )
        if sprite_url:
            embed.set_thumbnail(url=sprite_url)

        buckets_display = [
            ("🔴 4× Weakness",   buckets["4x"]),
            ("🟠 2× Weakness",   buckets["2x"]),
            ("🟢 ½× Resistance", buckets["0.5x"]),
            ("🟡 ¼× Resistance", buckets["0.25x"]),
            ("⚫ Immune",        buckets["0x"]),
        ]
        for label_str, types_list in buckets_display:
            if types_list:
                header = f"{label_str} ({len(types_list)})"
                embed.add_field(name=header, value=_fmt(types_list), inline=False)

        embed.set_footer(text=make_footer(str(interaction.guild_id or ""), "Gen 6+ rules  •  "))
        await interaction.followup.send(embed=embed)

    @weakness_cmd.autocomplete("ability")
    async def _ability_ac(self, interaction: discord.Interaction, current: str):
        pokemon = interaction.namespace.pokemon
        if not pokemon:
            return []
        data = await pokeapi.get_pokemon(normalize(pokemon))
        if not data:
            return []
        abilities = [a["ability"]["name"] for a in data.get("abilities", [])]
        cur = current.lower()
        return [
            app_commands.Choice(name=a.replace("-", " ").title(), value=a)
            for a in abilities if cur in a.lower()
        ][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(WeaknessCog(bot))
