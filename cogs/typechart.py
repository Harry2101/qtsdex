"""
cogs/typechart.py  —  /typechart
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.autocomplete import type_ac
from utils.embeds import FOOTER, error_embed, type_colour, TYPE_EMOJI
from utils.type_chart import ALL_TYPES, effectiveness


def _mult_emoji(m: float) -> str:
    return {0.0: "⚫", 0.25: "🟡", 0.5: "🟢", 2.0: "🟠", 4.0: "🔴"}.get(m, "⬜")


class TypeChartCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="typechart", description="Type chart lookup — matchup, full row, or help.")
    @app_commands.describe(attack_type="Attacking type", defend_type="Defending type")
    @app_commands.autocomplete(attack_type=type_ac, defend_type=type_ac)
    async def typechart_cmd(
        self,
        interaction: discord.Interaction,
        attack_type: Optional[str] = None,
        defend_type: Optional[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        if attack_type and defend_type:
            embed = self._matchup(attack_type.lower(), defend_type.lower())
        elif attack_type:
            embed = self._attack_row(attack_type.lower())
        else:
            embed = self._help()

        await interaction.followup.send(embed=embed)

    def _matchup(self, atk: str, dfn: str) -> discord.Embed:
        if atk not in ALL_TYPES:
            return error_embed("Unknown Type", f"**{atk}** is not a valid type.")
        if dfn not in ALL_TYPES:
            return error_embed("Unknown Type", f"**{dfn}** is not a valid type.")

        m   = effectiveness(atk, dfn)
        ae  = TYPE_EMOJI.get(atk, "❓")
        de  = TYPE_EMOJI.get(dfn, "❓")
        me  = _mult_emoji(m)

        descs = {
            0.0: f"{atk.title()} has **no effect** on {dfn.title()}.",
            0.5: f"{atk.title()} is **not very effective** against {dfn.title()} (½×).",
            1.0: f"{atk.title()} deals **normal damage** to {dfn.title()} (1×).",
            2.0: f"{atk.title()} is **super effective** against {dfn.title()} (2×)!",
            4.0: f"{atk.title()} is **extremely effective** against {dfn.title()} (4×)!!",
        }
        desc = descs.get(m, f"Multiplier: **{m}×**")

        embed = discord.Embed(
            title=f"{me}  {ae} {atk.title()} → {de} {dfn.title()}",
            description=desc,
            colour=type_colour(atk),
        )
        embed.add_field(name="Multiplier", value=f"**{m}×**", inline=True)
        embed.set_footer(text=FOOTER)
        return embed

    def _attack_row(self, atk: str) -> discord.Embed:
        if atk not in ALL_TYPES:
            return error_embed("Unknown Type", f"**{atk}** is not a valid type.")

        buckets: dict[float, list[str]] = {2.0: [], 1.0: [], 0.5: [], 0.0: []}
        for dfn in ALL_TYPES:
            m = effectiveness(atk, dfn)
            if m in buckets:
                buckets[m].append(dfn)

        def fmt(names: list[str]) -> str:
            return "  ".join(f"{TYPE_EMOJI.get(n,'❓')} {n.title()}" for n in names) or "—"

        embed = discord.Embed(
            title=f"{TYPE_EMOJI.get(atk,'❓')}  {atk.title()} — Attacking Chart",
            colour=type_colour(atk),
        )
        embed.add_field(name="✅ Super Effective (2×)",    value=fmt(buckets[2.0]), inline=False)
        embed.add_field(name="⬜ Normal (1×)",             value=fmt(buckets[1.0]), inline=False)
        embed.add_field(name="❌ Not Very Effective (½×)", value=fmt(buckets[0.5]), inline=False)
        embed.add_field(name="🚫 No Effect (0×)",         value=fmt(buckets[0.0]), inline=False)
        embed.set_footer(text=FOOTER)
        return embed

    def _help(self) -> discord.Embed:
        embed = discord.Embed(
            title="📊  Type Chart — How to Use",
            colour=0x5865F2,
            description=(
                "**Single Matchup** — provide both `attack_type` and `defend_type`\n"
                "*e.g. `/typechart attack_type:fire defend_type:grass` → 2×*\n\n"
                "**Full Attacking Row** — provide only `attack_type`\n"
                "*e.g. `/typechart attack_type:water`*\n\n"
                "**Related commands:**\n"
                "• `/type <type>` — full offensive & defensive relations\n"
                "• `/weakness <pokemon>` — weaknesses for a Pokémon\n\n"
                "**Key:** 🔴 4× · 🟠 2× · ⬜ 1× · 🟢 ½× · 🟡 ¼× · ⚫ 0×"
            ),
        )
        embed.set_footer(text=FOOTER)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(TypeChartCog(bot))
