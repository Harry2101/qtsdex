"""
services/icc_scheduler.py
Persistent timer scheduler for ICC.

Polls icc_timers every 30s. No asyncio.sleep timers.
All actions are idempotent — safe to re-fire after crash.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

from services import icc_db

log = logging.getLogger("qtsdex.icc_scheduler")

_bot_ref: discord.Client | None = None


def _fire_at(minutes: int = 5) -> str:
    """Return an ISO-8601 UTC timestamp `minutes` from now."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ── Timer creation helpers ───────────────────────────────────────────────────

async def schedule_helper_escalation(
    guild_id: str, org_id: int, org_category_id: int, minutes: int = 5,
) -> int:
    return await icc_db.create_timer(
        guild_id, "helper_escalation", _fire_at(minutes),
        org_id=org_id, org_category_id=org_category_id,
    )


async def schedule_buyer_reminder(
    guild_id: str, org_id: int, org_category_id: int, minutes: int = 5,
) -> int:
    return await icc_db.create_timer(
        guild_id, "buyer_reminder", _fire_at(minutes),
        org_id=org_id, org_category_id=org_category_id,
    )


async def cancel_helper_escalation(org_category_id: int) -> int:
    return await icc_db.cancel_timers(org_category_id, "helper_escalation")


async def cancel_buyer_reminders(org_category_id: int) -> int:
    return await icc_db.cancel_timers(org_category_id, "buyer_reminder")


# ── Dispatch logic ───────────────────────────────────────────────────────────

async def _dispatch_helper_escalation(timer: dict) -> None:
    """Ping the helper role if the category is still unclaimed."""
    bot = _bot_ref
    if not bot:
        return

    oc = await icc_db.get_org_category(timer["org_category_id"])
    if not oc:
        return
    # Gate: only fire if still unclaimed
    if oc["status"] != "unclaimed":
        return

    org = await icc_db.get_org(timer["org_id"])
    if not org or org["status"] != "published":
        return

    guild = bot.get_guild(int(timer["guild_id"]))
    if not guild:
        return

    channel_id = org.get("announcement_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return

    role_mention = ""
    if oc["helper_role_id"]:
        role = guild.get_role(int(oc["helper_role_id"]))
        if role:
            role_mention = role.mention
        else:
            role_mention = f"@Helper (role {oc['helper_role_id']})"

    cat_name = oc["name"]
    msg = f"{role_mention} **{cat_name}** is still unclaimed! Use `/icc claim category:{cat_name}` to claim it."
    try:
        await channel.send(msg, allowed_mentions=discord.AllowedMentions(roles=True))
    except discord.HTTPException as e:
        log.warning(f"Helper escalation send failed: {e}")


async def _dispatch_buyer_reminder(timer: dict) -> None:
    """Remind the buyer of missing channels, then reschedule."""
    bot = _bot_ref
    if not bot:
        return

    oc = await icc_db.get_org_category(timer["org_category_id"])
    if not oc:
        return
    # Gate: only fire if still in progress or claimed (not complete/unclaimed)
    if oc["status"] not in ("claimed", "in_progress"):
        return

    org = await icc_db.get_org(timer["org_id"])
    if not org or org["status"] != "published":
        return

    guild = bot.get_guild(int(timer["guild_id"]))
    if not guild:
        return

    # Compute missing channels
    all_channels = await icc_db.get_category_channels(oc["category_id"])
    done_channels = await icc_db.get_completed_channels(oc["id"])
    missing = [c for c in all_channels if c not in done_channels]

    if not missing:
        # Nothing left — category should be complete, skip
        return

    owner_id = oc.get("owner_id")
    if not owner_id:
        return

    cat_name = oc["name"]
    done_count = oc["channels_done"]
    required = oc["required_count"]

    missing_mentions = ", ".join(f"<#{c}>" for c in missing[:20])
    if len(missing) > 20:
        missing_mentions += f" +{len(missing) - 20} more"

    msg = (
        f"<@{owner_id}> **{cat_name}** needs {len(missing)} more channel(s) "
        f"({done_count}/{required}):\n{missing_mentions}"
    )

    # Try DM first, fall back to announcement channel
    sent = False
    member = guild.get_member(int(owner_id))
    if member:
        try:
            await member.send(msg)
            sent = True
        except discord.HTTPException:
            pass

    if not sent:
        channel_id = org.get("announcement_channel_id")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel:
                try:
                    await channel.send(msg, allowed_mentions=discord.AllowedMentions(users=True))
                    sent = True
                except discord.HTTPException as e:
                    log.warning(f"Buyer reminder send failed: {e}")

    # Reschedule next reminder
    await schedule_buyer_reminder(
        timer["guild_id"], timer["org_id"], timer["org_category_id"],
    )


_DISPATCH = {
    "helper_escalation": _dispatch_helper_escalation,
    "buyer_reminder": _dispatch_buyer_reminder,
}


# ── Polling loop ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def _poll_timers():
    try:
        due = await icc_db.get_due_timers()
        for timer in due:
            handler = _DISPATCH.get(timer["timer_type"])
            if handler:
                try:
                    await handler(timer)
                except Exception:
                    log.exception(f"Timer dispatch error for timer {timer['id']}")
            await icc_db.mark_timer_fired(timer["id"])
    except Exception:
        log.exception("ICC scheduler poll error")


def start_scheduler(bot: discord.Client) -> None:
    """Start the 30s polling loop. Called from icc_listener cog_load."""
    global _bot_ref
    _bot_ref = bot
    if not _poll_timers.is_running():
        _poll_timers.start()
        log.info("ICC scheduler started")


def stop_scheduler() -> None:
    """Stop the polling loop. Called from icc_listener cog_unload."""
    if _poll_timers.is_running():
        _poll_timers.cancel()
        log.info("ICC scheduler stopped")
