"""
services/catch_db.py
SQLite storage for the catch-tracking & grind-session system.

Tables:
  catch_sessions     — active / completed grind sessions per guild+channel
  burst_channels     — admin-registered burst channels per guild
  burst_sessions     — a single guild-wide burst session spanning many channels
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

CREATE TABLE IF NOT EXISTS burst_channels (
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    added_by    TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS burst_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     TEXT    NOT NULL,
    started_by   TEXT    NOT NULL,
    started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT,
    paused_at    TEXT,
    total_paused INTEGER NOT NULL DEFAULT 0,
    state        TEXT    NOT NULL DEFAULT 'active',  -- active|paused|ended
    label        TEXT    NOT NULL DEFAULT ''
);

-- Duel system
CREATE TABLE IF NOT EXISTS catch_duels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT    NOT NULL,
    channel_id      TEXT    NOT NULL,         -- channel where duel was initiated
    challenger_id   TEXT    NOT NULL,
    challenger_name TEXT    NOT NULL DEFAULT '',
    opponent_id     TEXT    NOT NULL,
    opponent_name   TEXT    NOT NULL DEFAULT '',
    mode            TEXT    NOT NULL DEFAULT 'free',  -- free|time|pokemon
    mode_value      INTEGER NOT NULL DEFAULT 0,       -- 0=free, seconds for time, count for pokemon
    state           TEXT    NOT NULL DEFAULT 'pending', -- pending|active|ended|declined|expired
    winner_id       TEXT    NOT NULL DEFAULT '',
    challenger_catches INTEGER NOT NULL DEFAULT 0,
    opponent_catches   INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS catch_duel_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    duel_id     INTEGER NOT NULL REFERENCES catch_duels(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL,
    pokemon_name TEXT   NOT NULL DEFAULT '',
    reaction_ms  INTEGER NOT NULL DEFAULT 0,
    caught_at    TEXT   NOT NULL DEFAULT (datetime('now'))
);

-- Accuracy tracking (failed catches)
CREATE TABLE IF NOT EXISTS catch_failed (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      TEXT    NOT NULL,
    channel_id    TEXT    NOT NULL,
    user_id       TEXT    NOT NULL,
    user_name     TEXT    NOT NULL DEFAULT '',
    pokemon_name  TEXT    NOT NULL DEFAULT '',
    failed_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ce_session    ON catch_events (session_id);
CREATE INDEX IF NOT EXISTS idx_ce_guild_user ON catch_events (guild_id, user_id, caught_at);
CREATE INDEX IF NOT EXISTS idx_ce_guild_ch   ON catch_events (guild_id, channel_id, caught_at);
CREATE INDEX IF NOT EXISTS idx_cs_guild_ch   ON catch_sessions (guild_id, channel_id, state);
CREATE INDEX IF NOT EXISTS idx_cud_guild     ON catch_user_daily (guild_id, user_id, day);
CREATE INDEX IF NOT EXISTS idx_bs_guild      ON burst_sessions (guild_id, state);
CREATE INDEX IF NOT EXISTS idx_bc_guild      ON burst_channels (guild_id);
CREATE INDEX IF NOT EXISTS idx_cd_guild      ON catch_duels (guild_id, state);
CREATE INDEX IF NOT EXISTS idx_cf_guild_user ON catch_failed (guild_id, user_id, failed_at);
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


# ── Burst channels ────────────────────────────────────────────────────────────

async def add_burst_channels(guild_id: str, channel_ids: list[str], added_by: str = "") -> tuple[list[str], list[str]]:
    """
    Register channels as burst channels. Returns (added, already_existed).
    """
    added: list[str]   = []
    already: list[str] = []
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            for cid in channel_ids:
                try:
                    await db.execute(
                        "INSERT INTO burst_channels (guild_id, channel_id, added_by) VALUES (?, ?, ?)",
                        (guild_id, cid, added_by),
                    )
                    added.append(cid)
                except aiosqlite.IntegrityError:
                    already.append(cid)
            await db.commit()
    return added, already


async def remove_burst_channels(guild_id: str, channel_ids: list[str]) -> tuple[list[str], list[str]]:
    """
    Unregister channels. Returns (removed, not_found).
    """
    removed: list[str]   = []
    not_found: list[str] = []
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            for cid in channel_ids:
                cur = await db.execute(
                    "DELETE FROM burst_channels WHERE guild_id=? AND channel_id=?",
                    (guild_id, cid),
                )
                (removed if cur.rowcount > 0 else not_found).append(cid)
            await db.commit()
    return removed, not_found


async def get_burst_channels(guild_id: str) -> list[str]:
    """Return list of registered burst channel IDs for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id FROM burst_channels WHERE guild_id=? ORDER BY added_at ASC",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def is_burst_channel(guild_id: str, channel_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM burst_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        ) as cur:
            return await cur.fetchone() is not None


async def clear_burst_channels(guild_id: str) -> int:
    """Remove all burst channels for a guild. Returns count removed."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "DELETE FROM burst_channels WHERE guild_id=?", (guild_id,)
            )
            await db.commit()
            return cur.rowcount


# ── Burst sessions ────────────────────────────────────────────────────────────

async def start_burst_session(guild_id: str, started_by: str, label: str = "") -> int:
    """Start a guild-wide burst session. Returns session ID."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO burst_sessions (guild_id, started_by, label, state) VALUES (?, ?, ?, 'active')",
                (guild_id, started_by, label),
            )
            await db.commit()
            return cur.lastrowid


async def get_active_burst_session(guild_id: str) -> Optional[dict]:
    """Return the active/paused burst session for the guild, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, started_by, started_at, paused_at, total_paused, state, label
               FROM burst_sessions
               WHERE guild_id=? AND state IN ('active','paused')
               ORDER BY started_at DESC LIMIT 1""",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "started_by": row[1], "started_at": row[2],
        "paused_at": row[3], "total_paused": row[4],
        "state": row[5], "label": row[6],
    }


async def pause_burst_session(session_id: int, paused_at: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE burst_sessions SET state='paused', paused_at=? WHERE id=? AND state='active'",
                (paused_at, session_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def resume_burst_session(session_id: int, extra_paused_ms: int) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE burst_sessions
                   SET state='active', paused_at=NULL,
                       total_paused = total_paused + ?
                   WHERE id=? AND state='paused'""",
                (extra_paused_ms // 1000, session_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def end_burst_session(session_id: int, ended_at: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE burst_sessions
                   SET state='ended', ended_at=?, paused_at=NULL
                   WHERE id=? AND state IN ('active','paused')""",
                (ended_at, session_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_burst_session_by_id(session_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, guild_id, started_by, started_at, ended_at,
                      paused_at, total_paused, state, label
               FROM burst_sessions WHERE id=?""",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "guild_id": row[1], "started_by": row[2],
        "started_at": row[3], "ended_at": row[4], "paused_at": row[5],
        "total_paused": row[6], "state": row[7], "label": row[8],
    }


async def get_burst_session_stats(burst_session_id: int, guild_id: str) -> dict:
    """
    Aggregate stats for a burst session — pulls from all catch_events in the
    guild that were recorded during the burst session's time window, across all
    registered burst channels.
    """
    burst = await get_burst_session_by_id(burst_session_id)
    if not burst:
        return {"users": [], "total_catches": 0, "fastest_ms": 0, "avg_ms": 0,
                "unique_pokemon": 0, "catchers": 0, "fastest_detail": None}

    started_at = burst["started_at"]
    ended_at   = burst.get("ended_at") or "9999-12-31"

    async with aiosqlite.connect(DB_PATH) as db:
        # Per-user breakdown across all burst channels
        async with db.execute(
            """SELECT user_id, user_name,
                      COUNT(*) as catches,
                      MIN(reaction_ms) as fastest,
                      AVG(reaction_ms) as avg_ms,
                      COUNT(DISTINCT pokemon_name) as unique_pkmn
               FROM catch_events
               WHERE guild_id=? AND is_session=1
                 AND caught_at >= ? AND caught_at <= ?
               GROUP BY user_id ORDER BY catches DESC""",
            (guild_id, started_at, ended_at),
        ) as cur:
            user_rows = await cur.fetchall()

        # Overall totals
        async with db.execute(
            """SELECT COUNT(*), MIN(reaction_ms), AVG(reaction_ms),
                      COUNT(DISTINCT pokemon_name), COUNT(DISTINCT user_id),
                      COUNT(DISTINCT channel_id)
               FROM catch_events
               WHERE guild_id=? AND is_session=1
                 AND caught_at >= ? AND caught_at <= ?""",
            (guild_id, started_at, ended_at),
        ) as cur:
            tot = await cur.fetchone()

        # Fastest detail
        async with db.execute(
            """SELECT user_name, pokemon_name, reaction_ms, caught_at
               FROM catch_events
               WHERE guild_id=? AND is_session=1 AND reaction_ms > 0
                 AND caught_at >= ? AND caught_at <= ?
               ORDER BY reaction_ms ASC LIMIT 1""",
            (guild_id, started_at, ended_at),
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
        "active_channels": tot[5] or 0 if tot else 0,
        "fastest_detail": {
            "user_name": fastest_row[0],
            "pokemon_name": fastest_row[1],
            "reaction_ms": fastest_row[2],
            "caught_at": fastest_row[3],
        } if fastest_row else None,
    }


# ── Accuracy / failed catches ─────────────────────────────────────────────────

async def record_failed_catch(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_name: str,
    pokemon_name: str,
) -> None:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO catch_failed
                   (guild_id, channel_id, user_id, user_name, pokemon_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, channel_id, user_id, user_name, pokemon_name),
            )
            await db.commit()


async def get_user_accuracy(guild_id: str, user_id: str, period: str = "all") -> dict:
    """Return {success, failed, accuracy_pct} for a user."""
    if period == "today":
        day_filt_s = "AND caught_at >= date('now')"
        day_filt_f = "AND failed_at >= date('now')"
    elif period == "week":
        day_filt_s = "AND caught_at >= date('now', '-6 days')"
        day_filt_f = "AND failed_at >= date('now', '-6 days')"
    elif period == "month":
        day_filt_s = "AND caught_at >= date('now', 'start of month')"
        day_filt_f = "AND failed_at >= date('now', 'start of month')"
    else:
        day_filt_s = day_filt_f = ""

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM catch_events WHERE guild_id=? AND user_id=? {day_filt_s}",
            (guild_id, user_id),
        ) as cur:
            success = (await cur.fetchone())[0] or 0
        async with db.execute(
            f"SELECT COUNT(*) FROM catch_failed WHERE guild_id=? AND user_id=? {day_filt_f}",
            (guild_id, user_id),
        ) as cur:
            failed = (await cur.fetchone())[0] or 0

    total = success + failed
    pct = round(success / total * 100, 1) if total > 0 else 0.0
    return {"success": success, "failed": failed, "total_attempts": total, "accuracy_pct": pct}


# ── Duels ─────────────────────────────────────────────────────────────────────

async def create_duel(
    guild_id: str,
    channel_id: str,
    challenger_id: str,
    challenger_name: str,
    opponent_id: str,
    opponent_name: str,
    mode: str,
    mode_value: int,
) -> int:
    """Create a pending duel. Returns duel ID."""
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO catch_duels
                   (guild_id, channel_id, challenger_id, challenger_name,
                    opponent_id, opponent_name, mode, mode_value, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (guild_id, channel_id, challenger_id, challenger_name,
                 opponent_id, opponent_name, mode, mode_value),
            )
            await db.commit()
            return cur.lastrowid


async def get_duel(duel_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, guild_id, channel_id, challenger_id, challenger_name,
                      opponent_id, opponent_name, mode, mode_value, state,
                      winner_id, challenger_catches, opponent_catches,
                      started_at, ended_at, created_at
               FROM catch_duels WHERE id=?""",
            (duel_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    keys = ["id","guild_id","channel_id","challenger_id","challenger_name",
            "opponent_id","opponent_name","mode","mode_value","state",
            "winner_id","challenger_catches","opponent_catches",
            "started_at","ended_at","created_at"]
    return dict(zip(keys, row))


async def accept_duel(duel_id: int, started_at: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE catch_duels SET state='active', started_at=? WHERE id=? AND state='pending'",
                (started_at, duel_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def decline_duel(duel_id: int) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE catch_duels SET state='declined' WHERE id=? AND state='pending'",
                (duel_id,),
            )
            await db.commit()
            return cur.rowcount > 0


async def expire_duel(duel_id: int) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE catch_duels SET state='expired' WHERE id=? AND state='pending'",
                (duel_id,),
            )
            await db.commit()
            return cur.rowcount > 0


async def record_duel_catch(
    duel_id: int,
    user_id: str,
    pokemon_name: str,
    reaction_ms: int,
    caught_at: str,
    is_challenger: bool,
) -> int:
    """Record a catch in a duel. Returns new catch count for that user."""
    col = "challenger_catches" if is_challenger else "opponent_catches"
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                f"UPDATE catch_duels SET {col} = {col} + 1 WHERE id=?",
                (duel_id,),
            )
            await db.execute(
                """INSERT INTO catch_duel_events (duel_id, user_id, pokemon_name, reaction_ms, caught_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (duel_id, user_id, pokemon_name, reaction_ms, caught_at),
            )
            await db.commit()
            async with db.execute(
                f"SELECT {col} FROM catch_duels WHERE id=?", (duel_id,)
            ) as cur:
                row = await cur.fetchone()
            return row[0] if row else 0


async def end_duel(duel_id: int, winner_id: str, ended_at: str) -> bool:
    async with _write_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE catch_duels SET state='ended', winner_id=?, ended_at=?
                   WHERE id=? AND state='active'""",
                (winner_id, ended_at, duel_id),
            )
            await db.commit()
            return cur.rowcount > 0


async def get_duel_head_to_head(guild_id: str, user_a: str, user_b: str) -> dict:
    """Get win/loss record between two users."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT winner_id FROM catch_duels
               WHERE guild_id=? AND state='ended'
                 AND ((challenger_id=? AND opponent_id=?)
                   OR (challenger_id=? AND opponent_id=?))""",
            (guild_id, user_a, user_b, user_b, user_a),
        ) as cur:
            rows = await cur.fetchall()

    a_wins = b_wins = draws = 0
    for row in rows:
        winner = row[0]
        if winner == user_a:
            a_wins += 1
        elif winner == user_b:
            b_wins += 1
        else:
            draws += 1

    return {"a_wins": a_wins, "b_wins": b_wins, "draws": draws, "total": len(rows)}


async def get_duel_stats(guild_id: str, user_id: str) -> dict:
    """Overall duel record for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT
                 SUM(CASE WHEN winner_id=? THEN 1 ELSE 0 END),
                 SUM(CASE WHEN winner_id != ? AND winner_id != '' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN winner_id='' THEN 1 ELSE 0 END),
                 COUNT(*)
               FROM catch_duels
               WHERE guild_id=? AND state='ended'
                 AND (challenger_id=? OR opponent_id=?)""",
            (user_id, user_id, guild_id, user_id, user_id),
        ) as cur:
            row = await cur.fetchone()

    wins   = row[0] or 0
    losses = row[1] or 0
    draws  = row[2] or 0
    total  = row[3] or 0
    return {"wins": wins, "losses": losses, "draws": draws, "total": total}


async def get_active_duel_for_user(guild_id: str, user_id: str) -> Optional[dict]:
    """Return the active duel where user is challenger or opponent, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id FROM catch_duels
               WHERE guild_id=? AND state='active'
                 AND (challenger_id=? OR opponent_id=?)
               LIMIT 1""",
            (guild_id, user_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return await get_duel(row[0])


async def get_duel_leaderboard(guild_id: str, limit: int = 10) -> list[dict]:
    """Return top duelists ranked by wins, then win-rate."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT
                   user_id,
                   user_name,
                   SUM(wins)   AS wins,
                   SUM(losses) AS losses,
                   SUM(draws)  AS draws,
                   SUM(total)  AS total
               FROM (
                   -- challenger perspective
                   SELECT challenger_id   AS user_id,
                          challenger_name AS user_name,
                          SUM(CASE WHEN winner_id = challenger_id THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN winner_id = opponent_id   THEN 1 ELSE 0 END) AS losses,
                          SUM(CASE WHEN winner_id = ''            THEN 1 ELSE 0 END) AS draws,
                          COUNT(*) AS total
                   FROM catch_duels
                   WHERE guild_id=? AND state='ended'
                   GROUP BY challenger_id, challenger_name
                   UNION ALL
                   -- opponent perspective
                   SELECT opponent_id   AS user_id,
                          opponent_name AS user_name,
                          SUM(CASE WHEN winner_id = opponent_id   THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN winner_id = challenger_id THEN 1 ELSE 0 END) AS losses,
                          SUM(CASE WHEN winner_id = ''            THEN 1 ELSE 0 END) AS draws,
                          COUNT(*) AS total
                   FROM catch_duels
                   WHERE guild_id=? AND state='ended'
                   GROUP BY opponent_id, opponent_name
               )
               GROUP BY user_id, user_name
               ORDER BY wins DESC, (CAST(wins AS REAL) / MAX(total, 1)) DESC
               LIMIT ?""",
            (guild_id, guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"user_id": r[0], "user_name": r[1], "wins": r[2], "losses": r[3], "draws": r[4], "total": r[5]}
        for r in rows
    ]
