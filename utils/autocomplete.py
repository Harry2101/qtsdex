"""
utils/autocomplete.py
Central autocomplete handlers for all slash commands.
Lists are lazily fetched once per session and reused.
"""

import logging
from typing import Optional

import discord
from discord import app_commands

from utils import pokeapi

log = logging.getLogger("qtsdex.autocomplete")

# Lazy-loaded name lists
_pokemon: list[str] = []
_moves: list[str] = []
_abilities: list[str] = []
_items: list[str] = []

TYPES = [
    "normal","fire","water","electric","grass","ice","fighting","poison",
    "ground","flying","psychic","bug","rock","ghost","dragon","dark","steel","fairy",
]

LEARN_METHODS = ["level-up", "machine", "egg"]


def _filter(names: list[str], query: str, limit: int = 25) -> list[str]:
    q = query.lower().strip()
    if not q:
        return names[:limit]
    # Also try hyphenated form so "solar beam" matches "solar-beam"
    q_hyph = q.replace(" ", "-")
    prefix = [n for n in names if n.startswith(q) or n.startswith(q_hyph)]
    contains = [
        n for n in names
        if (q in n or q_hyph in n) and not n.startswith(q) and not n.startswith(q_hyph)
    ]
    return (prefix + contains)[:limit]


def _choices(names: list[str]) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=n.replace("-", " ").title(), value=n)
        for n in names
    ]


# ── Loaders ──────────────────────────────────────────────────────────────────

async def _load_pokemon():
    global _pokemon
    if not _pokemon:
        _pokemon = await pokeapi.get_resource_list("pokemon", limit=10000)

async def _load_moves():
    global _moves
    if not _moves:
        _moves = await pokeapi.get_resource_list("move", limit=10000)

async def _load_abilities():
    global _abilities
    if not _abilities:
        _abilities = await pokeapi.get_resource_list("ability", limit=10000)

async def _load_items():
    global _items
    if not _items:
        _items = await pokeapi.get_resource_list("item", limit=2000)


# ── Autocomplete callbacks ────────────────────────────────────────────────────

async def pokemon_ac(interaction: discord.Interaction, current: str):
    await _load_pokemon()
    return _choices(_filter(_pokemon, current))

async def move_ac(interaction: discord.Interaction, current: str):
    await _load_moves()
    return _choices(_filter(_moves, current))

async def ability_ac(interaction: discord.Interaction, current: str):
    await _load_abilities()
    return _choices(_filter(_abilities, current))

async def type_ac(interaction: discord.Interaction, current: str):
    return _choices(_filter(TYPES, current))

async def learn_method_ac(interaction: discord.Interaction, current: str):
    return _choices(_filter(LEARN_METHODS, current))

async def item_ac(interaction: discord.Interaction, current: str):
    await _load_items()
    return _choices(_filter(_items, current))
