"""
services/pokemon_list_db.py
SQLite storage for the Pokemon List system.
Shares the ICC database file: data/icc.db

Tables:
  pokemon_lists          -- base Pokemon + their forms, grouped by category type
  pokemon_events         -- named events (e.g. "Valentine's 2025")
  pokemon_event_entries  -- custom Pokemon name strings within an event

Category types: rare, gmax, eevo, regional
"normal" is implicit -- any Pokemon not in the above 4 lists.
"""

import asyncio
import logging
from typing import Optional

import aiosqlite

DB_PATH = "data/icc.db"
log = logging.getLogger("qtsdex.pokemon_list_db")

_write_lock = asyncio.Lock()

CATEGORY_TYPES = ("rare", "gmax", "eevo", "regional")


# -- Schema initialisation ---------------------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS pokemon_lists (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                base_name     TEXT    NOT NULL,
                form_name     TEXT    NOT NULL,
                category_type TEXT    NOT NULL,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (base_name, form_name)
            );

            CREATE INDEX IF NOT EXISTS idx_pokemon_lists_base
                ON pokemon_lists (LOWER(base_name));
            CREATE INDEX IF NOT EXISTS idx_pokemon_lists_category
                ON pokemon_lists (category_type);

            CREATE TABLE IF NOT EXISTS pokemon_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pokemon_event_entries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id      INTEGER NOT NULL REFERENCES pokemon_events(id) ON DELETE CASCADE,
                pokemon_name  TEXT    NOT NULL,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (event_id, pokemon_name)
            );

            CREATE INDEX IF NOT EXISTS idx_pokemon_event_entries_event
                ON pokemon_event_entries (event_id);
        """)
        await db.commit()
    log.info("Pokemon list database initialised")


# -- Pokemon lists -----------------------------------------------------------

async def add_pokemon(base_name: str, forms: list[str], category_type: str) -> tuple[int, int]:
    """
    Add a base Pokemon and all its forms to a category list.
    Returns (added_count, already_existed_count).
    """
    added = 0
    existed = 0
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            for form in forms:
                try:
                    await db.execute(
                        """INSERT INTO pokemon_lists (base_name, form_name, category_type)
                           VALUES (?, ?, ?)""",
                        (base_name, form, category_type),
                    )
                    added += 1
                except aiosqlite.IntegrityError:
                    existed += 1
            await db.commit()
    return added, existed


async def remove_pokemon(base_name: str) -> int:
    """Remove a Pokemon and all its forms from all lists. Returns count removed."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM pokemon_lists WHERE LOWER(base_name)=LOWER(?)",
                (base_name,),
            )
            await db.commit()
            return cur.rowcount


async def get_pokemon_by_base(base_name: str) -> list[dict]:
    """Get all forms of a base Pokemon."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pokemon_lists WHERE LOWER(base_name)=LOWER(?)",
            (base_name,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_pokemon_list(category_type: str) -> list[dict]:
    """Get all Pokemon in a category type."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pokemon_lists WHERE category_type=? ORDER BY base_name, form_name",
            (category_type,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_pokemon() -> list[dict]:
    """Get all Pokemon across all category lists."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pokemon_lists ORDER BY category_type, base_name, form_name",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_base_names() -> list[str]:
    """Get distinct base names across all lists."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT base_name FROM pokemon_lists ORDER BY base_name",
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_category_type_for_pokemon(name: str) -> Optional[str]:
    """
    Check if a Pokemon name (base or form) is in any category list.
    Returns the category_type or None if not found (i.e. 'normal').
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Check form_name first (exact match for spawns like "Vivillon Fancy")
        async with db.execute(
            "SELECT category_type FROM pokemon_lists WHERE LOWER(form_name)=LOWER(?) LIMIT 1",
            (name,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]
        # Check base_name
        async with db.execute(
            "SELECT category_type FROM pokemon_lists WHERE LOWER(base_name)=LOWER(?) LIMIT 1",
            (name,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def find_base_name_for_spawn(spawn_name: str) -> Optional[str]:
    """
    Given a spawn name, find the base_name it belongs to.
    Checks form_name first, then base_name.
    Returns base_name or None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT base_name FROM pokemon_lists WHERE LOWER(form_name)=LOWER(?) LIMIT 1",
            (spawn_name,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]
        async with db.execute(
            "SELECT base_name FROM pokemon_lists WHERE LOWER(base_name)=LOWER(?) LIMIT 1",
            (spawn_name,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_all_form_names() -> list[str]:
    """Get all form names (for matching spawn embeds)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT form_name FROM pokemon_lists ORDER BY form_name",
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


# -- Events ------------------------------------------------------------------

async def create_event(name: str) -> Optional[int]:
    """Create an event. Returns ID or None if duplicate."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                cur = await db.execute(
                    "INSERT INTO pokemon_events (name) VALUES (?)", (name,),
                )
                await db.commit()
                return cur.lastrowid
            except aiosqlite.IntegrityError:
                return None


async def delete_event(name: str) -> bool:
    """Delete an event and all its entries."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM pokemon_events WHERE LOWER(name)=LOWER(?)", (name,),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_event(name: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pokemon_events WHERE LOWER(name)=LOWER(?)", (name,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_events(active_only: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "WHERE active=1" if active_only else ""
        async with db.execute(
            f"SELECT * FROM pokemon_events {where} ORDER BY name",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_event_active(name: str, active: bool) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE pokemon_events SET active=? WHERE LOWER(name)=LOWER(?)",
                (1 if active else 0, name),
            )
            await db.commit()
            return cur.rowcount > 0


# -- Event entries -----------------------------------------------------------

async def add_event_entry(event_id: int, pokemon_name: str) -> bool:
    """Add a Pokemon to an event. Returns True if added, False if duplicate."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute(
                    "INSERT INTO pokemon_event_entries (event_id, pokemon_name) VALUES (?, ?)",
                    (event_id, pokemon_name),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False


async def remove_event_entry(event_id: int, pokemon_name: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM pokemon_event_entries WHERE event_id=? AND LOWER(pokemon_name)=LOWER(?)",
                (event_id, pokemon_name),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_event_entries(event_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pokemon_name FROM pokemon_event_entries WHERE event_id=? ORDER BY pokemon_name",
            (event_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_all_event_pokemon_names() -> list[str]:
    """Get all active event Pokemon names (for autocomplete and spawn matching)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT DISTINCT ee.pokemon_name
               FROM pokemon_event_entries ee
               JOIN pokemon_events e ON e.id = ee.event_id
               WHERE e.active = 1
               ORDER BY ee.pokemon_name""",
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def is_event_pokemon(name: str) -> bool:
    """Check if a name matches an active event Pokemon."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM pokemon_event_entries ee
               JOIN pokemon_events e ON e.id = ee.event_id
               WHERE e.active = 1 AND LOWER(ee.pokemon_name)=LOWER(?)
               LIMIT 1""",
            (name,),
        ) as cur:
            return await cur.fetchone() is not None


async def is_pokemon_known(name: str) -> bool:
    """
    Check if a spawn name is known in ANY list (category lists or active events).
    Returns True if found anywhere, False if completely unknown.
    """
    cat_type = await get_category_type_for_pokemon(name)
    if cat_type is not None:
        return True
    if await is_event_pokemon(name):
        return True
    return False
