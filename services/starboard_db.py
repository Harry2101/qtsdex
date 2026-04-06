"""
services/starboard_db.py
SQLite storage for the starboard & leaderboard system.

Tables:
  starboard_config   — per-guild starboard channel settings
  shiny_catches      — individual shiny catch records for leaderboard
  champion_history   — weekly/monthly champion records for streaks & history
"""

import asyncio
import logging
import os
from typing import Optional

import aiosqlite

DB_PATH = "data/starboard.db"
log = logging.getLogger("qtsdex.starboard_db")

_write_lock = asyncio.Lock()

# In-memory cache for starboard config: guild_id -> {channel_id, prefix, suffix, shiny_count}
_config_cache: dict[str, dict] = {}


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS starboard_config (
                guild_id         TEXT PRIMARY KEY,
                channel_id       TEXT NOT NULL,
                prefix           TEXT NOT NULL DEFAULT '',
                suffix           TEXT NOT NULL DEFAULT '',
                shiny_count      INTEGER NOT NULL DEFAULT 0,
                announce_channel TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS shiny_catches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      TEXT NOT NULL,
                user_name     TEXT NOT NULL DEFAULT '',
                user_id       TEXT NOT NULL DEFAULT '',
                pokemon_name  TEXT NOT NULL DEFAULT '',
                message_id    TEXT NOT NULL DEFAULT '',
                caught_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS champion_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      TEXT NOT NULL,
                period_type   TEXT NOT NULL,
                period_label  TEXT NOT NULL,
                user_id       TEXT NOT NULL DEFAULT '',
                user_name     TEXT NOT NULL DEFAULT '',
                catch_count   INTEGER NOT NULL DEFAULT 0,
                total_catches INTEGER NOT NULL DEFAULT 0,
                recorded_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(guild_id, period_type, period_label)
            );

            CREATE INDEX IF NOT EXISTS idx_shiny_guild
                ON shiny_catches (guild_id);
            CREATE INDEX IF NOT EXISTS idx_shiny_caught_at
                ON shiny_catches (guild_id, caught_at);
            CREATE INDEX IF NOT EXISTS idx_shiny_user
                ON shiny_catches (guild_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_champion_guild
                ON champion_history (guild_id, period_type, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_champion_user
                ON champion_history (guild_id, user_id);
        """)
        await db.commit()
        # Migrations for older schemas
        for migration in [
            "ALTER TABLE starboard_config ADD COLUMN announce_channel TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE starboard_config ADD COLUMN weekly_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE starboard_config ADD COLUMN monthly_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE starboard_config ADD COLUMN announce_ping_role TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass  # Column already exists

        # Deduplicate shiny_catches: keep earliest row per (guild_id, message_id)
        await db.execute("""
            DELETE FROM shiny_catches
            WHERE id NOT IN (
                SELECT MIN(id) FROM shiny_catches
                WHERE message_id != ''
                GROUP BY guild_id, message_id
            ) AND message_id != ''
        """)
        await db.commit()

        # Now safe to add unique index (duplicates are gone)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shiny_message
                ON shiny_catches (guild_id, message_id)
        """)
        await db.commit()

        # Pre-load config cache
        async with db.execute(
            "SELECT guild_id, channel_id, prefix, suffix, shiny_count, announce_channel, weekly_enabled, monthly_enabled, announce_ping_role FROM starboard_config"
        ) as cur:
            async for row in cur:
                _config_cache[row[0]] = {
                    "channel_id": row[1],
                    "prefix": row[2],
                    "suffix": row[3],
                    "shiny_count": row[4],
                    "announce_channel": row[5] or "",
                    "weekly_enabled": bool(row[6]),
                    "monthly_enabled": bool(row[7]),
                    "announce_ping_role": row[8] or "",
                }


# ── Config ───────────────────────────────────────────────────────────────────

def get_config(guild_id: str) -> Optional[dict]:
    """Get cached starboard config for a guild."""
    return _config_cache.get(guild_id)


async def set_config(guild_id: str, channel_id: str, prefix: str = "", suffix: str = "", shiny_count: int = 0) -> None:
    """Set or update starboard config for a guild."""
    existing = _config_cache.get(guild_id, {})
    announce_channel = existing.get("announce_channel", "")
    weekly_enabled = existing.get("weekly_enabled", True)
    monthly_enabled = existing.get("monthly_enabled", True)
    announce_ping_role = existing.get("announce_ping_role", "")
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO starboard_config (guild_id, channel_id, prefix, suffix, shiny_count, announce_channel, weekly_enabled, monthly_enabled, announce_ping_role)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       channel_id=excluded.channel_id,
                       prefix=excluded.prefix,
                       suffix=excluded.suffix,
                       shiny_count=excluded.shiny_count""",
                (guild_id, channel_id, prefix, suffix, shiny_count, announce_channel, weekly_enabled, monthly_enabled, announce_ping_role),
            )
            await db.commit()
    _config_cache[guild_id] = {
        "channel_id": channel_id,
        "prefix": prefix,
        "suffix": suffix,
        "shiny_count": shiny_count,
        "announce_channel": announce_channel,
        "weekly_enabled": weekly_enabled,
        "monthly_enabled": monthly_enabled,
        "announce_ping_role": announce_ping_role,
    }


async def update_prefix_suffix(guild_id: str, prefix: str, suffix: str) -> bool:
    """Update only the prefix and suffix. Returns False if no config exists."""
    cfg = _config_cache.get(guild_id)
    if not cfg:
        return False
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE starboard_config SET prefix=?, suffix=? WHERE guild_id=?",
                (prefix, suffix, guild_id),
            )
            await db.commit()
    cfg["prefix"] = prefix
    cfg["suffix"] = suffix
    return True


async def set_shiny_count(guild_id: str, count: int) -> bool:
    """Manually set the shiny count. Returns False if no config exists."""
    cfg = _config_cache.get(guild_id)
    if not cfg:
        return False
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE starboard_config SET shiny_count=? WHERE guild_id=?",
                (count, guild_id),
            )
            await db.commit()
    cfg["shiny_count"] = count
    return True


async def increment_shiny_count(guild_id: str) -> int:
    """Increment shiny count by 1. Returns the new count."""
    cfg = _config_cache.get(guild_id)
    if not cfg:
        return 0
    new_count = cfg["shiny_count"] + 1
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE starboard_config SET shiny_count=? WHERE guild_id=?",
                (new_count, guild_id),
            )
            await db.commit()
    cfg["shiny_count"] = new_count
    return new_count


async def set_announce_channel(guild_id: str, channel_id: str) -> bool:
    """Set the announcement channel for weekly/monthly top catcher posts. Returns False if no config."""
    cfg = _config_cache.get(guild_id)
    if not cfg:
        return False
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE starboard_config SET announce_channel=? WHERE guild_id=?",
                (channel_id, guild_id),
            )
            await db.commit()
    cfg["announce_channel"] = channel_id
    return True


async def set_announcement_toggle(guild_id: str, period_type: str, enabled: bool) -> bool:
    """Enable or disable weekly/monthly announcements. period_type: 'week' or 'month'. Returns False if no config."""
    cfg = _config_cache.get(guild_id)
    if not cfg:
        return False
    col = "weekly_enabled" if period_type == "week" else "monthly_enabled"
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                f"UPDATE starboard_config SET {col}=? WHERE guild_id=?",
                (1 if enabled else 0, guild_id),
            )
            await db.commit()
    cfg[col] = enabled
    return True


async def set_announce_ping_role(guild_id: str, role_id: str) -> bool:
    """Set the role to ping in weekly/monthly champion announcements. Returns False if no config."""
    cfg = _config_cache.get(guild_id)
    if not cfg:
        return False
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE starboard_config SET announce_ping_role=? WHERE guild_id=?",
                (role_id, guild_id),
            )
            await db.commit()
    cfg["announce_ping_role"] = role_id
    return True


async def remove_config(guild_id: str) -> bool:
    """Remove starboard config for a guild."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM starboard_config WHERE guild_id=?", (guild_id,),
            )
            await db.commit()
            existed = cur.rowcount > 0
    _config_cache.pop(guild_id, None)
    return existed


# ── Shiny catches (leaderboard data) ────────────────────────────────────────

async def record_catch(
    guild_id: str,
    user_name: str,
    user_id: str,
    pokemon_name: str,
    message_id: str,
    caught_at: str = "",
) -> int:
    """Record a shiny catch. Skips duplicates (same guild + message). Returns the new row ID or 0 if duplicate.
    caught_at: ISO datetime string. If empty, uses current time (SQLite default)."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            if caught_at:
                cur = await db.execute(
                    """INSERT OR IGNORE INTO shiny_catches (guild_id, user_name, user_id, pokemon_name, message_id, caught_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (guild_id, user_name, user_id, pokemon_name, message_id, caught_at),
                )
            else:
                cur = await db.execute(
                    """INSERT OR IGNORE INTO shiny_catches (guild_id, user_name, user_id, pokemon_name, message_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (guild_id, user_name, user_id, pokemon_name, message_id),
                )
            await db.commit()
            return cur.lastrowid if cur.rowcount > 0 else 0


async def update_catch_timestamp(guild_id: str, message_id: str, caught_at: str) -> bool:
    """Update the caught_at timestamp for an existing catch record. Returns True if a row was updated."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE shiny_catches SET caught_at=? WHERE guild_id=? AND message_id=?",
                (caught_at, guild_id, message_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_leaderboard(guild_id: str, period: str = "all", limit: int = 10) -> list[tuple]:
    """
    Get shiny catch leaderboard for a guild.
    period: 'week', 'month', 'year', 'all'
    Returns list of (user_name, user_id, catch_count) sorted desc.
    """
    if period == "week":
        where_time = "AND caught_at >= datetime('now', '-7 days')"
    elif period == "month":
        where_time = "AND caught_at >= datetime('now', '-1 month')"
    elif period == "year":
        where_time = "AND caught_at >= datetime('now', '-1 year')"
    else:
        where_time = ""

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT user_name, user_id, COUNT(*) as cnt
                FROM shiny_catches
                WHERE guild_id=? {where_time}
                GROUP BY guild_id, user_id
                ORDER BY cnt DESC
                LIMIT ?""",
            (guild_id, limit),
        ) as cur:
            return await cur.fetchall()


async def get_catch_count(guild_id: str, period: str = "all") -> int:
    """Get total shiny catches for a guild in a time period."""
    if period == "week":
        where_time = "AND caught_at >= datetime('now', '-7 days')"
    elif period == "month":
        where_time = "AND caught_at >= datetime('now', '-1 month')"
    elif period == "year":
        where_time = "AND caught_at >= datetime('now', '-1 year')"
    else:
        where_time = ""

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM shiny_catches WHERE guild_id=? {where_time}",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ── Champion history ────────────────────────────────────────────────────────

async def record_champion(
    guild_id: str,
    period_type: str,
    period_label: str,
    user_id: str,
    user_name: str,
    catch_count: int,
    total_catches: int,
) -> int:
    """Record a champion for a period. Updates if already exists. Returns row ID."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO champion_history
                       (guild_id, period_type, period_label, user_id, user_name, catch_count, total_catches)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, period_type, period_label) DO UPDATE SET
                       user_id=excluded.user_id,
                       user_name=excluded.user_name,
                       catch_count=excluded.catch_count,
                       total_catches=excluded.total_catches""",
                (guild_id, period_type, period_label, user_id, user_name, catch_count, total_catches),
            )
            await db.commit()
            return cur.lastrowid


async def get_champion_streak(guild_id: str, period_type: str, user_id: str) -> int:
    """
    Get the current consecutive win streak for a user.
    Counts backwards from the most recent period of this type.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_id FROM champion_history
               WHERE guild_id=? AND period_type=?
               ORDER BY period_label DESC""",
            (guild_id, period_type),
        ) as cur:
            streak = 0
            async for row in cur:
                if row[0] == user_id:
                    streak += 1
                else:
                    break
            return streak


async def get_total_wins(guild_id: str, period_type: str, user_id: str) -> int:
    """Get total number of times a user has been champion for a period type."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) FROM champion_history
               WHERE guild_id=? AND period_type=? AND user_id=?""",
            (guild_id, period_type, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_champion_history(
    guild_id: str, period_type: str, limit: int = 10, offset: int = 0
) -> list[tuple]:
    """
    Get past champions for a period type, newest first.
    Returns list of (period_label, user_name, user_id, catch_count, total_catches).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT period_label, user_name, user_id, catch_count, total_catches
               FROM champion_history
               WHERE guild_id=? AND period_type=?
               ORDER BY period_label DESC
               LIMIT ? OFFSET ?""",
            (guild_id, period_type, limit, offset),
        ) as cur:
            return await cur.fetchall()


async def get_champion_history_count(guild_id: str, period_type: str) -> int:
    """Get total number of champion records for a period type."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM champion_history WHERE guild_id=? AND period_type=?",
            (guild_id, period_type),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def clear_champion_history(guild_id: str, period_type: str) -> int:
    """Delete all champion_history rows for a guild + period type. Returns number deleted."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM champion_history WHERE guild_id=? AND period_type=?",
                (guild_id, period_type),
            )
            await db.commit()
            return cur.rowcount


async def get_user_stats(guild_id: str, user_id: str) -> dict:
    """Get comprehensive stats for a user in a guild."""
    stats = {
        "total_catches": 0,
        "week_catches": 0,
        "month_catches": 0,
        "weekly_wins": 0,
        "monthly_wins": 0,
        "weekly_streak": 0,
        "monthly_streak": 0,
        "best_week": 0,
        "best_month": 0,
        "unique_pokemon": 0,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        # Total catches
        async with db.execute(
            "SELECT COUNT(*) FROM shiny_catches WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["total_catches"] = row[0] if row else 0

        # Week catches
        async with db.execute(
            "SELECT COUNT(*) FROM shiny_catches WHERE guild_id=? AND user_id=? AND caught_at >= datetime('now', '-7 days')",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["week_catches"] = row[0] if row else 0

        # Month catches
        async with db.execute(
            "SELECT COUNT(*) FROM shiny_catches WHERE guild_id=? AND user_id=? AND caught_at >= datetime('now', '-1 month')",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["month_catches"] = row[0] if row else 0

        # Weekly wins
        async with db.execute(
            "SELECT COUNT(*) FROM champion_history WHERE guild_id=? AND period_type='week' AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["weekly_wins"] = row[0] if row else 0

        # Monthly wins
        async with db.execute(
            "SELECT COUNT(*) FROM champion_history WHERE guild_id=? AND period_type='month' AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["monthly_wins"] = row[0] if row else 0

        # Best week (from champion_history)
        async with db.execute(
            "SELECT MAX(catch_count) FROM champion_history WHERE guild_id=? AND period_type='week' AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["best_week"] = row[0] if row and row[0] else 0

        # Best month (from champion_history)
        async with db.execute(
            "SELECT MAX(catch_count) FROM champion_history WHERE guild_id=? AND period_type='month' AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["best_month"] = row[0] if row and row[0] else 0

        # Unique pokemon
        async with db.execute(
            "SELECT COUNT(DISTINCT pokemon_name) FROM shiny_catches WHERE guild_id=? AND user_id=? AND pokemon_name != ''",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            stats["unique_pokemon"] = row[0] if row else 0

    # Streaks (uses separate queries)
    stats["weekly_streak"] = await get_champion_streak(guild_id, "week", user_id)
    stats["monthly_streak"] = await get_champion_streak(guild_id, "month", user_id)

    return stats


async def get_top_champions(guild_id: str, period_type: str, limit: int = 10) -> list[tuple]:
    """
    Get users ranked by most champion wins for a period type.
    Returns list of (user_name, user_id, win_count).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_name, user_id, COUNT(*) as wins
               FROM champion_history
               WHERE guild_id=? AND period_type=?
               GROUP BY user_id
               ORDER BY wins DESC
               LIMIT ?""",
            (guild_id, period_type, limit),
        ) as cur:
            return await cur.fetchall()
