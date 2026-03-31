"""
cogs/events.py  —  Shiny Hunt Checklists
Global checklists (shared across all servers).
Two lists: "normal" (permanent grind) and "event" (one active event).
Per-user catch tracking. Clean display, sort buttons, switch button.
"""

import asyncio
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import events_db
from utils import pokeapi
from utils.autocomplete import pokemon_ac
from utils.embeds import FOOTER, error_embed, type_colour
from utils.normalizer import normalize

# Global guild ID — all servers share the same checklists
GLOBAL_ID = "global"

LIST_CHOICES = [
    app_commands.Choice(name="🎯 Normal Grind", value="normal"),
    app_commands.Choice(name="✨ Event Hunt",   value="event"),
]

SORT_LABELS = {
    "alpha": "🔤 A–Z",
    "dex":   "#️⃣ Dex",
    "evo":   "🧬 Evo",
}


# ── Autocomplete ──────────────────────────────────────────────────────────────

async def _list_pokemon_ac(interaction: discord.Interaction, current: str):
    ctype = getattr(interaction.namespace, "list_type", "normal") or "normal"
    cl    = await events_db.ensure_checklist(GLOBAL_ID, ctype)
    names = await events_db.get_target_names(cl["id"])
    return [
        app_commands.Choice(name=n.replace("-", " ").title(), value=n)
        for n in names if current.lower() in n
    ][:25]


# ── Evo chain ─────────────────────────────────────────────────────────────────

async def _get_evo_chain(slug: str) -> tuple[list[str], int]:
    species = await pokeapi.fetch(f"pokemon-species/{slug}")
    if not species:
        return [slug], 0
    chain_url = species.get("evolution_chain", {}).get("url", "")
    if not chain_url:
        return [slug], 0
    chain_data = await pokeapi.fetch(chain_url)
    if not chain_data:
        return [slug], 0
    evo_id = int(chain_url.rstrip("/").split("/")[-1])
    slugs: list[str] = []

    def walk(node: dict):
        slugs.append(node["species"]["name"])
        for evo in node.get("evolves_to", []):
            walk(evo)

    walk(chain_data.get("chain", {}))
    return slugs, evo_id


async def _get_dex_id(slug: str) -> int:
    data = await pokeapi.get_pokemon(slug)
    return data["id"] if data else 0


async def _get_sprite(slug: str) -> Optional[str]:
    data = await pokeapi.get_pokemon(slug)
    if not data:
        return None
    return (
        data.get("sprites", {}).get("other", {})
        .get("official-artwork", {}).get("front_default")
        or data.get("sprites", {}).get("front_default")
    )


# ── Input parser ──────────────────────────────────────────────────────────────

def _parse_bulk(text: str) -> list[tuple[str, bool]]:
    text  = text.replace("\n", ",").replace(";", ",")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    out: list[tuple[str, bool]] = []
    for part in parts:
        evo_flag = bool(re.match(r"^-{1,2}evo\s+", part, re.IGNORECASE))
        clean    = re.sub(r"^-{1,2}evo\s+", "", part, flags=re.IGNORECASE).strip()
        if clean:
            out.append((normalize(clean), evo_flag))
    return out


# ── Sort ──────────────────────────────────────────────────────────────────────

def _sort_targets(targets: list[dict], sort: str) -> list[dict]:
    if sort == "dex":
        return sorted(targets, key=lambda t: t.get("dex_id", 0))
    if sort == "evo":
        return sorted(targets, key=lambda t: (t.get("evo_family_id", 0), t.get("dex_id", 0)))
    return sorted(targets, key=lambda t: t["pokemon"])


# ── Progress bar ──────────────────────────────────────────────────────────────

def _bar(caught: int, total: int, length: int = 12) -> str:
    if total == 0:
        return "`────────────` 0/0"
    filled = round((caught / total) * length)
    pct    = int((caught / total) * 100)
    return f"`{'█'*filled}{'░'*(length-filled)}` {caught}/{total} ({pct}%)"


# ── Checklist embed ───────────────────────────────────────────────────────────

def _checklist_embed(
    ctype:          str,
    label:          str,
    targets:        list[dict],
    caught_ids:     set[int],
    user:           discord.User | discord.Member,
    remaining_only: bool = False,
    sort:           str  = "alpha",
) -> discord.Embed:
    total     = len(targets)
    caught    = len(caught_ids)
    remaining = total - caught
    done      = remaining == 0 and total > 0

    type_label = "✨ Event Hunt" if ctype == "event" else "🎯 Normal Grind"
    event_name = f" — {label.title()}" if (ctype == "event" and label) else ""
    colour     = 0xFFD700 if done else (0x5865F2 if ctype == "normal" else 0xF95587)
    sort_label = SORT_LABELS.get(sort, "🔤 A–Z")

    embed = discord.Embed(title=f"{type_label}{event_name}", colour=colour)

    if total == 0:
        embed.description = "*Empty list.*\nAdd Pokémon with `/checklist add`."
    else:
        status = "🎉 **Complete!**" if done else f"**{remaining}** remaining"
        embed.description = f"{_bar(caught, total)}\n{status}"

        sorted_targets = _sort_targets(targets, sort)
        display = (
            [t for t in sorted_targets if t["id"] not in caught_ids]
            if remaining_only else sorted_targets
        )

        if display:
            lines = []
            last_fam = None
            for t in display:
                icon = "✅" if t["id"] in caught_ids else "⬜"
                name = t["pokemon"].replace("-", " ").title()

                # Show dex number only when sorted by dex
                dex_str = f" `#{t['dex_id']:04d}`" if (sort == "dex" and t.get("dex_id")) else ""

                # Blank separator between evo families when evo-sorted
                if sort == "evo" and t.get("evo_family_id", 0) != last_fam and last_fam is not None:
                    lines.append("")
                last_fam = t.get("evo_family_id", 0) if sort == "evo" else None

                lines.append(f"{icon}{dex_str} {name}")

            # Remove leading blank lines
            while lines and lines[0] == "":
                lines.pop(0)

            # Two columns for long lists
            real_lines = [l for l in lines if l]
            if len(real_lines) > 16:
                mid = (len(lines) + 1) // 2
                embed.add_field(name="\u200b", value="\n".join(lines[:mid]) or "\u200b",  inline=True)
                embed.add_field(name="\u200b", value="\n".join(lines[mid:]) or "\u200b",  inline=True)
            else:
                embed.add_field(name="Pokémon", value="\n".join(lines) or "\u200b", inline=False)
        elif remaining_only:
            embed.add_field(name="\u200b", value="🎉 All caught!", inline=False)

    embed.set_footer(
        text=f"QT's Dex  •  {user.display_name}  •  {sort_label}  •  powered by PokéAPI"
    )
    return embed


# ── Clear confirmation ────────────────────────────────────────────────────────

class ClearConfirmView(discord.ui.View):
    def __init__(self, parent: "ChecklistView"):
        super().__init__(timeout=30)
        self.parent = parent

    @discord.ui.button(label="Yes, clear my catches", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.parent.user.id:
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        await events_db.clear_user_catches(self.parent.checklist_id, str(self.parent.user.id))
        self.parent.caught_ids = set()
        self.parent._build_buttons()
        embed = self.parent._embed()
        embed.title = f"🗑️ Cleared  •  {embed.title}"
        await interaction.response.edit_message(embed=embed, view=self.parent)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.parent.user.id:
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        self.parent._build_buttons()
        await interaction.response.edit_message(embed=self.parent._embed(), view=self.parent)


# ── Checklist view ────────────────────────────────────────────────────────────

class ChecklistView(discord.ui.View):
    def __init__(
        self,
        checklist_id:   int,
        ctype:          str,
        label:          str,
        targets:        list[dict],
        caught_ids:     set[int],
        user:           discord.User | discord.Member,
        remaining_only: bool = False,
        sort:           str  = "alpha",
    ):
        super().__init__(timeout=300)
        self.checklist_id   = checklist_id
        self.ctype          = ctype
        self.label          = label
        self.targets        = targets
        self.caught_ids     = caught_ids
        self.user           = user
        self.remaining_only = remaining_only
        self.sort           = sort
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()

        # Row 0 — catch select
        uncaught = _sort_targets(
            [t for t in self.targets if t["id"] not in self.caught_ids], self.sort
        )
        if uncaught:
            sel = discord.ui.Select(
                placeholder="✅ Mark as caught…",
                options=[
                    discord.SelectOption(
                        label=t["pokemon"].replace("-", " ").title()[:100],
                        value=str(t["id"]), emoji="⬜",
                    )
                    for t in uncaught[:25]
                ],
                row=0,
            )
            sel.callback = self._catch
            self.add_item(sel)

        # Row 1 — uncatch select
        caught_list = _sort_targets(
            [t for t in self.targets if t["id"] in self.caught_ids], self.sort
        )
        if caught_list:
            sel2 = discord.ui.Select(
                placeholder="⬜ Unmark a catch…",
                options=[
                    discord.SelectOption(
                        label=t["pokemon"].replace("-", " ").title()[:100],
                        value=str(t["id"]), emoji="✅",
                    )
                    for t in caught_list[:25]
                ],
                row=1,
            )
            sel2.callback = self._uncatch
            self.add_item(sel2)

        # Row 2 — sort buttons
        for key, lbl in SORT_LABELS.items():
            btn = discord.ui.Button(
                label=lbl,
                style=discord.ButtonStyle.success if key == self.sort else discord.ButtonStyle.secondary,
                row=2,
            )
            btn.callback = self._make_sort_cb(key)
            self.add_item(btn)

        # Row 3 — utility
        toggle = discord.ui.Button(
            label="👁️ Show All" if self.remaining_only else "👁️ Remaining",
            style=discord.ButtonStyle.secondary, row=3,
        )
        toggle.callback = self._toggle_remaining
        self.add_item(toggle)

        other      = "event" if self.ctype == "normal" else "normal"
        switch_lbl = "✨ Event" if other == "event" else "🎯 Normal"
        switch     = discord.ui.Button(label=f"→ {switch_lbl}", style=discord.ButtonStyle.primary, row=3)
        switch.callback = self._switch
        self.add_item(switch)

        if self.caught_ids:
            clear = discord.ui.Button(label="🗑️ Clear", style=discord.ButtonStyle.danger, row=3)
            clear.callback = self._clear
            self.add_item(clear)

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    def _embed(self) -> discord.Embed:
        return _checklist_embed(
            self.ctype, self.label, self.targets, self.caught_ids,
            self.user, self.remaining_only, self.sort,
        )

    async def _refresh(self, interaction: discord.Interaction):
        self.caught_ids = await events_db.get_user_catches(
            self.checklist_id, str(self.user.id)
        )
        self._build_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _catch(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        await events_db.mark_caught(
            self.checklist_id, int(interaction.data["values"][0]), str(self.user.id)
        )
        await self._refresh(interaction)

    async def _uncatch(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        await events_db.unmark_caught(
            self.checklist_id, int(interaction.data["values"][0]), str(self.user.id)
        )
        await self._refresh(interaction)

    def _make_sort_cb(self, key: str):
        async def cb(interaction: discord.Interaction):
            if not self._guard(interaction):
                return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
            self.sort = key
            self._build_buttons()
            await interaction.response.edit_message(embed=self._embed(), view=self)
        return cb

    async def _toggle_remaining(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        self.remaining_only = not self.remaining_only
        self._build_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _switch(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        await interaction.response.defer()
        new_type   = "event" if self.ctype == "normal" else "normal"
        cl         = await events_db.ensure_checklist(GLOBAL_ID, new_type)
        targets    = await events_db.get_targets(cl["id"])
        caught_ids = await events_db.get_user_catches(cl["id"], str(self.user.id))
        self.checklist_id   = cl["id"]
        self.ctype          = new_type
        self.label          = cl["label"]
        self.targets        = targets
        self.caught_ids     = caught_ids
        self.remaining_only = False
        self._build_buttons()
        await interaction.edit_original_response(embed=self._embed(), view=self)

    async def _clear(self, interaction: discord.Interaction):
        if not self._guard(interaction):
            return await interaction.response.send_message("This isn't your checklist.", ephemeral=True)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⚠️ Clear all your catches?",
                description=(
                    f"Resets **{len(self.caught_ids)} catches**.\n"
                    "Targets stay — only your progress is cleared."
                ),
                colour=0xFEE75C,
            ),
            view=ClearConfirmView(parent=self),
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="event_setup",
        description="Set the active event hunt name (global, shared across all servers).",
    )
    @app_commands.describe(name="Event name e.g. 'Community Day May 2025'")
    async def event_setup(self, interaction: discord.Interaction, name: str):
        await events_db.rename_checklist(GLOBAL_ID, "event", name)
        embed = discord.Embed(
            title="✨ Event Updated",
            description=f"Active event is now **{name}** (visible everywhere).",
            colour=0xF95587,
        )
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed)

    checklist = app_commands.Group(
        name="checklist",
        description="Manage your global shiny hunt checklists.",
    )

    # /checklist add ──────────────────────────────────────────────────────────

    @checklist.command(name="add", description="Add Pokémon. Supports bulk CSV and --evo chains.")
    @app_commands.describe(
        pokemon="Names, CSV, or '--evo name' for full evo chains",
        list_type="Which checklist",
        include_evolutions="Add full evo chains for every Pokémon listed",
    )
    @app_commands.choices(list_type=LIST_CHOICES)
    async def cl_add(
        self,
        interaction:        discord.Interaction,
        pokemon:            str,
        list_type:          str  = "normal",
        include_evolutions: bool = False,
    ):
        await interaction.response.defer(thinking=True)

        cl     = await events_db.ensure_checklist(GLOBAL_ID, list_type)
        parsed = _parse_bulk(pokemon)
        if not parsed:
            return await interaction.followup.send(
                embed=error_embed("Bad Input", "Couldn't parse any Pokémon names."), ephemeral=True
            )

        to_add: list[tuple[str, int, int]] = []
        not_found: list[str] = []

        async def resolve(slug: str, want_evo: bool):
            if want_evo or include_evolutions:
                chain, fam_id = await _get_evo_chain(slug)
                for s in chain:
                    dex = await _get_dex_id(s)
                    to_add.append((s, dex, fam_id))
            else:
                dex = await _get_dex_id(slug)
                if dex == 0:
                    not_found.append(slug)
                else:
                    to_add.append((slug, dex, 0))

        await asyncio.gather(*[resolve(slug, evo) for slug, evo in parsed])

        # Deduplicate
        seen: set[str] = set()
        unique = [item for item in to_add if not (item[0] in seen or seen.add(item[0]))]

        added: list[str] = []
        skipped: list[str] = []
        for slug, dex_id, evo_fam in unique:
            ok, _ = await events_db.add_pokemon(cl["id"], slug, dex_id, evo_fam)
            (added if ok else skipped).append(slug.replace("-", " ").title())

        total_now  = await events_db.count_targets(cl["id"])
        list_label = "✨ Event Hunt" if list_type == "event" else "🎯 Normal Grind"

        embed = discord.Embed(
            title=f"{'✅' if added else '⚠️'}  {list_label}",
            colour=0x57F287 if added else 0xFEE75C,
        )
        if added:
            display = ", ".join(added[:30]) + (f" *+{len(added)-30} more*" if len(added) > 30 else "")
            embed.add_field(name=f"Added ({len(added)})", value=display, inline=False)
        if skipped:
            embed.add_field(name=f"Already on list ({len(skipped)})",
                            value=", ".join(skipped[:15]), inline=False)
        if not_found:
            embed.add_field(name=f"Not found ({len(not_found)})",
                            value=", ".join(not_found[:15]), inline=False)

        embed.set_footer(text=f"QT's Dex  •  {total_now} Pokémon on list  •  powered by PokéAPI")

        if added and unique:
            sprite = await _get_sprite(unique[0][0])
            if sprite:
                embed.set_thumbnail(url=sprite)

        await interaction.followup.send(embed=embed)

    # /checklist remove ───────────────────────────────────────────────────────

    @checklist.command(name="remove", description="Remove a Pokémon from a checklist.")
    @app_commands.describe(list_type="Which checklist", pokemon="Pokémon to remove")
    @app_commands.choices(list_type=LIST_CHOICES)
    @app_commands.autocomplete(pokemon=_list_pokemon_ac)
    async def cl_remove(
        self,
        interaction: discord.Interaction,
        pokemon:     str,
        list_type:   str = "normal",
    ):
        cl      = await events_db.ensure_checklist(GLOBAL_ID, list_type)
        ok, msg = await events_db.remove_pokemon(cl["id"], pokemon)
        await interaction.response.send_message(
            embed=discord.Embed(description=msg, colour=0x57F287 if ok else 0xED4245),
            ephemeral=not ok,
        )

    # /checklist view ─────────────────────────────────────────────────────────

    @checklist.command(name="view", description="View your shiny hunt checklist.")
    @app_commands.describe(list_type="Which checklist to view")
    @app_commands.choices(list_type=LIST_CHOICES)
    async def cl_view(
        self,
        interaction: discord.Interaction,
        list_type:   str = "normal",
    ):
        await interaction.response.defer(thinking=True)
        cl         = await events_db.ensure_checklist(GLOBAL_ID, list_type)
        targets    = await events_db.get_targets(cl["id"])
        caught_ids = await events_db.get_user_catches(cl["id"], str(interaction.user.id))
        view       = ChecklistView(
            checklist_id=cl["id"], ctype=list_type, label=cl["label"],
            targets=targets, caught_ids=caught_ids, user=interaction.user,
        )
        await interaction.followup.send(embed=view._embed(), view=view)

    # /checklist catch ────────────────────────────────────────────────────────

    @checklist.command(name="catch", description="Mark a Pokémon as caught.")
    @app_commands.describe(list_type="Which checklist", pokemon="Pokémon you caught")
    @app_commands.choices(list_type=LIST_CHOICES)
    @app_commands.autocomplete(pokemon=_list_pokemon_ac)
    async def cl_catch(
        self,
        interaction: discord.Interaction,
        pokemon:     str,
        list_type:   str = "normal",
    ):
        await interaction.response.defer(thinking=True)
        cl     = await events_db.ensure_checklist(GLOBAL_ID, list_type)
        target = await events_db.get_target_by_pokemon(cl["id"], pokemon)
        if not target:
            return await interaction.followup.send(
                embed=error_embed("Not on List",
                    f"**{pokemon.replace('-',' ').title()}** isn't on this checklist."),
                ephemeral=True,
            )
        await events_db.mark_caught(cl["id"], target["id"], str(interaction.user.id))
        targets    = await events_db.get_targets(cl["id"])
        caught_ids = await events_db.get_user_catches(cl["id"], str(interaction.user.id))
        view       = ChecklistView(
            checklist_id=cl["id"], ctype=list_type, label=cl["label"],
            targets=targets, caught_ids=caught_ids, user=interaction.user,
        )
        await interaction.followup.send(embed=view._embed(), view=view)

    # /checklist uncatch ──────────────────────────────────────────────────────

    @checklist.command(name="uncatch", description="Unmark a caught Pokémon.")
    @app_commands.describe(list_type="Which checklist", pokemon="Pokémon to unmark")
    @app_commands.choices(list_type=LIST_CHOICES)
    @app_commands.autocomplete(pokemon=_list_pokemon_ac)
    async def cl_uncatch(
        self,
        interaction: discord.Interaction,
        pokemon:     str,
        list_type:   str = "normal",
    ):
        await interaction.response.defer(thinking=True)
        cl     = await events_db.ensure_checklist(GLOBAL_ID, list_type)
        target = await events_db.get_target_by_pokemon(cl["id"], pokemon)
        if not target:
            return await interaction.followup.send(
                embed=error_embed("Not on List",
                    f"**{pokemon.replace('-',' ').title()}** isn't on this checklist."),
                ephemeral=True,
            )
        await events_db.unmark_caught(cl["id"], target["id"], str(interaction.user.id))
        targets    = await events_db.get_targets(cl["id"])
        caught_ids = await events_db.get_user_catches(cl["id"], str(interaction.user.id))
        view       = ChecklistView(
            checklist_id=cl["id"], ctype=list_type, label=cl["label"],
            targets=targets, caught_ids=caught_ids, user=interaction.user,
        )
        await interaction.followup.send(embed=view._embed(), view=view)

    # /checklist remaining ────────────────────────────────────────────────────

    @checklist.command(name="remaining", description="Show only Pokémon you haven't caught yet.")
    @app_commands.describe(list_type="Which checklist")
    @app_commands.choices(list_type=LIST_CHOICES)
    async def cl_remaining(
        self,
        interaction: discord.Interaction,
        list_type:   str = "normal",
    ):
        await interaction.response.defer(thinking=True)
        cl         = await events_db.ensure_checklist(GLOBAL_ID, list_type)
        targets    = await events_db.get_targets(cl["id"])
        caught_ids = await events_db.get_user_catches(cl["id"], str(interaction.user.id))
        view       = ChecklistView(
            checklist_id=cl["id"], ctype=list_type, label=cl["label"],
            targets=targets, caught_ids=caught_ids, user=interaction.user,
            remaining_only=True,
        )
        await interaction.followup.send(embed=view._embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
