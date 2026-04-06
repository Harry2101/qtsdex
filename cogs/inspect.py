"""
cogs/inspect.py  —  Owner-only user inspection tool

Slash commands:
  /inspect user <user>    — Full Discord profile + guild info for any user
  /inspect id <user_id>   — Same but by raw user ID (works even if not in server)
  /inspect catch <user>   — Shiny catch history & stats from the starboard DB
"""

import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import starboard_db

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))


def _is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d %b %Y, %H:%M UTC")


def _relative(dt: datetime | None) -> str:
    if not dt:
        return ""
    delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else datetime.now(timezone.utc) - dt
    days = delta.days
    if days < 1:
        return "(today)"
    if days < 30:
        return f"({days}d ago)"
    if days < 365:
        return f"({days // 30}mo ago)"
    return f"({days // 365}y ago)"


async def _build_user_embed(
    bot: commands.Bot,
    interaction: discord.Interaction,
    user: discord.User,
) -> discord.Embed:
    """Build the full inspection embed for a user."""

    # ── Try to get member object (guild-specific info) ──
    member: discord.Member | None = None
    if interaction.guild:
        try:
            member = interaction.guild.get_member(user.id) or await interaction.guild.fetch_member(user.id)
        except (discord.NotFound, discord.HTTPException):
            pass

    now = datetime.now(timezone.utc)
    colour = member.top_role.colour if member and member.top_role.colour.value else discord.Colour(0x5865F2)

    embed = discord.Embed(colour=colour)
    embed.set_author(name=f"🔍 Inspection — {user}", icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.with_size(512).url)

    # ── Identity ──
    id_lines = [
        f"**Username:** {user.name}",
        f"**Display name:** {user.display_name}",
        f"**ID:** `{user.id}`",
        f"**Bot:** {'Yes' if user.bot else 'No'}",
        f"**System:** {'Yes' if user.system else 'No'}",
    ]
    embed.add_field(name="👤 Identity", value="\n".join(id_lines), inline=False)

    # ── Account age ──
    created = user.created_at
    age_lines = [
        f"**Created:** {_fmt_dt(created)} {_relative(created)}",
    ]
    if member:
        joined = member.joined_at
        age_lines.append(f"**Joined server:** {_fmt_dt(joined)} {_relative(joined)}")
        if member.premium_since:
            age_lines.append(f"**Boosting since:** {_fmt_dt(member.premium_since)} {_relative(member.premium_since)}")
    embed.add_field(name="📅 Dates", value="\n".join(age_lines), inline=False)

    # ── Guild-specific info ──
    if member:
        roles = [r for r in member.roles if r.name != "@everyone"]
        roles_str = ", ".join(r.mention for r in reversed(roles)) if roles else "*none*"

        guild_lines = [
            f"**Nickname:** {member.nick or '—'}",
            f"**Top role:** {member.top_role.mention}",
            f"**Roles ({len(roles)}):** {roles_str}",
        ]

        perms = member.guild_permissions
        notable = []
        if perms.administrator:
            notable.append("Administrator")
        if perms.manage_guild:
            notable.append("Manage Server")
        if perms.manage_roles:
            notable.append("Manage Roles")
        if perms.manage_channels:
            notable.append("Manage Channels")
        if perms.ban_members:
            notable.append("Ban Members")
        if perms.kick_members:
            notable.append("Kick Members")
        if perms.moderate_members:
            notable.append("Timeout Members")
        if notable:
            guild_lines.append(f"**Key perms:** {', '.join(notable)}")

        status_map = {
            discord.Status.online: "🟢 Online",
            discord.Status.idle: "🟡 Idle",
            discord.Status.dnd: "🔴 Do Not Disturb",
            discord.Status.offline: "⚫ Offline",
        }
        guild_lines.append(f"**Status:** {status_map.get(member.status, '—')}")

        if member.activities:
            acts = []
            for act in member.activities:
                if isinstance(act, discord.Game):
                    acts.append(f"Playing *{act.name}*")
                elif isinstance(act, discord.Streaming):
                    acts.append(f"Streaming *{act.name}*")
                elif isinstance(act, discord.CustomActivity):
                    acts.append(f"Custom: {act.name or ''} {act.emoji or ''}")
                elif isinstance(act, discord.Activity):
                    acts.append(f"{act.type.name.title()} *{act.name}*")
            if acts:
                guild_lines.append(f"**Activity:** {' | '.join(acts)}")

        embed.add_field(name="🏠 In this server", value="\n".join(guild_lines), inline=False)
    else:
        embed.add_field(name="🏠 In this server", value="*Not a member of this server*", inline=False)

    # ── Shiny catch stats (from starboard DB) ──
    if interaction.guild:
        guild_id = str(interaction.guild.id)
        user_id = str(user.id)
        stats = await starboard_db.get_user_stats(guild_id, user_id)

        if stats["total_catches"] > 0:
            catch_lines = [
                f"✨ **All time:** {stats['total_catches']} catches",
                f"📅 **This week:** {stats['week_catches']}",
                f"🗓️ **This month:** {stats['month_catches']}",
                f"🦎 **Unique species:** {stats['unique_pokemon']}",
            ]
            if stats["weekly_wins"] > 0:
                catch_lines.append(f"🏆 **Weekly titles:** {stats['weekly_wins']}")
            if stats["monthly_wins"] > 0:
                catch_lines.append(f"👑 **Monthly titles:** {stats['monthly_wins']}")
            if stats["weekly_streak"] >= 2:
                catch_lines.append(f"🔥 **Weekly streak:** {stats['weekly_streak']}")
            if stats["monthly_streak"] >= 2:
                catch_lines.append(f"🔥 **Monthly streak:** {stats['monthly_streak']}")
            embed.add_field(name="✨ Shiny Catches", value="\n".join(catch_lines), inline=False)

    # ── Raw DB rows for unknown trainer diagnosis ──
    if interaction.guild:
        guild_id = str(interaction.guild.id)
        import aiosqlite
        async with aiosqlite.connect("data/starboard.db") as db:
            async with db.execute(
                """SELECT pokemon_name, user_name, user_id, caught_at
                   FROM shiny_catches
                   WHERE guild_id=? AND (user_id=? OR user_name LIKE ?)
                   ORDER BY caught_at DESC LIMIT 5""",
                (guild_id, str(user.id), f"%{user.name}%"),
            ) as cur:
                recent = await cur.fetchall()
        if recent:
            rows_text = "\n".join(
                f"`{r[3][:10]}` {r[0]} — uid={r[2] or '?'} name={r[1] or '?'}"
                for r in recent
            )
            embed.add_field(name="🗄️ Recent DB Rows", value=rows_text, inline=False)

    embed.set_footer(text=f"Owner inspection • {now.strftime('%d %b %Y %H:%M UTC')}")
    embed.timestamp = now
    return embed


# ── Cog ──────────────────────────────────────────────────────────────────────

class InspectCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    inspect = app_commands.Group(
        name="inspect",
        description="Owner-only tools to inspect users and data",
    )

    @inspect.command(name="user", description="Full inspection of a user by mention (owner only)")
    @app_commands.describe(user="The user to inspect")
    async def inspect_user(self, interaction: discord.Interaction, user: discord.User):
        if not _is_owner(interaction):
            return await interaction.response.send_message("🚫 Owner only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        embed = await _build_user_embed(self.bot, interaction, user)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @inspect.command(name="id", description="Full inspection of a user by raw ID (owner only)")
    @app_commands.describe(user_id="The Discord user ID to look up")
    async def inspect_id(self, interaction: discord.Interaction, user_id: str):
        if not _is_owner(interaction):
            return await interaction.response.send_message("🚫 Owner only.", ephemeral=True)

        if not user_id.isdigit():
            return await interaction.response.send_message("⚠️ Provide a valid numeric user ID.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            user = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            return await interaction.followup.send(f"❌ No user found with ID `{user_id}`.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ Discord API error: {e}", ephemeral=True)

        embed = await _build_user_embed(self.bot, interaction, user)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @inspect.command(name="unknown", description="Find and diagnose all 'unknown' catch records (owner only)")
    async def inspect_unknown(self, interaction: discord.Interaction):
        if not _is_owner(interaction):
            return await interaction.response.send_message("🚫 Owner only.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("⚠️ Must be used in a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        import aiosqlite
        guild_id = str(interaction.guild.id)
        async with aiosqlite.connect("data/starboard.db") as db:
            async with db.execute(
                """SELECT id, pokemon_name, user_name, user_id, message_id, caught_at
                   FROM shiny_catches
                   WHERE guild_id=? AND (user_id='' OR user_id IS NULL)
                   ORDER BY caught_at DESC LIMIT 20""",
                (guild_id,),
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            return await interaction.followup.send("✅ No unknown-user catch records found.", ephemeral=True)

        lines = [f"**{len(rows)} catch(es) with missing user ID:**\n"]
        for row_id, pname, uname, uid, mid, ts in rows:
            lines.append(
                f"`#{row_id}` {ts[:10]} — **{pname or '?'}**\n"
                f"  name=`{uname or 'none'}` uid=`{uid or 'none'}` msg=`{mid}`"
            )

        embed = discord.Embed(
            title="🔍 Unknown Catch Records",
            description="\n".join(lines),
            colour=0xFF6B35,
        )
        embed.set_footer(text="Use /inspect id to look up a specific user")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(InspectCog(bot))
