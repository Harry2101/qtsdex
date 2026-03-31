"""
cogs/pokemon.py  —  /pokemon
Clean info card + battle card with proper layout.
"""

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac
from utils.embeds import (
    FOOTER, error_embed, type_badges, type_colour,
    build_stat_lines, TYPE_EMOJI, DAMAGE_CLASS_EMOJI,
)
from utils.normalizer import normalize
from utils.type_chart import group_by_multiplier
from utils.user_prefs import get as get_pref, set_pref

# Lazy import to avoid circular — moves cog helpers imported inline in _go_moves

STYLE_LABELS = {
    "numbers": "🔢 Numbers",
    "bar":     "📊 Bar",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sprite(data: dict) -> str | None:
    return (
        data.get("sprites", {}).get("other", {})
        .get("official-artwork", {}).get("front_default")
        or data.get("sprites", {}).get("front_default")
    )


def _fmt_weak(types: list[str]) -> str:
    return "  ".join(f"{TYPE_EMOJI.get(t, '❓')} {t.title()}" for t in types)


def classify_role(stats: dict[str, int]) -> str:
    hp    = stats.get("hp", 0)
    atk   = stats.get("attack", 0)
    spatk = stats.get("special-attack", 0)
    dfn   = stats.get("defense", 0)
    spdef = stats.get("special-defense", 0)
    spd   = stats.get("speed", 0)
    bulk  = (hp + dfn + spdef) / 3
    off   = max(atk, spatk)
    if spd >= 100 and off >= 100: return "⚡ Speed Sweeper"
    if off >= 120 and bulk < 85:  return "💥 Glass Cannon"
    if bulk >= 100 and off < 80:  return "🛡️ Defensive Wall"
    if hp >= 90 and (dfn >= 90 or spdef >= 90) and off >= 80: return "🏔️ Bulky Attacker"
    if bulk >= 90 and off >= 90:  return "⚖️ Balanced Threat"
    if off >= 100:                return "💪 Attacker"
    return "🔄 All-Rounder"


# ── Fetchers ──────────────────────────────────────────────────────────────────

async def fetch_ability_effects(abilities: list[dict]) -> dict[str, str]:
    slugs   = [a["ability"]["name"] for a in abilities]
    results = await asyncio.gather(*[pokeapi.get_ability(s) for s in slugs])
    out: dict[str, str] = {}
    for slug, data in zip(slugs, results):
        if not data:
            continue
        for e in data.get("effect_entries", []):
            if e.get("language", {}).get("name") == "en":
                text = e.get("short_effect", "")
                if len(text) > 150:
                    text = text[:150].rsplit(" ", 1)[0] + "…"
                out[slug] = text
                break
    return out


async def fetch_top_moves(move_entries: list) -> list[dict]:
    candidates: list[str] = []
    seen: set[str] = set()
    for entry in move_entries:
        slug = entry["move"]["name"]
        if slug in seen:
            continue
        for vgd in entry.get("version_group_details", []):
            if vgd["move_learn_method"]["name"] in ("level-up", "machine"):
                candidates.append(slug)
                seen.add(slug)
                break
        if len(candidates) >= 40:
            break
    if not candidates:
        return []
    results    = await asyncio.gather(*[pokeapi.get_move(s) for s in candidates])
    with_power = [
        m for m in results
        if m and m.get("power") and m.get("damage_class", {}).get("name") != "status"
    ]
    with_power.sort(key=lambda m: m.get("power", 0), reverse=True)
    return with_power[:6]


# ── Move table for battle card ────────────────────────────────────────────────

def _battle_move_lines(moves: list[dict]) -> str:
    """
    Each line: TYPE_EMOJI  Name  ·  NNNbp  ·  NNN%
    Type emoji is outside the inline code, name is plain bold.
    """
    if not moves:
        return "*No damaging moves found*"
    lines = []
    for m in moves:
        mname = m["name"].replace("-", " ").title()
        mtype = m.get("type", {}).get("name", "normal")
        te    = TYPE_EMOJI.get(mtype, "❓")
        dc    = m.get("damage_class", {}).get("name", "")
        dce   = DAMAGE_CLASS_EMOJI.get(dc, "")
        bp    = m.get("power") or "—"
        acc   = f"{m['accuracy']}%" if m.get("accuracy") else "—"
        lines.append(f"{te}{dce} **{mname}** · `{bp}bp` · `{acc}`")
    return "\n".join(lines)


# ── Embed builders ────────────────────────────────────────────────────────────

def build_info_embed(data: dict, style: str) -> discord.Embed:
    name  = data["name"].replace("-", " ").title()
    dex   = data["id"]
    types = [t["type"]["name"] for t in data["types"]]
    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    total = sum(stats.values())

    abilities = []
    for a in data["abilities"]:
        label = a["ability"]["name"].replace("-", " ").title()
        abilities.append(f"• {label}" + (" *(hidden)*" if a["is_hidden"] else ""))

    embed = discord.Embed(title=f"#{dex:04d}  {name}", colour=type_colour(types[0]))

    embed.add_field(name="🏷️ Type",      value=type_badges(types),  inline=True)
    embed.add_field(name="🔮 Abilities", value="\n".join(abilities), inline=True)
    embed.add_field(name="\u200b",       value="\u200b",             inline=True)

    # Clean stat block — plain text, no code block wrestling
    embed.add_field(
        name="📈 Base Stats",
        value=build_stat_lines(stats, total, style),
        inline=False,
    )

    sp = _sprite(data)
    if sp:
        embed.set_thumbnail(url=sp)
    embed.set_footer(text=f"QT's Dex  •  {STYLE_LABELS[style]}  •  powered by PokéAPI")
    return embed


def build_battle_embed(
    data: dict,
    top_moves: list[dict],
    ability_effects: dict[str, str],
    style: str,
) -> discord.Embed:
    name  = data["name"].replace("-", " ").title()
    types = [t["type"]["name"] for t in data["types"]]
    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    t1    = types[0]
    t2    = types[1] if len(types) > 1 else None
    total = sum(stats.values())

    role    = classify_role(stats)
    buckets = group_by_multiplier(t1, t2)

    embed = discord.Embed(
        title=f"⚔️  {name}",
        description=f"{type_badges(types)}  ·  {role}",
        colour=type_colour(t1),
    )
    sp = _sprite(data)
    if sp:
        embed.set_thumbnail(url=sp)

    # ── Weaknesses — full width, only non-empty ───────────────────────────────
    weakness_rows = [
        ("🔴 4×",   buckets["4x"]),
        ("🟠 2×",   buckets["2x"]),
        ("🟢 ½×",   buckets["0.5x"]),
        ("🟡 ¼×",   buckets["0.25x"]),
        ("⚫ Immune", buckets["0x"]),
    ]
    weak_parts = []
    for label, lst in weakness_rows:
        if lst:
            weak_parts.append(f"**{label}** {_fmt_weak(lst)}")

    if weak_parts:
        embed.add_field(
            name="🎯 Type Matchups",
            value="\n".join(weak_parts),
            inline=False,
        )

    # ── Stats ─────────────────────────────────────────────────────────────────
    embed.add_field(
        name="📈 Stats",
        value=build_stat_lines(stats, total, style),
        inline=False,
    )

    # ── Abilities ─────────────────────────────────────────────────────────────
    ability_lines = []
    for a in data["abilities"]:
        slug   = a["ability"]["name"]
        label  = slug.replace("-", " ").title()
        tag    = " `H`" if a["is_hidden"] else ""
        effect = ability_effects.get(slug, "")
        if effect:
            ability_lines.append(f"**{label}**{tag} — {effect}")
        else:
            ability_lines.append(f"**{label}**{tag}")

    embed.add_field(
        name="🔮 Abilities",
        value="\n\n".join(ability_lines) or "—",
        inline=False,
    )

    # ── Strongest moves ───────────────────────────────────────────────────────
    embed.add_field(
        name="⚔️ Strongest Moves",
        value=_battle_move_lines(top_moves),
        inline=False,
    )

    embed.set_footer(text="QT's Dex  •  Battle Card  •  powered by PokéAPI")
    return embed


# ── View ──────────────────────────────────────────────────────────────────────

class PokemonView(discord.ui.View):
    def __init__(self, data: dict, user_id: int, style: str, start_mode: str = "info"):
        super().__init__(timeout=180)
        self.data             = data
        self.user_id          = user_id
        self.style            = style
        self.mode             = start_mode
        self.top_moves:       list[dict]     = []
        self.ability_effects: dict[str, str] = {}
        self._sync_buttons()

    def _sync_buttons(self):
        self.clear_items()
        if self.mode == "info":
            for key, label in STYLE_LABELS.items():
                btn = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.success if key == self.style else discord.ButtonStyle.secondary,
                    row=0,
                )
                btn.callback = self._make_style_cb(key)
                self.add_item(btn)
            b = discord.ui.Button(label="⚔️ Battle Card", style=discord.ButtonStyle.danger, row=1)
            b.callback = self._go_battle
            self.add_item(b)
            m = discord.ui.Button(label="📋 Moves", style=discord.ButtonStyle.secondary, row=1)
            m.callback = self._go_moves
            self.add_item(m)
        else:
            # Battle card: style buttons + back button
            for key, label in STYLE_LABELS.items():
                btn = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.success if key == self.style else discord.ButtonStyle.secondary,
                    row=0,
                )
                btn.callback = self._make_battle_style_cb(key)
                self.add_item(btn)
            b = discord.ui.Button(label="📋 Info", style=discord.ButtonStyle.primary, row=1)
            b.callback = self._go_info
            self.add_item(b)

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    def _make_style_cb(self, style: str):
        async def cb(interaction: discord.Interaction):
            if not self._guard(interaction):
                return await interaction.response.send_message(
                    "Only the person who ran this command can switch styles.", ephemeral=True
                )
            self.style = style
            set_pref(self.user_id, "stat_style", style)
            self._sync_buttons()
            await interaction.response.edit_message(
                embed=build_info_embed(self.data, self.style), view=self
            )
        return cb

    def _make_battle_style_cb(self, style: str):
        async def cb(interaction: discord.Interaction):
            if not self._guard(interaction):
                return await interaction.response.send_message(
                    "Only the person who ran this command can switch styles.", ephemeral=True
                )
            self.style = style
            set_pref(self.user_id, "stat_style", style)
            self._sync_buttons()
            await interaction.response.edit_message(
                embed=build_battle_embed(self.data, self.top_moves, self.ability_effects, self.style),
                view=self,
            )
        return cb

    async def _go_battle(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can switch views.", ephemeral=True
            )
        await interaction.response.defer()
        if not self.top_moves or not self.ability_effects:
            self.top_moves, self.ability_effects = await asyncio.gather(
                fetch_top_moves(self.data.get("moves", [])),
                fetch_ability_effects(self.data.get("abilities", [])),
            )
        self.mode = "battle"
        set_pref(self.user_id, "last_pokemon_mode", "battle")
        self._sync_buttons()
        await interaction.edit_original_response(
            embed=build_battle_embed(self.data, self.top_moves, self.ability_effects, self.style),
            view=self,
        )

    async def _go_moves(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can do that.", ephemeral=True
            )
        await interaction.response.defer(thinking=True)

        # Import here to avoid circular imports
        from cogs.moves import _collect, MovesView

        name   = self.data["name"].replace("-", " ").title()
        types  = [t["type"]["name"] for t in self.data["types"]]
        colour = type_colour(types[0])
        raw    = _collect(self.data.get("moves", []), None)

        if not raw:
            return await interaction.followup.send(
                embed=error_embed("No Moves", f"**{name}** has no moves."),
                ephemeral=True,
            )

        view = MovesView(raw_moves=raw, title=f"{name} — Moves", colour=colour)
        await view.ensure_fetched()
        view._sync_buttons()
        await interaction.followup.send(embed=view.embed(), view=view)

    async def _go_info(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can switch views.", ephemeral=True
            )
        self.mode = "info"
        set_pref(self.user_id, "last_pokemon_mode", "info")
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=build_info_embed(self.data, self.style), view=self
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class PokemonCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="pokemon",
        description="Look up a Pokémon's stats, types, abilities and more.",
    )
    @app_commands.describe(pokemon="Pokémon name or Dex number")
    @app_commands.autocomplete(pokemon=pokemon_ac)
    async def pokemon_cmd(self, interaction: discord.Interaction, pokemon: str):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_pokemon(normalize(pokemon))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{pokemon}** wasn't found. Try autocomplete."),
                ephemeral=True,
            )

        style = get_pref(interaction.user.id, "stat_style")
        if style not in STYLE_LABELS:
            style = "numbers"
        start_mode = get_pref(interaction.user.id, "last_pokemon_mode") or "info"

        view = PokemonView(data=data, user_id=interaction.user.id, style=style, start_mode=start_mode)

        if start_mode == "battle":
            view.top_moves, view.ability_effects = await asyncio.gather(
                fetch_top_moves(data.get("moves", [])),
                fetch_ability_effects(data.get("abilities", [])),
            )
            embed = build_battle_embed(data, view.top_moves, view.ability_effects, style)
        else:
            embed = build_info_embed(data, style)

        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(PokemonCog(bot))
