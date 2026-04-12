"""
cogs/help.py  —  /help
Interactive help menu with dropdown navigation between categorised sections.

/help [section]
  • No argument  → opens the Home overview page with section listing
  • section arg  → autocomplete shows only sections the invoking user can see;
                   opens that section directly

Visibility rules:
  • Everyone          : Pokédex, Type Tools, Shiny Checklists, Checklist Friends,
                        Catch Tracker, Catch Duels, Changelog, Tips & Tricks
  • Incense Manager   : Mass Incense Manager  (QTs server only)
  • Server Admin      : + Starboard, Channel Management, Org, Pokémon Lists
"""

import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import guild_settings_db
from services.guild_settings_db import make_footer, get_bot_name

OWNER_ID  = int(os.getenv("OWNER_ID", "145065060568530944"))
_QT_GUILD = "1477887017034584248"


# ── Permission helpers ───────────────────────────────────────────────────────


async def _can_see_incense(interaction: discord.Interaction) -> bool:
    """True if the user holds the configured Incense Manager role or is an admin/owner."""
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
    """True if the user is a server admin or the bot owner."""
    if interaction.user.id == OWNER_ID:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


# ── Section builders ─────────────────────────────────────────────────────────
# Each returns (emoji, title, embed).  The emoji + title appear in the dropdown.


def _section_home(bot_name: str, sections: list[tuple[str, str]], gid: str) -> discord.Embed:
    listing = "\n".join(f"{emoji}  **{title}**" for emoji, title, _ in sections)
    embed = discord.Embed(
        title=f"📖  {bot_name} — Command Reference",
        description=(
            "Your all-in-one Pokémon companion for PvP, dex lookups and clan operations.\n"
            "All slash commands support **autocomplete** — just start typing!\n\n"
            "**Jump to a section using the dropdown below, or run `/help section:<name>`:**\n\n"
            f"{listing}"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return embed


def _section_pokedex(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="🔍  Pokédex Commands", colour=0x5865F2)
    embed.add_field(
        name="Pokémon lookup",
        value=(
            "`/pokemon <name>`  — Stats, types, abilities, sprite\n"
            "   ↳ Buttons: ⚔️ Battle Card  ·  📋 Moves\n\n"
            "`/pokemon_moves <name>`  — Full paginated move list\n"
            "   ↳ Filter by type  ·  🔍 Move details  ·  📢 Share\n\n"
            "`/sprite <name>`  — Full-size sprite, normal or shiny\n"
            "   ↳ Toggle button to switch between normal ↔ shiny"
        ),
        inline=False,
    )
    embed.add_field(
        name="Move, ability & item lookup",
        value=(
            "`/move <name>`  — Power, accuracy, PP, type, effect\n\n"
            "`/ability <name>`  — Effect description + Pokémon that have it\n\n"
            "`/item <name>`  — Held item or battle item effect"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🔍", "Pokédex", embed)


def _section_types(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="🏷️  Type Tools", colour=0x5865F2)
    embed.add_field(
        name="Commands",
        value=(
            "`/type <type>`  — Full offensive & defensive matchup chart for a type\n\n"
            "`/weakness <pokemon>`  — 4× / 2× / ½× / 0× breakdown by Pokémon name\n\n"
            "`/weakness type1:<type> type2:<type>`  — Custom dual-type matchup\n\n"
            "`/typechart`  — Single matchup lookup, full attacking row, or usage help"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🏷️", "Type Tools", embed)


def _section_checklist(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="✨  Shiny Hunt Checklists", colour=0x5865F2)
    embed.add_field(
        name="Viewing & sorting",
        value=(
            "`/checklist view`  — Your checklist with live catch/uncatch dropdowns\n"
            "   ↳ Sort buttons: A–Z  ·  Dex #  ·  Evo Group  ·  Remaining\n"
            "   ↳ Extra buttons: Switch list  ·  👥 Friends"
        ),
        inline=False,
    )
    embed.add_field(
        name="Managing your list",
        value=(
            "`/checklist add <pokemon>`  — Add Pokémon (bulk CSV supported)\n"
            "   ↳ `--evo charmander` expands to the full Charmander line\n"
            "   ↳ `include_evolutions:True` expands every entry to its full chain\n"
            "   ↳ Example: `/checklist add pokemon:charmander, bulbasaur`\n\n"
            "`/checklist remove <pokemon>`  — Remove Pokémon (bulk CSV + `--evo` supported)\n\n"
            "`/checklist catch <pokemon>`  — Mark as caught ✅ ✨\n\n"
            "`/checklist uncatch <pokemon>`  — Unmark a catch\n\n"
            "`/checklist remaining`  — Show only uncaught Pokémon\n\n"
            "`/event_setup <name>`  — Name the active event hunt (e.g. Community Day)"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("✨", "Shiny Hunt Checklists", embed)


def _section_friends(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="👥  Checklist Friends", colour=0x5865F2)
    embed.add_field(
        name="Commands",
        value=(
            "`/checklist friend add @user`  — Send a friend request\n\n"
            "`/checklist friend accept @user`  — Accept a pending request\n\n"
            "`/checklist friend decline @user`  — Decline a request\n\n"
            "`/checklist friend remove @user`  — Remove a friend\n\n"
            "`/checklist friend list`  — See your current friends\n\n"
            "`/checklist friend requests`  — View pending incoming requests\n\n"
            "   ↳ Or use the **👥 Friends** button directly on your checklist!"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("👥", "Checklist Friends", embed)


def _section_catch_tracker(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="🎯  Catch Tracker & Grind Sessions", colour=0x5865F2)
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
            "`!catchpause`  — Pause the timer (paused time excluded from duration)\n"
            "`!catchresume`  — Resume a paused session\n"
            "`!catchstatus`  — Live stats for the running session\n"
            "   ↳ Aliases: `!cstart` · `!cstop` · `!cpause` · `!cresume` · `!cstatus`"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚡ Multi-channel burst sessions  *(admin only)*",
        value=(
            "`!burststart [label]`  — Start a burst session across **all** registered burst channels\n"
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
            "   ↳ `channel:` — single  ·  `category:` — whole category (up to 3)\n"
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
            "`/catches stats [user]`  — Personal stats\n"
            "   ↳ Today · week · month · all-time · fastest · avg react · streak · unique Pokémon\n\n"
            "`/catches leaderboard [period]`  — Server leaderboard with speed bars\n"
            "   ↳ Periods: today / week / month / all\n\n"
            "`/catches today`  — Quick snapshot of today's catches for everyone\n\n"
            "`/catches fastest`  — All-time fastest individual catches in the server\n\n"
            "`/catches session <id>`  — Replay any past session by its ID"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Tips",
        value=(
            "• Stats build passively in **every** channel — no session needed\n"
            "• Burst sessions count catches across all registered channels simultaneously\n"
            "• `!catchstart` in a channel takes priority over an active burst session there\n"
            "• Session IDs are shown when a session ends — use `/catches session <id>` to revisit"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🎯", "Catch Tracker", embed)


def _section_duels(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="⚔️  Catch Duels", colour=0xFF6B35)
    embed.add_field(
        name="Starting a duel",
        value=(
            "`!duel @user`  — Free duel: catch for fun, no winner declared\n"
            "`!duel @user time 15m`  — Timed: most catches in X minutes wins\n"
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
            "`!duelforfeit`  — End your duel early (counted as a forfeit)\n"
            "   ↳ Aliases: `!duelstop` · `!duelend`\n\n"
            "Catches are tracked automatically across all registered burst channels.\n"
            "Timed duels end when the timer expires; race duels end when someone hits the target."
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


def _section_starboard(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="⭐  Starboard & Shiny Counter  *(admin only)*", colour=0x5865F2)
    embed.add_field(
        name="⚙️ Setup",
        value=(
            "`/starboard init <channel>`  — Set starboard channel & count existing shinies\n"
            "`/starboard format prefix: suffix:`  — Set channel name format (e.g. `✨42✨`)\n"
            "`/starboard announcechannel <channel>`  — Set weekly/monthly announcement channel\n"
            "`/starboard remove`  — Unlink the starboard channel\n"
            "`/starboard status`  — View current configuration"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Leaderboard & announcements",
        value=(
            "`/starboard leaderboard [period]`  — Interactive leaderboard (week / month / all)\n"
            "   ↳ Buttons: period switch · History · Hall of Fame · My Stats\n"
            "`/starboard post [period]`  — Preview & post an announcement on demand\n"
            "`/starboard count`  — View the current shiny count\n"
            "`/starboard setcount <count>`  — Manually override the shiny count"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔧 Maintenance",
        value="`/starboard resync`  — Re-read channel history & fix catch timestamps from message dates",
        inline=False,
    )
    embed.add_field(
        name="🤖 Auto-behaviour",
        value=(
            "When Operation Dex posts a shiny catch in the starboard channel, the count "
            "increments and the channel name updates automatically.\n"
            "Weekly (Monday) and monthly (1st) top-catcher announcements post to the configured "
            "announce channel with champion tracking & streaks."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("⭐", "Starboard & Shiny Counter", embed)


def _section_channels(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="🔧  Channel Management  *(admin only)*", colour=0x5865F2)
    embed.add_field(
        name="✨ Create",
        value=(
            "`/channel create count: prefix:`  — Bulk-create numbered text channels\n"
            "   ↳ `start_number:` — starting number (default: 1)\n"
            "   ↳ `category:` — create inside a specific category\n"
            "   ↳ `after:` — insert after a specific channel\n"
            "   ↳ Example: `/channel create count:20 prefix:♡- start_number:30 category:#hunts`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🗑️ Delete",
        value=(
            "`/channel delete channel:`  — Delete a single channel\n\n"
            "`/channel delete from_channel: to_channel:`  — Delete a consecutive range\n"
            "   ↳ Scoped to channels in the same category, ordered by position\n\n"
            "`/channel delete category:`  — Delete all channels in a category + the category itself\n"
            "   ↳ `category2:` `category3:` — delete up to **3 categories** at once\n\n"
            "All options can be **combined** in a single command.\n"
            "A confirmation embed is always shown before anything is deleted."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🔧", "Channel Management", embed)


def _section_incense(gid: str, is_admin: bool = False) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="🌿  Mass Incense Manager  *(QTs server only)*", colour=0x57F287)
    embed.add_field(
        name="⚡ Quick commands  (prefix)",
        value=(
            "`!pause` / `!p`  — Lock **all** active incense channels simultaneously\n"
            "`!pause <group>` / `!p <group>`  — Lock only channels in a named group\n"
            "`!resume` / `!r`  — Unlock **all** paused incense channels simultaneously\n"
            "`!resume <group>` / `!r <group>`  — Unlock only channels in a named group\n"
            "`!incset <id1> <id2> ...`  — Bulk register channels by ID"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Channel registration",
        value=(
            "`/incense add`  — Register channels as incense channels\n"
            "`/incense remove`  — Unregister channels\n"
            "`/incense recursive`  — Register every channel after this one (optional end channel)\n"
            "`/incense status`  — Paginated overview: ▶️ live · ⏸️ paused · 💤 idle\n"
            "`/incense clear [channel]`  — Clear a channel's active incense record\n\n"
            "All registration commands support: `channel:` · `category:` (up to 3) · `from_channel:` + `to_channel:`"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Groups  — target a subset with !p / !r",
        value=(
            "`/incense group create <name>`  — Create a group (max 3 per server, e.g. `clan` · `main`)\n"
            "`/incense group add <name>`  — Add channels to a group (single, category, or range)\n"
            "`/incense group remove <name>`  — Remove channels from a group\n"
            "`/incense group list`  — See all groups, their channels, and live/paused/idle state\n"
            "`/incense group delete <name>`  — Delete a group (channels stay registered)\n\n"
            "*Example: `!p clan` pauses only the channels in the `clan` group.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Manual lock / unlock",
        value=(
            "`/incense lock`  — Lock one or more channels\n"
            "`/incense unlock`  — Unlock one or more channels\n"
            "   ↳ Both support: `channel:` · `category:` (up to 3) · `from_channel:` + `to_channel:`\n"
            "   ↳ Defaults to the current channel if no selector is given\n\n"
            "`/incense lock-all [group]`  — Slash equivalent of `!pause [group]`\n"
            "`/incense unlock-all [group]`  — Slash equivalent of `!resume [group]`\n"
            "   ↳ Both support group autocomplete"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔍 Inspect & repair",
        value=(
            "`/incense channel-info [channel]`  — Full state for one channel:\n"
            "   ↳ Registration · lock state · active incense · group membership · recent log\n"
            "   ↳ **🔒 Lock** / **🔓 Unlock** buttons to act immediately\n\n"
            "`/incense resync`  — Scan history, fix missed locks, clean old bot messages\n"
            "   ↳ *Use when a pre-existing or 'cracked' incense wasn't auto-detected*"
        ),
        inline=False,
    )
    if is_admin:
        embed.add_field(
            name="⚙️ Setup  *(admin only)*",
            value=(
                "`/incense setup role <role>`  — Set the role that can manage incenses\n"
                "`/incense setup bot <id>`  — Set which bot ID to watch for incense activations\n"
                "`/incense setup view`  — View current configuration\n"
                "`/incense log [user] [channel]`  — View the full audit log"
            ),
            inline=False,
        )
    embed.add_field(
        name="🤖 Auto-behaviour",
        value=(
            "When Operation Dex activates an incense in a registered channel, the channel is "
            "**locked automatically** and a notification is posted.\n"
            "Run `!resume` (or `/incense unlock-all`) when all channels are ready to go live."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("🌿", "Mass Incense Manager", embed)


def _section_icc(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="🧪  Org  *(organizer/admin)*", colour=0x57F287)
    embed.add_field(
        name="Org lifecycle",
        value=(
            "`/org start [label]`  — Create a draft org with interactive control panel\n"
            "`/org publish`  — Go live: posts the org dashboard with claim buttons\n"
            "`/org status [org_id]`  — View interactive dashboard (draft or published)\n"
            "`/org cancel [reason]`  — Cancel an active or draft org\n"
            "`/org repost`  — Re-send the org dashboard if it was deleted\n"
            "`/org history [limit]`  — View past completed/cancelled orgs"
        ),
        inline=False,
    )
    embed.add_field(
        name="Interactive dashboard",
        value=(
            "The published org dashboard is a live-updating embed with buttons:\n"
            "• **Category buttons** — tap to claim (FCFS); tap again (red) to opt out\n"
            "• **📢 Ping** — pings the announce role (organizer only, one-time)\n"
            "• **Refresh** — manually refresh the panel\n"
            "• **Cancel Org** — cancel from the panel (organizer only)\n\n"
            "The panel auto-updates when channels are bought or a category completes."
        ),
        inline=False,
    )
    embed.add_field(
        name="Claiming  *(anyone)*",
        value=(
            "Tap a category button on the org dashboard to claim it.\n"
            "Tap the red button again to opt out (only before any progress is made).\n\n"
            "Slash fallbacks:\n"
            "`/org claim <category>`  ·  `/org unclaim <category>`\n"
            "`/org progress [category]`  — Progress bars + missing channels\n"
            "`/org mark_bought <channel>`  — Manually mark a channel as bought"
        ),
        inline=False,
    )
    embed.add_field(
        name="Reserves  *(buyers)*",
        value=(
            "`/org reserve pick <pokemon>`  — Reserve a Pokémon (FCFS across the whole org)\n"
            "   ↳ Autocomplete shows eligible Pokémon from normal + event lists\n"
            "   ↳ Reserving e.g. *Vivillon* covers **all** Vivillon forms automatically\n"
            "   ↳ Reserves **lock** when the org is published — no new picks after that\n"
            "`/org reserve release <pokemon>`  — Free your reserve\n"
            "`/org reserve list [user]`  — See reserves (admins can check any user)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Admin overrides",
        value=(
            "`/org assign <category> <user>`  — Force-assign a category\n"
            "`/org drop <category>`  — Force a category back to unclaimed\n"
            "`/org admin mark_complete <category>`  — Force-complete a category\n"
            "`/org admin reset_category <category>`  — Wipe progress and unclaim\n"
            "`/org admin timer_status`  — View pending escalation/reminder timers\n"
            "`/org admin cancel_timer <id>`  — Cancel a specific timer\n"
            "`/org admin audit [user]`  — View the Org action log"
        ),
        inline=False,
    )
    embed.add_field(
        name="Setup  *(admin only)*",
        value=(
            "`/org setup admin_role <role>`  — Set the Org admin role\n"
            "`/org setup organizer_role <role>`  — Set who can create orgs\n"
            "`/org setup helper_role <category> <role>`  — Set escalation role per category\n"
            "`/org setup announce_ping_role <role>`  — Set the 📢 ping role\n"
            "`/org category create <name> <count> [coins]`  — Create a category\n"
            "`/org category edit <category> [count] [coins] [reserve_slots]`  — Edit a category\n"
            "`/org category delete / list / view`  — Manage categories\n"
            "`/org category add_channels / remove_channels`  — Map channels to a category"
        ),
        inline=False,
    )
    embed.add_field(
        name="🤖 Auto-behaviour",
        value=(
            "When Op Dex posts **Incense Activated!** in a mapped channel, the channel is marked "
            "bought and the dashboard updates live.\n"
            "When Op Dex posts **A wild X appeared!**, the bot pings the reserver if matched.\n"
            "Steal alerts fire if someone else catches a reserved Pokémon.\n"
            "Unclaimed categories escalate to the helper role after **5 min**.\n"
            "Buyers get reminders every **5 min** until their category is complete."
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid, "Org"))
    return ("🧪", "Org", embed)


def _section_pokemon_lists(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="📋  Pokémon Lists  *(admin only)*",
        description=(
            "Manage the master Pokémon lists used by **Org** for reserve eligibility and spawn matching.\n"
            "Built-in categories: **rare · gmax · eevo · regional** — anything else is classed as **normal**.\n"
            "Adding a Pokémon automatically fetches **all its forms** from PokéAPI."
        ),
        colour=0xFEE75C,
    )
    embed.add_field(
        name="Category lists",
        value=(
            "`/pokemon_list add <category> <name>`  — Add a Pokémon + all forms to a list\n"
            "   ↳ e.g. `/pokemon_list add rare Mewtwo` also adds Mega Mewtwo X/Y\n"
            "`/pokemon_list remove <name>`  — Remove a Pokémon and all its forms from all lists\n"
            "`/pokemon_list view <category>`  — View all Pokémon in a category"
        ),
        inline=False,
    )
    embed.add_field(
        name="Events  — custom spawn strings with no form grouping",
        value=(
            "`/pokemon_list event create <name>`  — Create a named event (e.g. *Valentine's 2025*)\n"
            "`/pokemon_list event delete <name>`  — Delete an event and all its entries\n"
            "`/pokemon_list event add <event> <pokemon>`  — Add a custom string (e.g. *Valentine Pikachu*)\n"
            "`/pokemon_list event remove <event> <pokemon>`  — Remove a Pokémon from an event\n"
            "`/pokemon_list event view <event>`  — View all Pokémon in an event\n"
            "`/pokemon_list event list`  — List all events with status and count"
        ),
        inline=False,
    )
    embed.add_field(
        name="How lists affect Org reserves",
        value=(
            "• **rare / gmax / eevo / regional** → not reservable; used for spawn identification\n"
            "• **normal** → eligible for `/org reserve pick` (anything not in the above 4)\n"
            "• **event** Pokémon → also reservable; matched by exact name (no form grouping)\n"
            "• Spawn names not matched in **any** list → bot owner is DMed to flag it"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid, "Pokémon Lists"))
    return ("📋", "Pokémon Lists", embed)


def _section_changelog(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(
        title="📰  Changelog",
        description=(
            "`/changelog channel`  — See where changelogs are posted\n\n"
            "`/changelog setup #channel`  — Set the changelog channel *(admin)*\n\n"
            "`/changelog post version: changes:`  — Post a new changelog entry *(bot owner)*"
        ),
        colour=0x5865F2,
    )
    embed.set_footer(text=make_footer(gid))
    return ("📰", "Changelog", embed)


def _section_tips(gid: str) -> tuple[str, str, discord.Embed]:
    embed = discord.Embed(title="💡  Tips & Tricks", colour=0x5865F2)
    embed.add_field(
        name="Pokédex",
        value=(
            "• `/pokemon` Battle Card → weaknesses, key stats, strongest moves at a glance\n"
            "• `/pokemon_moves` type filter cycles through all move types in one click\n"
            "• `/sprite shiny:True` shows the shiny variant with a normal ↔ shiny toggle"
        ),
        inline=False,
    )
    embed.add_field(
        name="Checklists",
        value=(
            "• Checklists are **per-user** — your collection is private by default\n"
            "• Share progress with the **👥 Friends** system or the Friends button on your checklist\n"
            "• `--evo` in add/remove expands to the full evolution chain in one go"
        ),
        inline=False,
    )
    embed.add_field(
        name="General",
        value=(
            "• All slash commands support **autocomplete** — just start typing a name\n"
            "• `/help section:<name>` jumps straight to any section you have access to"
        ),
        inline=False,
    )
    embed.set_footer(text=make_footer(gid))
    return ("💡", "Tips & Tricks", embed)


# ── Navigation view ──────────────────────────────────────────────────────────


class _HelpSelect(discord.ui.Select):
    """Dropdown that switches between help sections."""

    def __init__(self, sections: list[tuple[str, str, discord.Embed]], bot_name: str, gid: str):
        self.sections = sections
        self.bot_name = bot_name
        self.gid      = gid

        options = [
            discord.SelectOption(
                label="Home",
                emoji="📖",
                value="home",
                description="Overview of all available sections",
            )
        ]
        for i, (emoji, title, _) in enumerate(sections):
            options.append(discord.SelectOption(label=title, emoji=emoji, value=str(i)))

        super().__init__(placeholder="Jump to a section…", options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "home":
            embed = _section_home(self.bot_name, self.sections, self.gid)
        else:
            _, _, embed = self.sections[int(value)]
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    """Persistent view holding the section-select dropdown."""

    def __init__(
        self,
        sections:  list[tuple[str, str, discord.Embed]],
        bot_name:  str,
        gid:       str,
        author_id: int,
    ):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.add_item(_HelpSelect(sections, bot_name, gid))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Run `/help` to open your own help menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Cog ──────────────────────────────────────────────────────────────────────


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _build_sections(
        self,
        gid:          str,
        admin:        bool,
        show_incense: bool,
    ) -> list[tuple[str, str, discord.Embed]]:
        """
        Build the ordered section list for the given user's permission level.
        Always-visible sections come first; gated sections are inserted based on flags.
        """
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
            sections.append(_section_pokemon_lists(gid))
        sections.append(_section_changelog(gid))
        sections.append(_section_tips(gid))
        return sections

    async def _section_autocomplete(
        self,
        interaction: discord.Interaction,
        current:     str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for /help section: — only shows sections the invoking user can see."""
        gid          = str(interaction.guild_id or "")
        is_qt        = gid == _QT_GUILD
        admin        = _is_admin(interaction)
        show_incense = is_qt and await _can_see_incense(interaction)
        sections     = await self._build_sections(gid, admin, show_incense)

        choices = [app_commands.Choice(name="📖 Home — overview of all sections", value="home")]
        for emoji, title, _ in sections:
            choices.append(app_commands.Choice(name=f"{emoji} {title}", value=title))

        if current:
            choices = [c for c in choices if current.lower() in c.name.lower()]
        return choices[:25]

    @app_commands.command(name="help", description="Show all available commands.")
    @app_commands.describe(section="Jump straight to a specific section (optional, supports autocomplete)")
    @app_commands.autocomplete(section=_section_autocomplete)
    async def help_cmd(self, interaction: discord.Interaction, section: Optional[str] = None):
        gid          = str(interaction.guild_id or "")
        bot_name     = get_bot_name(gid)
        is_qt        = gid == _QT_GUILD
        show_incense = is_qt and await _can_see_incense(interaction)
        admin        = _is_admin(interaction)

        sections = await self._build_sections(gid, admin, show_incense)
        view     = HelpView(sections, bot_name, gid, interaction.user.id)

        # Resolve which embed to show first
        if section:
            if section == "home":
                embed = _section_home(bot_name, sections, gid)
            else:
                match = next(
                    (e for _, title, e in sections if title.lower() == section.lower()),
                    None,
                )
                # Fall back to home if the typed value doesn't match (e.g. manual entry)
                embed = match if match is not None else _section_home(bot_name, sections, gid)
        else:
            embed = _section_home(bot_name, sections, gid)

        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
