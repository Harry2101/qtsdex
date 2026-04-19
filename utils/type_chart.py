"""
utils/type_chart.py
Gen 6+ type effectiveness — baked in, zero API calls needed.
"""

ALL_TYPES = [
    "normal","fire","water","electric","grass","ice","fighting","poison",
    "ground","flying","psychic","bug","rock","ghost","dragon","dark","steel","fairy",
]

# attack → {defend: multiplier}  (only non-1.0 entries)
_CHART: dict[str, dict[str, float]] = {
    "normal":   {"rock": 0.5, "ghost": 0.0, "steel": 0.5},
    "fire":     {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 2.0, "bug": 2.0, "rock": 0.5, "dragon": 0.5, "steel": 2.0},
    "water":    {"fire": 2.0, "water": 0.5, "grass": 0.5, "ground": 2.0, "rock": 2.0, "dragon": 0.5},
    "electric": {"water": 2.0, "electric": 0.5, "grass": 0.5, "ground": 0.0, "flying": 2.0, "dragon": 0.5},
    "grass":    {"fire": 0.5, "water": 2.0, "grass": 0.5, "poison": 0.5, "ground": 2.0, "flying": 0.5, "bug": 0.5, "rock": 2.0, "dragon": 0.5, "steel": 0.5},
    "ice":      {"fire": 0.5, "water": 0.5, "grass": 2.0, "ice": 0.5, "ground": 2.0, "flying": 2.0, "dragon": 2.0, "steel": 0.5},
    "fighting": {"normal": 2.0, "ice": 2.0, "poison": 0.5, "flying": 0.5, "psychic": 0.5, "bug": 0.5, "rock": 2.0, "ghost": 0.0, "dark": 2.0, "steel": 2.0, "fairy": 0.5},
    "poison":   {"grass": 2.0, "poison": 0.5, "ground": 0.5, "rock": 0.5, "ghost": 0.5, "steel": 0.0, "fairy": 2.0},
    "ground":   {"fire": 2.0, "electric": 2.0, "grass": 0.5, "poison": 2.0, "flying": 0.0, "bug": 0.5, "rock": 2.0, "steel": 2.0},
    "flying":   {"electric": 0.5, "grass": 2.0, "fighting": 2.0, "bug": 2.0, "rock": 0.5, "steel": 0.5},
    "psychic":  {"fighting": 2.0, "poison": 2.0, "psychic": 0.5, "dark": 0.0, "steel": 0.5},
    "bug":      {"fire": 0.5, "grass": 2.0, "fighting": 0.5, "poison": 0.5, "flying": 0.5, "psychic": 2.0, "ghost": 0.5, "dark": 2.0, "steel": 0.5, "fairy": 0.5},
    "rock":     {"fire": 2.0, "ice": 2.0, "fighting": 0.5, "ground": 0.5, "flying": 2.0, "bug": 2.0, "steel": 0.5},
    "ghost":    {"normal": 0.0, "psychic": 2.0, "ghost": 2.0, "dark": 0.5},
    "dragon":   {"dragon": 2.0, "steel": 0.5, "fairy": 0.0},
    "dark":     {"fighting": 0.5, "psychic": 2.0, "ghost": 2.0, "dark": 0.5, "fairy": 0.5},
    "steel":    {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2.0, "rock": 2.0, "steel": 0.5, "fairy": 2.0, "poison": 0.0, "grass": 0.5, "flying": 0.5, "normal": 0.5, "psychic": 0.5, "bug": 0.5, "dragon": 0.5, "dark": 0.5},
    "fairy":    {"fire": 0.5, "fighting": 2.0, "poison": 0.5, "dragon": 2.0, "dark": 2.0, "steel": 0.5},
}


def effectiveness(atk: str, dfn: str) -> float:
    return _CHART.get(atk.lower(), {}).get(dfn.lower(), 1.0)


def dual_effectiveness(atk: str, t1: str, t2: str | None = None) -> float:
    m = effectiveness(atk, t1)
    if t2:
        m *= effectiveness(atk, t2)
    return m


def defending_chart(t1: str, t2: str | None = None) -> dict[str, float]:
    return {atk: dual_effectiveness(atk, t1, t2) for atk in ALL_TYPES}


def group_by_multiplier(t1: str, t2: str | None = None) -> dict[str, list[str]]:
    chart = defending_chart(t1, t2)
    return _bucketize(chart)


def _bucketize(chart: dict[str, float]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {"4x": [], "2x": [], "1x": [], "0.5x": [], "0.25x": [], "0x": []}
    for atk, m in sorted(chart.items()):
        if m >= 4.0:   buckets["4x"].append(atk)
        elif m == 2.0: buckets["2x"].append(atk)
        elif m == 1.0: buckets["1x"].append(atk)
        elif m == 0.5: buckets["0.5x"].append(atk)
        elif m == 0.25: buckets["0.25x"].append(atk)
        elif m == 0.0: buckets["0x"].append(atk)
    return buckets


# Abilities that alter defensive type matchups.
# Each entry maps an ability slug (PokeAPI kebab-case) to a function
# that mutates the chart dict in place.
_ABILITY_IMMUNITIES: dict[str, dict[str, float]] = {
    "levitate":        {"ground": 0.0},
    "flash-fire":      {"fire": 0.0},
    "water-absorb":    {"water": 0.0},
    "dry-skin":        {"water": 0.0},  # fire takes +25% dmg but type mult unchanged
    "storm-drain":     {"water": 0.0},
    "volt-absorb":     {"electric": 0.0},
    "motor-drive":     {"electric": 0.0},
    "lightning-rod":   {"electric": 0.0},
    "sap-sipper":      {"grass": 0.0},
    "earth-eater":     {"ground": 0.0},
    "well-baked-body": {"fire": 0.0},
    "purifying-salt":  {"ghost": 0.5},
    "thick-fat":       {"fire": 0.5, "ice": 0.5},
    "heatproof":       {"fire": 0.5},
    "water-bubble":    {"fire": 0.5},
    "fluffy":          {"fire": 2.0},
}


def apply_ability(chart: dict[str, float], ability: str | None) -> tuple[dict[str, float], str | None]:
    """Return (modified_chart, note). Note is a short string to show in the embed."""
    if not ability:
        return chart, None
    ability = ability.lower().replace("_", "-")

    if ability == "wonder-guard":
        new = {atk: (m if m > 1.0 else 0.0) for atk, m in chart.items()}
        return new, "Wonder Guard — only super-effective moves land."

    if ability in ("filter", "solid-rock", "prism-armor"):
        new = {atk: (m * 0.75 if m > 1.0 else m) for atk, m in chart.items()}
        label = {"filter": "Filter", "solid-rock": "Solid Rock", "prism-armor": "Prism Armor"}[ability]
        return new, f"{label} — super-effective damage reduced (×¾)."

    overrides = _ABILITY_IMMUNITIES.get(ability)
    if overrides is None:
        return chart, None

    new = dict(chart)
    for atk, mult in overrides.items():
        new[atk] = mult

    label = ability.replace("-", " ").title()
    return new, f"{label} — matchups adjusted."


def group_by_multiplier_with_ability(
    t1: str, t2: str | None = None, ability: str | None = None
) -> tuple[dict[str, list[str]], str | None]:
    chart = defending_chart(t1, t2)
    chart, note = apply_ability(chart, ability)
    return _bucketize(chart), note
