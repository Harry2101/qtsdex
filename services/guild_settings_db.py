"""
services/guild_settings_db.py
Per-server settings stored in SQLite.
Handles incense manager role configuration and Operation Dex bot ID per guild.
"""

import asyncio
import os
from typing import Optional

import aiosqlite

DB_PATH = "data/guild_settings.db"

_write_lock = asyncio.Lock()

# In-memory cache: guild_id -> {key: value}
_cache: dict[str, dict[str, str]] = {}


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id  TEXT NOT NULL,
                key       TEXT NOT NULL,
                value     TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            );
        """)
        await db.commit()
        # Pre-load cache
        async with db.execute("SELECT guild_id, key, value FROM guild_settings") as cur:
            async for row in cur:
                _cache.setdefault(row[0], {})[row[1]] = row[2]


async def get(guild_id: str, key: str) -> Optional[str]:
    """Get a guild setting (cached)."""
    cached = _cache.get(guild_id, {}).get(key)
    if cached is not None:
        return cached
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM guild_settings WHERE guild_id=? AND key=?",
            (guild_id, key),
        ) as cur:
            row = await cur.fetchone()
            if row:
                _cache.setdefault(guild_id, {})[key] = row[0]
                return row[0]
    return None


async def set_val(guild_id: str, key: str, value: str) -> None:
    """Set a guild setting."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO guild_settings (guild_id, key, value)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value""",
                (guild_id, key, value),
            )
            await db.commit()
    _cache.setdefault(guild_id, {})[key] = value


async def delete(guild_id: str, key: str) -> bool:
    """Delete a guild setting. Returns True if it existed."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM guild_settings WHERE guild_id=? AND key=?",
                (guild_id, key),
            )
            await db.commit()
            existed = cur.rowcount > 0
    if guild_id in _cache:
        _cache[guild_id].pop(key, None)
    return existed


# ── Convenience: Incense Manager Role ────────────────────────────────────────

async def get_incense_role(guild_id: str) -> Optional[int]:
    """Get the incense manager role ID for a guild."""
    val = await get(guild_id, "incense_manager_role")
    return int(val) if val else None


async def set_incense_role(guild_id: str, role_id: int) -> None:
    """Set the incense manager role for a guild."""
    await set_val(guild_id, "incense_manager_role", str(role_id))


async def clear_incense_role(guild_id: str) -> bool:
    """Remove the incense manager role setting for a guild."""
    return await delete(guild_id, "incense_manager_role")


# ── Convenience: Operation Dex Bot ID ────────────────────────────────────────

async def get_opdex_bot_id(guild_id: str) -> Optional[int]:
    """Get the Operation Dex bot ID for a guild (falls back to default)."""
    val = await get(guild_id, "opdex_bot_id")
    return int(val) if val else None


async def set_opdex_bot_id(guild_id: str, bot_id: int) -> None:
    """Set the Operation Dex bot ID for a guild."""
    await set_val(guild_id, "opdex_bot_id", str(bot_id))
