"""
services/icc_org_service.py
Business logic for org lifecycle: create (draft), publish, cancel, complete.
"""

import logging
from datetime import datetime, timezone

from services import icc_db, icc_scheduler, icc_reserve_service

log = logging.getLogger("qtsdex.icc_org_service")


async def create_draft_org(
    guild_id: str, organizer_id: str, label: str = "",
) -> tuple[bool, str, int | None]:
    """
    Create a draft org.  Returns (success, message, org_id).
    Blocks if a draft or published org already exists.
    """
    existing = await icc_db.get_org_any_active(guild_id)
    if existing:
        status = existing["status"]
        return False, f"An org is already {status} (ID {existing['id']}).", None

    categories = await icc_db.get_categories(guild_id)
    if not categories:
        return False, "No categories configured. Use `/icc category create` first.", None

    org_id = await icc_db.create_org(guild_id, organizer_id, label)

    # Create org_category rows for every active category
    for cat in categories:
        await icc_db.create_org_category(org_id, cat["id"], guild_id)

    await icc_db.log_action(
        guild_id, organizer_id, "org_created",
        f"Draft org {org_id} created with {len(categories)} categories",
    )
    return True, f"Draft org **{org_id}** created with {len(categories)} categories.", org_id


async def publish_org(
    guild_id: str, user_id: str,
    announcement_channel_id: str, announcement_message_id: str = "",
) -> tuple[bool, str, dict | None]:
    """
    Transition draft → published.  Starts helper escalation timers.
    Returns (success, message, org_dict).
    """
    org = await icc_db.get_draft_org(guild_id)
    if not org:
        return False, "No draft org to publish.", None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await icc_db.update_org_status(
        org["id"], "published",
        published_at=now,
        announcement_channel_id=announcement_channel_id,
        announcement_message_id=announcement_message_id,
    )

    # Lock all declared reserves
    await icc_reserve_service.lock_org_reserves(org["id"], guild_id)

    # Schedule helper escalation for every unclaimed category
    org_cats = await icc_db.get_org_categories(org["id"])
    for oc in org_cats:
        if oc["status"] == "unclaimed":
            await icc_scheduler.schedule_helper_escalation(
                guild_id, org["id"], oc["id"],
            )

    await icc_db.log_action(
        guild_id, user_id, "org_published",
        f"Org {org['id']} published in channel {announcement_channel_id}",
    )

    # Return refreshed org
    updated = await icc_db.get_org(org["id"])
    return True, f"Org **{org['id']}** is now live!", updated


async def cancel_org(
    guild_id: str, user_id: str, org_id: int | None = None,
    reason: str = "",
) -> tuple[bool, str]:
    """Cancel a draft or published org."""
    if org_id:
        org = await icc_db.get_org(org_id)
        if not org or org["guild_id"] != guild_id:
            return False, "Org not found."
    else:
        org = await icc_db.get_org_any_active(guild_id)
        if not org:
            return False, "No active org to cancel."

    if org["status"] not in ("draft", "published"):
        return False, f"Org {org['id']} is already {org['status']}."

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await icc_db.update_org_status(
        org["id"], "cancelled", cancelled_at=now, cancelled_by=user_id,
    )
    await icc_db.cancel_org_timers(org["id"])

    detail = f"Org {org['id']} cancelled"
    if reason:
        detail += f": {reason}"
    await icc_db.log_action(guild_id, user_id, "org_cancelled", detail)
    return True, f"Org **{org['id']}** cancelled."


async def check_org_completion(org_id: int, guild_id: str) -> bool:
    """
    Check if all categories in the org are complete.
    If so, mark the org as complete and cancel remaining timers.
    Returns True if org just completed.
    """
    org_cats = await icc_db.get_org_categories(org_id)
    if not org_cats:
        return False

    for oc in org_cats:
        if oc["status"] != "complete":
            return False

    # All complete
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await icc_db.update_org_status(org_id, "complete", completed_at=now)
    await icc_db.cancel_org_timers(org_id)
    await icc_db.log_action(guild_id, "system", "org_completed", f"Org {org_id} all categories complete")
    return True
