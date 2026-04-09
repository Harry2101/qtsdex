"""
cogs/catch_tracker.py  —  Catch Tracker & Grind Session System

Passively listens to Pokémon spawns and catch confirmations from the
Operation Dex bot in ANY channel, recording who caught what and how fast.

── Single-channel sessions (prefix, anyone) ──────────────────────────────────
  !catchstart [label]   — start a session in THIS channel only
  !catchstop            — end session + show summary
  !catchpause           — pause timer
  !catchresume          — resume timer
  !catchstatus          — live stats for active session

── Multi-channel burst sessions (prefix, admin/owner) ────────────────────────
  !burststart [label]   — start a burst session across ALL registered burst channels
  !burststop            — end burst session + full summary
  !burstpause           — pause the burst timer
  !burstresume          — resume the burst timer
  !burststatus          — live stats across all burst channels

── Burst channel management (slash, admin/owner) ─────────────────────────────
  /catches burst add    — add channels (single, category, or from→to range)
  /catches burst remove — remove channels (same selectors)
  /catches burst list   — show all registered burst channels
  /catches burst clear  — remove all burst channels

── Stats & leaderboards (slash, anyone) ──────────────────────────────────────
  /catches stats [user]             — personal lifetime stats
  /catches leaderboard [period]     — server leaderboard
  /catches session <id>             — replay a past session
  /catches fastest                  — all-time fastest catches

Detection logic:
  Spawns  → Op Dex embed whose title matches "A wild … appeared!"
  Catches → message <@OpDex> c <name>, confirmed by Op Dex reply with "Catch ID:"
  Burst   → a catch in a registered burst channel while a burst session is active
            is automatically counted in the burst session totals.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import catch_db, guild_settings_db
from services.guild_settings_db import make_footer

# ── Duel mode constants ───────────────────────────────────────────────────────
DUEL_MODE_FREE    = "free"      # just catch for fun, no winner
DUEL_MODE_TIME    = "time"      # X seconds, whoever has more catches wins
DUEL_MODE_POKEMON = "pokemon"   # first to catch X pokemon wins

_DUEL_INVITE_TIMEOUT = 60       # seconds opponent has to accept/decline

# In-memory duel state: duel_id → DuelState
_active_duels: dict[int, "DuelState"] = {}

# Failed-catch detection: Op Dex failure keywords
_FAIL_KEYWORDS = [
    "that's not", "wrong", "escape", "ran away",
    "got away", "incorrect", "not the right", "not right",
    "failed", "miss",
]
_FAIL_RE = re.compile("|".join(_FAIL_KEYWORDS), re.IGNORECASE)

log = logging.getLogger("qtsdex.catch_tracker")

OWNER_ID     = int(os.getenv("OWNER_ID", "145065060568530944"))
DEFAULT_OPDEX = int(os.getenv("DEFAULT_OPDEX_BOT_ID", "1471263987340410978"))

# Max seconds a spawn is "alive" for reaction time tracking
_SPAWN_TTL_S = 120

# ── Regex patterns ────────────────────────────────────────────────────────────

# Spawn embed title: "A wild Hippopotas appeared!" with optional surrounding emojis/text
_SPAWN_TITLE_RE = re.compile(
    r"a\s+wild\s+(?:jester\s+)?(.+?)\s+appeared",
    re.IGNORECASE,
)

# Catch command: <@BOT_ID> c <pokemon name>
# Handles optional ! prefix, case-insensitive
_CATCH_CMD_RE = re.compile(
    r"^<@!?(\d+)>\s+c\s+(.+)",
    re.IGNORECASE,
)

# Confirmation patterns from Op Dex reply
# "Snagged it!" / "Gotcha!" / "You caught" / etc. — followed later by the pokemon name + Catch ID
# We look for "Catch ID: 123456" as the definitive success marker
_CONFIRM_CATCH_ID_RE = re.compile(r"Catch\s+ID[:\s]+(\d+)", re.IGNORECASE)
# Extract pokemon name from "Hippopotas ♂ (Catch ID: …)"
_CONFIRM_POKEMON_RE  = re.compile(r"^\*?\*?([A-Za-z][A-Za-z\-\' ]+?)\s*[♂♀✦★⬡\(]", re.MULTILINE)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ms(ms: int) -> str:
    """Format milliseconds as a human-friendly string."""
    if ms <= 0:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms/1000:.2f}s"


def _fmt_duration(seconds: int) -> str:
    """Format seconds as Xh Ym Zs."""
    if seconds <= 0:
        return "0s"
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    parts  = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _speed_bar(ms: int, max_ms: int = 10000, width: int = 10) -> str:
    """Visual bar representing catch speed (shorter = faster)."""
    if ms <= 0 or max_ms <= 0:
        return "░" * width
    ratio  = min(ms / max_ms, 1.0)
    filled = max(1, round(ratio * width))
    return "█" * filled + "░" * (width - filled)


async def _get_opdex_id(guild_id: str) -> int:
    custom = await guild_settings_db.get_opdex_bot_id(guild_id)
    return custom or DEFAULT_OPDEX


def _is_slash_privileged(interaction: discord.Interaction) -> bool:
    if interaction.user.id == OWNER_ID:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def _is_privileged(ctx: commands.Context) -> bool:
    if ctx.author.id == OWNER_ID:
        return True
    perms = getattr(ctx.author, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _sort_channel_ids(guild: discord.Guild, ids: list[str]) -> list[str]:
    """Sort channel IDs by their position in the server (category then channel)."""
    def _pos(cid: str) -> tuple[int, int]:
        ch = guild.get_channel(int(cid))
        if ch is None:
            return (99999, 99999)
        cat_pos = ch.category.position if ch.category else -1
        return (cat_pos, ch.position)
    return sorted(ids, key=_pos)


def _channel_list_str(guild: discord.Guild, ids: list[str], numbered: bool = False) -> str:
    """Format a list of channel IDs as mentions, sorted by server position."""
    sorted_ids = _sort_channel_ids(guild, ids)
    lines = []
    for i, cid in enumerate(sorted_ids):
        ch   = guild.get_channel(int(cid))
        name = ch.mention if ch else f"<#{cid}>"
        lines.append(f"{i+1}. {name}" if numbered else name)
    # Chunk into rows of 4 for readability
    if not numbered:
        rows = [" · ".join(lines[i:i+4]) for i in range(0, len(lines), 4)]
        return "\n".join(rows)
    return "\n".join(lines)


def _burst_session_embed(
    burst: dict,
    stats: dict,
    channel_ids: list[str],
    guild_id: str,
    ended: bool = False,
) -> discord.Embed:
    """Rich embed for a burst session summary or live status."""
    label   = burst.get("label") or ""
    state   = burst.get("state", "active")
    started = burst.get("started_at", "")

    try:
        start_dt = datetime.fromisoformat(started).replace(tzinfo=timezone.utc)
    except Exception:
        start_dt = _now_utc()

    now    = _now_utc()
    end_dt = now
    if ended and burst.get("ended_at"):
        try:
            end_dt = datetime.fromisoformat(burst["ended_at"]).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    elapsed_s = int((end_dt - start_dt).total_seconds())
    paused_s  = burst.get("total_paused", 0) or 0
    net_s     = max(0, elapsed_s - paused_s)
    total     = stats.get("total_catches", 0)
    rate_str  = f"{total/(net_s/60):.1f}/min" if net_s >= 60 and total > 0 else "—"

    icon  = "🏁" if ended else ("⏸️" if state == "paused" else "⚡")
    title = f"{icon} Burst Session"
    if label:
        title += f" — {label}"

    colour = 0xF1C40F if not ended else 0x57F287
    embed  = discord.Embed(title=title, colour=colour)

    embed.add_field(name="⏱️ Duration",
                    value=f"`{_fmt_duration(net_s)}`" + (f"\n*(+{_fmt_duration(paused_s)} paused)*" if paused_s else ""),
                    inline=True)
    embed.add_field(name="🎯 Total Catches", value=f"`{total}`", inline=True)
    embed.add_field(name="⚡ Rate", value=f"`{rate_str}`", inline=True)

    if stats.get("active_channels", 0):
        embed.add_field(name="📡 Active Channels", value=f"`{stats['active_channels']}`", inline=True)
    if stats.get("unique_pokemon", 0):
        embed.add_field(name="🔢 Unique Pokémon", value=f"`{stats['unique_pokemon']}`", inline=True)
    if stats.get("avg_ms", 0):
        embed.add_field(name="📊 Avg React", value=f"`{_fmt_ms(stats['avg_ms'])}`", inline=True)
    if stats.get("fastest_ms", 0):
        fd  = stats.get("fastest_detail")
        val = f"`{_fmt_ms(stats['fastest_ms'])}`"
        if fd:
            val += f"\n{fd['user_name']} — {fd['pokemon_name']}"
        embed.add_field(name="🏆 Fastest Catch", value=val, inline=True)

    users = stats.get("users", [])
    if users:
        lines  = []
        medals = ["🥇", "🥈", "🥉"]
        for i, u in enumerate(users):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            name  = u.get("user_name") or f"<@{u['user_id']}>"
            c     = u["catches"]
            spd   = _fmt_ms(u["fastest_ms"]) if u.get("fastest_ms") else "—"
            pct   = f"{c/total*100:.0f}%" if total > 0 else "—"
            lines.append(f"{medal} **{name}** — {c} ({pct}) · fastest {spd}")
        embed.add_field(name="👥 Catchers", value="\n".join(lines), inline=False)

    embed.set_footer(text=make_footer(guild_id) + f" • {len(channel_ids)} burst channel(s)")
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Duel state (in-memory)
# ─────────────────────────────────────────────────────────────────────────────

class DuelState:
    """Tracks a live duel between two players."""
    def __init__(self, duel_id: int, guild_id: str, channel_id: str,
                 challenger_id: str, opponent_id: str,
                 mode: str, mode_value: int):
        self.duel_id       = duel_id
        self.guild_id      = guild_id
        self.channel_id    = channel_id
        self.challenger_id = challenger_id
        self.opponent_id   = opponent_id
        self.mode          = mode
        self.mode_value    = mode_value  # seconds or pokemon count
        self.ch_catches    = 0
        self.op_catches    = 0
        self.started_at    = _now_utc()
        self._timer_task: Optional[asyncio.Task] = None
        self.ended         = False

    def record(self, user_id: str) -> tuple[int, int]:
        """Increment catch count. Returns (ch_catches, op_catches)."""
        if user_id == self.challenger_id:
            self.ch_catches += 1
        elif user_id == self.opponent_id:
            self.op_catches += 1
        return self.ch_catches, self.op_catches

    def elapsed_s(self) -> int:
        return int((_now_utc() - self.started_at).total_seconds())

    def winner_id(self) -> str:
        """Return winner_id or '' for draw."""
        if self.ch_catches > self.op_catches:
            return self.challenger_id
        if self.op_catches > self.ch_catches:
            return self.opponent_id
        return ""


def _mode_label(mode: str, mode_value: int) -> str:
    if mode == DUEL_MODE_FREE:
        return "Free Catch (no limit)"
    if mode == DUEL_MODE_TIME:
        return f"Time Limit — {_fmt_duration(mode_value)}"
    if mode == DUEL_MODE_POKEMON:
        return f"First to {mode_value} Pokémon"
    return mode


def _duel_invite_embed(
    challenger: discord.Member,
    opponent: discord.Member,
    mode: str,
    mode_value: int,
    burst_count: int,
    duel_id: int,
    guild_id: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="⚔️ 1v1 Blitz Catching Challenge!",
        colour=0xFF6B35,
    )
    embed.description = (
        f"**{challenger.display_name}** is challenging **{opponent.display_name}** "
        f"to a 1v1 blitz catching duel!\n"
        f"**{burst_count}** burst channel(s) in play."
    )
    embed.add_field(name="🎮 Mode", value=f"`{_mode_label(mode, mode_value)}`", inline=True)
    embed.add_field(name="📡 Arena", value=f"`{burst_count}` burst channels", inline=True)
    embed.add_field(name="⏳ Expires", value="Invite expires in 60s", inline=True)
    embed.set_thumbnail(url=challenger.display_avatar.url)
    embed.set_footer(text=f"Duel #{duel_id} · {make_footer(guild_id)}")
    return embed


def _duel_status_embed(
    state: DuelState,
    ch_name: str,
    op_name: str,
    ch_avatar: str,
    op_avatar: str,
    ended: bool = False,
    winner_id: str = "",
    guild_id: str = "",
) -> discord.Embed:
    ch_c = state.ch_catches
    op_c = state.op_catches
    elapsed = _fmt_duration(state.elapsed_s())

    if ended:
        if not winner_id:
            title = "⚔️ Duel Ended — **Draw!**"
            colour = 0x99AAB5
        elif winner_id == state.challenger_id:
            title = f"⚔️ Duel Ended — 🏆 {ch_name} Wins!"
            colour = 0x57F287
        else:
            title = f"⚔️ Duel Ended — 🏆 {op_name} Wins!"
            colour = 0x57F287
    else:
        title = "⚔️ Duel — Live"
        colour = 0xFF6B35

    embed = discord.Embed(title=title, colour=colour)
    embed.add_field(
        name=f"🔴 {ch_name}",
        value=f"`{ch_c}` catches",
        inline=True,
    )
    embed.add_field(
        name="vs",
        value=f"`{elapsed}`" if not ended else "—",
        inline=True,
    )
    embed.add_field(
        name=f"🔵 {op_name}",
        value=f"`{op_c}` catches",
        inline=True,
    )
    embed.add_field(
        name="🎮 Mode",
        value=f"`{_mode_label(state.mode, state.mode_value)}`",
        inline=False,
    )
    embed.set_thumbnail(url=ch_avatar)
    embed.set_footer(text=f"Duel #{state.duel_id}" + (f" · {make_footer(guild_id)}" if guild_id else ""))
    return embed


# ─────────────────────────────────────────────────────────────────────────────
# Duel invite view (Accept / Decline buttons)
# ─────────────────────────────────────────────────────────────────────────────

class DuelInviteView(discord.ui.View):
    def __init__(self, duel_id: int, opponent_id: int, cog: "CatchTrackerCog"):
        super().__init__(timeout=_DUEL_INVITE_TIMEOUT)
        self.duel_id     = duel_id
        self.opponent_id = opponent_id
        self.cog         = cog
        self.result      = None  # "accepted" | "declined" | None (timeout)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                "Only the challenged player can respond.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Accept ✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = "accepted"
        self.stop()
        await self.cog._start_duel(interaction, self.duel_id)

    @discord.ui.button(label="Decline ❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = "declined"
        self.stop()
        await catch_db.decline_duel(self.duel_id)
        embed = discord.Embed(
            description=f"❌ {interaction.user.display_name} declined the duel.",
            colour=0xED4245,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        await catch_db.expire_duel(self.duel_id)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory spawn tracker
# Key: (guild_id, channel_id)  →  {"pokemon": str, "msg_id": str, "ts": datetime}
# ─────────────────────────────────────────────────────────────────────────────

_pending_spawns: dict[tuple, dict] = {}

# In-memory catch-command tracker — maps catch msg_id → spawn info so we can
# match the bot's async confirmation reply.
# Key: (guild_id, channel_id, user_id)  →  {"spawn": dict, "catch_msg_id": str, "ts": datetime}
_pending_catches: dict[tuple, dict] = {}


# ── Session summary embed ─────────────────────────────────────────────────────

def _session_embed(
    session: dict,
    stats: dict,
    guild_id: str,
    ended: bool = False,
) -> discord.Embed:
    """Build a rich embed for a session summary or live status."""
    label     = session.get("label") or ""
    state     = session.get("state", "active")
    started   = session.get("started_at", "")

    # Parse started_at
    try:
        start_dt = datetime.fromisoformat(started).replace(tzinfo=timezone.utc)
    except Exception:
        start_dt = _now_utc()

    # Net session duration (subtract paused time)
    now       = _now_utc()
    end_dt    = now
    if ended and session.get("ended_at"):
        try:
            end_dt = datetime.fromisoformat(session["ended_at"]).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    elapsed_s   = int((end_dt - start_dt).total_seconds())
    paused_s    = session.get("total_paused", 0) or 0
    net_s       = max(0, elapsed_s - paused_s)
    total       = stats.get("total_catches", 0)
    rate_str    = f"{total/(net_s/60):.1f}/min" if net_s >= 60 and total > 0 else "—"

    title_icon  = "🏁" if ended else ("⏸️" if state == "paused" else "▶️")
    title_text  = f"{title_icon} Grind Session"
    if label:
        title_text += f" — {label}"

    colour = 0x5865F2 if not ended else 0x57F287

    embed = discord.Embed(title=title_text, colour=colour)

    # Header line
    embed.add_field(
        name="⏱️ Duration",
        value=f"`{_fmt_duration(net_s)}`" + (f"\n*(+{_fmt_duration(paused_s)} paused)*" if paused_s else ""),
        inline=True,
    )
    embed.add_field(
        name="🎯 Total Catches",
        value=f"`{total}`",
        inline=True,
    )
    embed.add_field(
        name="⚡ Rate",
        value=f"`{rate_str}`",
        inline=True,
    )

    if stats.get("unique_pokemon", 0):
        embed.add_field(name="🔢 Unique Pokémon", value=f"`{stats['unique_pokemon']}`", inline=True)
    if stats.get("avg_ms", 0):
        embed.add_field(name="📊 Avg React", value=f"`{_fmt_ms(stats['avg_ms'])}`", inline=True)
    if stats.get("fastest_ms", 0):
        fd = stats.get("fastest_detail")
        val = f"`{_fmt_ms(stats['fastest_ms'])}`"
        if fd:
            val += f"\n{fd['user_name']} — {fd['pokemon_name']}"
        embed.add_field(name="🏆 Fastest Catch", value=val, inline=True)

    # Per-user breakdown
    users = stats.get("users", [])
    if users:
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, u in enumerate(users):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            name  = u.get("user_name") or f"<@{u['user_id']}>"
            c     = u["catches"]
            spd   = _fmt_ms(u["fastest_ms"]) if u.get("fastest_ms") else "—"
            pct   = f"{c/total*100:.0f}%" if total > 0 else "—"
            lines.append(f"{medal} **{name}** — {c} catches ({pct}) · fastest {spd}")
        embed.add_field(name="👥 Catchers", value="\n".join(lines), inline=False)

    embed.set_footer(text=make_footer(guild_id))
    return embed


def _stats_embed(stats: dict, user: discord.User | discord.Member, guild_id: str) -> discord.Embed:
    """Personal lifetime stats embed."""
    total    = stats.get("total", 0)
    today    = stats.get("today", 0)
    week     = stats.get("week", 0)
    month    = stats.get("month", 0)
    fastest  = stats.get("fastest_ms", 0)
    avg      = stats.get("avg_ms", 0)
    days     = stats.get("active_days", 0)
    streak   = stats.get("day_streak", 0)
    best_day = stats.get("best_day", 0)
    best_d   = stats.get("best_day_date", "")
    unique   = stats.get("unique_pokemon", 0)

    embed = discord.Embed(
        title=f"📊 Catch Stats — {user.display_name}",
        colour=0xF1C40F,
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    # Catches over time
    embed.add_field(
        name="🎯 Catches",
        value=(
            f"Today: **{today}**\n"
            f"This week: **{week}**\n"
            f"This month: **{month}**\n"
            f"All-time: **{total}**"
        ),
        inline=True,
    )

    # Speed stats
    fastest_str = _fmt_ms(fastest) if fastest else "—"
    avg_str     = _fmt_ms(avg) if avg else "—"
    embed.add_field(
        name="⚡ Speed",
        value=(
            f"Fastest ever: **{fastest_str}**\n"
            f"Average react: **{avg_str}**\n"
            f"Today fastest: **{_fmt_ms(stats.get('today_fastest_ms',0)) if stats.get('today_fastest_ms') else '—'}**\n"
            f"Week fastest: **{_fmt_ms(stats.get('week_fastest_ms',0)) if stats.get('week_fastest_ms') else '—'}**"
        ),
        inline=True,
    )

    # Activity
    embed.add_field(
        name="📅 Activity",
        value=(
            f"Active days: **{days}**\n"
            f"Day streak: **{streak}** {'🔥' if streak >= 3 else ''}\n"
            f"Best day: **{best_day}** catches"
            + (f" ({best_d})" if best_d else "") + "\n"
            f"Unique Pokémon: **{unique}**"
        ),
        inline=False,
    )

    embed.set_footer(text=make_footer(guild_id))
    return embed


def _leaderboard_embed(
    rows: list[dict],
    period: str,
    guild_name: str,
    guild_id: str,
    total_guild: int = 0,
) -> discord.Embed:
    period_label = {
        "today": "Today",
        "week": "This Week",
        "month": "This Month",
        "all": "All Time",
    }.get(period, period.title())

    embed = discord.Embed(
        title=f"🏆 Catch Leaderboard — {period_label}",
        colour=0xE91E63,
        description=f"**{guild_name}** • {total_guild} total catches {period_label.lower()}" if total_guild else f"**{guild_name}**",
    )

    if not rows:
        embed.description = (embed.description or "") + "\n\n*No catches recorded yet!*"
        embed.set_footer(text=make_footer(guild_id))
        return embed

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, row in enumerate(rows):
        medal   = medals[i] if i < 3 else f"**{i+1}.**"
        name    = row.get("user_name") or f"<@{row['user_id']}>"
        catches = row["catches"]
        fastest = _fmt_ms(row["fastest_ms"]) if row.get("fastest_ms") else "—"
        avg     = _fmt_ms(row["avg_ms"]) if row.get("avg_ms") else "—"
        bar     = _speed_bar(row.get("fastest_ms", 0))
        lines.append(
            f"{medal} **{name}** — `{catches}` catches\n"
            f"  └ fastest `{fastest}` · avg `{avg}` · `{bar}`"
        )

    embed.add_field(name="\u200b", value="\n".join(lines), inline=False)
    embed.set_footer(text=make_footer(guild_id))
    return embed


# ─────────────────────────────────────────────────────────────────────────────

class CatchTrackerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _opdex_id(self, guild_id: str) -> int:
        return await _get_opdex_id(guild_id)

    def _clean_pokemon_name(self, raw: str) -> str:
        """Normalise a pokemon name scraped from a message."""
        return raw.strip().lower().replace("-", " ").title()

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return

        guild_id    = str(message.guild.id)
        channel_id  = str(message.channel.id)
        opdex_id    = await self._opdex_id(guild_id)

        # ── 1. Spawn detection (embed from Op Dex) ────────────────────────────
        if message.author.id == opdex_id and message.embeds:
            for embed in message.embeds:
                title = embed.title or ""
                m = _SPAWN_TITLE_RE.search(title)
                if m:
                    pokemon_name = self._clean_pokemon_name(m.group(1))
                    key = (guild_id, channel_id)
                    _pending_spawns[key] = {
                        "pokemon":  pokemon_name,
                        "msg_id":   str(message.id),
                        "ts":       _now_utc(),
                    }
                    log.debug(f"Spawn detected: {pokemon_name} in #{message.channel.name}")
                    return  # don't process further for spawn messages

        # ── 2. Catch command detection (user → Op Dex) ────────────────────────
        content = message.content.strip()
        cm = _CATCH_CMD_RE.match(content)
        if cm:
            mentioned_id = int(cm.group(1))
            if mentioned_id == opdex_id:
                pokemon_attempted = self._clean_pokemon_name(cm.group(2))
                key = (guild_id, channel_id)
                spawn = _pending_spawns.get(key)

                if spawn:
                    age_s = (_now_utc() - spawn["ts"]).total_seconds()
                    if age_s <= _SPAWN_TTL_S:
                        # Store pending catch, waiting for bot confirmation
                        pend_key = (guild_id, channel_id, str(message.author.id))
                        _pending_catches[pend_key] = {
                            "spawn":         spawn,
                            "pokemon_typed": pokemon_attempted,
                            "catch_msg_id":  str(message.id),
                            "catcher_id":    str(message.author.id),
                            "catcher_name":  message.author.display_name,
                            "ts":            _now_utc(),
                        }
                        log.debug(f"Catch attempt: {message.author.display_name} → {pokemon_attempted}")
            return  # not a confirmation yet

        # ── 3. Confirmation detection (Op Dex response) ───────────────────────
        if message.author.id == opdex_id:
            text = message.content or ""
            # Also check embeds for confirmation text
            for emb in message.embeds:
                if emb.description:
                    text += "\n" + emb.description

            is_success = bool(_CONFIRM_CATCH_ID_RE.search(text))
            is_failure = (not is_success) and bool(_FAIL_RE.search(text))

            # ── 3a. Failed catch ──────────────────────────────────────────────
            if is_failure:
                # Identify which pending catch this failure belongs to
                now = _now_utc()
                fail_key = None
                fail_age = float("inf")
                for pend_key, pend in list(_pending_catches.items()):
                    pk_guild, pk_channel, _ = pend_key
                    if pk_guild != guild_id or pk_channel != channel_id:
                        continue
                    age = (now - pend["ts"]).total_seconds()
                    if age > 30:
                        del _pending_catches[pend_key]
                        continue
                    if age < fail_age:
                        fail_age = age
                        fail_key = pend_key
                if message.mentions:
                    for uid in message.mentions:
                        k = (guild_id, channel_id, str(uid.id))
                        if k in _pending_catches:
                            fail_key = k
                            break
                if fail_key:
                    pend = _pending_catches.pop(fail_key)
                    await catch_db.record_failed_catch(
                        guild_id     = guild_id,
                        channel_id   = channel_id,
                        user_id      = pend["catcher_id"],
                        user_name    = pend["catcher_name"],
                        pokemon_name = pend.get("pokemon_typed") or pend["spawn"].get("pokemon", ""),
                    )
                    log.debug(f"❌ Failed catch: {pend['catcher_name']}")
                return

            if not is_success:
                return  # not a catch confirmation, not a failure — ignore

            # ── 3b. Successful catch ──────────────────────────────────────────
            # Find which pending catch this confirms.
            now = _now_utc()
            best_key  = None
            best_age  = float("inf")

            for pend_key, pend in list(_pending_catches.items()):
                pk_guild, pk_channel, pk_user = pend_key
                if pk_guild != guild_id or pk_channel != channel_id:
                    continue
                age = (now - pend["ts"]).total_seconds()
                if age > 30:  # stale
                    del _pending_catches[pend_key]
                    continue
                if age < best_age:
                    best_age = age
                    best_key = pend_key

            # If the message mentions a specific user, prefer that
            if message.mentions:
                for uid in message.mentions:
                    k = (guild_id, channel_id, str(uid.id))
                    if k in _pending_catches:
                        best_key = k
                        break

            if best_key is None:
                return

            pend  = _pending_catches.pop(best_key)
            spawn = pend["spawn"]

            # Compute reaction time: spawn ts → catch command ts
            reaction_ms = int((pend["ts"] - spawn["ts"]).total_seconds() * 1000)
            reaction_ms = max(0, reaction_ms)

            # Extract confirmed pokemon name from the confirmation message
            pkmn_match = _CONFIRM_POKEMON_RE.search(text)
            pokemon_name = (
                self._clean_pokemon_name(pkmn_match.group(1))
                if pkmn_match
                else pend.get("pokemon_typed") or spawn.get("pokemon", "")
            )

            # Remove the spawn so another catch for same spawn doesn't double-count
            _pending_spawns.pop((guild_id, channel_id), None)

            catcher_id   = pend["catcher_id"]
            catcher_name = pend["catcher_name"]
            caught_at    = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
            spawned_at   = spawn["ts"].strftime("%Y-%m-%d %H:%M:%S")
            today_str    = _now_utc().strftime("%Y-%m-%d")

            # Resolve session: prefer single-channel session, then burst session,
            # then fall back to passive (for lifetime tracking only).
            session      = await catch_db.get_active_session(guild_id, channel_id)
            burst        = await catch_db.get_active_burst_session(guild_id)
            in_burst_ch  = await catch_db.is_burst_channel(guild_id, channel_id)

            if session and session["state"] == "active":
                session_id = session["id"]
                is_session = True
            elif burst and burst["state"] == "active" and in_burst_ch:
                session_id = await self._get_or_create_burst_proxy_session(
                    guild_id, channel_id, burst
                )
                is_session = True
            else:
                session_id = await self._get_or_create_passive_session(guild_id)
                is_session = False

            row_id = await catch_db.record_catch(
                session_id   = session_id,
                guild_id     = guild_id,
                channel_id   = channel_id,
                user_id      = catcher_id,
                user_name    = catcher_name,
                pokemon_name = pokemon_name,
                spawn_msg_id = spawn["msg_id"],
                catch_msg_id = pend["catch_msg_id"],
                confirm_msg_id = str(message.id),
                spawned_at   = spawned_at,
                caught_at    = caught_at,
                reaction_ms  = reaction_ms,
                is_session   = is_session,
            )

            if row_id == 0:
                return  # duplicate

            # Flush daily aggregate for this user
            await catch_db.upsert_daily(
                guild_id      = guild_id,
                user_id       = catcher_id,
                user_name     = catcher_name,
                day           = today_str,
                catches       = 1,
                total_ms      = reaction_ms,
                fastest_ms    = reaction_ms,
                pokemon_names = [pokemon_name],
            )

            log.info(
                f"✅ Catch recorded: {catcher_name} caught {pokemon_name} "
                f"in {reaction_ms}ms (session={'yes' if is_session else 'passive'})"
            )

            # ── 3c. Duel tracking ─────────────────────────────────────────────
            await self._on_duel_catch(
                guild_id     = guild_id,
                channel_id   = channel_id,
                catcher_id   = catcher_id,
                catcher_name = catcher_name,
                pokemon_name = pokemon_name,
                reaction_ms  = reaction_ms,
                caught_at    = caught_at,
            )

    # ── Session helpers ───────────────────────────────────────────────────────

    _passive_sessions: dict[str, int] = {}       # f"{guild_id}:{date}" → session_id
    _burst_proxy_sessions: dict[str, int] = {}   # f"{guild_id}:{channel_id}:{burst_id}" → session_id

    async def _get_or_create_passive_session(self, guild_id: str) -> int:
        """Return (or lazily create) the guild-wide passive session for today."""
        today = _now_utc().strftime("%Y-%m-%d")
        cache_key = f"{guild_id}:{today}"
        if cache_key in self._passive_sessions:
            return self._passive_sessions[cache_key]
        sid = await catch_db.start_session(
            guild_id   = guild_id,
            channel_id = "passive",
            started_by = "0",
            label      = f"passive:{today}",
        )
        self._passive_sessions[cache_key] = sid
        return sid

    async def _get_or_create_burst_proxy_session(
        self, guild_id: str, channel_id: str, burst: dict
    ) -> int:
        """
        Each burst channel needs its own catch_sessions row (FK requirement).
        We create one proxy session per channel per burst session and cache it.
        """
        cache_key = f"{guild_id}:{channel_id}:{burst['id']}"
        if cache_key in self._burst_proxy_sessions:
            return self._burst_proxy_sessions[cache_key]
        sid = await catch_db.start_session(
            guild_id   = guild_id,
            channel_id = channel_id,
            started_by = burst["started_by"],
            label      = f"burst#{burst['id']}:{burst.get('label', '')}",
        )
        self._burst_proxy_sessions[cache_key] = sid
        return sid

    # ── Duel helpers ──────────────────────────────────────────────────────────

    async def _on_duel_catch(
        self,
        guild_id: str,
        channel_id: str,
        catcher_id: str,
        catcher_name: str,
        pokemon_name: str,
        reaction_ms: int,
        caught_at: str,
    ) -> None:
        """Called after every confirmed catch. Updates any active duel."""
        duel_state = None
        for ds in _active_duels.values():
            if ds.guild_id != guild_id:
                continue
            if catcher_id not in (ds.challenger_id, ds.opponent_id):
                continue
            duel_state = ds
            break

        if duel_state is None or duel_state.ended:
            return

        is_challenger = (catcher_id == duel_state.challenger_id)
        ch_c = await catch_db.record_duel_catch(
            duel_id      = duel_state.duel_id,
            user_id      = catcher_id,
            pokemon_name = pokemon_name,
            reaction_ms  = reaction_ms,
            caught_at    = caught_at,
            is_challenger = is_challenger,
        )
        duel_state.record(catcher_id)

        # Check win condition
        mode = duel_state.mode
        if mode == DUEL_MODE_POKEMON and duel_state.mode_value > 0:
            if duel_state.ch_catches >= duel_state.mode_value or duel_state.op_catches >= duel_state.mode_value:
                await self._end_duel(duel_state, reason="pokemon")
        # Time-based is handled by a timer task; free mode never auto-ends

    async def _start_duel(self, interaction: discord.Interaction, duel_id: int) -> None:
        """Called when opponent accepts — marks duel active and starts timer if needed."""
        started_at = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        ok = await catch_db.accept_duel(duel_id, started_at)
        if not ok:
            await interaction.response.send_message("❌ Duel could not be started.", ephemeral=True)
            return

        duel = await catch_db.get_duel(duel_id)
        if not duel:
            await interaction.response.send_message("❌ Duel not found.", ephemeral=True)
            return

        ds = DuelState(
            duel_id       = duel_id,
            guild_id      = duel["guild_id"],
            channel_id    = duel["channel_id"],
            challenger_id = duel["challenger_id"],
            opponent_id   = duel["opponent_id"],
            mode          = duel["mode"],
            mode_value    = duel["mode_value"],
        )
        _active_duels[duel_id] = ds

        # Fetch member objects for display
        guild = interaction.guild
        ch_member = guild.get_member(int(duel["challenger_id"])) if guild else None
        op_member = interaction.user

        ch_name   = duel["challenger_name"]
        op_name   = duel["opponent_name"]
        ch_avatar = ch_member.display_avatar.url if ch_member else ""
        op_avatar = op_member.display_avatar.url

        embed = _duel_status_embed(ds, ch_name, op_name, ch_avatar, op_avatar,
                                   guild_id=duel["guild_id"])
        embed.description = (
            f"⚔️ Duel started! Both players are now catching across burst channels.\n"
            f"{_mode_label(duel['mode'], duel['mode_value'])}"
        )
        await interaction.response.edit_message(embed=embed, view=None)

        # Start timer task for time-based duels
        if ds.mode == DUEL_MODE_TIME and ds.mode_value > 0:
            async def _timer():
                await asyncio.sleep(ds.mode_value)
                if not ds.ended:
                    await self._end_duel(ds, reason="time")
            ds._timer_task = asyncio.create_task(_timer())

    async def _end_duel(self, ds: DuelState, reason: str = "manual") -> None:
        """Finalise a duel, post result to the original channel."""
        if ds.ended:
            return
        ds.ended = True
        if ds._timer_task and not ds._timer_task.done():
            ds._timer_task.cancel()

        winner_id = ds.winner_id()
        ended_at  = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        await catch_db.end_duel(ds.duel_id, winner_id, ended_at)
        _active_duels.pop(ds.duel_id, None)

        # Fetch h2h for the result
        h2h = await catch_db.get_duel_head_to_head(ds.guild_id, ds.challenger_id, ds.opponent_id)

        # Try to post result in the duel's channel
        channel = self.bot.get_channel(int(ds.channel_id))
        if not channel:
            return

        guild = channel.guild
        ch_member = guild.get_member(int(ds.challenger_id))
        op_member = guild.get_member(int(ds.opponent_id))
        ch_name   = ch_member.display_name if ch_member else ds.challenger_id
        op_name   = op_member.display_name if op_member else ds.opponent_id
        ch_avatar = ch_member.display_avatar.url if ch_member else ""
        op_avatar = op_member.display_avatar.url if op_member else ""

        embed = _duel_status_embed(ds, ch_name, op_name, ch_avatar, op_avatar,
                                   ended=True, winner_id=winner_id, guild_id=ds.guild_id)

        reason_str = {
            "pokemon": f"First to {ds.mode_value} caught!",
            "time":    f"Time's up! ({_fmt_duration(ds.mode_value)})",
            "manual":  "Duel ended manually.",
        }.get(reason, "")

        h2h_str = f"{ch_name} {h2h['a_wins']}–{h2h['b_wins']} {op_name} all-time"
        embed.add_field(name="📊 Head-to-Head", value=h2h_str, inline=False)
        if reason_str:
            embed.add_field(name="🏁 Reason", value=reason_str, inline=False)

        await channel.send(embed=embed)

    # ── Prefix commands ───────────────────────────────────────────────────────

    @commands.command(name="catchstart", aliases=["cstart"])
    async def catchstart(self, ctx: commands.Context, *, label: str = ""):
        """Start a grind session in this channel. Optional label."""
        if not ctx.guild:
            return
        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        existing = await catch_db.get_active_session(guild_id, channel_id)
        if existing:
            state_word = "paused" if existing["state"] == "paused" else "already running"
            await ctx.send(
                f"⚠️ A session is {state_word} in this channel "
                f"(started by <@{existing['started_by']}>, label: `{existing['label'] or 'none'}`). "
                f"Use `!catchstop` to end it or `!catchresume` to resume."
            )
            return

        sid = await catch_db.start_session(
            guild_id   = guild_id,
            channel_id = channel_id,
            started_by = str(ctx.author.id),
            label      = label or "",
        )

        embed = discord.Embed(
            title="▶️ Grind Session Started!",
            description=(
                f"Tracking catches in <#{channel_id}>.\n"
                f"Session ID: `{sid}`"
                + (f"\nLabel: **{label}**" if label else "")
            ),
            colour=0x57F287,
        )
        embed.add_field(
            name="Commands",
            value="`!catchstop` · `!catchpause` · `!catchresume` · `!catchstatus`",
            inline=False,
        )
        embed.set_footer(text=make_footer(guild_id))
        await ctx.send(embed=embed)

    @commands.command(name="catchstop", aliases=["cstop", "catchend"])
    async def catchstop(self, ctx: commands.Context):
        """End the active session and show a full summary."""
        if not ctx.guild:
            return
        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        session = await catch_db.get_active_session(guild_id, channel_id)
        if not session:
            await ctx.send("❌ No active session in this channel.")
            return

        ended_at = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        await catch_db.end_session(session["id"], ended_at)
        session["ended_at"] = ended_at
        session["state"]    = "ended"

        stats = await catch_db.get_session_stats(session["id"])
        embed = _session_embed(session, stats, guild_id, ended=True)
        await ctx.send(embed=embed)

    @commands.command(name="catchpause", aliases=["cpause"])
    async def catchpause(self, ctx: commands.Context):
        """Pause the active session (timer stops)."""
        if not ctx.guild:
            return
        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        session = await catch_db.get_active_session(guild_id, channel_id)
        if not session:
            await ctx.send("❌ No active session in this channel.")
            return
        if session["state"] == "paused":
            await ctx.send("⏸️ Session is already paused. Use `!catchresume` to continue.")
            return

        paused_at = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        await catch_db.pause_session(session["id"], paused_at)
        await ctx.send("⏸️ Session paused. Use `!catchresume` to continue.")

    @commands.command(name="catchresume", aliases=["cresume"])
    async def catchresume(self, ctx: commands.Context):
        """Resume a paused session."""
        if not ctx.guild:
            return
        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        session = await catch_db.get_active_session(guild_id, channel_id)
        if not session:
            await ctx.send("❌ No active session in this channel.")
            return
        if session["state"] == "active":
            await ctx.send("▶️ Session is already running!")
            return

        # Calculate how long it was paused
        paused_at_str = session.get("paused_at") or ""
        extra_ms = 0
        if paused_at_str:
            try:
                paused_dt = datetime.fromisoformat(paused_at_str).replace(tzinfo=timezone.utc)
                extra_ms  = int((_now_utc() - paused_dt).total_seconds() * 1000)
            except Exception:
                pass

        resumed_at = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        await catch_db.resume_session(session["id"], resumed_at, extra_ms)
        await ctx.send("▶️ Session resumed! Catches are being tracked again.")

    @commands.command(name="catchstatus", aliases=["cstatus", "catchlive"])
    async def catchstatus(self, ctx: commands.Context):
        """Show live stats for the active session."""
        if not ctx.guild:
            return
        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        session = await catch_db.get_active_session(guild_id, channel_id)
        if not session:
            await ctx.send("❌ No active session in this channel. Start one with `!catchstart`.")
            return

        stats = await catch_db.get_session_stats(session["id"])
        embed = _session_embed(session, stats, guild_id, ended=False)
        await ctx.send(embed=embed)

    # ── Burst prefix commands ─────────────────────────────────────────────────

    @commands.command(name="burststart", aliases=["bstart"])
    async def burststart(self, ctx: commands.Context, *, label: str = ""):
        """Start a multi-channel burst session across all registered burst channels. Admin/owner only."""
        if not ctx.guild:
            return
        if not await _is_privileged(ctx):
            await ctx.send("🚫 Only admins or the bot owner can start a burst session.")
            return

        guild_id = str(ctx.guild.id)
        existing = await catch_db.get_active_burst_session(guild_id)
        if existing:
            state_word = "paused" if existing["state"] == "paused" else "already running"
            await ctx.send(
                f"⚠️ A burst session is {state_word} "
                f"(started by <@{existing['started_by']}>, label: `{existing['label'] or 'none'}`). "
                f"Use `!burststop` to end it or `!burstresume` to resume."
            )
            return

        channels = await catch_db.get_burst_channels(guild_id)
        if not channels:
            await ctx.send(
                "❌ No burst channels registered yet!\n"
                "Use `/catches burst add` to register channels first."
            )
            return

        sid = await catch_db.start_burst_session(
            guild_id   = guild_id,
            started_by = str(ctx.author.id),
            label      = label or "",
        )
        # Invalidate burst proxy session cache so new proxies are created for this session
        keys_to_drop = [k for k in self._burst_proxy_sessions if k.startswith(guild_id + ":")]
        for k in keys_to_drop:
            del self._burst_proxy_sessions[k]

        ch_mentions = " ".join(f"<#{c}>" for c in channels[:10])
        extra       = f" *(+{len(channels)-10} more)*" if len(channels) > 10 else ""

        embed = discord.Embed(
            title="⚡ Burst Session Started!",
            description=(
                f"Tracking catches across **{len(channels)}** channel(s).\n"
                f"Session ID: `{sid}`"
                + (f"\nLabel: **{label}**" if label else "")
            ),
            colour=0xF1C40F,
        )
        embed.add_field(name="📡 Channels", value=ch_mentions + extra, inline=False)
        embed.add_field(
            name="Commands",
            value="`!burststop` · `!burstpause` · `!burstresume` · `!burststatus`",
            inline=False,
        )
        embed.set_footer(text=make_footer(guild_id))
        await ctx.send(embed=embed)

    @commands.command(name="burststop", aliases=["bstop", "burstend"])
    async def burststop(self, ctx: commands.Context):
        """End the active burst session and show a full summary."""
        if not ctx.guild:
            return
        if not await _is_privileged(ctx):
            await ctx.send("🚫 Only admins or the bot owner can stop a burst session.")
            return

        guild_id = str(ctx.guild.id)
        burst    = await catch_db.get_active_burst_session(guild_id)
        if not burst:
            await ctx.send("❌ No active burst session in this server.")
            return

        ended_at = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        await catch_db.end_burst_session(burst["id"], ended_at)
        burst["ended_at"] = ended_at
        burst["state"]    = "ended"

        # End all proxy catch_sessions for this burst
        proxy_keys = [k for k in self._burst_proxy_sessions if k.startswith(guild_id + ":")]
        for k in proxy_keys:
            await catch_db.end_session(self._burst_proxy_sessions.pop(k), ended_at)

        stats = await catch_db.get_burst_session_stats(burst["id"], guild_id)
        channels = await catch_db.get_burst_channels(guild_id)
        embed = _burst_session_embed(burst, stats, channels, guild_id, ended=True)
        await ctx.send(embed=embed)

    @commands.command(name="burstpause", aliases=["bpause"])
    async def burstpause(self, ctx: commands.Context):
        """Pause the active burst session (timer stops)."""
        if not ctx.guild:
            return
        if not await _is_privileged(ctx):
            await ctx.send("🚫 Only admins or the bot owner can pause a burst session.")
            return

        guild_id = str(ctx.guild.id)
        burst    = await catch_db.get_active_burst_session(guild_id)
        if not burst:
            await ctx.send("❌ No active burst session in this server.")
            return
        if burst["state"] == "paused":
            await ctx.send("⏸️ Burst session is already paused. Use `!burstresume` to continue.")
            return

        paused_at = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
        await catch_db.pause_burst_session(burst["id"], paused_at)
        await ctx.send("⏸️ Burst session paused. Use `!burstresume` to continue.")

    @commands.command(name="burstresume", aliases=["bresume"])
    async def burstresume(self, ctx: commands.Context):
        """Resume a paused burst session."""
        if not ctx.guild:
            return
        if not await _is_privileged(ctx):
            await ctx.send("🚫 Only admins or the bot owner can resume a burst session.")
            return

        guild_id = str(ctx.guild.id)
        burst    = await catch_db.get_active_burst_session(guild_id)
        if not burst:
            await ctx.send("❌ No active burst session in this server.")
            return
        if burst["state"] == "active":
            await ctx.send("▶️ Burst session is already running!")
            return

        extra_ms = 0
        if burst.get("paused_at"):
            try:
                paused_dt = datetime.fromisoformat(burst["paused_at"]).replace(tzinfo=timezone.utc)
                extra_ms  = int((_now_utc() - paused_dt).total_seconds() * 1000)
            except Exception:
                pass

        await catch_db.resume_burst_session(burst["id"], extra_ms)
        await ctx.send("▶️ Burst session resumed! Catches in all burst channels are being tracked.")

    @commands.command(name="burststatus", aliases=["bstatus", "burstlive"])
    async def burststatus(self, ctx: commands.Context):
        """Show live stats for the active burst session."""
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id)
        burst    = await catch_db.get_active_burst_session(guild_id)
        if not burst:
            await ctx.send("❌ No active burst session. Start one with `!burststart`.")
            return

        stats    = await catch_db.get_burst_session_stats(burst["id"], guild_id)
        channels = await catch_db.get_burst_channels(guild_id)
        embed    = _burst_session_embed(burst, stats, channels, guild_id, ended=False)
        await ctx.send(embed=embed)

    # ── Slash commands ────────────────────────────────────────────────────────

    _catches = app_commands.Group(name="catches", description="Catch tracking stats & leaderboards")
    _burst   = app_commands.Group(name="burst", description="Manage burst channels", parent=_catches)

    @_catches.command(name="stats", description="View catch stats for yourself or another user")
    @app_commands.describe(
        user="The user to look up (defaults to yourself)",
        show_accuracy="Show accuracy / failed catch rate (default: yes)",
    )
    async def catches_stats(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        show_accuracy: bool = True,
    ):
        await interaction.response.defer()
        guild_id  = str(interaction.guild_id)
        target    = user or interaction.user
        stats     = await catch_db.get_user_lifetime_stats(guild_id, str(target.id))

        if stats["total"] == 0 and stats["active_days"] == 0:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"No catches recorded yet for **{target.display_name}**.",
                    colour=0xED4245,
                ).set_footer(text=make_footer(guild_id)),
                ephemeral=True,
            )
            return

        embed = _stats_embed(stats, target, guild_id)

        if show_accuracy:
            acc = await catch_db.get_user_accuracy(guild_id, str(target.id))
            if acc["total_attempts"] > 0:
                bar_filled = round(acc["accuracy_pct"] / 10)
                bar = "🟩" * bar_filled + "🟥" * (10 - bar_filled)
                embed.add_field(
                    name="🎯 Accuracy",
                    value=(
                        f"{bar}\n"
                        f"**{acc['accuracy_pct']}%** — "
                        f"{acc['success']} caught / {acc['failed']} missed "
                        f"({acc['total_attempts']} attempts)"
                    ),
                    inline=False,
                )

        await interaction.followup.send(embed=embed)

    @_catches.command(name="leaderboard", description="Catch leaderboard for the server")
    @app_commands.describe(period="Time period (today / week / month / all)")
    @app_commands.choices(period=[
        app_commands.Choice(name="Today",      value="today"),
        app_commands.Choice(name="This Week",  value="week"),
        app_commands.Choice(name="This Month", value="month"),
        app_commands.Choice(name="All Time",   value="all"),
    ])
    async def catches_leaderboard(self, interaction: discord.Interaction, period: str = "week"):
        await interaction.response.defer()
        guild_id   = str(interaction.guild_id)
        rows       = await catch_db.get_leaderboard(guild_id, period, limit=10)
        total      = await catch_db.get_guild_total_catches(guild_id, period)
        guild_name = interaction.guild.name if interaction.guild else "Server"

        embed = _leaderboard_embed(rows, period, guild_name, guild_id, total)
        await interaction.followup.send(embed=embed)

    @_catches.command(name="session", description="Show summary for a specific past session")
    @app_commands.describe(session_id="The session ID (shown when a session ends)")
    async def catches_session(self, interaction: discord.Interaction, session_id: int):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        session  = await catch_db.get_session_by_id(session_id)

        if not session or session["guild_id"] != guild_id:
            await interaction.followup.send("❌ Session not found.", ephemeral=True)
            return

        stats = await catch_db.get_session_stats(session_id)
        ended = session["state"] == "ended"
        embed = _session_embed(session, stats, guild_id, ended=ended)
        await interaction.followup.send(embed=embed)

    @_catches.command(name="fastest", description="All-time fastest catches in the server")
    async def catches_fastest(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        rows     = await catch_db.get_fastest_catches(guild_id, limit=10)

        embed = discord.Embed(
            title="⚡ Fastest Catches — All Time",
            colour=0x00BCD4,
        )

        if not rows:
            embed.description = "*No timed catches recorded yet!*"
            embed.set_footer(text=make_footer(guild_id))
            await interaction.followup.send(embed=embed)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            name  = row["user_name"]
            pkmn  = row["pokemon_name"]
            ms    = _fmt_ms(row["reaction_ms"])
            lines.append(f"{medal} **{name}** caught **{pkmn}** in `{ms}`")

        embed.description = "\n".join(lines)
        embed.set_footer(text=make_footer(guild_id))
        await interaction.followup.send(embed=embed)

    @_catches.command(name="today", description="Quick view of today's catches for everyone")
    async def catches_today(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id   = str(interaction.guild_id)
        rows       = await catch_db.get_leaderboard(guild_id, "today", limit=15)
        total      = await catch_db.get_guild_total_catches(guild_id, "today")
        guild_name = interaction.guild.name if interaction.guild else "Server"

        embed = _leaderboard_embed(rows, "today", guild_name, guild_id, total)
        embed.title = "📅 Today's Catches"
        await interaction.followup.send(embed=embed)

    # ── /catches burst subgroup ───────────────────────────────────────────────

    @_burst.command(name="add", description="Register burst channels — single, whole category, or from→to range")
    @app_commands.describe(
        channel="A single channel to add",
        category="Add all text channels in this category",
        category2="Add all channels in a second category (optional)",
        category3="Add all channels in a third category (optional)",
        from_channel="Start of a range (inclusive, same category)",
        to_channel="End of a range (inclusive, same category)",
    )
    async def burst_add(
        self,
        interaction: discord.Interaction,
        channel:      Optional[discord.TextChannel]     = None,
        category:     Optional[discord.CategoryChannel] = None,
        category2:    Optional[discord.CategoryChannel] = None,
        category3:    Optional[discord.CategoryChannel] = None,
        from_channel: Optional[discord.TextChannel]     = None,
        to_channel:   Optional[discord.TextChannel]     = None,
    ):
        if not _is_slash_privileged(interaction):
            await interaction.response.send_message("🚫 Admin or owner only.", ephemeral=True)
            return

        targets: list[discord.TextChannel] = []

        if channel:
            targets.append(channel)

        for cat in [category, category2, category3]:
            if cat:
                targets.extend(
                    ch for ch in cat.channels
                    if isinstance(ch, discord.TextChannel) and ch not in targets
                )

        if from_channel and to_channel:
            cat  = from_channel.category
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == cat],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                await interaction.response.send_message(
                    "❌ Couldn't resolve range — both channels must be in the same category.",
                    ephemeral=True,
                )
                return
            if si > ei:
                si, ei = ei, si
            for ch in pool[si:ei + 1]:
                if ch not in targets:
                    targets.append(ch)
        elif from_channel or to_channel:
            await interaction.response.send_message(
                "⚠️ Provide **both** `from_channel` and `to_channel` for a range.", ephemeral=True
            )
            return

        if not targets:
            await interaction.response.send_message(
                "⚠️ No channels specified. Use `channel`, `category`, or `from_channel`+`to_channel`.",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        added, already = await catch_db.add_burst_channels(
            guild_id, [str(ch.id) for ch in targets], str(interaction.user.id)
        )

        embed = discord.Embed(title="📡 Burst Channels Updated", colour=0x57F287)
        if added:
            embed.add_field(
                name=f"✅ Added ({len(added)})",
                value=_channel_list_str(interaction.guild, added),
                inline=False,
            )
        if already:
            embed.add_field(
                name=f"⚪ Already registered ({len(already)})",
                value=_channel_list_str(interaction.guild, already),
                inline=False,
            )
        embed.set_footer(text=make_footer(guild_id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @_burst.command(name="remove", description="Unregister burst channels — single, whole category, or from→to range")
    @app_commands.describe(
        channel="A single channel to remove",
        category="Remove all channels in this category",
        category2="Remove all channels in a second category (optional)",
        category3="Remove all channels in a third category (optional)",
        from_channel="Start of a range to remove (inclusive)",
        to_channel="End of a range to remove (inclusive)",
    )
    async def burst_remove(
        self,
        interaction: discord.Interaction,
        channel:      Optional[discord.TextChannel]     = None,
        category:     Optional[discord.CategoryChannel] = None,
        category2:    Optional[discord.CategoryChannel] = None,
        category3:    Optional[discord.CategoryChannel] = None,
        from_channel: Optional[discord.TextChannel]     = None,
        to_channel:   Optional[discord.TextChannel]     = None,
    ):
        if not _is_slash_privileged(interaction):
            await interaction.response.send_message("🚫 Admin or owner only.", ephemeral=True)
            return

        targets: list[discord.TextChannel] = []

        if channel:
            targets.append(channel)

        for cat in [category, category2, category3]:
            if cat:
                targets.extend(
                    ch for ch in cat.channels
                    if isinstance(ch, discord.TextChannel) and ch not in targets
                )

        if from_channel and to_channel:
            cat  = from_channel.category
            pool = sorted(
                [ch for ch in interaction.guild.text_channels if ch.category == cat],
                key=lambda c: c.position,
            )
            try:
                si = next(i for i, c in enumerate(pool) if c.id == from_channel.id)
                ei = next(i for i, c in enumerate(pool) if c.id == to_channel.id)
            except StopIteration:
                await interaction.response.send_message(
                    "❌ Couldn't resolve range — both channels must be in the same category.",
                    ephemeral=True,
                )
                return
            if si > ei:
                si, ei = ei, si
            for ch in pool[si:ei + 1]:
                if ch not in targets:
                    targets.append(ch)
        elif from_channel or to_channel:
            await interaction.response.send_message(
                "⚠️ Provide **both** `from_channel` and `to_channel` for a range.", ephemeral=True
            )
            return

        if not targets:
            await interaction.response.send_message(
                "⚠️ No channels specified.", ephemeral=True
            )
            return

        guild_id = str(interaction.guild_id)
        removed, not_found = await catch_db.remove_burst_channels(
            guild_id, [str(ch.id) for ch in targets]
        )

        embed = discord.Embed(title="📡 Burst Channels Updated", colour=0xED4245)
        if removed:
            embed.add_field(
                name=f"🗑️ Removed ({len(removed)})",
                value=_channel_list_str(interaction.guild, removed),
                inline=False,
            )
        if not_found:
            embed.add_field(
                name=f"⚪ Not registered ({len(not_found)})",
                value=_channel_list_str(interaction.guild, not_found),
                inline=False,
            )
        embed.set_footer(text=make_footer(guild_id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @_burst.command(name="list", description="Show all registered burst channels")
    async def burst_list(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        channel_ids = await catch_db.get_burst_channels(guild_id)

        embed = discord.Embed(title="📡 Registered Burst Channels", colour=0x5865F2)
        if not channel_ids:
            embed.description = (
                "*No burst channels registered yet.*\n"
                "Use `/catches burst add` to register channels."
            )
        else:
            # Sort by server position
            sorted_ids = _sort_channel_ids(interaction.guild, channel_ids)
            embed.description = _channel_list_str(interaction.guild, sorted_ids, numbered=True)
            embed.set_footer(text=make_footer(guild_id) + f" • {len(channel_ids)} channel(s)")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @_burst.command(name="clear", description="Remove ALL registered burst channels for this server")
    async def burst_clear(self, interaction: discord.Interaction):
        if not _is_slash_privileged(interaction):
            await interaction.response.send_message("🚫 Admin or owner only.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        count    = await catch_db.clear_burst_channels(guild_id)

        embed = discord.Embed(
            title="🗑️ Burst Channels Cleared",
            description=f"Removed **{count}** burst channel(s)." if count else "*No channels were registered.*",
            colour=0xED4245 if count else 0x99AAB5,
        )
        embed.set_footer(text=make_footer(guild_id))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Duel prefix command ───────────────────────────────────────────────────

    @commands.command(name="duel", aliases=["catchduel", "1v1"])
    async def duel(self, ctx: commands.Context, opponent: discord.Member, *, args: str = ""):
        """
        Challenge someone to a 1v1 blitz catching duel.

        Modes (append after @user):
          (nothing)          → free  — catch for fun, no winner declared
          time 30m           → time  — 30 min timer, most catches wins (also 5m, 10m, 15m, 1h)
          pokemon 500        → first to 500 catches wins (also 1000, 2000, etc.)

        Examples:
          !duel @Alice
          !duel @Alice time 15m
          !duel @Alice pokemon 1000
        """
        if not ctx.guild:
            return
        if opponent.bot or opponent.id == ctx.author.id:
            await ctx.send("❌ You can't duel a bot or yourself.")
            return

        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        # Check neither player is already in a duel
        for uid in (str(ctx.author.id), str(opponent.id)):
            existing = await catch_db.get_active_duel_for_user(guild_id, uid)
            if existing:
                await ctx.send(f"❌ <@{uid}> is already in an active duel.")
                return

        # Parse mode
        mode       = DUEL_MODE_FREE
        mode_value = 0
        args_lower = args.strip().lower()

        if args_lower.startswith("time"):
            # e.g. "time 15m" or "time 30"
            parts = args_lower.split()
            raw   = parts[1] if len(parts) > 1 else ""
            secs  = 0
            if raw.endswith("h"):
                secs = int(raw[:-1]) * 3600
            elif raw.endswith("m"):
                secs = int(raw[:-1]) * 60
            elif raw.isdigit():
                secs = int(raw) * 60  # assume minutes if bare number
            if secs <= 0:
                await ctx.send("❌ Invalid time. Examples: `time 5m`, `time 30m`, `time 1h`")
                return
            mode       = DUEL_MODE_TIME
            mode_value = secs

        elif args_lower.startswith("pokemon") or args_lower.startswith("poke"):
            parts = args_lower.split()
            raw   = parts[1] if len(parts) > 1 else ""
            if not raw.isdigit() or int(raw) <= 0:
                await ctx.send("❌ Invalid count. Examples: `pokemon 500`, `pokemon 1000`")
                return
            mode       = DUEL_MODE_POKEMON
            mode_value = int(raw)

        # Count burst channels as the "arena"
        burst_channels = await catch_db.get_burst_channels(guild_id)
        burst_count    = len(burst_channels)

        # Create the duel record
        duel_id = await catch_db.create_duel(
            guild_id        = guild_id,
            channel_id      = channel_id,
            challenger_id   = str(ctx.author.id),
            challenger_name = ctx.author.display_name,
            opponent_id     = str(opponent.id),
            opponent_name   = opponent.display_name,
            mode            = mode,
            mode_value      = mode_value,
        )

        embed = _duel_invite_embed(
            challenger  = ctx.author,
            opponent    = opponent,
            mode        = mode,
            mode_value  = mode_value,
            burst_count = burst_count,
            duel_id     = duel_id,
            guild_id    = guild_id,
        )
        view = DuelInviteView(duel_id=duel_id, opponent_id=opponent.id, cog=self)
        msg  = await ctx.send(content=opponent.mention, embed=embed, view=view)

        # Wait for timeout then clean up message if not answered
        await view.wait()
        if view.result is None:
            # Timed out
            embed_to = discord.Embed(
                description=f"⏰ Duel invite expired — {opponent.display_name} didn't respond.",
                colour=0x99AAB5,
            )
            try:
                await msg.edit(embed=embed_to, view=None)
            except discord.NotFound:
                pass

    @commands.command(name="duelforfeit", aliases=["duelstop", "duelend"])
    async def duelforfeit(self, ctx: commands.Context):
        """Forfeit / end your current active duel."""
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id)
        duel     = await catch_db.get_active_duel_for_user(guild_id, str(ctx.author.id))
        if not duel:
            await ctx.send("❌ You're not in an active duel.")
            return

        ds = _active_duels.get(duel["id"])
        if ds:
            await self._end_duel(ds, reason="manual")
        else:
            # Edge case: state in DB but not in memory (bot restart)
            ended_at  = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
            winner_id = ""
            if duel["challenger_catches"] > duel["opponent_catches"]:
                winner_id = duel["challenger_id"]
            elif duel["opponent_catches"] > duel["challenger_catches"]:
                winner_id = duel["opponent_id"]
            await catch_db.end_duel(duel["id"], winner_id, ended_at)
            await ctx.send("🏁 Duel ended.")

    # ── Duel slash commands ───────────────────────────────────────────────────

    _duel_group = app_commands.Group(name="duel", description="1v1 blitz catching duels", parent=_catches)

    @_duel_group.command(name="h2h", description="Head-to-head duel record between two players")
    @app_commands.describe(player1="First player", player2="Second player")
    async def duel_h2h(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member,
    ):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        h2h      = await catch_db.get_duel_head_to_head(guild_id, str(player1.id), str(player2.id))

        if h2h["total"] == 0:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"No duels on record between **{player1.display_name}** and **{player2.display_name}**.",
                    colour=0x99AAB5,
                ).set_footer(text=make_footer(guild_id)),
            )
            return

        embed = discord.Embed(
            title="⚔️ Head-to-Head Record",
            colour=0xFF6B35,
        )
        embed.set_thumbnail(url=player1.display_avatar.url)
        embed.add_field(
            name=f"🔴 {player1.display_name}",
            value=f"**{h2h['a_wins']}** wins",
            inline=True,
        )
        embed.add_field(name="vs", value=f"`{h2h['total']} duels`", inline=True)
        embed.add_field(
            name=f"🔵 {player2.display_name}",
            value=f"**{h2h['b_wins']}** wins",
            inline=True,
        )
        if h2h["draws"]:
            embed.add_field(name="Draws", value=str(h2h["draws"]), inline=False)
        embed.set_footer(text=make_footer(guild_id))
        await interaction.followup.send(embed=embed)

    @_duel_group.command(name="record", description="View your duel win/loss record")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def duel_record(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        target   = user or interaction.user
        rec      = await catch_db.get_duel_stats(guild_id, str(target.id))

        embed = discord.Embed(
            title=f"⚔️ Duel Record — {target.display_name}",
            colour=0xFF6B35,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        total = rec["total"]
        if total == 0:
            embed.description = "*No duels on record yet.*"
        else:
            win_rate = round(rec["wins"] / total * 100) if total > 0 else 0
            embed.add_field(name="🏆 Wins",   value=str(rec["wins"]),   inline=True)
            embed.add_field(name="💀 Losses", value=str(rec["losses"]), inline=True)
            embed.add_field(name="🤝 Draws",  value=str(rec["draws"]),  inline=True)
            embed.add_field(name="📊 Win Rate", value=f"{win_rate}%", inline=True)
            embed.add_field(name="🎮 Total",   value=str(total),         inline=True)
        embed.set_footer(text=make_footer(guild_id))
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CatchTrackerCog(bot))
