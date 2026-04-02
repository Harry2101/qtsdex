"""
services/events_db.py
SQLite storage for two permanent checklists per server.
Schema v3: adds friends + friend_requests tables for checklist sharing.
Migration is additive — existing DBs get new columns/tables safely.
"""

import os
import aiosqlite

DB_PATH = "data/events.db"


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS checklists (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         TEXT    NOT NULL,
                checklist_type   TEXT    NOT NULL CHECK(checklist_type IN ('normal','event')),
                label            TEXT    NOT NULL DEFAULT '',
                UNIQUE(guild_id, checklist_type)
            );

            CREATE TABLE IF NOT EXISTS checklist_targets (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id   INTEGER NOT NULL REFERENCES checklists(id) ON DELETE CASCADE,
                pokemon        TEXT    NOT NULL,
                dex_id         INTEGER NOT NULL DEFAULT 0,
                evo_family_id  INTEGER NOT NULL DEFAULT 0,
                UNIQUE(checklist_id, pokemon)
            );

            CREATE TABLE IF NOT EXISTS user_catches (
                checklist_id   INTEGER NOT NULL REFERENCES checklists(id)  ON DELETE CASCADE,
                target_id      INTEGER NOT NULL REFERENCES checklist_targets(id) ON DELETE CASCADE,
                user_id        TEXT    NOT NULL,
                PRIMARY KEY (checklist_id, target_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS friends (
                user_id    TEXT NOT NULL,
                friend_id  TEXT NOT NULL,
                since      TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, friend_id)
            );

            CREATE TABLE IF NOT EXISTS friend_requests (
                from_id    TEXT NOT NULL,
                to_id      TEXT NOT NULL,
                created    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (from_id, to_id)
            );
        """)
        # Migrate existing DBs — only add columns that don't exist yet
        async with db.execute("PRAGMA table_info(checklist_targets)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        migrations = {
            "dex_id":        "INTEGER NOT NULL DEFAULT 0",
            "evo_family_id": "INTEGER NOT NULL DEFAULT 0",
        }
        for col_name, col_def in migrations.items():
            if col_name not in existing:
                await db.execute(
                    f"ALTER TABLE checklist_targets ADD COLUMN {col_name} {col_def}"
                )
        await db.commit()


# ── Checklist bootstrap ───────────────────────────────────────────────────────

async def ensure_checklist(guild_id: str, ctype: str, label: str = "") -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO checklists (guild_id, checklist_type, label)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, checklist_type) DO NOTHING""",
            (guild_id, ctype, label),
        )
        await db.commit()
        async with db.execute(
            "SELECT id, label FROM checklists WHERE guild_id=? AND checklist_type=?",
            (guild_id, ctype),
        ) as cur:
            row = await cur.fetchone()
            return {"id": row[0], "label": row[1]}


async def rename_checklist(guild_id: str, ctype: str, new_label: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE checklists SET label=? WHERE guild_id=? AND checklist_type=?",
            (new_label, guild_id, ctype),
        )
        await db.commit()


# ── Targets ───────────────────────────────────────────────────────────────────

async def add_pokemon(
    checklist_id:  int,
    pokemon:       str,
    dex_id:        int = 0,
    evo_family_id: int = 0,
) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """INSERT INTO checklist_targets
                   (checklist_id, pokemon, dex_id, evo_family_id)
                   VALUES (?, ?, ?, ?)""",
                (checklist_id, pokemon.lower(), dex_id, evo_family_id),
            )
            await db.commit()
            return True, f"Added **{pokemon.replace('-', ' ').title()}**."
        except aiosqlite.IntegrityError:
            return False, f"**{pokemon.replace('-', ' ').title()}** is already on this list."


async def remove_pokemon(checklist_id: int, pokemon: str) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM checklist_targets WHERE checklist_id=? AND pokemon=?",
            (checklist_id, pokemon.lower()),
        )
        await db.commit()
        if cur.rowcount:
            return True, f"Removed **{pokemon.replace('-', ' ').title()}**."
        return False, f"**{pokemon.replace('-', ' ').title()}** wasn't on this list."


async def get_targets(checklist_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, pokemon, dex_id, evo_family_id
               FROM checklist_targets WHERE checklist_id=? ORDER BY pokemon""",
            (checklist_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"id": r[0], "pokemon": r[1], "dex_id": r[2], "evo_family_id": r[3]}
                for r in rows
            ]


async def clear_targets(checklist_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM checklist_targets WHERE checklist_id=?",
            (checklist_id,),
        )
        await db.commit()
        return cur.rowcount


async def get_target_by_pokemon(checklist_id: int, pokemon: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, pokemon, dex_id, evo_family_id
               FROM checklist_targets WHERE checklist_id=? AND pokemon=?""",
            (checklist_id, pokemon.lower()),
        ) as cur:
            row = await cur.fetchone()
            return (
                {"id": row[0], "pokemon": row[1], "dex_id": row[2], "evo_family_id": row[3]}
                if row else None
            )


async def get_target_names(checklist_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pokemon FROM checklist_targets WHERE checklist_id=? ORDER BY pokemon",
            (checklist_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def count_targets(checklist_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM checklist_targets WHERE checklist_id=?",
            (checklist_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ── Catches ───────────────────────────────────────────────────────────────────

async def mark_caught(checklist_id: int, target_id: int, user_id: str) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO user_catches (checklist_id, target_id, user_id) VALUES (?,?,?)",
                (checklist_id, target_id, user_id),
            )
            await db.commit()
            return True, "Marked as caught ✅"
        except aiosqlite.IntegrityError:
            return False, "Already marked as caught."


async def unmark_caught(checklist_id: int, target_id: int, user_id: str) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM user_catches WHERE checklist_id=? AND target_id=? AND user_id=?",
            (checklist_id, target_id, user_id),
        )
        await db.commit()
        if cur.rowcount:
            return True, "Unmarked ⬜"
        return False, "That wasn't marked as caught."


async def clear_user_catches(checklist_id: int, user_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM user_catches WHERE checklist_id=? AND user_id=?",
            (checklist_id, user_id),
        )
        await db.commit()
        return cur.rowcount


async def get_user_catches(checklist_id: int, user_id: str) -> set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT target_id FROM user_catches WHERE checklist_id=? AND user_id=?",
            (checklist_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
            return {r[0] for r in rows}


# ── Friends ──────────────────────────────────────────────────────────────────

async def send_friend_request(from_id: str, to_id: str) -> str:
    """Send a friend request. Returns a status string."""
    if from_id == to_id:
        return "self"
    async with aiosqlite.connect(DB_PATH) as db:
        # Already friends?
        async with db.execute(
            "SELECT 1 FROM friends WHERE user_id=? AND friend_id=?",
            (from_id, to_id),
        ) as cur:
            if await cur.fetchone():
                return "already_friends"
        # Pending request already?
        async with db.execute(
            "SELECT 1 FROM friend_requests WHERE from_id=? AND to_id=?",
            (from_id, to_id),
        ) as cur:
            if await cur.fetchone():
                return "already_sent"
        # They sent us one? Auto-accept.
        async with db.execute(
            "SELECT 1 FROM friend_requests WHERE from_id=? AND to_id=?",
            (to_id, from_id),
        ) as cur:
            if await cur.fetchone():
                await db.execute("DELETE FROM friend_requests WHERE from_id=? AND to_id=?", (to_id, from_id))
                await db.execute("INSERT OR IGNORE INTO friends (user_id, friend_id) VALUES (?,?)", (from_id, to_id))
                await db.execute("INSERT OR IGNORE INTO friends (user_id, friend_id) VALUES (?,?)", (to_id, from_id))
                await db.commit()
                return "auto_accepted"
        # Send new request
        await db.execute("INSERT OR IGNORE INTO friend_requests (from_id, to_id) VALUES (?,?)", (from_id, to_id))
        await db.commit()
        return "sent"


async def accept_friend_request(from_id: str, to_id: str) -> bool:
    """Accept a pending request. Returns True if one existed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM friend_requests WHERE from_id=? AND to_id=?",
            (from_id, to_id),
        )
        if cur.rowcount == 0:
            return False
        await db.execute("INSERT OR IGNORE INTO friends (user_id, friend_id) VALUES (?,?)", (from_id, to_id))
        await db.execute("INSERT OR IGNORE INTO friends (user_id, friend_id) VALUES (?,?)", (to_id, from_id))
        await db.commit()
        return True


async def decline_friend_request(from_id: str, to_id: str) -> bool:
    """Decline a pending request. Returns True if one existed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM friend_requests WHERE from_id=? AND to_id=?",
            (from_id, to_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def remove_friend(user_id: str, friend_id: str) -> bool:
    """Remove a friendship (both directions). Returns True if existed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur1 = await db.execute("DELETE FROM friends WHERE user_id=? AND friend_id=?", (user_id, friend_id))
        cur2 = await db.execute("DELETE FROM friends WHERE user_id=? AND friend_id=?", (friend_id, user_id))
        await db.commit()
        return (cur1.rowcount + cur2.rowcount) > 0


async def get_friends(user_id: str) -> list[str]:
    """Return list of friend user IDs."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT friend_id FROM friends WHERE user_id=? ORDER BY since",
            (user_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_incoming_requests(user_id: str) -> list[str]:
    """Return list of user IDs who sent requests to this user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT from_id FROM friend_requests WHERE to_id=? ORDER BY created",
            (user_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_outgoing_requests(user_id: str) -> list[str]:
    """Return list of user IDs this user has sent requests to."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT to_id FROM friend_requests WHERE from_id=? ORDER BY created",
            (user_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_friend_count(user_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM friends WHERE user_id=?", (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
