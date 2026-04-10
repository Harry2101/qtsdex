"""
utils/icc_checks.py
Permission check helpers for ICC commands.
Uses guild_settings_db for role storage (icc_admin_role, icc_organizer_role).
"""

import os

import discord
from discord import Interaction

from services import guild_settings_db

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))


async def is_icc_admin(interaction: Interaction) -> bool:
    """Check if the user is an ICC admin (admin perm, configured role, or bot owner)."""
    if interaction.user.id == OWNER_ID:
        return True
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_id_str = await guild_settings_db.get(str(interaction.guild_id), "icc_admin_role")
    if role_id_str:
        role_id = int(role_id_str)
        if any(r.id == role_id for r in interaction.user.roles):
            return True
    return False


async def is_icc_organizer(interaction: Interaction) -> bool:
    """Check if the user is an ICC organizer (organizer role, or ICC admin)."""
    if await is_icc_admin(interaction):
        return True
    if not interaction.guild:
        return False
    role_id_str = await guild_settings_db.get(str(interaction.guild_id), "icc_organizer_role")
    if role_id_str:
        role_id = int(role_id_str)
        if any(r.id == role_id for r in interaction.user.roles):
            return True
    return False
