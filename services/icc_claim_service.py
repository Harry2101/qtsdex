"""
services/icc_claim_service.py
FCFS claim, unclaim, admin assign, admin drop.
All operations require org status='published'.
"""

import logging
from datetime import datetime, timezone

from services import icc_db, icc_scheduler

log = logging.getLogger("qtsdex.icc_claim_service")


async def claim_category(
    guild_id: str, user_id: str, category_name: str,
) -> tuple[bool, str]:
    """
    FCFS claim a category in the published org.
    Returns (success, message).
    """
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    oc = await icc_db.get_org_category_by_name(org["id"], category_name)
    if not oc:
        return False, f"Category **{category_name}** not found in this org."

    if oc["owner_id"]:
        return False, f"**{category_name}** is already claimed by <@{oc['owner_id']}>."

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await icc_db.update_org_category(
        oc["id"], owner_id=user_id, claimed_at=now, status="claimed",
    )

    # Cancel helper escalation, schedule buyer reminder
    await icc_scheduler.cancel_helper_escalation(oc["id"])
    await icc_scheduler.schedule_buyer_reminder(guild_id, org["id"], oc["id"])

    await icc_db.log_action(
        guild_id, user_id, "category_claimed",
        f"{category_name} claimed in org {org['id']}",
    )
    return True, f"You claimed **{category_name}** ({oc['required_count']} channels)."


async def unclaim_category(
    guild_id: str, user_id: str, category_name: str,
) -> tuple[bool, str]:
    """
    Release claim on own category. Only allowed if no progress has been made.
    """
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    oc = await icc_db.get_org_category_by_name(org["id"], category_name)
    if not oc:
        return False, f"Category **{category_name}** not found."

    if oc["owner_id"] != user_id:
        return False, f"You don't own **{category_name}**."

    if oc["channels_done"] > 0:
        return False, f"**{category_name}** already has {oc['channels_done']} channel(s) done. Cannot unclaim."

    await icc_db.update_org_category(
        oc["id"], owner_id=None, claimed_at=None, status="unclaimed",
    )

    # Cancel buyer reminders, restart helper escalation
    await icc_scheduler.cancel_buyer_reminders(oc["id"])
    await icc_scheduler.schedule_helper_escalation(guild_id, org["id"], oc["id"])

    await icc_db.log_action(
        guild_id, user_id, "category_unclaimed",
        f"{category_name} unclaimed in org {org['id']}",
    )
    return True, f"**{category_name}** is now unclaimed."


async def admin_assign(
    guild_id: str, admin_id: str, category_name: str, target_user_id: str,
) -> tuple[bool, str]:
    """Admin override: forcibly assign a category to a user."""
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    oc = await icc_db.get_org_category_by_name(org["id"], category_name)
    if not oc:
        return False, f"Category **{category_name}** not found."

    old_owner = oc["owner_id"]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await icc_db.update_org_category(
        oc["id"],
        owner_id=target_user_id,
        claimed_at=now,
        assigned_by=admin_id,
        status="claimed" if oc["channels_done"] == 0 else "in_progress",
    )

    # Reset timers
    await icc_scheduler.cancel_helper_escalation(oc["id"])
    await icc_scheduler.cancel_buyer_reminders(oc["id"])
    await icc_scheduler.schedule_buyer_reminder(guild_id, org["id"], oc["id"])

    detail = f"{category_name} assigned to {target_user_id} by admin {admin_id}"
    if old_owner:
        detail += f" (was {old_owner})"
    await icc_db.log_action(guild_id, admin_id, "category_assigned_override", detail)

    msg = f"**{category_name}** assigned to <@{target_user_id}>."
    if old_owner:
        msg += f" (was <@{old_owner}>)"
    return True, msg


async def admin_drop(
    guild_id: str, admin_id: str, category_name: str,
) -> tuple[bool, str]:
    """Admin forces a category back to unclaimed."""
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    oc = await icc_db.get_org_category_by_name(org["id"], category_name)
    if not oc:
        return False, f"Category **{category_name}** not found."

    if not oc["owner_id"]:
        return False, f"**{category_name}** is already unclaimed."

    old_owner = oc["owner_id"]
    await icc_db.update_org_category(
        oc["id"], owner_id=None, claimed_at=None, assigned_by=None, status="unclaimed",
    )

    # Cancel buyer reminders, restart helper escalation
    await icc_scheduler.cancel_buyer_reminders(oc["id"])
    await icc_scheduler.schedule_helper_escalation(guild_id, org["id"], oc["id"])

    await icc_db.log_action(
        guild_id, admin_id, "category_dropped",
        f"{category_name} dropped from {old_owner} by admin {admin_id}",
    )
    return True, f"**{category_name}** dropped (was <@{old_owner}>)."
