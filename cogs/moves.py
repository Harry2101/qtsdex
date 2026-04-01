"""
cogs/moves.py  —  /pokemon_moves
Paginated move list with type emoji, TM numbers, power/accuracy/PP,
ephemeral move detail, share button, and type-filter cycling button.
"""

import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import pokeapi
from utils.autocomplete import pokemon_ac, learn_method_ac
from utils.embeds import (
    FOOTER, error_embed, type_colour,
    TYPE_EMOJI, TYPE_COLOURS, DAMAGE_CLASS_EMOJI,
)
from utils.normalizer import normalize

ALLOWED  = {"level-up", "machine", "egg"}
PER_PAGE = 18


# ── Collect ───────────────────────────────────────────────────────────────────

def _collect(move_data: list, method_filter: Optional[str]) -> list[tuple[str, str, int]]:
    seen: set[str] = set()
    out:  list[tuple[str, str, int]] = []
    for entry in move_data:
        slug = entry["move"]["name"]
        if slug in seen:
            continue
        for vgd in entry.get("version_group_details", []):
            method = vgd["move_learn_method"]["name"]
            level  = vgd["level_learned_at"]
            if method not in ALLOWED:
                continue
            if method_filter and method != method_filter:
                continue
            out.append((slug, method, level))
            seen.add(slug)
            break
    out.sort(key=lambda x: (x[1] != "level-up", x[2], x[0]))
    return out


# ── Fetch ─────────────────────────────────────────────────────────────────────

async def fetch_move_details(slugs: list[str]) -> dict[str, dict]:
    results = await asyncio.gather(*[pokeapi.get_move(s) for s in slugs])
    return {s: d for s, d in zip(slugs, results) if d}


async def get_tm_number(move_slug: str) -> Optional[int]:
    data = await pokeapi.get_move(move_slug)
    if not data:
        return None
    machines = data.get("machines", [])
    if not machines:
        return None
    machine_url = machines[0].get("machine", {}).get("url", "")
    if not machine_url:
        return None
    machine_data = await pokeapi.fetch(machine_url)
    if not machine_data:
        return None
    item_name = machine_data.get("item", {}).get("name", "")
    if item_name.startswith("tm"):
        try:
            return int(item_name[2:])
        except ValueError:
            pass
    return None


# ── Format ────────────────────────────────────────────────────────────────────

def _format_line(
    slug:        str,
    method:      str,
    level:       int,
    detail:      Optional[dict],
    tm_number:   Optional[int],
) -> str:
    display_name = slug.replace("-", " ").title()

    if detail:
        mtype = detail.get("type", {}).get("name", "normal")
        bp    = detail.get("power")
        acc   = detail.get("accuracy")
        pp    = detail.get("pp")
        dc    = detail.get("damage_class", {}).get("name", "")
    else:
        mtype, bp, acc, pp, dc = "normal", None, None, None, ""

    te  = TYPE_EMOJI.get(mtype, "❓")
    dce = DAMAGE_CLASS_EMOJI.get(dc, "")

    if method == "level-up":
        tag = "`Evo `" if level == 0 else f"`Lv{level:<3}`"
    elif method == "machine":
        tag = f"`TM{tm_number:<3}`" if tm_number is not None else "`TM  `"
    else:
        tag = "`Egg `"

    parts = []
    if bp:  parts.append(f"{bp}bp")
    if acc: parts.append(f"{acc}%")
    if pp:  parts.append(f"PP{pp}")
    suffix = "  `" + "  ".join(parts) + "`" if parts else ""

    return f"{te}{dce} {tag} **{display_name}**{suffix}"


def _paginate(lines: list[str]) -> list[str]:
    pages = []
    for i in range(0, max(len(lines), 1), PER_PAGE):
        pages.append("\n".join(lines[i:i + PER_PAGE]) or "*No moves.*")
    return pages


# ── Move detail embed ─────────────────────────────────────────────────────────

def _move_detail_embed(data: dict) -> discord.Embed:
    name       = data["name"].replace("-", " ").title()
    mtype      = data.get("type", {}).get("name", "normal")
    dc         = data.get("damage_class", {}).get("name", "")
    power      = data.get("power") or "—"
    accuracy   = data.get("accuracy") or "—"
    pp         = data.get("pp") or "—"
    priority   = data.get("priority", 0)
    eff_chance = data.get("effect_chance")
    target     = data.get("target", {}).get("name", "—").replace("-", " ").title()

    effect = ""
    for e in data.get("effect_entries", []):
        if e.get("language", {}).get("name") == "en":
            effect = e.get("short_effect", "")
            if eff_chance:
                effect = effect.replace("$effect_chance", str(eff_chance))
            break

    te  = TYPE_EMOJI.get(mtype, "❓")
    dce = DAMAGE_CLASS_EMOJI.get(dc, "")
    pri = f"+{priority}" if priority > 0 else str(priority)

    embed = discord.Embed(title=f"{te}  {name}", colour=type_colour(mtype))
    embed.add_field(name="💥 Class",    value=f"{dce} {dc.title()}", inline=True)
    embed.add_field(name="💪 Power",    value=str(power),            inline=True)
    embed.add_field(name="🎯 Accuracy", value=f"{accuracy}%" if accuracy != "—" else "—", inline=True)
    embed.add_field(name="🔵 PP",       value=str(pp),               inline=True)
    embed.add_field(name="⚡ Priority", value=pri,                   inline=True)
    embed.add_field(name="🎯 Target",   value=target,                inline=True)
    if effect:
        embed.add_field(name="📖 Effect", value=effect[:500], inline=False)
    embed.set_footer(text="King's Dex  •  Move Details  •  powered by PokéAPI")
    return embed


# ── View ──────────────────────────────────────────────────────────────────────

class MovesView(discord.ui.View):
    def __init__(self, raw_moves: list[tuple[str, str, int]], title: str, colour: int):
        super().__init__(timeout=180)
        self.raw_moves = raw_moves
        self.base_title = title
        self.base_colour = colour
        self.current     = 0

        # Populated by ensure_fetched()
        self._details:    dict[str, dict]          = {}
        self._tm_nums:    dict[str, Optional[int]] = {}
        self._all_lines:  list[str]                = []  # all moves, formatted
        self._type_map:   dict[str, str]           = {}  # slug → type name
        self._fetched     = False

        # Type filter state
        # _type_cycle: [None, "fire", "water", ...] — None = show all
        self._type_cycle: list[Optional[str]] = [None]
        self._type_idx:   int                 = 0   # index into _type_cycle

        # Filtered view (recalculated on type change)
        self._filtered_raw:   list[tuple[str, str, int]] = raw_moves
        self._filtered_lines: list[str]                  = []
        self._pages:          list[str]                  = []

        self._sync_buttons()

    # ── Fetch ─────────────────────────────────────────────────────────────────

    async def ensure_fetched(self):
        if self._fetched:
            return
        self._fetched = True

        slugs = [s for s, _, _ in self.raw_moves]

        detail_map    = await fetch_move_details(slugs)
        self._details = detail_map

        machine_slugs = [s for s, m, _ in self.raw_moves if m == "machine"]
        tm_results    = await asyncio.gather(*[get_tm_number(s) for s in machine_slugs])
        self._tm_nums = dict(zip(machine_slugs, tm_results))

        # Build type map and all lines
        self._type_map = {}
        self._all_lines = []
        for slug, method, level in self.raw_moves:
            detail = self._details.get(slug)
            if detail:
                self._type_map[slug] = detail.get("type", {}).get("name", "normal")
            tm_num = self._tm_nums.get(slug) if method == "machine" else None
            self._all_lines.append(_format_line(slug, method, level, detail, tm_num))

        # Build type cycle: None (all) + sorted unique types present
        types_present = sorted(set(self._type_map.values()))
        self._type_cycle = [None] + types_present

        # Initial filter = show all
        self._apply_filter()

    def _apply_filter(self):
        """Rebuild filtered lines and pages based on current type filter."""
        active = self._type_cycle[self._type_idx] if self._type_cycle else None

        if active is None:
            self._filtered_raw   = self.raw_moves
            self._filtered_lines = self._all_lines
        else:
            filtered_pairs = [
                (i, entry)
                for i, entry in enumerate(self.raw_moves)
                if self._type_map.get(entry[0]) == active
            ]
            self._filtered_raw   = [e for _, e in filtered_pairs]
            self._filtered_lines = [self._all_lines[i] for i, _ in filtered_pairs]

        self._pages  = _paginate(self._filtered_lines) if self._filtered_lines else ["*No moves.*"]
        self.current = 0

    # ── Embed ─────────────────────────────────────────────────────────────────

    @property
    def _active_type(self) -> Optional[str]:
        if not self._type_cycle:
            return None
        return self._type_cycle[self._type_idx]

    def embed(self) -> discord.Embed:
        active = self._active_type

        if active:
            colour = TYPE_COLOURS.get(active, self.base_colour)
            te     = TYPE_EMOJI.get(active, "❓")
            title  = f"{self.base_title}  [{te} {active.title()}]"
        else:
            colour = self.base_colour
            title  = self.base_title

        total   = len(self._filtered_raw)
        content = self._pages[self.current] if self._pages else "*No moves.*"

        e = discord.Embed(title=title, description=content, colour=colour)
        e.set_footer(
            text=f"King's Dex  •  Page {self.current+1}/{len(self._pages)}  •  {total} moves"
                 + (f"  •  {active.title()} only" if active else "")
        )
        return e

    # ── Buttons ───────────────────────────────────────────────────────────────

    def _sync_buttons(self):
        self.clear_items()

        # Row 0: Prev | Next | Type Filter
        prev = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.grey,
            disabled=self.current == 0,
            row=0,
        )
        nxt = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.grey,
            disabled=self.current >= max(len(self._pages) - 1, 0),
            row=0,
        )
        prev.callback = self._prev
        nxt.callback  = self._next
        self.add_item(prev)
        self.add_item(nxt)

        # Type filter button — cycles through types
        if len(self._type_cycle) > 1:
            active     = self._active_type
            next_idx   = (self._type_idx + 1) % len(self._type_cycle)
            next_type  = self._type_cycle[next_idx]

            if active is None:
                # Currently showing all — button previews first type
                first_type = self._type_cycle[1] if len(self._type_cycle) > 1 else None
                btn_label  = f"{TYPE_EMOJI.get(first_type, '🔍')} Filter Type" if first_type else "Filter Type"
                btn_style  = discord.ButtonStyle.secondary
            else:
                if next_type is None:
                    btn_label = "🔄 Show All"
                    btn_style = discord.ButtonStyle.success
                else:
                    btn_label = f"{TYPE_EMOJI.get(next_type, '❓')} {next_type.title()}"
                    btn_style = discord.ButtonStyle.primary

            type_btn          = discord.ui.Button(label=btn_label, style=btn_style, row=0)
            type_btn.callback = self._cycle_type
            self.add_item(type_btn)

        # Row 1: Move Details
        detail_btn          = discord.ui.Button(label="🔍 Move Details", style=discord.ButtonStyle.primary, row=1)
        detail_btn.callback = self._show_detail_picker
        self.add_item(detail_btn)

    async def _prev(self, interaction: discord.Interaction):
        self.current -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.current += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _cycle_type(self, interaction: discord.Interaction):
        self._type_idx = (self._type_idx + 1) % len(self._type_cycle)
        self._apply_filter()
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _show_detail_picker(self, interaction: discord.Interaction):
        if not self._pages:
            return await interaction.response.send_message("Still loading…", ephemeral=True)

        start      = self.current * PER_PAGE
        page_moves = self._filtered_raw[start:start + PER_PAGE]
        if not page_moves:
            return await interaction.response.send_message("No moves on this page.", ephemeral=True)

        options = []
        for slug, method, level in page_moves:
            label  = slug.replace("-", " ").title()[:100]
            detail = self._details.get(slug)
            mtype  = detail.get("type", {}).get("name", "normal") if detail else "normal"
            bp     = detail.get("power")    if detail else None
            acc    = detail.get("accuracy") if detail else None
            pp     = detail.get("pp")       if detail else None
            te     = TYPE_EMOJI.get(mtype, "❓")

            parts = []
            if bp:  parts.append(f"{bp}bp")
            if acc: parts.append(f"{acc}%")
            if pp:  parts.append(f"PP{pp}")
            desc = "  ".join(parts) if parts else "Status move"

            options.append(discord.SelectOption(
                label=label, value=slug, emoji=te, description=desc[:100]
            ))

        select          = discord.ui.Select(placeholder="Pick a move for full details…", options=options[:25])
        picker_view     = discord.ui.View(timeout=60)
        picker_view.add_item(select)

        async def on_select(sel_interaction: discord.Interaction):
            chosen = select.values[0]
            detail = self._details.get(chosen) or await pokeapi.get_move(chosen)
            if not detail:
                return await sel_interaction.response.send_message("Couldn't load move.", ephemeral=True)

            move_embed  = _move_detail_embed(detail)
            share_view  = discord.ui.View(timeout=60)
            share_btn   = discord.ui.Button(label="📢 Share to Chat", style=discord.ButtonStyle.primary)

            async def on_share(share_interaction: discord.Interaction):
                await share_interaction.response.send_message(embed=_move_detail_embed(detail))
                share_btn.disabled = True
                share_btn.label    = "✅ Shared!"
                await sel_interaction.edit_original_response(view=share_view)

            share_btn.callback = on_share
            share_view.add_item(share_btn)
            await sel_interaction.response.send_message(embed=move_embed, view=share_view, ephemeral=True)

        select.callback = on_select
        await interaction.response.send_message("Select a move:", view=picker_view, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class MovesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="pokemon_moves",
        description="List moves a Pokémon can learn — with type, power, accuracy and PP.",
    )
    @app_commands.describe(
        pokemon="Pokémon name or Dex number",
        learn_method="Filter: level-up, machine, or egg (optional)",
    )
    @app_commands.autocomplete(pokemon=pokemon_ac, learn_method=learn_method_ac)
    async def moves_cmd(
        self,
        interaction:  discord.Interaction,
        pokemon:      str,
        learn_method: Optional[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        data = await pokeapi.get_pokemon(normalize(pokemon))
        if not data:
            return await interaction.followup.send(
                embed=error_embed("Not Found", f"**{pokemon}** wasn't found."), ephemeral=True
            )

        name   = data["name"].replace("-", " ").title()
        types  = [t["type"]["name"] for t in data["types"]]
        colour = type_colour(types[0])
        raw    = _collect(data.get("moves", []), learn_method)

        if not raw:
            label = learn_method.replace("-", " ").title() if learn_method else "any method"
            return await interaction.followup.send(
                embed=error_embed("No Moves", f"**{name}** has no moves via **{label}**."),
                ephemeral=True,
            )

        method_tag = f" — {learn_method.replace('-', ' ').title()}" if learn_method else ""
        view       = MovesView(raw_moves=raw, title=f"{name} — Moves{method_tag}", colour=colour)

        await view.ensure_fetched()
        view._sync_buttons()
        await interaction.followup.send(embed=view.embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(MovesCog(bot))
