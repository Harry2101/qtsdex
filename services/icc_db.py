"""
services/icc_db.py
SQLite storage for the ICC (Incense Control Center) subsystem.
Isolated database file: data/icc.db

Tables:
  icc_categories         — category definitions (Rare, Gmax, etc.) per guild
  icc_category_channels  — maps each category to fixed Discord channel IDs
  icc_orgs               — one row per org event
  icc_org_categories     — join: which categories are active in an org + progress
  icc_channel_completions— ground-truth per-channel completion records
  icc_timers             — persisted scheduler queue (survives restart)
  icc_audit_log          — every state-changing action
"""

import asyncio
import logging
import os
from typing import Optional

import aiosqlite

DB_PATH = "data/icc.db"
log = logging.getLogger("qtsdex.icc_db")

_write_lock = asyncio.Lock()


# ── Schema initialisation ────────────────────────────────────────────────────

async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS icc_categories (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                required_count  INTEGER NOT NULL,
                coin_value      INTEGER NOT NULL DEFAULT 0,
                helper_role_id  TEXT    NOT NULL DEFAULT '',
                display_order   INTEGER NOT NULL DEFAULT 0,
                active          INTEGER NOT NULL DEFAULT 1,
                reserve_slots   INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (guild_id, name)
            );

            CREATE INDEX IF NOT EXISTS idx_icc_categories_guild
                ON icc_categories (guild_id);

            CREATE TABLE IF NOT EXISTS icc_category_channels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id  INTEGER NOT NULL REFERENCES icc_categories(id) ON DELETE CASCADE,
                guild_id     TEXT    NOT NULL,
                channel_id   TEXT    NOT NULL,
                UNIQUE (guild_id, channel_id)
            );

            CREATE INDEX IF NOT EXISTS idx_icc_cat_channels_category
                ON icc_category_channels (category_id);
            CREATE INDEX IF NOT EXISTS idx_icc_cat_channels_channel
                ON icc_category_channels (guild_id, channel_id);

            CREATE TABLE IF NOT EXISTS icc_orgs (
                id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id                  TEXT    NOT NULL,
                organizer_id              TEXT    NOT NULL,
                label                     TEXT    NOT NULL DEFAULT '',
                status                    TEXT    NOT NULL DEFAULT 'draft',
                announcement_channel_id   TEXT    NOT NULL DEFAULT '',
                announcement_message_id   TEXT    NOT NULL DEFAULT '',
                started_at                TEXT    NOT NULL DEFAULT (datetime('now')),
                published_at              TEXT,
                completed_at              TEXT,
                cancelled_at              TEXT,
                cancelled_by              TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_icc_orgs_guild_status
                ON icc_orgs (guild_id, status);

            CREATE TABLE IF NOT EXISTS icc_org_categories (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id          INTEGER NOT NULL REFERENCES icc_orgs(id) ON DELETE CASCADE,
                category_id     INTEGER NOT NULL REFERENCES icc_categories(id),
                guild_id        TEXT    NOT NULL,
                owner_id        TEXT,
                claimed_at      TEXT,
                assigned_by     TEXT,
                channels_done   INTEGER NOT NULL DEFAULT 0,
                status          TEXT    NOT NULL DEFAULT 'unclaimed',
                completed_at    TEXT,
                UNIQUE (org_id, category_id)
            );

            CREATE INDEX IF NOT EXISTS idx_icc_org_cats_org
                ON icc_org_categories (org_id);
            CREATE INDEX IF NOT EXISTS idx_icc_org_cats_owner
                ON icc_org_categories (org_id, owner_id);
            CREATE INDEX IF NOT EXISTS idx_icc_org_cats_status
                ON icc_org_categories (org_id, status);

            CREATE TABLE IF NOT EXISTS icc_channel_completions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id              INTEGER NOT NULL REFERENCES icc_orgs(id) ON DELETE CASCADE,
                org_category_id     INTEGER NOT NULL REFERENCES icc_org_categories(id) ON DELETE CASCADE,
                guild_id            TEXT    NOT NULL,
                channel_id          TEXT    NOT NULL,
                detection_method    TEXT    NOT NULL DEFAULT 'auto',
                completed_by        TEXT    NOT NULL DEFAULT '',
                completed_at        TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (org_id, channel_id)
            );

            CREATE INDEX IF NOT EXISTS idx_icc_completions_org_cat
                ON icc_channel_completions (org_category_id);
            CREATE INDEX IF NOT EXISTS idx_icc_completions_channel
                ON icc_channel_completions (org_id, channel_id);

            CREATE TABLE IF NOT EXISTS icc_timers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        TEXT    NOT NULL,
                timer_type      TEXT    NOT NULL,
                org_id          INTEGER REFERENCES icc_orgs(id) ON DELETE CASCADE,
                org_category_id INTEGER REFERENCES icc_org_categories(id) ON DELETE CASCADE,
                fire_at         TEXT    NOT NULL,
                fired           INTEGER NOT NULL DEFAULT 0,
                cancelled       INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_icc_timers_pending
                ON icc_timers (fired, cancelled, fire_at)
                WHERE fired = 0 AND cancelled = 0;
            CREATE INDEX IF NOT EXISTS idx_icc_timers_org_cat
                ON icc_timers (org_category_id, timer_type, fired, cancelled);

            CREATE TABLE IF NOT EXISTS icc_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT    NOT NULL,
                user_id     TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                details     TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_icc_audit_guild
                ON icc_audit_log (guild_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS icc_org_reserves (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id          INTEGER NOT NULL REFERENCES icc_orgs(id) ON DELETE CASCADE,
                org_category_id INTEGER NOT NULL REFERENCES icc_org_categories(id) ON DELETE CASCADE,
                owner_id        TEXT    NOT NULL,
                pokemon_name    TEXT    NOT NULL,
                base_name       TEXT    NOT NULL,
                locked          INTEGER NOT NULL DEFAULT 0,
                released        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (org_id, base_name)
            );

            CREATE INDEX IF NOT EXISTS idx_icc_reserves_org
                ON icc_org_reserves (org_id, released);
            CREATE INDEX IF NOT EXISTS idx_icc_reserves_owner
                ON icc_org_reserves (org_id, owner_id);
        """)

        # Migration: add reserve_slots to existing icc_categories tables
        try:
            await db.execute("ALTER TABLE icc_categories ADD COLUMN reserve_slots INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists

        await db.commit()
    log.info("ICC database initialised")


# ── Categories ───────────────────────────────────────────────────────────────

async def create_category(
    guild_id: str, name: str, required_count: int, coin_value: int,
    display_order: int = 0,
) -> Optional[int]:
    """Create a category. Returns its ID, or None on duplicate."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                cur = await db.execute(
                    """INSERT INTO icc_categories
                       (guild_id, name, required_count, coin_value, display_order)
                       VALUES (?, ?, ?, ?, ?)""",
                    (guild_id, name, required_count, coin_value, display_order),
                )
                await db.commit()
                return cur.lastrowid
            except aiosqlite.IntegrityError:
                return None


async def update_category(
    category_id: int, *, required_count: Optional[int] = None,
    coin_value: Optional[int] = None, helper_role_id: Optional[str] = None,
    active: Optional[bool] = None, display_order: Optional[int] = None,
    reserve_slots: Optional[int] = None,
) -> bool:
    """Update category fields. Returns True if row found."""
    sets = []
    params = []
    if required_count is not None:
        sets.append("required_count=?"); params.append(required_count)
    if coin_value is not None:
        sets.append("coin_value=?"); params.append(coin_value)
    if helper_role_id is not None:
        sets.append("helper_role_id=?"); params.append(helper_role_id)
    if active is not None:
        sets.append("active=?"); params.append(1 if active else 0)
    if display_order is not None:
        sets.append("display_order=?"); params.append(display_order)
    if reserve_slots is not None:
        sets.append("reserve_slots=?"); params.append(reserve_slots)
    if not sets:
        return False
    params.append(category_id)
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"UPDATE icc_categories SET {', '.join(sets)} WHERE id=?", params,
            )
            await db.commit()
            return cur.rowcount > 0


async def delete_category(category_id: int) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("DELETE FROM icc_categories WHERE id=?", (category_id,))
            await db.commit()
            return cur.rowcount > 0


async def get_categories(guild_id: str, include_inactive: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "guild_id=?" if include_inactive else "guild_id=? AND active=1"
        async with db.execute(
            f"SELECT * FROM icc_categories WHERE {where} ORDER BY display_order, name",
            (guild_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_category(category_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM icc_categories WHERE id=?", (category_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_category_by_name(guild_id: str, name: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_categories WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Category channels ────────────────────────────────────────────────────────

async def add_category_channels(
    category_id: int, guild_id: str, channel_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Add channels to a category. Returns (added, already_mapped)."""
    added = []
    already = []
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            for cid in channel_ids:
                try:
                    await db.execute(
                        """INSERT INTO icc_category_channels (category_id, guild_id, channel_id)
                           VALUES (?, ?, ?)""",
                        (category_id, guild_id, cid),
                    )
                    added.append(cid)
                except aiosqlite.IntegrityError:
                    already.append(cid)
            await db.commit()
    return added, already


async def remove_category_channels(
    category_id: int, guild_id: str, channel_ids: list[str],
) -> tuple[list[str], list[str]]:
    """Remove channels from a category. Returns (removed, not_found)."""
    removed = []
    not_found = []
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            for cid in channel_ids:
                cur = await db.execute(
                    """DELETE FROM icc_category_channels
                       WHERE category_id=? AND guild_id=? AND channel_id=?""",
                    (category_id, guild_id, cid),
                )
                if cur.rowcount > 0:
                    removed.append(cid)
                else:
                    not_found.append(cid)
            await db.commit()
    return removed, not_found


async def get_category_channels(category_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id FROM icc_category_channels WHERE category_id=?",
            (category_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_channel_category(guild_id: str, channel_id: str) -> Optional[dict]:
    """Look up which category a channel belongs to."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.* FROM icc_categories c
               JOIN icc_category_channels cc ON cc.category_id = c.id
               WHERE cc.guild_id=? AND cc.channel_id=?""",
            (guild_id, channel_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Orgs ─────────────────────────────────────────────────────────────────────

async def create_org(guild_id: str, organizer_id: str, label: str = "") -> int:
    """Create a draft org. Returns its ID."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO icc_orgs (guild_id, organizer_id, label, status)
                   VALUES (?, ?, ?, 'draft')""",
                (guild_id, organizer_id, label),
            )
            await db.commit()
            return cur.lastrowid


async def get_active_org(guild_id: str) -> Optional[dict]:
    """Get the current published org for a guild (if any)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_orgs WHERE guild_id=? AND status='published' LIMIT 1",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_draft_org(guild_id: str) -> Optional[dict]:
    """Get the current draft org for a guild (if any)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_orgs WHERE guild_id=? AND status='draft' LIMIT 1",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_org(org_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM icc_orgs WHERE id=?", (org_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_org_status(
    org_id: int, status: str, **extra_fields,
) -> bool:
    """Transition an org's status. extra_fields go directly to SET clause."""
    sets = ["status=?"]
    params = [status]
    for k, v in extra_fields.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(org_id)
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"UPDATE icc_orgs SET {', '.join(sets)} WHERE id=?", params,
            )
            await db.commit()
            return cur.rowcount > 0


async def get_org_any_active(guild_id: str) -> Optional[dict]:
    """Get any org that is draft or published (blocks new org creation)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_orgs WHERE guild_id=? AND status IN ('draft','published') LIMIT 1",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Reserves ─────────────────────────────────────────────────────────────────

async def create_reserve(
    org_id: int, org_category_id: int, owner_id: str,
    pokemon_name: str, base_name: str,
) -> Optional[int]:
    """Create a reserve pick. Returns ID, or None if base_name already reserved in this org."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                cur = await db.execute(
                    """INSERT INTO icc_org_reserves
                       (org_id, org_category_id, owner_id, pokemon_name, base_name)
                       VALUES (?, ?, ?, ?, ?)""",
                    (org_id, org_category_id, owner_id, pokemon_name, base_name),
                )
                await db.commit()
                return cur.lastrowid
            except aiosqlite.IntegrityError:
                return None


async def get_reserves_for_org(org_id: int, active_only: bool = True) -> list[dict]:
    """Get all reserves for an org. active_only=True excludes released."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "org_id=? AND released=0" if active_only else "org_id=?"
        async with db.execute(
            f"SELECT * FROM icc_org_reserves WHERE {where} ORDER BY created_at",
            (org_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_reserves_for_owner(org_id: int, owner_id: str) -> list[dict]:
    """Get active reserves for a specific owner in an org."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_org_reserves WHERE org_id=? AND owner_id=? AND released=0",
            (org_id, owner_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_reserves_for_org_category(org_category_id: int) -> list[dict]:
    """Get active reserves for a specific org_category."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icc_org_reserves WHERE org_category_id=? AND released=0",
            (org_category_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def release_reserve(reserve_id: int) -> bool:
    """Mark a reserve as released."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE icc_org_reserves SET released=1 WHERE id=? AND released=0",
                (reserve_id,),
            )
            await db.commit()
            return cur.rowcount > 0


async def lock_reserves_for_org(org_id: int) -> int:
    """Lock all active reserves when an org is published. Returns count locked."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE icc_org_reserves SET locked=1 WHERE org_id=? AND released=0",
                (org_id,),
            )
            await db.commit()
            return cur.rowcount


async def find_reserve_by_spawn(org_id: int, spawn_name: str) -> Optional[dict]:
    """Find an active, locked reserve matching a spawn name (by base_name or pokemon_name)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Check against base_name and pokemon_name (exact match, case-insensitive)
        async with db.execute(
            """SELECT * FROM icc_org_reserves
               WHERE org_id=? AND locked=1 AND released=0
                 AND (LOWER(pokemon_name)=LOWER(?) OR LOWER(base_name)=LOWER(?))""",
            (org_id, spawn_name, spawn_name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_reserve_by_owner_and_name(
    org_id: int, owner_id: str, base_name: str,
) -> Optional[dict]:
    """Get a specific reserve by owner and base_name."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM icc_org_reserves
               WHERE org_id=? AND owner_id=? AND LOWER(base_name)=LOWER(?) AND released=0""",
            (org_id, owner_id, base_name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Org categories ───────────────────────────────────────────────────────────

async def create_org_category(org_id: int, category_id: int, guild_id: str) -> int:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO icc_org_categories (org_id, category_id, guild_id)
                   VALUES (?, ?, ?)""",
                (org_id, category_id, guild_id),
            )
            await db.commit()
            return cur.lastrowid


async def get_org_categories(org_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT oc.*, c.name, c.required_count, c.coin_value,
                      c.helper_role_id
               FROM icc_org_categories oc
               JOIN icc_categories c ON c.id = oc.category_id
               WHERE oc.org_id = ?
               ORDER BY c.display_order, c.name""",
            (org_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_org_category(org_category_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT oc.*, c.name, c.required_count, c.coin_value,
                      c.helper_role_id
               FROM icc_org_categories oc
               JOIN icc_categories c ON c.id = oc.category_id
               WHERE oc.id=?""",
            (org_category_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_org_category_by_name(org_id: int, category_name: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT oc.*, c.name, c.required_count, c.coin_value,
                      c.helper_role_id
               FROM icc_org_categories oc
               JOIN icc_categories c ON c.id = oc.category_id
               WHERE oc.org_id=? AND c.name=? COLLATE NOCASE""",
            (org_id, category_name),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_org_category(org_category_id: int, **fields) -> bool:
    if not fields:
        return False
    sets = []
    params = []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(org_category_id)
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"UPDATE icc_org_categories SET {', '.join(sets)} WHERE id=?", params,
            )
            await db.commit()
            return cur.rowcount > 0


# ── Channel completions ──────────────────────────────────────────────────────

async def mark_channel_complete(
    org_id: int, org_category_id: int, guild_id: str,
    channel_id: str, method: str = "auto", completed_by: str = "",
) -> bool:
    """Mark a channel as complete. Returns True if newly marked, False if already done."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute(
                    """INSERT INTO icc_channel_completions
                       (org_id, org_category_id, guild_id, channel_id, detection_method, completed_by)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (org_id, org_category_id, guild_id, channel_id, method, completed_by),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False


async def get_completed_channels(org_category_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id FROM icc_channel_completions WHERE org_category_id=?",
            (org_category_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def is_channel_completed(org_id: int, channel_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM icc_channel_completions WHERE org_id=? AND channel_id=?",
            (org_id, channel_id),
        ) as cur:
            return await cur.fetchone() is not None


async def resolve_channel_to_org_category(
    guild_id: str, org_id: int, channel_id: str,
) -> Optional[dict]:
    """Given a channel, find which org_category it belongs to in the given org."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT oc.*, c.name, c.required_count, c.coin_value,
                      c.helper_role_id
               FROM icc_category_channels cc
               JOIN icc_org_categories oc ON oc.category_id = cc.category_id AND oc.org_id = ?
               JOIN icc_categories c ON c.id = oc.category_id
               WHERE cc.guild_id=? AND cc.channel_id=?""",
            (org_id, guild_id, channel_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Timers ───────────────────────────────────────────────────────────────────

async def create_timer(
    guild_id: str, timer_type: str, fire_at: str,
    org_id: Optional[int] = None, org_category_id: Optional[int] = None,
) -> int:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO icc_timers
                   (guild_id, timer_type, org_id, org_category_id, fire_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, timer_type, org_id, org_category_id, fire_at),
            )
            await db.commit()
            return cur.lastrowid


async def cancel_timers(
    org_category_id: int, timer_type: Optional[str] = None,
) -> int:
    """Cancel all pending timers for an org_category. Returns count cancelled."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            if timer_type:
                cur = await db.execute(
                    """UPDATE icc_timers SET cancelled=1
                       WHERE org_category_id=? AND timer_type=? AND fired=0 AND cancelled=0""",
                    (org_category_id, timer_type),
                )
            else:
                cur = await db.execute(
                    """UPDATE icc_timers SET cancelled=1
                       WHERE org_category_id=? AND fired=0 AND cancelled=0""",
                    (org_category_id,),
                )
            await db.commit()
            return cur.rowcount


async def cancel_org_timers(org_id: int) -> int:
    """Cancel all pending timers for an entire org."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE icc_timers SET cancelled=1
                   WHERE org_id=? AND fired=0 AND cancelled=0""",
                (org_id,),
            )
            await db.commit()
            return cur.rowcount


async def get_due_timers() -> list[dict]:
    """Get all timers that are due to fire."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM icc_timers
               WHERE fired=0 AND cancelled=0 AND fire_at <= datetime('now')
               ORDER BY fire_at""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def mark_timer_fired(timer_id: int) -> None:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE icc_timers SET fired=1 WHERE id=?", (timer_id,),
            )
            await db.commit()


async def get_pending_timers(org_id: int) -> list[dict]:
    """Get all pending timers for an org (for status display)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM icc_timers
               WHERE org_id=? AND fired=0 AND cancelled=0
               ORDER BY fire_at""",
            (org_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def cancel_timer_by_id(timer_id: int) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE icc_timers SET cancelled=1 WHERE id=? AND fired=0 AND cancelled=0",
                (timer_id,),
            )
            await db.commit()
            return cur.rowcount > 0


# ── Audit log ────────────────────────────────────────────────────────────────

async def log_action(
    guild_id: str, user_id: str, action: str, details: str = "",
) -> None:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO icc_audit_log (guild_id, user_id, action, details)
                   VALUES (?, ?, ?, ?)""",
                (guild_id, user_id, action, details),
            )
            await db.commit()


async def get_audit_log(
    guild_id: str, limit: int = 20, user_id: Optional[str] = None,
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        conditions = ["guild_id=?"]
        params: list = [guild_id]
        if user_id:
            conditions.append("user_id=?")
            params.append(user_id)
        params.append(limit)
        where = " AND ".join(conditions)
        async with db.execute(
            f"SELECT * FROM icc_audit_log WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
