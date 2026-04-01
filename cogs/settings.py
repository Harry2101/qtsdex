"""
cogs/settings.py  —  /settings
Per-user stat display preference — ephemeral, persisted to disk.
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import FOOTER
from utils.user_prefs import get as get_pref, set_pref

STYLES: dict[str, str] = {
    "numbers": "🔢  Plain numbers — clean and simple",
    "bar":     "📊  Bar chart — classic visual fill",
}


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="settings",
        description="Personalise how King's Dex displays Pokémon stats.",
    )
    async def settings_cmd(self, interaction: discord.Interaction):
        # get() now sanitises old "tiers" → "numbers" automatically
        current = get_pref(interaction.user.id, "stat_style")
        # Extra guard in case value is somehow still invalid
        if current not in STYLES:
            current = "numbers"

        embed = discord.Embed(
            title="⚙️  King's Dex — Your Settings",
            description=(
                f"**Current stat style:** {STYLES[current]}\n\n"
                "Pick a new style below — takes effect immediately on `/pokemon`."
            ),
            colour=0x5865F2,
        )
        embed.set_footer(text="King's Dex  •  Settings persist across sessions")
        await interaction.response.send_message(
            embed=embed,
            view=SettingsView(interaction.user.id, current),
            ephemeral=True,
        )


class SettingsView(discord.ui.View):
    def __init__(self, user_id: int, current: str):
        super().__init__(timeout=60)
        for key, label in STYLES.items():
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if key == current else discord.ButtonStyle.secondary,
                custom_id=f"stat_{key}",
            )
            btn.callback = self._make_cb(key)
            self.add_item(btn)

    def _make_cb(self, style: str):
        async def cb(interaction: discord.Interaction):
            set_pref(interaction.user.id, "stat_style", style)
            embed = discord.Embed(
                title="✅  Setting Saved",
                description=(
                    f"Stat style set to:\n**{STYLES[style]}**\n\n"
                    "Run `/pokemon` to see it in action!"
                ),
                colour=0x57F287,
            )
            embed.set_footer(text="King's Dex  •  Settings persist across sessions")
            await interaction.response.edit_message(embed=embed, view=None)
        return cb


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
