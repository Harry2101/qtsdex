"""
cogs/type_lookup.py  —  /type
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import type_ac
from utils.embeds import FOOTER, error_embed, type_colour, TYPE_EMOJI


def _fmt(names: list[str]) -> str:
    if not names:
        return "—"
    return "  ".join(f"{TYPE_EMOJI.get(n,'❓')} {n.title()}" for n in sorted(names))


class TypeLookupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="type", description="Show a type's offensive and defensive matchups.")
    @app_commands.describe(type_name="Pokémon type")
    @app_commands.autocomplete(type_name=type_ac)
    async def type_cmd(self, interaction: discord.Interaction, type_name: str):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_type(type_name.lower())
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{type_name}** is not a valid type."), ephemeral=True
            )

        await interaction.followup.send(embed=self._build(data))

    def _build(self, data: dict) -> discord.Embed:
        t    = data["name"]
        rel  = data.get("damage_relations", {})
        emoji = TYPE_EMOJI.get(t, "❓")

        to2   = [x["name"] for x in rel.get("double_damage_to", [])]
        toH   = [x["name"] for x in rel.get("half_damage_to", [])]
        to0   = [x["name"] for x in rel.get("no_damage_to", [])]
        frm2  = [x["name"] for x in rel.get("double_damage_from", [])]
        frmH  = [x["name"] for x in rel.get("half_damage_from", [])]
        frm0  = [x["name"] for x in rel.get("no_damage_from", [])]

        embed = discord.Embed(title=f"{emoji}  {t.title()} Type", colour=type_colour(t))

        embed.add_field(name="⚔️ Attacking",            value="_ _",        inline=False)
        embed.add_field(name="✅ Super Effective (2×)", value=_fmt(to2),     inline=True)
        embed.add_field(name="❌ Not Very Effective (½×)", value=_fmt(toH),  inline=True)
        embed.add_field(name="🚫 No Effect (0×)",       value=_fmt(to0),    inline=True)

        embed.add_field(name="🛡️ Defending",            value="_ _",        inline=False)
        embed.add_field(name="🔴 Weak To (×2)",         value=_fmt(frm2),   inline=True)
        embed.add_field(name="🟢 Resists (×½)",         value=_fmt(frmH),   inline=True)
        embed.add_field(name="⚫ Immune (×0)",          value=_fmt(frm0),   inline=True)

        embed.set_footer(text=FOOTER)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(TypeLookupCog(bot))
