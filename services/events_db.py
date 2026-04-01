"""
services/events_db.py
SQLite storage for two permanent checklists per server.
Schema v2: checklist_targets gains dex_id + evo_family_id for sorting.
Migration is additive — existing DBs get the new columns via ALTER TABLE.
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
