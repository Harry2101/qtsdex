"""
services/catch_db.py
SQLite storage for the catch-tracking & grind-session system.

Tables:
  catch_sessions     — active / completed grind sessions per guild+channel
  catch_events       — individual catch records (pokemon spawned, caught, who, how fast)
  catch_user_daily   — per-user daily aggregates (for cheap daily/weekly/monthly rollups)

Design notes:
  • Raw catch_events are the ground truth; aggregates are derived.
  • We only flush pending in-memory events to the DB in batches (every N catches
    or on session end) to minimise write pressure.
  • Daily aggregates are upserted once per day per user; cheap rolling stats
    come from summing these rows rather than scanning all raw events.
"""

import asyncio
import logging
import os
from typing import Optional

import aiosqlite

DB_PATH = "data/catch.db"
log = logging.getLogger("qtsdex.catch_db")

_write_lock = asyncio.Lock()

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS catch_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      TEXT    NOT NULL,
    channel_id    TEXT    NOT NULL,
    started_by    TEXT    NOT NULL,          -- user_id who ran !catchstart
    started_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT,                      -- NULL = still active / paused
    paused_at     TEXT,                      -- NULL = not paused
    total_paused  INTEGER NOT NULL DEFAULT 0,-- seconds accumulated in pauses
    state         TEXT    NOT NULL DEFAULT 'active',  -- active|paused|ended
    label         TEXT    NOT NULL DEFAULT ''         -- optional user label
);

CREATE TABLE IF NOT EXISTS catch_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES catch_sessions(id) ON DELETE CASCADE,
    guild_id      TEXT    NOT NULL,
    channel_id    TEXT    NOT NULL,
    user_id       TEXT    NOT NULL,
    user_name     TEXT    NOT NULL DEFAULT '',
    pokemon_name  TEXT    NOT NULL DEFAULT '',
    spawn_msg_id  TEXT    NOT NULL DEFAULT '',   -- message ID of the spawn embed
    catch_msg_id  TEXT    NOT NULL DEFAULT '',   -- message ID of the !c command
    confirm_msg_id TEXT   NOT NULL DEFAULT '',   -- message ID of the bot's confirm
    spawned_at    TEXT    NOT NULL,              -- ISO datetime
    caught_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    reaction_ms   INTEGER NOT NULL DEFAULT 0,    -- ms from spawn to catch command
    is_session    INTEGER NOT NULL DEFAULT 1,    -- 1 = inside a session, 0 = passive
    UNIQUE(guild_id, confirm_msg_id)             -- deduplicate on bot confirm message
);

CREATE TABLE IF NOT EXISTS catch_user_daily (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      TEXT    NOT NULL,
    user_id       TEXT    NOT NULL,
    user_name     TEXT    NOT NULL DEFAULT '',
    day           TEXT    NOT NULL,              -- YYYY-MM-DD
    catches       INTEGER NOT NULL DEFAULT 0,
    total_ms      INTEGER NOT NULL DEFAULT 0,    -- sum of reaction_ms for the day
    fastest_ms    INTEGER NOT NULL DEFAULT 0,    -- best reaction time that day
    unique_pokemon TEXT   NOT NULL DEFAULT '',   -- JSON list for dedup later
    UNIQUE(guild_id, user_id, day)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ce_session    ON catch_events (session_id);
CREATE INDEX IF NOT EXISTS idx_ce_guild_user ON catch_events (guild_id, user_id, caught_at);
CREATE INDEX IF NOT EXISTS idx_ce_guild_ch   ON catch_events (guild_id, channel_id, caught_at);
CREATE INDEX IF NOT EXISTS idx_cs_guild_ch   ON catch_sessions (guild_id, channel_id, state);
CREATE INDEX IF NOT EXISTS idx_cud_guild     ON catch_user_daily (guild_id, user_id, day);
"""

_MIGRATIONS = [
    # add columns if upgrading from older schema
]


async def init_db() -> None:
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
        for migration in _MIGRATIONS:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass


# ── Sessions ──────────────────────────────────────────────────────────────────

async def start_session(
    guild_id: str,
    channel_id: str,
    started_by: str,
    label: str = "",
) -> int:
    """Create a new session. Returns the session ID."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO catch_sessions (guild_id, channel_id, started_by, label, state)
                   VALUES (?, ?, ?, ?, 'active')""",
                (guild_id, channel_id, started_by, label),
            )
            await db.commit()
            return cur.lastrowid


async def get_active_session(guild_id: str, channel_id: str) -> Optional[dict]:
    """Return the active/paused session for this channel, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, started_by, started_at, paused_at, total_paused, state, label
               FROM catch_sessions
               WHERE guild_id=? AND channel_id=? AND state IN ('active','paused')
               ORDER BY started_at DESC LIMIT 1""",
            (guild_id, channel_id),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "started_by": row[1],
                "started_at": row[2],
                "paused_at": row[3],
                "total_paused": row[4],
                "state": row[5],
                "label": row[6],
            }


async def pause_session(session_id: int, paused_at: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE catch_sessions SET state='paused', paused_at=? WHERE id=? AND state='active'",
                (paused_at, session_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def resume_session(session_id: int, resumed_at_iso: str, extra_paused_ms: int) -> bool:
    """Resume a paused session. extra_paused_ms = ms elapsed since paused_at."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE catch_sessions
                   SET state='active', paused_at=NULL,
                       total_paused = total_paused + ?
                   WHERE id=? AND state='paused'""",
                (extra_paused_ms // 1000, session_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def end_session(session_id: int, ended_at: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE catch_sessions
                   SET state='ended', ended_at=?, paused_at=NULL
                   WHERE id=? AND state IN ('active','paused')""",
                (ended_at, session_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_session_by_id(session_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, guild_id, channel_id, started_by, started_at, ended_at,
                      paused_at, total_paused, state, label
               FROM catch_sessions WHERE id=?""",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0], "guild_id": row[1], "channel_id": row[2],
                "started_by": row[3], "started_at": row[4], "ended_at": row[5],
                "paused_at": row[6], "total_paused": row[7],
                "state": row[8], "label": row[9],
            }


# ── Catch events ──────────────────────────────────────────────────────────────

async def record_catch(
    session_id: int,
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_name: str,
    pokemon_name: str,
    spawn_msg_id: str,
    catch_msg_id: str,
    confirm_msg_id: str,
    spawned_at: str,
    caught_at: str,
    reaction_ms: int,
    is_session: bool = True,
) -> int:
    """
    Insert a catch event. Returns new row ID or 0 if duplicate (same confirm_msg_id).
    """
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT OR IGNORE INTO catch_events
                   (session_id, guild_id, channel_id, user_id, user_name,
                    pokemon_name, spawn_msg_id, catch_msg_id, confirm_msg_id,
                    spawned_at, caught_at, reaction_ms, is_session)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, guild_id, channel_id, user_id, user_name,
                    pokemon_name, spawn_msg_id, catch_msg_id, confirm_msg_id,
                    spawned_at, caught_at, reaction_ms, 1 if is_session else 0,
                ),
            )
            await db.commit()
            return cur.lastrowid if cur.rowcount > 0 else 0


async def get_session_catches(session_id: int) -> list[dict]:
    """Return all catch events for a session, newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_id, user_name, pokemon_name, caught_at, reaction_ms
               FROM catch_events WHERE session_id=? ORDER BY caught_at DESC""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "user_id": r[0], "user_name": r[1], "pokemon_name": r[2],
            "caught_at": r[3], "reaction_ms": r[4],
        }
        for r in rows
    ]


async def get_session_stats(session_id: int) -> dict:
    """
    Aggregate stats for a single session.
    Returns dict with per-user breakdown and overall totals.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Per-user breakdown
        async with db.execute(
            """SELECT user_id, user_name,
                      COUNT(*) as catches,
                      MIN(reaction_ms) as fastest,
                      AVG(reaction_ms) as avg_ms,
                      COUNT(DISTINCT pokemon_name) as unique_pkmn
               FROM catch_events
               WHERE session_id=?
               GROUP BY user_id ORDER BY catches DESC""",
            (session_id,),
        ) as cur:
            user_rows = await cur.fetchall()

        # Overall totals
        async with db.execute(
            """SELECT COUNT(*) as total,
                      MIN(reaction_ms) as fastest,
                      AVG(reaction_ms) as avg_ms,
                      COUNT(DISTINCT pokemon_name) as unique_pkmn,
                      COUNT(DISTINCT user_id) as catchers
               FROM catch_events WHERE session_id=?""",
            (session_id,),
        ) as cur:
            tot = await cur.fetchone()

        # Fastest single catch detail
        async with db.execute(
            """SELECT user_name, pokemon_name, reaction_ms, caught_at
               FROM catch_events WHERE session_id=? AND reaction_ms > 0
               ORDER BY reaction_ms ASC LIMIT 1""",
            (session_id,),
        ) as cur:
            fastest_row = await cur.fetchone()

    users = [
        {
            "user_id": r[0], "user_name": r[1], "catches": r[2],
            "fastest_ms": r[3] or 0, "avg_ms": round(r[4] or 0),
            "unique_pokemon": r[5],
        }
        for r in user_rows
    ]
    total = tot[0] if tot else 0
    return {
        "users": users,
        "total_catches": total,
        "fastest_ms": tot[1] or 0 if tot else 0,
        "avg_ms": round(tot[2] or 0) if tot else 0,
        "unique_pokemon": tot[3] or 0 if tot else 0,
        "catchers": tot[4] or 0 if tot else 0,
        "fastest_detail": {
            "user_name": fastest_row[0],
            "pokemon_name": fastest_row[1],
            "reaction_ms": fastest_row[2],
            "caught_at": fastest_row[3],
        } if fastest_row else None,
    }


# ── Daily aggregates ──────────────────────────────────────────────────────────

async def upsert_daily(
    guild_id: str,
    user_id: str,
    user_name: str,
    day: str,          # YYYY-MM-DD
    catches: int,
    total_ms: int,
    fastest_ms: int,
    pokemon_names: list[str],
) -> None:
    """
    Upsert today's aggregate for a user.
    Called once at the end of each session (or batch flush) — not per-catch.
    """
    import json
    names_json = json.dumps(sorted(set(pokemon_names)))
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            # Fetch existing to merge properly
            async with db.execute(
                "SELECT catches, total_ms, fastest_ms, unique_pokemon FROM catch_user_daily WHERE guild_id=? AND user_id=? AND day=?",
                (guild_id, user_id, day),
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                prev_names = set()
                try:
                    prev_names = set(json.loads(existing[3] or "[]"))
                except Exception:
                    pass
                merged_names = json.dumps(sorted(prev_names | set(pokemon_names)))
                new_fastest = min(fastest_ms, existing[2]) if existing[2] > 0 else fastest_ms
                await db.execute(
                    """UPDATE catch_user_daily
                       SET user_name=?, catches=catches+?, total_ms=total_ms+?,
                           fastest_ms=?, unique_pokemon=?
                       WHERE guild_id=? AND user_id=? AND day=?""",
                    (user_name, catches, total_ms, new_fastest, merged_names,
                     guild_id, user_id, day),
                )
            else:
                await db.execute(
                    """INSERT INTO catch_user_daily
                       (guild_id, user_id, user_name, day, catches, total_ms, fastest_ms, unique_pokemon)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (guild_id, user_id, user_name, day, catches, total_ms, fastest_ms, names_json),
                )
            await db.commit()


# ── Leaderboards & stats queries ──────────────────────────────────────────────

async def get_leaderboard(
    guild_id: str,
    period: str = "all",   # 'today' | 'week' | 'month' | 'all'
    limit: int = 10,
) -> list[dict]:
    """
    Catch leaderboard from daily aggregates.
    Returns [{user_id, user_name, catches, fastest_ms, avg_ms}] sorted by catches desc.
    """
    if period == "today":
        day_filter = "AND day = date('now')"
    elif period == "week":
        day_filter = "AND day >= date('now', '-6 days')"
    elif period == "month":
        day_filter = "AND day >= date('now', 'start of month')"
    else:
        day_filter = ""

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT user_id, user_name,
                       SUM(catches) as total,
                       MIN(fastest_ms) as fastest,
                       CASE WHEN SUM(catches) > 0
                            THEN CAST(SUM(total_ms) AS REAL) / SUM(catches)
                            ELSE 0 END as avg_ms
                FROM catch_user_daily
                WHERE guild_id=? AND catches > 0 {day_filter}
                GROUP BY user_id ORDER BY total DESC LIMIT ?""",
            (guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()

    return [
        {
            "user_id": r[0], "user_name": r[1], "catches": r[2],
            "fastest_ms": r[3] or 0, "avg_ms": round(r[4] or 0),
        }
        for r in rows
    ]


async def get_user_lifetime_stats(guild_id: str, user_id: str) -> dict:
    """
    Full lifetime stats for a user across all time from daily aggregates.
    Includes daily/weekly/monthly breakdowns.
    """
    import json

    async with aiosqlite.connect(DB_PATH) as db:
        # All-time totals
        async with db.execute(
            """SELECT SUM(catches), MIN(fastest_ms), SUM(total_ms),
                      COUNT(DISTINCT day), MAX(user_name)
               FROM catch_user_daily WHERE guild_id=? AND user_id=?""",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        total = row[0] or 0
        fastest_all = row[1] or 0
        total_ms_all = row[2] or 0
        active_days = row[3] or 0
        user_name = row[4] or ""

        # Today
        async with db.execute(
            """SELECT SUM(catches), MIN(fastest_ms)
               FROM catch_user_daily WHERE guild_id=? AND user_id=? AND day=date('now')""",
            (guild_id, user_id),
        ) as cur:
            r = await cur.fetchone()
        today = r[0] or 0
        today_fastest = r[1] or 0

        # This week
        async with db.execute(
            """SELECT SUM(catches), MIN(fastest_ms)
               FROM catch_user_daily WHERE guild_id=? AND user_id=? AND day >= date('now','-6 days')""",
            (guild_id, user_id),
        ) as cur:
            r = await cur.fetchone()
        week = r[0] or 0
        week_fastest = r[1] or 0

        # This month
        async with db.execute(
            """SELECT SUM(catches), MIN(fastest_ms)
               FROM catch_user_daily WHERE guild_id=? AND user_id=? AND day >= date('now','start of month')""",
            (guild_id, user_id),
        ) as cur:
            r = await cur.fetchone()
        month = r[0] or 0
        month_fastest = r[1] or 0

        # Best single day
        async with db.execute(
            """SELECT catches, day FROM catch_user_daily
               WHERE guild_id=? AND user_id=? ORDER BY catches DESC LIMIT 1""",
            (guild_id, user_id),
        ) as cur:
            r = await cur.fetchone()
        best_day = r[0] if r else 0
        best_day_date = r[1] if r else ""

        # Unique pokemon all-time (union of JSON arrays)
        async with db.execute(
            "SELECT unique_pokemon FROM catch_user_daily WHERE guild_id=? AND user_id=? AND unique_pokemon != ''",
            (guild_id, user_id),
        ) as cur:
            pkmn_rows = await cur.fetchall()
        all_pkmn: set[str] = set()
        for pr in pkmn_rows:
            try:
                all_pkmn.update(json.loads(pr[0]))
            except Exception:
                pass

        # Current catch streak (consecutive days with at least 1 catch)
        async with db.execute(
            """SELECT day FROM catch_user_daily
               WHERE guild_id=? AND user_id=? AND catches > 0
               ORDER BY day DESC""",
            (guild_id, user_id),
        ) as cur:
            day_rows = await cur.fetchall()

    from datetime import date, timedelta
    streak = 0
    check = date.today()
    day_set = {r[0] for r in day_rows}
    while check.isoformat() in day_set:
        streak += 1
        check -= timedelta(days=1)

    avg_all = round(total_ms_all / total) if total > 0 else 0

    return {
        "user_name": user_name,
        "total": total,
        "today": today,
        "today_fastest_ms": today_fastest,
        "week": week,
        "week_fastest_ms": week_fastest,
        "month": month,
        "month_fastest_ms": month_fastest,
        "fastest_ms": fastest_all,
        "avg_ms": avg_all,
        "active_days": active_days,
        "best_day": best_day,
        "best_day_date": best_day_date,
        "unique_pokemon": len(all_pkmn),
        "day_streak": streak,
    }


async def get_catch_rate_history(
    guild_id: str,
    user_id: str,
    days: int = 7,
) -> list[dict]:
    """Return per-day catch counts for the last N days (for sparkline / trend display)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT day, catches FROM catch_user_daily
                WHERE guild_id=? AND user_id=? AND day >= date('now', '-{days-1} days')
                ORDER BY day ASC""",
            (guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
    return [{"day": r[0], "catches": r[1]} for r in rows]


async def get_guild_total_catches(guild_id: str, period: str = "all") -> int:
    """Total catches across all users for a guild in a period."""
    if period == "today":
        filt = "AND day = date('now')"
    elif period == "week":
        filt = "AND day >= date('now', '-6 days')"
    elif period == "month":
        filt = "AND day >= date('now', 'start of month')"
    else:
        filt = ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COALESCE(SUM(catches), 0) FROM catch_user_daily WHERE guild_id=? {filt}",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def get_fastest_catches(guild_id: str, limit: int = 5) -> list[dict]:
    """All-time fastest individual catches for a guild from raw events."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_name, pokemon_name, reaction_ms, caught_at
               FROM catch_events
               WHERE guild_id=? AND reaction_ms > 0
               ORDER BY reaction_ms ASC LIMIT ?""",
            (guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"user_name": r[0], "pokemon_name": r[1], "reaction_ms": r[2], "caught_at": r[3]}
        for r in rows
    ]
