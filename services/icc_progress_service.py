"""
services/icc_progress_service.py
Mark channels done, compute progress, cascade to org completion.
Only operates on published orgs.
"""

import logging

from services import icc_db, icc_scheduler, icc_org_service

log = logging.getLogger("qtsdex.icc_progress_service")


async def mark_channel_done(
    guild_id: str, channel_id: str,
    method: str = "auto", completed_by: str = "",
) -> tuple[bool, str, dict | None]:
    """
    Mark a channel as bought in the active published org.
    Returns (success, message, org_category_dict | None).
    Idempotent: returns False with a message if already marked.
    """
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active.", None

    oc = await icc_db.resolve_channel_to_org_category(guild_id, org["id"], channel_id)
    if not oc:
        return False, "Channel is not mapped to any category in this org.", None

    # Idempotency: check if already completed
    already = await icc_db.is_channel_completed(org["id"], channel_id)
    if already:
        return False, f"<#{channel_id}> is already marked done.", oc

    # Insert completion
    inserted = await icc_db.mark_channel_complete(
        org["id"], oc["id"], guild_id, channel_id, method, completed_by,
    )
    if not inserted:
        # Race condition — another handler got there first
        return False, f"<#{channel_id}> is already marked done.", oc

    # Increment channels_done
    new_done = oc["channels_done"] + 1
    required = oc["required_count"]
    is_complete = new_done >= required

    updates = {"channels_done": new_done}
    if is_complete:
        from datetime import datetime, timezone
        updates["status"] = "complete"
        updates["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    elif oc["status"] == "claimed":
        updates["status"] = "in_progress"

    await icc_db.update_org_category(oc["id"], **updates)

    await icc_db.log_action(
        guild_id, completed_by or "auto", "channel_completed",
        f"#{channel_id} done in {oc['name']} ({new_done}/{required}) method={method}",
    )

    if is_complete:
        await icc_scheduler.cancel_buyer_reminders(oc["id"])
        log.info(f"Category {oc['name']} complete ({new_done}/{required}) in org {org['id']}")

        # Check if entire org is now complete
        org_done = await icc_org_service.check_org_completion(org["id"], guild_id)
        if org_done:
            log.info(f"Org {org['id']} all categories complete")

    # Refresh and return
    refreshed = await icc_db.get_org_category(oc["id"])
    return True, f"<#{channel_id}> marked done in **{oc['name']}** ({new_done}/{required}).", refreshed


async def admin_force_complete(
    guild_id: str, admin_id: str, category_name: str,
) -> tuple[bool, str]:
    """Admin force-marks a category as fully complete."""
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    oc = await icc_db.get_org_category_by_name(org["id"], category_name)
    if not oc:
        return False, f"Category **{category_name}** not found."

    if oc["status"] == "complete":
        return False, f"**{category_name}** is already complete."

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await icc_db.update_org_category(
        oc["id"],
        channels_done=oc["required_count"],
        status="complete",
        completed_at=now,
    )
    await icc_scheduler.cancel_buyer_reminders(oc["id"])
    await icc_scheduler.cancel_helper_escalation(oc["id"])

    await icc_db.log_action(
        guild_id, admin_id, "category_force_completed",
        f"{category_name} force-completed by admin in org {org['id']}",
    )

    org_done = await icc_org_service.check_org_completion(org["id"], guild_id)
    msg = f"**{category_name}** force-marked as complete."
    if org_done:
        msg += " All categories done — org is complete!"
    return True, msg


async def admin_reset_category(
    guild_id: str, admin_id: str, category_name: str,
) -> tuple[bool, str]:
    """Admin resets a category's progress and owner back to unclaimed."""
    org = await icc_db.get_active_org(guild_id)
    if not org:
        return False, "No published org is active."

    oc = await icc_db.get_org_category_by_name(org["id"], category_name)
    if not oc:
        return False, f"Category **{category_name}** not found."

    await icc_db.update_org_category(
        oc["id"],
        owner_id=None, claimed_at=None, assigned_by=None,
        channels_done=0, status="unclaimed", completed_at=None,
    )

    # Cancel all timers for this category, restart helper escalation
    await icc_db.cancel_timers(oc["id"])
    await icc_scheduler.schedule_helper_escalation(guild_id, org["id"], oc["id"])

    await icc_db.log_action(
        guild_id, admin_id, "category_reset",
        f"{category_name} reset to unclaimed by admin in org {org['id']}",
    )
    return True, f"**{category_name}** reset to unclaimed (progress wiped)."
