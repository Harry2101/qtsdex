"""
cogs/pokemon.py  —  /pokemon
Battle card with toggleable strongest-moves panel.
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac
from utils.embeds import (
    FOOTER, error_embed, type_badges, type_colour,
    build_stat_lines, TYPE_EMOJI, DAMAGE_CLASS_EMOJI, make_footer,
)
from utils.limitless import fetch_meta
from utils.normalizer import normalize
from utils.type_chart import group_by_multiplier

# Lazy import to avoid circular — moves cog helpers imported inline in _go_moves

DUELERS_ROLE_ID = 1483608023539650560


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


# ── Move table ────────────────────────────────────────────────────────────────

def _battle_move_lines(moves: list[dict]) -> str:
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

def build_battle_embed(
    data: dict,
    ability_effects: dict[str, str],
    guild_id: str = "",
    pokemon_arg: str = "",
) -> discord.Embed:
    name  = data["name"].replace("-", " ").title()
    types = [t["type"]["name"] for t in data["types"]]
    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    dex   = data["id"]
    t1    = types[0]
    t2    = types[1] if len(types) > 1 else None
    total = sum(stats.values())

    role    = classify_role(stats)
    buckets = group_by_multiplier(t1, t2)

    embed = discord.Embed(
        title=f"#{dex:04d}  {name}",
        description=f"{type_badges(types)}  ·  {role}",
        colour=type_colour(t1),
    )
    sp = _sprite(data)
    if sp:
        embed.set_thumbnail(url=sp)

    weakness_rows = [
        ("🔴 4×",    buckets["4x"]),
        ("🟠 2×",    buckets["2x"]),
        ("🟢 ½×",    buckets["0.5x"]),
        ("🟡 ¼×",    buckets["0.25x"]),
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

    embed.add_field(
        name="📈 Base Stats",
        value=build_stat_lines(stats, total),
        inline=False,
    )

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

    suffix = f"/pokemon {pokemon_arg}" if pokemon_arg else ""
    embed.set_footer(text=make_footer(guild_id, suffix))
    return embed


# ── VGC meta embed ────────────────────────────────────────────────────────────

def _meta_lines(entries: list[dict]) -> str:
    if not entries:
        return "*No data*"
    return "\n".join(f"**{e['name']}** — `{e['pct']}`" for e in entries)


def build_meta_embed(
    meta: dict,
    name: str,
    colour: int,
    guild_id: str = "",
    pokemon_arg: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{name} — VGC Meta Usage",
        description="Tournament usage stats from [Limitless VGC](https://limitlessvgc.com/)",
        colour=colour,
    )

    if meta.get("partners"):
        embed.add_field(
            name="👥 Top Partners",
            value=_meta_lines(meta["partners"]),
            inline=False,
        )
    if meta.get("items"):
        embed.add_field(
            name="🎒 Top Items",
            value=_meta_lines(meta["items"]),
            inline=False,
        )
    if meta.get("moves"):
        embed.add_field(
            name="⚔️ Top Moves",
            value=_meta_lines(meta["moves"]),
            inline=False,
        )
    if meta.get("abilities"):
        embed.add_field(
            name="🔮 Abilities",
            value=_meta_lines(meta["abilities"]),
            inline=False,
        )

    suffix = f"/pokemon {pokemon_arg}" if pokemon_arg else ""
    embed.set_footer(text=make_footer(guild_id, suffix))
    return embed


# ── View ──────────────────────────────────────────────────────────────────────

class PokemonView(discord.ui.View):
    def __init__(self, data: dict, user_id: int, guild_id: str = "", ability_effects: dict[str, str] | None = None, pokemon_arg: str = ""):
        super().__init__(timeout=180)
        self.data             = data
        self.user_id          = user_id
        self.guild_id         = guild_id
        self.pokemon_arg      = pokemon_arg
        self.top_moves:       list[dict]     = []
        self.ability_effects: dict[str, str] = ability_effects or {}
        self.moves_shown      = False
        self._sync_buttons()

    def _sync_buttons(self):
        self.clear_items()
        moves_btn = discord.ui.Button(
            label="⚔️ Strongest Moves" if not self.moves_shown else "❌ Hide Moves",
            style=discord.ButtonStyle.secondary if not self.moves_shown else discord.ButtonStyle.danger,
            row=0,
        )
        moves_btn.callback = self._go_top_moves
        self.add_item(moves_btn)

        movelist_btn = discord.ui.Button(
            label="📋 Move List",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        movelist_btn.callback = self._go_moves
        self.add_item(movelist_btn)

        meta_btn = discord.ui.Button(
            label="📊 VGC Meta",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        meta_btn.callback = self._go_meta
        self.add_item(meta_btn)

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _go_top_moves(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can do that.", ephemeral=True
            )
        await interaction.response.defer()

        if not self.top_moves:
            self.top_moves = await fetch_top_moves(self.data.get("moves", []))

        self.moves_shown = not self.moves_shown
        self._sync_buttons()

        base_embed = build_battle_embed(self.data, self.ability_effects, self.guild_id, self.pokemon_arg)

        if self.moves_shown:
            warning = (
                "> ⚠️ **Ranked by base power only** — does not account for accuracy, coverage, sets, or meta.\n"
                f"> For real move advice, do some research or ping <@&{DUELERS_ROLE_ID}>!"
            )
            base_embed.add_field(name="\u200b", value=warning, inline=False)
            base_embed.add_field(
                name="⚔️ Strongest Moves",
                value=_battle_move_lines(self.top_moves),
                inline=False,
            )

        await interaction.edit_original_response(embed=base_embed, view=self)

    async def _go_moves(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can do that.", ephemeral=True
            )
        await interaction.response.defer(thinking=True)

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

        view = MovesView(raw_moves=raw, title=f"{name} — Moves", colour=colour, guild_id=self.guild_id)
        await view.ensure_fetched()
        view._sync_buttons()
        await interaction.followup.send(embed=view.embed(), view=view)

    async def _go_meta(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can do that.", ephemeral=True
            )
        await interaction.response.defer(thinking=True)

        name   = self.data["name"].replace("-", " ").title()
        types  = [t["type"]["name"] for t in self.data["types"]]
        colour = type_colour(types[0])

        meta = await fetch_meta(self.data["name"])
        if not meta:
            return await interaction.followup.send(
                embed=error_embed(
                    "No VGC Data",
                    f"**{name}** has no usage data on Limitless VGC.",
                ),
                ephemeral=True,
            )

        embed = build_meta_embed(meta, name, colour, self.guild_id, self.pokemon_arg)
        await interaction.followup.send(embed=embed)


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

        gid             = str(interaction.guild_id or "")
        ability_effects = await fetch_ability_effects(data.get("abilities", []))
        view = PokemonView(data=data, user_id=interaction.user.id, guild_id=gid, ability_effects=ability_effects, pokemon_arg=pokemon)
        embed = build_battle_embed(data, ability_effects, gid, pokemon)

        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(PokemonCog(bot))
