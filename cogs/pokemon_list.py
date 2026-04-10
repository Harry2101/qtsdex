"""
cogs/pokemon_list.py
Commands for managing the Pokemon list system (category lists + events).

Subgroups:
  /pokemon_list <category> add/remove/view   -- manage rare/gmax/eevo/regional lists
  /pokemon_list event create/delete/add/remove/view/list -- manage events

Adding a Pokemon auto-fetches ALL its forms from PokeAPI.
Events contain custom strings (no form grouping).
"""

import logging

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from services import pokemon_list_db
from services.guild_settings_db import make_footer
from utils.icc_checks import is_icc_admin
from utils import pokeapi

log = logging.getLogger("qtsdex.pokemon_list")


# -- PokeAPI form fetcher ----------------------------------------------------

async def _fetch_forms(pokemon_name: str) -> list[str] | None:
    """
    Fetch all forms for a Pokemon from PokeAPI (via pokemon-species endpoint).
    Returns a list of form names, or None if the species doesn't exist.
    The base name is always included.
    """
    species = await pokeapi.fetch(f"pokemon-species/{pokemon_name.lower()}")
    if not species:
        return None

    forms = []
    base = species.get("name", pokemon_name.lower()).title()
    forms.append(base)

    # Get varieties (e.g. Mega, Alolan, etc.)
    for variety in species.get("varieties", []):
        poke_data = await pokeapi.fetch(variety["pokemon"]["url"])
        if not poke_data:
            continue
        # Get form names from the forms list
        for form_ref in poke_data.get("forms", []):
            form_data = await pokeapi.fetch(form_ref["url"])
            if not form_data:
                continue
            form_name = form_data.get("form_name", "")
            if form_name:
                nice_name = f"{base} {form_name.replace('-', ' ').title()}"
                if nice_name not in forms:
                    forms.append(nice_name)
            else:
                # Default form — use the variety name if different
                variety_name = variety["pokemon"]["name"]
                if variety_name != species["name"]:
                    nice = variety_name.replace("-", " ").title()
                    if nice not in forms:
                        forms.append(nice)

    return forms


# -- Autocomplete helpers ----------------------------------------------------

async def _category_type_autocomplete(
    interaction: Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    types = list(pokemon_list_db.CATEGORY_TYPES)
    return [
        app_commands.Choice(name=t, value=t)
        for t in types if current.lower() in t.lower()
    ][:25]


async def _pokemon_autocomplete(
    interaction: Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    names = await pokemon_list_db.get_all_base_names()
    return [
        app_commands.Choice(name=n, value=n)
        for n in names if current.lower() in n.lower()
    ][:25]


async def _event_autocomplete(
    interaction: Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    events = await pokemon_list_db.get_all_events()
    return [
        app_commands.Choice(name=e["name"], value=e["name"])
        for e in events if current.lower() in e["name"].lower()
    ][:25]


async def _event_entry_autocomplete(
    interaction: Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    # Try to get event name from the other option
    event_name = None
    for opt in interaction.data.get("options", []):
        # Walk subcommand options
        if "options" in opt:
            for sub in opt["options"]:
                if "options" in sub:
                    for param in sub["options"]:
                        if param["name"] == "event" and param.get("value"):
                            event_name = param["value"]
    if not event_name:
        return []
    event = await pokemon_list_db.get_event(event_name)
    if not event:
        return []
    entries = await pokemon_list_db.get_event_entries(event["id"])
    return [
        app_commands.Choice(name=e, value=e)
        for e in entries if current.lower() in e.lower()
    ][:25]


# -- Cog ---------------------------------------------------------------------

class PokemonListCog(commands.Cog):
    """Pokemon list management for ICC reserves."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- Group tree -----------------------------------------------------------

    plist = app_commands.Group(name="pokemon_list", description="Manage Pokemon lists for Org")
    event_group = app_commands.Group(name="event", description="Manage Pokemon events", parent=plist)

    # -- /pokemon_list add ---------------------------------------------------

    @plist.command(name="add", description="Add a Pokemon to a category list (auto-fetches all forms)")
    @app_commands.describe(
        category="Category type (rare/gmax/eevo/regional)",
        name="Pokemon name (base species)",
    )
    @app_commands.autocomplete(category=_category_type_autocomplete)
    async def plist_add(self, interaction: Interaction, category: str, name: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        if category not in pokemon_list_db.CATEGORY_TYPES:
            return await interaction.response.send_message(
                f"Invalid category. Use one of: {', '.join(pokemon_list_db.CATEGORY_TYPES)}", ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        forms = await _fetch_forms(name)
        if forms is None:
            return await interaction.followup.send(
                f"**{name}** not found on PokeAPI. Check the spelling.", ephemeral=True,
            )

        base_name = forms[0]  # Title-cased base name
        added, existed = await pokemon_list_db.add_pokemon(base_name, forms, category)

        form_list = ", ".join(forms[:20])
        if len(forms) > 20:
            form_list += f" +{len(forms) - 20} more"

        await interaction.followup.send(
            f"Added **{base_name}** to **{category}** ({added} new, {existed} existed).\n"
            f"Forms: {form_list}",
            ephemeral=True,
        )

    # -- /pokemon_list remove ------------------------------------------------

    @plist.command(name="remove", description="Remove a Pokemon and all its forms from all lists")
    @app_commands.describe(name="Pokemon base name")
    @app_commands.autocomplete(name=_pokemon_autocomplete)
    async def plist_remove(self, interaction: Interaction, name: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        count = await pokemon_list_db.remove_pokemon(name)
        if count == 0:
            return await interaction.response.send_message(f"**{name}** not found in any list.", ephemeral=True)
        await interaction.response.send_message(f"Removed **{name}** ({count} form entries deleted).", ephemeral=True)

    # -- /pokemon_list view --------------------------------------------------

    @plist.command(name="view", description="View Pokemon in a category list")
    @app_commands.describe(category="Category type (rare/gmax/eevo/regional)")
    @app_commands.autocomplete(category=_category_type_autocomplete)
    async def plist_view(self, interaction: Interaction, category: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        if category not in pokemon_list_db.CATEGORY_TYPES:
            return await interaction.response.send_message("Invalid category type.", ephemeral=True)

        entries = await pokemon_list_db.get_pokemon_list(category)
        if not entries:
            return await interaction.response.send_message(f"No Pokemon in **{category}** list.", ephemeral=True)

        # Group by base_name
        groups: dict[str, list[str]] = {}
        for e in entries:
            groups.setdefault(e["base_name"], []).append(e["form_name"])

        lines = []
        for base, forms in sorted(groups.items()):
            form_str = ", ".join(forms[:5])
            if len(forms) > 5:
                form_str += f" +{len(forms) - 5}"
            lines.append(f"**{base}** ({len(forms)} forms): {form_str}")

        desc = "\n".join(lines[:30])
        if len(lines) > 30:
            desc += f"\n+{len(lines) - 30} more..."

        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        embed = discord.Embed(
            title=f"Pokemon List \u2014 {category.upper()}",
            description=desc,
            colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, f"{len(groups)} species"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- /pokemon_list event create ------------------------------------------

    @event_group.command(name="create", description="Create a new event")
    @app_commands.describe(name="Event name (e.g. Valentine's 2025)")
    async def event_create(self, interaction: Interaction, name: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        eid = await pokemon_list_db.create_event(name)
        if eid is None:
            return await interaction.response.send_message(f"Event **{name}** already exists.", ephemeral=True)
        await interaction.response.send_message(f"Event **{name}** created (ID {eid}).", ephemeral=True)

    # -- /pokemon_list event delete ------------------------------------------

    @event_group.command(name="delete", description="Delete an event and all its Pokemon")
    @app_commands.describe(event="Event name")
    @app_commands.autocomplete(event=_event_autocomplete)
    async def event_delete(self, interaction: Interaction, event: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        ok = await pokemon_list_db.delete_event(event)
        if not ok:
            return await interaction.response.send_message(f"Event **{event}** not found.", ephemeral=True)
        await interaction.response.send_message(f"Event **{event}** deleted.", ephemeral=True)

    # -- /pokemon_list event add ---------------------------------------------

    @event_group.command(name="add", description="Add a Pokemon name to an event")
    @app_commands.describe(event="Event name", pokemon="Pokemon name string (e.g. Valentine Pikachu)")
    @app_commands.autocomplete(event=_event_autocomplete)
    async def event_add(self, interaction: Interaction, event: str, pokemon: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        ev = await pokemon_list_db.get_event(event)
        if not ev:
            return await interaction.response.send_message(f"Event **{event}** not found.", ephemeral=True)
        ok = await pokemon_list_db.add_event_entry(ev["id"], pokemon)
        if not ok:
            return await interaction.response.send_message(f"**{pokemon}** already in **{event}**.", ephemeral=True)
        await interaction.response.send_message(f"Added **{pokemon}** to **{event}**.", ephemeral=True)

    # -- /pokemon_list event remove ------------------------------------------

    @event_group.command(name="remove", description="Remove a Pokemon from an event")
    @app_commands.describe(event="Event name", pokemon="Pokemon name to remove")
    @app_commands.autocomplete(event=_event_autocomplete, pokemon=_event_entry_autocomplete)
    async def event_remove(self, interaction: Interaction, event: str, pokemon: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        ev = await pokemon_list_db.get_event(event)
        if not ev:
            return await interaction.response.send_message(f"Event **{event}** not found.", ephemeral=True)
        ok = await pokemon_list_db.remove_event_entry(ev["id"], pokemon)
        if not ok:
            return await interaction.response.send_message(f"**{pokemon}** not in **{event}**.", ephemeral=True)
        await interaction.response.send_message(f"Removed **{pokemon}** from **{event}**.", ephemeral=True)

    # -- /pokemon_list event view --------------------------------------------

    @event_group.command(name="view", description="View Pokemon in an event")
    @app_commands.describe(event="Event name")
    @app_commands.autocomplete(event=_event_autocomplete)
    async def event_view(self, interaction: Interaction, event: str):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        ev = await pokemon_list_db.get_event(event)
        if not ev:
            return await interaction.response.send_message(f"Event **{event}** not found.", ephemeral=True)
        entries = await pokemon_list_db.get_event_entries(ev["id"])
        if not entries:
            return await interaction.response.send_message(f"No Pokemon in **{event}**.", ephemeral=True)

        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        status = "Active" if ev["active"] else "Inactive"
        embed = discord.Embed(
            title=f"Event \u2014 {ev['name']} [{status}]",
            description="\n".join(f"\u2022 {e}" for e in entries),
            colour=0x57F287 if ev["active"] else 0xED4245,
        )
        embed.set_footer(text=make_footer(guild_id, f"{len(entries)} Pokemon"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- /pokemon_list event list --------------------------------------------

    @event_group.command(name="list", description="List all events")
    async def event_list(self, interaction: Interaction):
        if not await is_icc_admin(interaction):
            return await interaction.response.send_message("You need Org admin permissions.", ephemeral=True)
        events = await pokemon_list_db.get_all_events()
        if not events:
            return await interaction.response.send_message("No events configured.", ephemeral=True)

        lines = []
        for ev in events:
            entries = await pokemon_list_db.get_event_entries(ev["id"])
            status = "\u2705" if ev["active"] else "\u274c"
            lines.append(f"{status} **{ev['name']}** \u2014 {len(entries)} Pokemon")

        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        embed = discord.Embed(
            title="Pokemon Events",
            description="\n".join(lines),
            colour=0x5865F2,
        )
        embed.set_footer(text=make_footer(guild_id, f"{len(events)} events"))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PokemonListCog(bot))
