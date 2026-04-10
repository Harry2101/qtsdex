"""
services/icc_reserve_service.py
Business logic for reserve picks: FCFS pick, release, lock on publish.

Reserves are declared after claiming a category in a published org.
They lock when the org starts (published). Buyers can release at any time.
FCFS is enforced across the ENTIRE org — one base_name per org.
"""

import logging

from services import icc_db, pokemon_list_db

log = logging.getLogger("qtsdex.icc_reserve_service")


async def pick_reserve(
    guild_id: str, user_id: str, pokemon_name: str,
) -> tuple[bool, str]:
    """
    FCFS reserve pick.  The user must own a category with available reserve slots.
    pokemon_name must be from normal or event lists (not rare/gmax/eevo/regional).
    Returns (success, message).
    """
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    # Find user's org_categories (they must own at least one)
    org_cats = await icc_db.get_org_categories(org["id"])
    owned_cats = [oc for oc in org_cats if oc["owner_id"] == user_id]
    if not owned_cats:
        return False, "You don't own any category in this org."

    # Check the Pokemon is eligible (normal or event, not rare/gmax/eevo/regional)
    cat_type = await pokemon_list_db.get_category_type_for_pokemon(pokemon_name)
    if cat_type in pokemon_list_db.CATEGORY_TYPES:
        return False, f"**{pokemon_name}** is in the **{cat_type}** list and cannot be reserved."

    # Determine base_name for form grouping
    base_name = await pokemon_list_db.find_base_name_for_spawn(pokemon_name)
    is_event = await pokemon_list_db.is_event_pokemon(pokemon_name)

    if base_name is None and not is_event:
        # Unknown Pokemon — still allow it but use the name as-is
        base_name = pokemon_name

    if base_name is None:
        # Event Pokemon — no form grouping, use raw name as base
        base_name = pokemon_name

    # Find a category with available reserve slots
    target_oc = None
    for oc in owned_cats:
        cat = await icc_db.get_category(oc["category_id"])
        if not cat:
            continue
        slots = cat.get("reserve_slots", 0)
        if slots <= 0:
            continue
        # Count current reserves for this org_category
        current = await icc_db.get_reserves_for_org_category(oc["id"])
        if len(current) < slots:
            target_oc = oc
            break

    if not target_oc:
        return False, "You have no available reserve slots in any of your categories."

    # FCFS check — unique (org_id, base_name)
    reserve_id = await icc_db.create_reserve(
        org["id"], target_oc["id"], user_id, pokemon_name, base_name,
    )
    if reserve_id is None:
        return False, f"**{base_name}** is already reserved by someone in this org."

    await icc_db.log_action(
        guild_id, user_id, "reserve_pick",
        f"Reserved {pokemon_name} (base: {base_name}) in {target_oc['name']} org {org['id']}",
    )
    return True, f"Reserved **{pokemon_name}** (covers all {base_name} forms) in **{target_oc['name']}**."


async def release_reserve(
    guild_id: str, user_id: str, pokemon_name: str,
) -> tuple[bool, str]:
    """Release a reserve. Can be done at any time."""
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    # Find the reserve by base_name
    base_name = await pokemon_list_db.find_base_name_for_spawn(pokemon_name)
    if base_name is None:
        base_name = pokemon_name

    reserve = await icc_db.get_reserve_by_owner_and_name(org["id"], user_id, base_name)
    if not reserve:
        return False, f"You don't have a reserve for **{pokemon_name}**."

    await icc_db.release_reserve(reserve["id"])

    await icc_db.log_action(
        guild_id, user_id, "reserve_release",
        f"Released reserve {pokemon_name} (base: {base_name}) in org {org['id']}",
    )
    return True, f"Released reserve on **{pokemon_name}**. It's now free for all."


async def lock_org_reserves(org_id: int, guild_id: str) -> int:
    """Lock all reserves when org publishes. Returns count locked."""
    count = await icc_db.lock_reserves_for_org(org_id)
    if count:
        await icc_db.log_action(
            guild_id, "system", "reserves_locked",
            f"Locked {count} reserve(s) for org {org_id}",
        )
    return count


async def get_user_reserves(guild_id: str, user_id: str) -> list[dict]:
    """Get all active reserves for a user in the active org."""
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return []
    return await icc_db.get_reserves_for_owner(org["id"], user_id)


async def match_spawn_to_reserve(org_id: int, spawn_name: str) -> dict | None:
    """
    Check if a spawn name matches any active locked reserve.
    Checks by form group (base_name) and direct pokemon_name match.
    Also checks event Pokemon names.
    """
    # Direct match on reserves table
    reserve = await icc_db.find_reserve_by_spawn(org_id, spawn_name)
    if reserve:
        return reserve

    # Try to resolve to a base_name and match that
    base_name = await pokemon_list_db.find_base_name_for_spawn(spawn_name)
    if base_name and base_name.lower() != spawn_name.lower():
        reserve = await icc_db.find_reserve_by_spawn(org_id, base_name)
        if reserve:
            return reserve

    return None
