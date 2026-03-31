"""
services/incense_db.py
SQLite storage for the incense management system.
Designed for concurrent access from 50–200 channels.

Tables:
  incense_channels  — registered incense channels per guild
  active_incenses   — channels currently running an incense (paused or live)
"""

import asyncio
import logging
import os
from typing import Optional

import aiosqlite

DB_PATH = "data/incense.db"
log     = logging.getLogger("qtsdex.incense_db")

# Write lock to serialise concurrent DB writes safely
_write_lock = asyncio.Lock()


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS incense_channels (
                guild_id    TEXT NOT NULL,
                channel_id  TEXT NOT NULL,
                added_by    TEXT NOT NULL DEFAULT '',
                added_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (guild_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS active_incenses (
                guild_id      TEXT NOT NULL,
                channel_id    TEXT NOT NULL,
                incense_type  TEXT NOT NULL DEFAULT 'Standard',
                total_spawns  INTEGER NOT NULL DEFAULT 0,
                paused        INTEGER NOT NULL DEFAULT 0,
                started_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (guild_id, channel_id)
            );
        """)
        await db.commit()


# ── Incense channels ──────────────────────────────────────────────────────────

async def add_channel(guild_id: str, channel_id: str, added_by: str = "") -> bool:
    """Register a channel as an incense channel. Returns True if newly added."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute(
                    """INSERT INTO incense_channels (guild_id, channel_id, added_by)
                       VALUES (?, ?, ?)""",
                    (guild_id, channel_id, added_by),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False


async def remove_channel(guild_id: str, channel_id: str) -> bool:
    """Unregister a channel. Returns True if it existed."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM incense_channels WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_channels(guild_id: str) -> list[str]:
    """All registered incense channel IDs for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id FROM incense_channels WHERE guild_id=? ORDER BY added_at",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def is_incense_channel(guild_id: str, channel_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM incense_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ) as cur:
            return await cur.fetchone() is not None


# ── Active incenses ───────────────────────────────────────────────────────────

async def register_incense(
    guild_id:     str,
    channel_id:   str,
    incense_type: str = "Standard",
    total_spawns: int = 0,
) -> None:
    """Record that an incense has started in a channel."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO active_incenses
                   (guild_id, channel_id, incense_type, total_spawns, paused)
                   VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                     incense_type=excluded.incense_type,
                     total_spawns=excluded.total_spawns,
                     paused=0,
                     started_at=datetime('now')""",
                (guild_id, channel_id, incense_type, total_spawns),
            )
            await db.commit()


async def set_paused(guild_id: str, channel_id: str, paused: bool) -> None:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE active_incenses SET paused=? WHERE guild_id=? AND channel_id=?",
                (1 if paused else 0, guild_id, channel_id),
            )
            await db.commit()


async def clear_incense(guild_id: str, channel_id: str) -> None:
    """Remove incense record (when it expires or is manually cleared)."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM active_incenses WHERE guild_id=? AND channel_id=?",
                (guild_id, channel_id),
            )
            await db.commit()


async def get_active_incenses(guild_id: str) -> list[dict]:
    """All active incense records for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT channel_id, incense_type, total_spawns, paused, started_at
               FROM active_incenses WHERE guild_id=?
               ORDER BY started_at""",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "channel_id":   r[0],
                    "incense_type": r[1],
                    "total_spawns": r[2],
                    "paused":       bool(r[3]),
                    "started_at":   r[4],
                }
                for r in rows
            ]


async def has_active_incense(guild_id: str, channel_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM active_incenses WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ) as cur:
            return await cur.fetchone() is not None


async def get_incense(guild_id: str, channel_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT channel_id, incense_type, total_spawns, paused, started_at
               FROM active_incenses WHERE guild_id=? AND channel_id=?""",
            (guild_id, channel_id),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "channel_id":   row[0],
                "incense_type": row[1],
                "total_spawns": row[2],
                "paused":       bool(row[3]),
                "started_at":   row[4],
            }
