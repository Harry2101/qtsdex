"""
utils/meta.py
Curated competitive meta data — common held items by role and type.
PokéAPI has no competitive data, so this is hand-maintained.
"""

# Role → common held items (label, one-liner effect)
ROLE_ITEMS: dict[str, list[tuple[str, str]]] = {
    "sweeper": [
        ("Choice Scarf",  "Boosts Speed 1.5× but locks into one move"),
        ("Choice Band",   "Boosts Attack 1.5× but locks into one move"),
        ("Choice Specs",  "Boosts Sp. Atk 1.5× but locks into one move"),
        ("Life Orb",      "Boosts all moves 1.3× — costs 10% HP per use"),
    ],
    "tank": [
        ("Leftovers",     "Restores 1/16 max HP every turn"),
        ("Rocky Helmet",  "Deals 1/6 HP damage to contact attackers"),
        ("Assault Vest",  "Boosts Sp. Def 1.5× but prevents status moves"),
        ("Eviolite",      "Boosts Def and Sp. Def 1.5× on unevolved Pokémon"),
    ],
    "wall": [
        ("Leftovers",     "Restores 1/16 max HP every turn"),
        ("Shed Shell",    "Allows switching out even when trapped"),
        ("Black Sludge",  "Restores HP for Poison types, damages others"),
        ("Heavy-Duty Boots", "Prevents hazard damage on entry"),
    ],
    "balanced": [
        ("Leftovers",     "Restores 1/16 max HP every turn"),
        ("Life Orb",      "Boosts all moves 1.3× — costs 10% HP per use"),
        ("Heavy-Duty Boots", "Prevents hazard damage on entry"),
        ("Weakness Policy","Sharply boosts Atk and Sp. Atk when hit super effectively"),
    ],
    "default": [
        ("Leftovers",     "Restores 1/16 max HP every turn"),
        ("Life Orb",      "Boosts all moves 1.3× — costs 10% HP per use"),
        ("Choice Scarf",  "Boosts Speed 1.5× but locks into one move"),
        ("Heavy-Duty Boots", "Prevents hazard damage on entry"),
    ],
}

# Type → signature / commonly associated items
TYPE_ITEMS: dict[str, list[tuple[str, str]]] = {
    "fire":     [("Choice Specs", "Sp. Atk ×1.5"), ("Charcoal", "Boosts Fire moves 20%")],
    "water":    [("Mystic Water", "Boosts Water moves 20%"), ("Choice Specs", "Sp. Atk ×1.5")],
    "grass":    [("Miracle Seed", "Boosts Grass moves 20%"), ("Leftovers", "Restores 1/16 HP/turn")],
    "electric": [("Magnet", "Boosts Electric moves 20%"), ("Choice Scarf", "Speed ×1.5")],
    "psychic":  [("Twisted Spoon", "Boosts Psychic moves 20%"), ("Life Orb", "Moves ×1.3")],
    "dragon":   [("Dragon Fang", "Boosts Dragon moves 20%"), ("Choice Scarf", "Speed ×1.5")],
    "dark":     [("Black Glasses", "Boosts Dark moves 20%"), ("Life Orb", "Moves ×1.3")],
    "steel":    [("Metal Coat", "Boosts Steel moves 20%"), ("Assault Vest", "Sp. Def ×1.5")],
    "ghost":    [("Spell Tag", "Boosts Ghost moves 20%"), ("Life Orb", "Moves ×1.3")],
    "ice":      [("Never-Melt Ice", "Boosts Ice moves 20%"), ("Choice Scarf", "Speed ×1.5")],
    "fighting": [("Black Belt", "Boosts Fighting moves 20%"), ("Choice Band", "Attack ×1.5")],
    "poison":   [("Black Sludge", "Restores HP for Poison types"), ("Assault Vest", "Sp. Def ×1.5")],
    "ground":   [("Soft Sand", "Boosts Ground moves 20%"), ("Choice Band", "Attack ×1.5")],
    "flying":   [("Sharp Beak", "Boosts Flying moves 20%"), ("Heavy-Duty Boots", "No hazard damage")],
    "bug":      [("Silver Powder", "Boosts Bug moves 20%"), ("Choice Band", "Attack ×1.5")],
    "rock":     [("Hard Stone", "Boosts Rock moves 20%"), ("Weakness Policy", "Atk+Sp.Atk ×2 on hit")],
    "normal":   [("Silk Scarf", "Boosts Normal moves 20%"), ("Leftovers", "Restores 1/16 HP/turn")],
    "fairy":    [("Fairy Feather", "Boosts Fairy moves 20%"), ("Life Orb", "Moves ×1.3")],
}


def get_likely_items(role_key: str, primary_type: str) -> list[tuple[str, str]]:
    """
    Return a deduplicated merged list of likely held items
    based on the Pokémon's role and primary type.
    """
    role_list = ROLE_ITEMS.get(role_key, ROLE_ITEMS["default"])
    type_list = TYPE_ITEMS.get(primary_type, [])

    seen: set[str] = set()
    merged: list[tuple[str, str]] = []
    for item in type_list + role_list:
        if item[0] not in seen:
            seen.add(item[0])
            merged.append(item)

    return merged[:5]  # cap at 5
