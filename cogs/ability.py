"""
cogs/ability.py  —  /ability
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import ability_ac
from utils.embeds import FOOTER, error_embed
from utils.normalizer import normalize


class AbilityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ability", description="Look up a Pokémon ability — what it does and who has it.")
    @app_commands.describe(ability="Ability name")
    @app_commands.autocomplete(ability=ability_ac)
    async def ability_cmd(self, interaction: discord.Interaction, ability: str):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_ability(normalize(ability))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{ability}** wasn't found."), ephemeral=True
            )

        await interaction.followup.send(embed=self._build(data))

    def _build(self, data: dict) -> discord.Embed:
        name         = data["name"].replace("-", " ").title()
        effect_short = effect_long = ""

        for e in data.get("effect_entries", []):
            if e.get("language", {}).get("name") == "en":
                effect_short = e.get("short_effect", "")
                effect_long  = e.get("effect", "")
                break

        pokemon_entries = data.get("pokemon", [])
        normal_holders  = [p["pokemon"]["name"].replace("-", " ").title() for p in pokemon_entries if not p["is_hidden"]]
        hidden_holders  = [p["pokemon"]["name"].replace("-", " ").title() for p in pokemon_entries if p["is_hidden"]]

        embed = discord.Embed(title=f"🔮  {name}", colour=0x5865F2)

        if effect_short:
            embed.add_field(name="📖 Effect", value=effect_short[:500], inline=False)

        if effect_long and effect_long.strip() != effect_short.strip():
            trimmed = effect_long[:900] + ("…" if len(effect_long) > 900 else "")
            embed.add_field(name="📚 Full Effect", value=trimmed, inline=False)

        if normal_holders:
            display = ", ".join(normal_holders[:20])
            if len(normal_holders) > 20:
                display += f" *+{len(normal_holders)-20} more*"
            embed.add_field(name=f"🐾 Pokémon ({len(normal_holders)})", value=display, inline=False)

        if hidden_holders:
            display = ", ".join(hidden_holders[:10])
            if len(hidden_holders) > 10:
                display += f" *+{len(hidden_holders)-10} more*"
            embed.add_field(name=f"🔒 Hidden Ability in ({len(hidden_holders)})", value=display, inline=False)

        embed.set_footer(text=FOOTER)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(AbilityCog(bot))
