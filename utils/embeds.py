"""
utils/embeds.py
Shared embed helpers, colours, emoji, and stat rendering.
"""

import discord
from services.guild_settings_db import make_footer

FOOTER = "King's Dex  •  powered by PokéAPI"  # fallback constant — use make_footer(guild_id) when guild is known

TYPE_COLOURS: dict[str, int] = {
    "normal":   0xA8A77A, "fire":     0xEE8130, "water":    0x6390F0,
    "electric": 0xF7D02C, "grass":    0x7AC74C, "ice":      0x96D9D6,
    "fighting": 0xC22E28, "poison":   0xA33EA1, "ground":   0xE2BF65,
    "flying":   0xA98FF3, "psychic":  0xF95587, "bug":      0xA6B91A,
    "rock":     0xB6A136, "ghost":    0x735797, "dragon":   0x6F35FC,
    "dark":     0x705746, "steel":    0xB7B7CE, "fairy":    0xD685AD,
}

TYPE_EMOJI: dict[str, str] = {
    "normal":   "⬜", "fire":     "🔥", "water":    "💧",
    "electric": "⚡", "grass":    "🌿", "ice":      "❄️",
    "fighting": "🥊", "poison":   "☠️", "ground":   "🌍",
    "flying":   "🌬️", "psychic":  "🔮", "bug":      "🐛",
    "rock":     "🪨", "ghost":    "👻", "dragon":   "🐉",
    "dark":     "🌑", "steel":    "⚙️", "fairy":    "✨",
}

DAMAGE_CLASS_EMOJI: dict[str, str] = {
    "physical": "💥",
    "special":  "✨",
    "status":   "📊",
}

STAT_KEYS_ORDERED = [
    "hp", "attack", "defense", "special-attack", "special-defense", "speed",
]

STAT_EMOJI: dict[str, str] = {
    "hp":              "❤️",
    "attack":          "⚔️",
    "defense":         "🛡️",
    "special-attack":  "🔥",
    "special-defense": "💎",
    "speed":           "💨",
}

STAT_LABEL: dict[str, str] = {
    "hp":              "HP",
    "attack":          "Atk",
    "defense":         "Def",
    "special-attack":  "Sp.Atk",
    "special-defense": "Sp.Def",
    "speed":           "Speed",
}

_BAR_MAX = 255
_BAR_LEN = 8


def type_colour(t: str) -> int:
    return TYPE_COLOURS.get(t.lower(), 0x5865F2)

def type_badge(t: str) -> str:
    return f"{TYPE_EMOJI.get(t.lower(), '❓')} {t.title()}"

def type_badges(types: list[str]) -> str:
    return "  ".join(type_badge(t) for t in types)

def error_embed(title: str, desc: str, guild_id: str = "") -> discord.Embed:
    e = discord.Embed(title=f"❌  {title}", description=desc, colour=0xED4245)
    e.set_footer(text=make_footer(guild_id))
    return e

def base_embed(title: str, desc: str = "", colour: int = 0x5865F2, guild_id: str = "") -> discord.Embed:
    e = discord.Embed(title=title, description=desc, colour=colour)
    e.set_footer(text=make_footer(guild_id))
    return e


def _bar(value: int) -> str:
    filled = round((value / _BAR_MAX) * _BAR_LEN)
    return "█" * filled + "░" * (_BAR_LEN - filled)


def build_stat_lines(stats: dict[str, int], total: int) -> str:
    """
    Plain text stat block — emojis + labels + bar + value on each line.
    Always shows both bar chart and numeric value.
    """
    lines = []
    for key in STAT_KEYS_ORDERED:
        val   = max(0, stats.get(key, 0))  # clamp to 0
        emoji = STAT_EMOJI[key]
        label = f"{STAT_LABEL[key]:<7}"    # fixed width label (ASCII only, so safe)
        lines.append(f"{emoji} `{label}` {_bar(val)} **{val}**")
    lines.append(f"📊 `{'BST':<7}` **{total}**")
    return "\n".join(lines)


# Keep old name as alias so nothing else breaks
def build_stat_block(stats: dict[str, int], total: int) -> str:
    return build_stat_lines(stats, total)

def stat_emoji_labels() -> str:
    """Legacy — kept for compatibility, not used in new layout."""
    lines = [f"{STAT_EMOJI[k]} {STAT_LABEL[k]}" for k in STAT_KEYS_ORDERED]
    lines.append("📊 BST")
    return "\n".join(lines)
