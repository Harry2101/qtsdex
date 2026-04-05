"""
services/starboard_db.py
SQLite storage for the starboard & leaderboard system.

Tables:
  starboard_config  — per-guild starboard channel settings
  shiny_catches     — individual shiny catch records for leaderboard
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

            CREATE INDEX IF NOT EXISTS idx_shiny_guild
                ON shiny_catches (guild_id);
            CREATE INDEX IF NOT EXISTS idx_shiny_caught_at
                ON shiny_catches (guild_id, caught_at);
            CREATE INDEX IF NOT EXISTS idx_shiny_user
                ON shiny_catches (guild_id, user_id);
        """)
        await db.commit()
        # Add announce_channel column if upgrading from older schema
        try:
            await db.execute("ALTER TABLE starboard_config ADD COLUMN announce_channel TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # Column already exists

        # Pre-load config cache
        async with db.execute("SELECT guild_id, channel_id, prefix, suffix, shiny_count, announce_channel FROM starboard_config") as cur:
            async for row in cur:
                _config_cache[row[0]] = {
                    "channel_id": row[1],
                    "prefix": row[2],
                    "suffix": row[3],
                    "shiny_count": row[4],
                    "announce_channel": row[5] or "",
                }


# ── Config ───────────────────────────────────────────────────────────────────

def get_config(guild_id: str) -> Optional[dict]:
    """Get cached starboard config for a guild."""
    return _config_cache.get(guild_id)


async def set_config(guild_id: str, channel_id: str, prefix: str = "", suffix: str = "", shiny_count: int = 0) -> None:
    """Set or update starboard config for a guild."""
    existing = _config_cache.get(guild_id, {})
    announce_channel = existing.get("announce_channel", "")
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO starboard_config (guild_id, channel_id, prefix, suffix, shiny_count, announce_channel)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       channel_id=excluded.channel_id,
                       prefix=excluded.prefix,
                       suffix=excluded.suffix,
                       shiny_count=excluded.shiny_count""",
                (guild_id, channel_id, prefix, suffix, shiny_count, announce_channel),
            )
            await db.commit()
    _config_cache[guild_id] = {
        "channel_id": channel_id,
        "prefix": prefix,
        "suffix": suffix,
        "shiny_count": shiny_count,
        "announce_channel": announce_channel,
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
) -> int:
    """Record a shiny catch. Returns the new row ID."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO shiny_catches (guild_id, user_name, user_id, pokemon_name, message_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, user_name, user_id, pokemon_name, message_id),
            )
            await db.commit()
            return cur.lastrowid


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
