"""
cogs/item.py  —  /item
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import item_ac
from utils.embeds import FOOTER, error_embed
from utils.normalizer import normalize

CATEGORY_EMOJI: dict[str, str] = {
    "held-items":        "🎽",
    "choice":            "🔒",
    "type-enhancement":  "⬆️",
    "stat-boosts":       "📈",
    "mega-stones":       "💎",
    "berries":           "🍓",
    "z-crystals":        "💠",
    "plates":            "🪩",
    "memories":          "💾",
    "in-a-pinch":        "🆘",
    "species-specific":  "🌟",
    "jewels":            "💎",
    "evolution":         "🔄",
    "vitamins":          "💊",
    "medicine":          "🩹",
    "other":             "🎒",
}


class ItemCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="item", description="Look up a held item or battle item — what it does.")
    @app_commands.describe(item="Item name (e.g. leftovers, choice-scarf, eviolite)")
    @app_commands.autocomplete(item=item_ac)
    async def item_cmd(self, interaction: discord.Interaction, item: str):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_item(normalize(item))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{item}** wasn't found. Try autocomplete."),
                ephemeral=True,
            )

        await interaction.followup.send(embed=self._build(data))

    def _build(self, data: dict) -> discord.Embed:
        name     = data["name"].replace("-", " ").title()
        category = data.get("category", {}).get("name", "other")
        cat_emoji = CATEGORY_EMOJI.get(category, "🎒")
        cost     = data.get("cost", 0)

        effect_short = effect_long = flavor = ""
        for e in data.get("effect_entries", []):
            if e.get("language", {}).get("name") == "en":
                effect_short = e.get("short_effect", "")
                effect_long  = e.get("effect", "")
                break
        for f in reversed(data.get("flavor_text_entries", [])):
            if f.get("language", {}).get("name") == "en":
                flavor = f.get("text", "").replace("\n", " ").replace("\x0c", " ")
                break

        sprite_url = (data.get("sprites") or {}).get("default")

        embed = discord.Embed(title=f"{cat_emoji}  {name}", colour=0xF0A500)

        embed.add_field(name="🏷️ Category", value=category.replace("-", " ").title(), inline=True)
        embed.add_field(name="💰 Buy Price", value=f"₽{cost:,}" if cost else "Not sold", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        if effect_short:
            embed.add_field(name="📖 Effect", value=effect_short[:500], inline=False)

        if effect_long and effect_long.strip() != effect_short.strip():
            trimmed = effect_long[:800] + ("…" if len(effect_long) > 800 else "")
            embed.add_field(name="📚 Full Effect", value=trimmed, inline=False)

        if flavor:
            embed.add_field(name="📜 Game Description", value=f"*{flavor[:300]}*", inline=False)

        if sprite_url:
            embed.set_thumbnail(url=sprite_url)

        embed.set_footer(text=FOOTER)
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(ItemCog(bot))
