"""
cogs/sprite.py  —  /sprite
Shows a full-size Pokémon sprite — official artwork or shiny.
Clickable/zoomable since Discord renders full images inline.
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac
from utils.embeds import FOOTER, error_embed, type_colour, type_badges, TYPE_EMOJI
from utils.normalizer import normalize


def _get_sprites(data: dict) -> dict[str, str | None]:
    sprites  = data.get("sprites", {})
    other    = sprites.get("other", {})
    artwork  = other.get("official-artwork", {})
    home     = other.get("home", {})
    showdown = other.get("showdown", {})

    return {
        "official_normal": artwork.get("front_default"),
        "official_shiny":  artwork.get("front_shiny"),
        "home_normal":     home.get("front_default"),
        "home_shiny":      home.get("front_shiny"),
        "default_normal":  sprites.get("front_default"),
        "default_shiny":   sprites.get("front_shiny"),
        "showdown_normal": showdown.get("front_default"),
        "showdown_shiny":  showdown.get("front_shiny"),
    }


def _pick_sprite(sprites: dict, shiny: bool) -> str | None:
    if shiny:
        return (
            sprites["official_shiny"]
            or sprites["home_shiny"]
            or sprites["default_shiny"]
            or sprites["showdown_shiny"]
        )
    return (
        sprites["official_normal"]
        or sprites["home_normal"]
        or sprites["default_normal"]
        or sprites["showdown_normal"]
    )


class SpriteView(discord.ui.View):
    def __init__(self, data: dict, shiny: bool, user_id: int):
        super().__init__(timeout=120)
        self.data    = data
        self.shiny   = shiny
        self.user_id = user_id
        self._sync()

    def _sync(self):
        self.clear_items()
        sprites = _get_sprites(self.data)
        has_shiny = _pick_sprite(sprites, shiny=True) is not None

        normal_btn = discord.ui.Button(
            label="Normal",
            style=discord.ButtonStyle.success if not self.shiny else discord.ButtonStyle.secondary,
            emoji="🎨",
            row=0,
        )
        shiny_btn = discord.ui.Button(
            label="✨ Shiny" if has_shiny else "✨ Shiny (N/A)",
            style=discord.ButtonStyle.success if self.shiny else discord.ButtonStyle.secondary,
            disabled=not has_shiny,
            row=0,
        )
        normal_btn.callback = self._go_normal
        shiny_btn.callback  = self._go_shiny
        self.add_item(normal_btn)
        self.add_item(shiny_btn)

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    def _build_embed(self) -> discord.Embed:
        return build_sprite_embed(self.data, self.shiny)

    async def _go_normal(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can switch.", ephemeral=True
            )
        self.shiny = False
        self._sync()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _go_shiny(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can switch.", ephemeral=True
            )
        self.shiny = True
        self._sync()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


def build_sprite_embed(data: dict, shiny: bool) -> discord.Embed:
    name    = data["name"].replace("-", " ").title()
    dex     = data["id"]
    types   = [t["type"]["name"] for t in data["types"]]
    sprites = _get_sprites(data)
    url     = _pick_sprite(sprites, shiny)

    label   = "✨ Shiny" if shiny else "Normal"
    colour  = 0xFFD700 if shiny else type_colour(types[0])

    embed = discord.Embed(
        title=f"{'✨ ' if shiny else ''}#{dex:04d}  {name}",
        description=f"{type_badges(types)}\n{label} sprite",
        colour=colour,
    )

    if url:
        # set_image gives the large clickable/zoomable image
        embed.set_image(url=url)
    else:
        embed.description += "\n\n*No sprite available for this variant.*"

    embed.set_footer(text=f"King's Dex  •  Click image to zoom  •  powered by PokéAPI")
    return embed


class SpriteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="sprite",
        description="Show a full-size Pokémon sprite — tap the image to zoom in.",
    )
    @app_commands.describe(
        pokemon="Pokémon name or Dex number",
        shiny="Show shiny variant (default: False)",
    )
    @app_commands.autocomplete(pokemon=pokemon_ac)
    async def sprite_cmd(
        self,
        interaction: discord.Interaction,
        pokemon: str,
        shiny:   bool = False,
    ):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_pokemon(normalize(pokemon))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{pokemon}** wasn't found. Try autocomplete."),
                ephemeral=True,
            )

        sprites = _get_sprites(data)
        url     = _pick_sprite(sprites, shiny)

        if not url and shiny:
            # Gracefully fall back and tell the user
            fallback = _pick_sprite(sprites, shiny=False)
            if fallback:
                return await interaction.followup.send(
                    content=f"⚠️ No shiny sprite found for **{pokemon.title()}** — showing normal instead.",
                    embed=build_sprite_embed(data, shiny=False),
                    view=SpriteView(data=data, shiny=False, user_id=interaction.user.id),
                )
            return await interaction.followup.send(
                embed=error_embed("No Sprite", f"No sprite found for **{pokemon.title()}**."),
                ephemeral=True,
            )

        embed = build_sprite_embed(data, shiny)
        view  = SpriteView(data=data, shiny=shiny, user_id=interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(SpriteCog(bot))
