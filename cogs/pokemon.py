"""
cogs/pokemon.py  —  /pokemon
Battle card with move list and VGC meta panel.
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac
from utils.embeds import (
    error_embed, type_badges, type_colour,
    build_stat_lines, TYPE_EMOJI, make_footer,
)
from utils.limitless import fetch_meta
from utils.normalizer import normalize
from utils.type_chart import group_by_multiplier_with_ability, ability_alters_matchups
from utils.user_prefs import get as get_pref, set_pref

# Lazy import to avoid circular — moves cog helpers imported inline in _go_moves


# ── Shared loader ─────────────────────────────────────────────────────────────

async def _load_pokemon(pokemon: str) -> tuple[dict | None, list[str], str | None]:
    """
    Fetch Pokémon data + species varieties for a user-supplied name.
    Returns (data, varieties, fallback_notice).
    varieties is a list of slugs (empty if only one form or species fetch fails).
    fallback_notice is set if we fell back from an unimplemented form.
    """
    slug = normalize(pokemon)
    data = await pokeapi.get_pokemon(slug)

    fallback_notice: str | None = None
    if not data:
        base = _strip_form_suffix(slug)
        if base and base != slug:
            data = await pokeapi.get_pokemon(base)
            if data:
                form_label = slug.replace("-", " ").title()
                fallback_notice = f"**{form_label}** doesn't exist yet — showing base form instead."
                slug = base

    if not data:
        return None, [], None

    # Fetch species for variety list (parallel with data already fetched)
    species = await pokeapi.get_species(data["name"])
    varieties: list[str] = []
    if species:
        all_vars = [v["pokemon"]["name"] for v in species.get("varieties", [])]
        if len(all_vars) > 1:
            varieties = all_vars

    return data, varieties, fallback_notice


# ── Helpers ───────────────────────────────────────────────────────────────────

_FORM_SUFFIXES = (
    "-mega-x", "-mega-y", "-mega",
    "-primal", "-gmax", "-eternamax",
)

def _strip_form_suffix(slug: str) -> str | None:
    for suffix in _FORM_SUFFIXES:
        if slug.endswith(suffix):
            return slug[: -len(suffix)]
    return None

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


# ── Embed builders ────────────────────────────────────────────────────────────

def build_battle_embed(
    data: dict,
    ability_effects: dict[str, str],
    guild_id: str = "",
    pokemon_arg: str = "",
    meta: dict | None = None,
) -> discord.Embed:
    name  = data["name"].replace("-", " ").title()
    types = [t["type"]["name"] for t in data["types"]]
    stats = {s["stat"]["name"]: s["base_stat"] for s in data["stats"]}
    dex   = data["id"]
    t1    = types[0]
    t2    = types[1] if len(types) > 1 else None
    total = sum(stats.values())

    role = classify_role(stats)

    # Pick the canonical ability for matchup calc:
    # sole ability if only one, else meta-top ability, else first non-hidden.
    abilities_raw = data.get("abilities", [])
    canonical_ability: str | None = None
    if len(abilities_raw) == 1:
        canonical_ability = abilities_raw[0]["ability"]["name"]
    elif meta and meta.get("abilities"):
        meta_top = meta["abilities"][0]["name"].lower().replace(" ", "-")
        if any(a["ability"]["name"] == meta_top for a in abilities_raw):
            canonical_ability = meta_top
    if not canonical_ability and abilities_raw:
        for a in abilities_raw:
            if not a.get("is_hidden"):
                canonical_ability = a["ability"]["name"]
                break
        if not canonical_ability:
            canonical_ability = abilities_raw[0]["ability"]["name"]

    matchup_ability = canonical_ability if ability_alters_matchups(canonical_ability) else None
    buckets, ability_note = group_by_multiplier_with_ability(t1, t2, matchup_ability)

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
        matchup_title = "🎯 Type Matchups"
        if matchup_ability:
            matchup_title += f" (with {matchup_ability.replace('-', ' ').title()})"
        value = "\n\n".join(weak_parts)
        if ability_note:
            value = f"> ⚠️ *{ability_note}*\n\n{value}"
        embed.add_field(
            name=matchup_title,
            value=value,
            inline=False,
        )

    embed.add_field(
        name="📈 Base Stats",
        value=build_stat_lines(stats, total),
        inline=False,
    )

    # Top meta ability slug (first = highest usage), or sole ability if only one
    top_meta_ability: str | None = None
    if meta:
        abilities = data["abilities"]
        if len(abilities) == 1:
            top_meta_ability = abilities[0]["ability"]["name"].replace("-", " ")
        elif meta.get("abilities"):
            top_meta_ability = meta["abilities"][0]["name"].lower().replace("-", " ")

    ability_lines = []
    for a in data["abilities"]:
        slug   = a["ability"]["name"]
        label  = slug.replace("-", " ").title()
        tag    = " `H`" if a["is_hidden"] else ""
        effect = ability_effects.get(slug, "")
        highlight = "✅ " if top_meta_ability and slug.replace("-", " ") == top_meta_ability else ""
        shield = " 🛡️" if ability_alters_matchups(slug) else ""
        if effect:
            ability_lines.append(f"{highlight}**{label}**{tag}{shield} — {effect}")
        else:
            ability_lines.append(f"{highlight}**{label}**{tag}{shield}")

    embed.add_field(
        name="🔮 Abilities",
        value="\n\n".join(ability_lines) or "—",
        inline=False,
    )

    # ── Meta fields (injected when VGC Meta is active) ────────────────────────
    if meta:
        if meta.get("partners"):
            embed.add_field(
                name="🤝 Common Cores",
                value="\n".join(f"**{e['name']}** — `{e['pct']}`" for e in meta["partners"]),
                inline=False,
            )
        if meta.get("items"):
            embed.add_field(
                name="🎒 Top Items",
                value="\n".join(f"**{e['name']}** — `{e['pct']}`" for e in meta["items"]),
                inline=False,
            )
        if meta.get("moves"):
            embed.add_field(
                name="⚔️ Meta Moves",
                value="\n".join(f"**{e['name']}** — `{e['pct']}`" for e in meta["moves"]),
                inline=False,
            )
        embed.add_field(
            name="\u200b",
            value=(
                "> ⚠️ **Data may not reflect your current meta.** "
                "Verify moves, items, and abilities are legal in your format."
            ),
            inline=False,
        )

    base_suffix = f"/pokemon {pokemon_arg}" if pokemon_arg else "/pokemon"
    suffix = f"{base_suffix}  •  📋 all moves · 📊 meta · 🔍 search"
    embed.set_footer(text=make_footer(guild_id, suffix))
    return embed


# ── View ──────────────────────────────────────────────────────────────────────

class PokemonView(discord.ui.View):
    def __init__(self, data: dict, user_id: int, guild_id: str = "", ability_effects: dict[str, str] | None = None, pokemon_arg: str = "", varieties: list[str] | None = None):
        super().__init__(timeout=180)
        self.data             = data
        self.user_id          = user_id
        self.guild_id         = guild_id
        self.pokemon_arg      = pokemon_arg
        self.ability_effects: dict[str, str] = ability_effects or {}
        self.cached_meta:     dict | None    = None
        self.meta_shown       = False
        self.varieties:       list[str]      = varieties or []
        self._sync_buttons()

    def _sync_buttons(self):
        self.clear_items()

        movelist_btn = discord.ui.Button(
            label="Move List",
            emoji="📋",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        movelist_btn.callback = self._go_moves
        self.add_item(movelist_btn)

        meta_btn = discord.ui.Button(
            label="Meta",
            emoji="📊",
            style=discord.ButtonStyle.primary if self.meta_shown else discord.ButtonStyle.secondary,
            row=0,
        )
        meta_btn.callback = self._go_meta
        self.add_item(meta_btn)

        search_btn = discord.ui.Button(
            label="Search",
            emoji="🔍",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        search_btn.callback = self._go_search
        self.add_item(search_btn)

        if self.varieties:
            current_slug = self.data["name"]
            options = [
                discord.SelectOption(
                    label=slug.replace("-", " ").title(),
                    value=slug,
                    default=(slug == current_slug),
                )
                for slug in self.varieties[:25]
            ]
            forms_select = discord.ui.Select(
                placeholder="Alt forms & megas…",
                options=options,
                row=1,
            )
            forms_select.callback = self._go_select
            self.add_item(forms_select)

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

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
        await interaction.response.defer()

        if not self.cached_meta:
            self.cached_meta = await fetch_meta(self.data["name"])

        if not self.cached_meta and not self.meta_shown:
            name = self.data["name"].replace("-", " ").title()
            return await interaction.followup.send(
                embed=error_embed("No VGC Data", f"**{name}** has no usage data on Limitless VGC."),
                ephemeral=True,
            )

        self.meta_shown = not self.meta_shown
        set_pref(self.user_id, "pokemon_meta_shown", self.meta_shown)
        self._sync_buttons()

        embed = build_battle_embed(
            self.data,
            self.ability_effects,
            self.guild_id,
            self.pokemon_arg,
            meta=self.cached_meta if self.meta_shown else None,
        )
        await interaction.edit_original_response(embed=embed, view=self)

    async def _go_select(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can do that.", ephemeral=True
            )
        await interaction.response.defer()

        slug = interaction.data["values"][0]
        data, _, fallback_notice = await _load_pokemon(slug)
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{slug}** wasn't found."),
                ephemeral=True,
            )

        self.data            = data
        self.pokemon_arg     = slug
        self.ability_effects = await fetch_ability_effects(data.get("abilities", []))
        self.cached_meta     = None
        if self.meta_shown:
            self.cached_meta = await fetch_meta(data["name"])
            if not self.cached_meta:
                self.meta_shown = False
        self._sync_buttons()

        embed = build_battle_embed(
            data, self.ability_effects, self.guild_id, slug,
            meta=self.cached_meta if self.meta_shown else None,
        )
        if fallback_notice:
            embed.description = f"> ℹ️ {fallback_notice}\n{embed.description}"
        await interaction.edit_original_response(embed=embed, view=self)

    async def _go_search(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message(
                "Only the person who ran this command can do that.", ephemeral=True
            )
        await interaction.response.send_modal(
            _PokemonSearchModal(view=self)
        )


# ── Search modal ──────────────────────────────────────────────────────────────

class _PokemonSearchModal(discord.ui.Modal, title="Search Pokémon"):
    query = discord.ui.TextInput(
        label="Name or Dex number",
        placeholder="e.g. Garchomp, 445, rotom-wash…",
        min_length=1,
        max_length=50,
    )

    def __init__(self, view: "PokemonView"):
        super().__init__()
        self._parent_view = view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        pokemon = self.query.value.strip()
        data, varieties, fallback_notice = await _load_pokemon(pokemon)

        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{pokemon}** wasn't found."),
                ephemeral=True,
            )

        v = self._parent_view
        v.data            = data
        v.pokemon_arg     = pokemon
        v.varieties       = varieties
        v.ability_effects = await fetch_ability_effects(data.get("abilities", []))
        v.cached_meta     = None
        if v.meta_shown:
            v.cached_meta = await fetch_meta(data["name"])
            if not v.cached_meta:
                v.meta_shown = False
        v._sync_buttons()

        embed = build_battle_embed(
            data, v.ability_effects, v.guild_id, pokemon,
            meta=v.cached_meta if v.meta_shown else None,
        )
        if fallback_notice:
            embed.description = f"> ℹ️ {fallback_notice}\n{embed.description}"

        await interaction.edit_original_response(embed=embed, view=v)


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

        data, varieties, fallback_notice = await _load_pokemon(pokemon)

        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{pokemon}** wasn't found. Try autocomplete."),
                ephemeral=True,
            )

        gid             = str(interaction.guild_id or "")
        ability_effects = await fetch_ability_effects(data.get("abilities", []))

        meta_pref = bool(get_pref(interaction.user.id, "pokemon_meta_shown"))
        cached_meta = await fetch_meta(data["name"]) if meta_pref else None
        meta_shown = bool(cached_meta)

        view = PokemonView(
            data=data,
            user_id=interaction.user.id,
            guild_id=gid,
            ability_effects=ability_effects,
            pokemon_arg=pokemon,
            varieties=varieties,
        )
        view.cached_meta = cached_meta
        view.meta_shown  = meta_shown
        view._sync_buttons()

        embed = build_battle_embed(
            data, ability_effects, gid, pokemon,
            meta=cached_meta if meta_shown else None,
        )

        if fallback_notice:
            embed.description = f"> ℹ️ {fallback_notice}\n{embed.description}"

        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(PokemonCog(bot))
