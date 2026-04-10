"""
cogs/help.py  —  /help
Interactive help menu with dropdown navigation between categorised sections.
Incense section shown only to users with the configured Incense Manager role
(or server admins). Starboard & Channel sections shown to admins only.
"""

import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import guild_settings_db
from services.guild_settings_db import make_footer, get_bot_name

OWNER_ID = int(os.getenv("OWNER_ID", "145065060568530944"))

# ── Section builders ────────────────────────────────────────────────────────
# Each returns (emoji, title, embed) for the dropdown.


def _section_home(bot_name: str, sections: list[tuple[str, str]], gid: str) -> discord.Embed:
    """Overview page listing all available sections."""
    listing = "\n".join(f"{emoji} **{title}**" for emoji, title, _ in sections)
    embed = discord.Embed(
        title=f"📖  {bot_name} — Command Reference",
        description=(
            "Your all-in-one Pokémon companion for PvP, dex lookups and clan operations.\n"
            "All slash commands support autocomplete — just start typing!\n\n"
            "**Use the dropdown below to jump to a section:**\n\n"
            f"{listing}"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return embed


def _section_pokedex(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="🔍  Pokédex Commands",
        description=(
            "`/pokemon <name>`  — Stats, types, abilities, sprite\n"
            "   ↳ Buttons: ⚔️ Battle Card · 📋 Moves\n\n"
            "`/pokemon_moves <name>`  — Full paginated move list\n"
            "   ↳ Filter by type · 🔍 Move Details · 📢 Share\n\n"
            "`/move <name>`  — Power, accuracy, PP, type, effect\n\n"
            "`/ability <name>`  — Effect description + Pokémon that have it\n\n"
            "`/item <name>`  — Held item or battle item effect\n\n"
            "`/sprite <name>`  — Full-size sprite, normal or shiny"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🔍", "Pokédex", embed)


def _section_types(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="🏷️  Type Tools",
        description=(
            "`/type <type>`  — Offensive & defensive matchup chart\n\n"
            "`/weakness <pokemon>`  — 4× / 2× / ½× / 0× breakdown by Pokémon\n\n"
            "`/weakness type1:<type> type2:<type>`  — Custom dual-type matchup\n\n"
            "`/typechart`  — Single matchup, full attacking row, or help"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🏷️", "Type Tools", embed)


def _section_checklist(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="✨  Shiny Hunt Checklists",
        description=(
            "`/checklist view`  — Your checklist with live catch/uncatch dropdowns\n"
            "   ↳ Buttons: sort A–Z · Dex # · Evo Group · Remaining · Switch list · 👥 Friends\n\n"
            "`/checklist add <pokemon>`  — Add Pokémon (bulk CSV, `--evo` chains supported)\n"
            "   ↳ Example: `/checklist add pokemon:charmander, bulbasaur`\n"
            "   ↳ Evo chains: `/checklist add pokemon:--evo charmander, --evo bulbasaur`\n"
            "   ↳ Toggle: `include_evolutions:True` to expand every mon to its full chain\n\n"
            "`/checklist catch <pokemon>`  — Mark as caught ✅ ✨\n\n"
            "`/checklist uncatch <pokemon>`  — Unmark a catch\n\n"
            "`/checklist remaining`  — Show only uncaught Pokémon\n\n"
            "`/checklist remove <pokemon>`  — Remove Pokémon (bulk CSV + `--evo` supported)\n\n"
            "`/event_setup <name>`  — Name the active event hunt (e.g. Community Day)"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("✨", "Shiny Hunt Checklists", embed)


def _section_friends(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="👥  Checklist Friends",
        description=(
            "`/checklist friend add @user`  — Send a friend request\n\n"
            "`/checklist friend accept @user`  — Accept a request\n\n"
            "`/checklist friend decline @user`  — Decline a request\n\n"
            "`/checklist friend remove @user`  — Remove a friend\n\n"
            "`/checklist friend list`  — See your friends\n\n"
            "`/checklist friend requests`  — View pending requests\n\n"
            "   ↳ Or use the 👥 Friends button on your checklist!"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("👥", "Checklist Friends", embed)


def _section_starboard(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="⭐  Starboard & Shiny Counter  *(admin only)*",
        colour=0x5865F2,
    )
    embed.add_field(
        name="Setup",
        value=(
            "`/starboard init <channel>`  — Set starboard channel & count existing shinies\n"
            "`/starboard format prefix: suffix:`  — Set channel name format (e.g. ✨42✨)\n"
            "`/starboard announcechannel <channel>`  — Set weekly/monthly announcement channel\n"
            "`/starboard remove`  — Unlink the starboard channel\n"
            "`/starboard status`  — Show current configuration"
        ),
        inline=False,
    )
    embed.add_field(
        name="Leaderboard & Announcements",
        value=(
            "`/starboard leaderboard [period]`  — Interactive leaderboard (week/month/all)\n"
            "   ↳ Buttons: period switch · History · Hall of Fame · My Stats\n"
            "`/starboard post [period]`  — Preview & post announcements on demand\n"
            "`/starboard count`  — View the current shiny count\n"
            "`/starboard setcount <count>`  — Manually override the shiny count"
        ),
        inline=False,
    )
    embed.add_field(
        name="Maintenance",
        value=(
            "`/starboard resync`  — Re-read channel & fix catch timestamps from message dates"
        ),
        inline=False,
    )
    embed.add_field(
        name="Auto-behaviour",
        value=(
            "When Operation Dex posts a shiny catch in the starboard channel, the count "
            "increments and the channel name updates automatically.\n"
            "Weekly (Monday) and monthly (1st) top catcher announcements are posted "
            "to the configured announce channel with champion tracking & streaks."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("⭐", "Starboard & Shiny Counter", embed)


def _section_channels(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="🔧  Channel Management  *(admin only)*",
        description=(
            "`/channel create count: prefix: start_number:`  — Bulk-create channels\n"
            "   ↳ e.g. `/channel create count:20 prefix:♡- start_number:30`\n"
            "   ↳ Optional: `category:` and `after:` for positioning\n\n"
            "`/channel delete channel:`  — Delete a single channel\n\n"
            "`/channel delete from_channel: to_channel:`  — Delete a range of channels\n"
            "   ↳ Both modes require confirmation before deletion"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🔧", "Channel Management", embed)


def _section_incense(gid: str, is_admin: bool = False) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="🌿  Mass Incense Manager  *(QTs server only)*",
        colour=0x5865F2,
    )
    embed.add_field(
        name="⚡ Prefix commands",
        value=(
            "`!pause`  — Lock **all** active incense channels simultaneously\n"
            "`!pause <group>`  — Lock only the channels in a named group\n"
            "`!resume`  — Unlock **all** paused incense channels simultaneously\n"
            "`!resume <group>`  — Unlock only the channels in a named group\n"
            "`!incset <id1> <id2> ...`  — Bulk register channels by ID\n\n"
            "*Aliases: `!p` · `!r`*"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Channel registration",
        value=(
            "`/incense add`  — Register channels (single, category, range, up to 3 categories)\n"
            "`/incense remove`  — Unregister channels (same flexible options)\n"
            "`/incense recursive`  — Register all channels after this one (optional end channel)\n"
            "`/incense status`  — See all channels: ▶️ live / ⏸️ paused / 💤 idle\n"
            "`/incense clear [channel]`  — Clear a channel's incense record"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Groups  — target a subset with !p / !r",
        value=(
            "`/incense group create <name>`  — Create a group (max 3, e.g. `clan` · `main`)\n"
            "`/incense group add <name> <ch> [ch2] [ch3]`  — Add channels to a group\n"
            "`/incense group remove <name> <ch> [ch2] [ch3]`  — Remove channels from a group\n"
            "`/incense group list`  — See all groups and their channels\n"
            "`/incense group delete <name>`  — Delete a group (channels stay registered)\n\n"
            "*Example: `!p clan` pauses only clan-group channels.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔧 Manual lock / resync",
        value=(
            "`/incense lock [channel]`  — Lock a specific channel\n"
            "`/incense unlock [channel]`  — Unlock a specific channel\n"
            "`/incense resync`  — Scan history, fix any missed locks, clean old bot messages\n"
            "   ↳ *Use this when a pre-existing or 'cracked' incense wasn't detected*"
        ),
        inline=False,
    )
    if is_admin:
        embed.add_field(
            name="⚙️ Setup  *(admin only)*",
            value=(
                "`/incense setup role <role>`  — Set who can use incense manager commands\n"
                "`/incense setup bot <id>`  — Set the Operation Dex bot to watch\n"
                "`/incense setup view`  — View current configuration\n"
                "`/incense log [user] [channel]`  — View the full audit log"
            ),
            inline=False,
        )
    embed.add_field(
        name="🤖 Auto-behaviour",
        value=(
            "When Operation Dex activates an incense in a registered channel, "
            "the channel is **locked automatically** and a notification is posted.\n"
            "Run `!resume` (or `!resume <group>`) when all channels are ready to go live simultaneously."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🌿", "Mass Incense Manager", embed)


def _section_catch_tracker(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="🎯  Catch Tracker & Grind Sessions",
        colour=0x5865F2,
    )
    embed.add_field(
        name="🔍 How it works",
        value=(
            "The bot silently watches every channel for Operation Dex spawns and catch "
            "confirmations. Every successful catch is recorded automatically — "
            "no setup needed for lifetime stats to build up.\n"
            "Reaction time is measured from the **spawn embed → your `c` command**."
        ),
        inline=False,
    )
    embed.add_field(
        name="▶️ Single-channel sessions  *(anyone)*",
        value=(
            "`!catchstart [label]`  — Start timing catches in **this** channel\n"
            "`!catchstop`  — End session and show full summary\n"
            "`!catchpause`  — Pause the timer (paused time is excluded from duration)\n"
            "`!catchresume`  — Resume a paused session\n"
            "`!catchstatus`  — Live stats for the running session\n"
            "   ↳ Aliases: `!cstart` · `!cstop` · `!cpause` · `!cresume` · `!cstatus`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚡ Multi-channel burst sessions  *(admin only)*",
        value=(
            "`!burststart [label]`  — Start a burst session across **all** registered burst channels at once\n"
            "`!burststop`  — End burst session and show combined summary\n"
            "`!burstpause` / `!burstresume`  — Pause or resume the burst timer\n"
            "`!burststatus`  — Live combined stats across all burst channels\n"
            "   ↳ Aliases: `!bstart` · `!bstop` · `!bpause` · `!bresume` · `!bstatus`"
        ),
        inline=False,
    )
    embed.add_field(
        name="📡 Burst channel setup  *(admin only)*",
        value=(
            "`/catches burst add`  — Register channels as burst channels\n"
            "   ↳ `channel:` — single channel\n"
            "   ↳ `category:` — every text channel in a category *(up to 3 categories at once)*\n"
            "   ↳ `from_channel:` + `to_channel:` — consecutive range within a category\n"
            "`/catches burst remove`  — Unregister channels *(same selectors as add)*\n"
            "`/catches burst list`  — See all registered burst channels\n"
            "`/catches burst clear`  — Remove all burst channels"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Stats & leaderboards  *(anyone)*",
        value=(
            "`/catches stats [user]`  — Personal stats: today · week · month · all-time · fastest · avg react · streak · unique Pokémon\n"
            "`/catches leaderboard [period]`  — Server leaderboard with speed bars *(today / week / month / all)*\n"
            "`/catches today`  — Quick snapshot of today's catches for everyone\n"
            "`/catches fastest`  — All-time fastest individual catches in the server\n"
            "`/catches session <id>`  — Replay any past session by its ID"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Tips",
        value=(
            "• Stats build passively in **every** channel — you don't need a session running\n"
            "• Burst sessions count catches across all registered channels simultaneously\n"
            "• Single-channel `!catchstart` takes priority over a burst session in the same channel\n"
            "• Session IDs are shown when a session ends — use `/catches session <id>` to revisit them"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🎯", "Catch Tracker", embed)


def _section_duels(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="⚔️  Catch Duels",
        colour=0xFF6B35,
    )
    embed.add_field(
        name="Starting a duel",
        value=(
            "`!duel @user`  — Free duel: catch for fun, no winner declared\n"
            "`!duel @user time 15m`  — Timed duel: most catches in X minutes wins\n"
            "   ↳ Valid times: `5m` · `10m` · `15m` · `30m` · `1h`\n"
            "`!duel @user pokemon 500`  — Race: first to catch X Pokémon wins\n"
            "   ↳ Aliases: `!catchduel` · `!1v1`\n\n"
            "The opponent gets **60 seconds** to accept or decline via buttons."
        ),
        inline=False,
    )
    embed.add_field(
        name="During a duel",
        value=(
            "`!duelforfeit`  — End your current duel early (counted as a forfeit)\n"
            "   ↳ Aliases: `!duelstop` · `!duelend`\n\n"
            "Catches are tracked automatically across all registered burst channels.\n"
            "Timed duels end when the timer runs out; race duels end when someone hits the target."
        ),
        inline=False,
    )
    embed.add_field(
        name="Stats & leaderboards",
        value=(
            "`/duel record [user]`  — Win/loss/draw record with win rate\n"
            "`/duel leaderboard`  — Top 10 duelists in the server by wins\n"
            "`/duel h2h @player1 @player2`  — Head-to-head record between two players"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("⚔️", "Catch Duels", embed)


def _section_icc(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="🧪  Incense Control Center (ICC)  *(organizer/admin)*",
        colour=0x57F287,
    )
    embed.add_field(
        name="Org lifecycle",
        value=(
            "`/icc start [label]`  — Create a draft org (invisible until published)\n"
            "`/icc publish`  — Go live: post announcement, open claims, start timers\n"
            "`/icc status [org_id]`  — Full dashboard: owners, progress, status\n"
            "`/icc cancel [reason]`  — Cancel active or draft org"
        ),
        inline=False,
    )
    embed.add_field(
        name="Claiming  *(anyone)*",
        value=(
            "`/icc claim <category>`  — FCFS-claim an unclaimed category\n"
            "`/icc unclaim <category>`  — Release your claim (before any progress)\n"
            "`/icc progress [category]`  — Progress bars + missing channels\n"
            "`/icc mark_bought <channel>`  — Manually mark a channel as bought"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin overrides",
        value=(
            "`/icc assign <category> <user>`  — Force-assign a category\n"
            "`/icc drop <category>`  — Force category back to unclaimed\n"
            "`/icc admin mark_complete <category>`  — Force-complete a category\n"
            "`/icc admin reset_category <category>`  — Wipe progress and unclaim\n"
            "`/icc admin timer_status`  — View pending escalation/reminder timers\n"
            "`/icc admin cancel_timer <id>`  — Cancel a specific timer\n"
            "`/icc admin audit [user]`  — View ICC action log"
        ),
        inline=False,
    )
    embed.add_field(
        name="Setup & categories  *(admin only)*",
        value=(
            "`/icc setup admin_role <role>`  — Set ICC admin role\n"
            "`/icc setup organizer_role <role>`  — Set org creator role\n"
            "`/icc setup helper_role <category> <role>`  — Set escalation role per category\n"
            "`/icc category create/edit/delete/list/view`  — Manage categories\n"
            "`/icc category add_channels / remove_channels`  — Map channels to categories"
        ),
        inline=False,
    )
    embed.add_field(
        name="Auto-behaviour",
        value=(
            "When Operation Dex posts **🧪 Incense Activated!** in a mapped channel,\n"
            "the channel is automatically marked bought in the active org.\n"
            "Unclaimed categories escalate to the helper role after **5 min**.\n"
            "Buyers receive reminders every **5 min** until their category is complete."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid, "ICC"))
    return ("🧪", "Incense Control Center", embed)


def _section_changelog(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="📋  Changelog",
        description=(
            "`/changelog channel`  — See where changelogs are posted\n\n"
            "`/changelog setup #channel`  — Set the changelog channel *(admin)*\n\n"
            "`/changelog post version: changes:`  — Post a new changelog *(bot owner)*"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("📋", "Changelog", embed)


def _section_tips(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="💡  Tips & Tricks",
        description=(
            "• `/pokemon` Battle Card → weaknesses, key stats, strongest moves at a glance\n\n"
            "• `/pokemon_moves` type filter cycles through move types in one click\n\n"
            "• `/sprite shiny:True` shows the shiny variant with a toggle button\n\n"
            "• Checklists are **per-user** — your collection is private, share via friends\n\n"
            "• All slash commands support **autocomplete** — just start typing a name!"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("💡", "Tips & Tricks", embed)


# ── Permission check ────────────────────────────────────────────────────────


async def _can_see_incense(interaction: discord.Interaction) -> bool:
    """Check if the user should see incense commands in help."""
    if interaction.user.id == OWNER_ID:
        return True
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_id = await guild_settings_db.get_incense_role(str(interaction.guild_id))
    if role_id and any(r.id == role_id for r in getattr(interaction.user, "roles", [])):
        return True
    return False


def _is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id == OWNER_ID:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return perms.administrator if perms else False


# ── Navigation view ─────────────────────────────────────────────────────────


class _HelpSelect(discord.ui.Select):
    """Dropdown that switches between help sections."""

    def __init__(self, sections: list[tuple[str, str, discord.Embed]], bot_name: str, gid: str):
        self.sections = sections
        self.bot_name = bot_name
        self.gid = gid

        options = [
            discord.SelectOption(label="Home", emoji="📖", value="home", description="Overview of all sections"),
        ]
        for i, (emoji, title, _) in enumerate(sections):
            options.append(discord.SelectOption(label=title, emoji=emoji, value=str(i)))

        super().__init__(placeholder="Jump to a section...", options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "home":
            embed = _section_home(self.bot_name, self.sections, self.gid)
        else:
            _, _, embed = self.sections[int(value)]
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    """Persistent view with a section-select dropdown."""

    def __init__(
        self,
        sections: list[tuple[str, str, discord.Embed]],
        bot_name: str,
        gid: str,
        author_id: int,
    ):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.add_item(_HelpSelect(sections, bot_name, gid))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Use `/help` to open your own help menu.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ─────────────────────────────────────────────────────────────────────


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show all available commands.")
    async def help_cmd(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id or "")
        bot_name = get_bot_name(gid)
        is_qt = gid == "1477887017034584248"
        show_incense = is_qt and await _can_see_incense(interaction)
        admin = _is_admin(interaction)

        # Build the list of available sections for this user
        sections: list[tuple[str, str, discord.Embed]] = [
            _section_pokedex(gid),
            _section_types(gid),
            _section_checklist(gid),
            _section_friends(gid),
            _section_catch_tracker(gid),
            _section_duels(gid),
        ]
        if admin:
            sections.append(_section_starboard(gid))
            sections.append(_section_channels(gid))
        if show_incense:
            sections.append(_section_incense(gid, is_admin=admin))
        if admin:
            sections.append(_section_icc(gid))
        sections.append(_section_changelog(gid))
        sections.append(_section_tips(gid))

        home_embed = _section_home(bot_name, sections, gid)
        view = HelpView(sections, bot_name, gid, interaction.user.id)

        await interaction.response.send_message(embed=home_embed, view=view, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
