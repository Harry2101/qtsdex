# 🎮 King's Dex

A production-grade Discord bot for Pokémon PvP assistance and clan management — built for the QTs clan.

Includes a full **Pokédex lookup system** and a **Mass Incense Management system** for coordinated clan hunts across 50–200 channels.

---

## ✨ Feature Overview

### 📖 Pokédex Commands
| Command | Description |
|---|---|
| `/pokemon <name>` | Stats, types, abilities, sprite — Info + Battle Card views |
| `/pokemon_moves <name>` | Paginated move list with type filter, TM numbers, BP/Acc/PP |
| `/move <name>` | Full move details — power, accuracy, PP, effect, priority |
| `/ability <name>` | Ability effect + which Pokémon have it |
| `/type <name>` | Type's offensive & defensive matchup chart |
| `/weakness [pokemon/type]` | 4×/2×/½×/0× breakdown |
| `/typechart [attack] [defend]` | Type chart lookup |
| `/item <name>` | Held item / battle item effect |
| `/sprite <name>` | Full-size sprite, normal or shiny |
| `/settings` | Personal stat display style |

### 🌿 Incense Manager Commands
| Command | Who | Description |
|---|---|---|
| `!pause` | Organizer | Pause all active incenses (lock all channels) |
| `!resume` | Organizer | Resume all paused incenses (unlock all channels) |
| `!incset ID1 ID2...` | Organizer | Bulk register incense channels by ID |
| `/inc_add #channel` | Organizer | Register a single channel as incense channel |
| `/inc_remove #channel` | Organizer | Remove a channel from incense list |
| `/inc_lock [#channel]` | Organizer | Lock a specific incense channel |
| `/inc_unlock [#channel]` | Organizer | Unlock a specific incense channel |
| `/inc_set_recursive` | Organizer | Register every channel after this one as an inc channel |
| `/inc_status` | Organizer | View all channels: live / paused / idle |
| `/inc_clear [#channel]` | Organizer | Clear incense record after it expires |

### ✨ Shiny Hunt Checklists
| Command | Description |
|---|---|
| `/checklist view` | Your shiny hunt checklist (Normal or Event) |
| `/checklist add` | Add Pokémon — bulk CSV, `--evo` chains, optional full evo chain |
| `/checklist catch` | Mark a Pokémon as caught |
| `/checklist uncatch` | Unmark a catch |
| `/checklist remaining` | Show only uncaught Pokémon |
| `/checklist remove` | Remove a Pokémon from the list |
| `/event_setup <name>` | Set the active event name |

---

## 🌿 Incense System — How It Works

### Auto-lock
When **Operation Dex bot** (ID: `1471263987340410978`) sends an "Incense Activated!" message in a **registered incense channel**, King's Dex automatically:
1. Records the incense (type, spawn count) in the database
2. Denies `Send Messages` for Operation Dex in that channel — locking it
3. Posts a notification in the channel

### Mass Pause / Resume
Once all channels have locked their incenses, the organizer runs `!resume` to unlock all channels simultaneously, starting the mass hunt.

- `!pause` → locks all channels with active incenses
- `!resume` → unlocks all channels with active incenses

### Locking Mechanism
"Locking" a channel = denying `Send Messages` for Operation Dex bot via a channel permission override. The channel remains visible and accessible to members — only the spawn bot is blocked.

### Who Can Use Incense Commands
- Role ID `1483594894713946212` (Organizer role) — full access

---

## 🚀 Setup Guide

### Prerequisites
- Python 3.11+
- Git
- VS Code (recommended)

### Step 1 — Create Discord Bot
1. Go to https://discord.com/developers/applications
2. **New Application** → name it → **Create**
3. **Bot** → **Reset Token** → copy and save it
4. Enable **Privileged Intents**:
   - ✅ **Server Members Intent** — needed for permission resolution
   - ✅ **Message Content Intent** — needed to read Operation Dex messages
5. **OAuth2 → URL Generator**:
   - Scopes: ✅ `bot` + ✅ `applications.commands`
   - Bot Permissions: ✅ `Send Messages` + ✅ `Embed Links` + ✅ `Read Message History` + ✅ `Manage Roles` + ✅ `Manage Channels`
   - Copy the invite URL

> ⚠️ **Message Content Intent is required** for the incense auto-lock to work. Enable it in the Developer Portal under Bot → Privileged Gateway Intents.

### Step 2 — Clone / Download

```bash
git clone https://github.com/YOUR_USERNAME/qtsdex.git
cd qtsdex
code .
```

### Step 3 — Virtual Environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows
# or: source venv/bin/activate  (Mac/Linux)

pip install -r requirements.txt
```

### Step 4 — Configure .env

```bash
copy .env.example .env
```

Fill in your `.env`:

```env
DISCORD_TOKEN=your_bot_token_here

# Comma-separated guild IDs for instant slash command sync
DEV_GUILD_IDS=your_main_guild_id,your_second_guild_id

LOG_LEVEL=INFO
```

### Step 5 — Clear old commands (first time or after changes)

```bash
python clear_commands.py
```

### Step 6 — Run

```bash
python bot.py
```

Expected startup output:
```
✅ Databases initialised
✅ Loaded cogs.pokemon
✅ Loaded cogs.incense
...
🔄 Synced 22 commands to guild ...
🤖 King's Dex ready
```

### Step 7 — Invite & Configure

1. Paste the OAuth2 URL into your browser and invite to your clan server
2. Register your incense channels:
   ```
   !incset 111111111 222222222 333333333 ...
   ```
   Or use `/inc_set_recursive` in the channel just before your first incense channel

---

## ⚙️ Incense Setup Workflow (First Time)

```
1. Invite bot to your clan server
2. Give bot: Send Messages, Embed Links, Manage Channels permissions
3. Run: !incset [all 50 channel IDs]
   — or use /inc_set_recursive in the channel before your first inc channel
4. Done — the bot now watches all registered channels automatically
```

### During a Mass Incense

```
Members buy their incenses and activate them in their channels
  → Bot auto-locks each channel as it activates
  → Bot posts "Incense Auto-Paused" notification in each channel

Organizer checks: /inc_status
  → See how many are locked vs still waiting

When all are ready:
  → Organizer runs: !resume
  → All channels unlock simultaneously
  → Each channel gets "Incense Resumed! 🎉" notification
  → Hunt begins!

After hunt ends:
  → Run: /inc_clear in each channel (or they'll be cleared on next activation)
```

---

## 📁 Project Structure

```
qtsdex/
├── bot.py                    # Entry point
├── clear_commands.py         # One-time command wipe utility
├── requirements.txt
├── .env.example
│
├── cogs/
│   ├── incense.py            # 🌿 Incense Manager (all incense commands)
│   ├── pokemon.py            # /pokemon
│   ├── moves.py              # /pokemon_moves
│   ├── move_lookup.py        # /move
│   ├── ability.py            # /ability
│   ├── type_lookup.py        # /type
│   ├── weakness.py           # /weakness
│   ├── typechart.py          # /typechart
│   ├── item.py               # /item
│   ├── sprite.py             # /sprite
│   ├── settings.py           # /settings
│   └── events.py             # /checklist
│
├── services/
│   ├── incense_db.py         # 🌿 SQLite for incense channels + state
│   └── events_db.py          # SQLite for shiny hunt checklists
│
└── utils/
    ├── pokeapi.py            # Async PokéAPI client (24h TTL cache)
    ├── autocomplete.py       # All autocomplete handlers
    ├── embeds.py             # Embed helpers, colours, stat rendering
    ├── normalizer.py         # "Mr Mime" → "mr-mime"
    ├── type_chart.py         # Gen 6+ type effectiveness (baked in)
    └── user_prefs.py         # Per-user settings
```

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from Discord Developer Portal |
| `DEV_GUILD_IDS` | ❌ | Comma-separated guild IDs for instant sync |
| `LOG_LEVEL` | ❌ | `DEBUG` / `INFO` / `WARNING` (default: `INFO`) |

---

## 🔐 Permissions Required

The bot needs these Discord permissions:
- `Send Messages` — to post notifications
- `Embed Links` — to format embeds
- `Read Message History` — to detect Operation Dex messages
- `Manage Channels` — to set permission overrides (lock/unlock channels)

In the Developer Portal, enable:
- **Server Members Intent**
- **Message Content Intent**

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| Auto-lock not working | Enable Message Content Intent in Dev Portal → Bot → Privileged Intents |
| `!pause` / `!resume` not working | Check user has Organizer role or is the owner |
| Channels not locking | Bot needs Manage Channels permission in those channels |
| Duplicate slash commands | Run `python clear_commands.py` once |
| Commands not appearing | Set `DEV_GUILD_IDS` for instant sync, or wait ~1h for global |
| `DISCORD_TOKEN not set` | Check `.env` exists with correct token |

---

## 📜 Credits

- [PokéAPI](https://pokeapi.co/) — Pokémon data
- [discord.py](https://discordpy.readthedocs.io/) — Python Discord library
- [aiosqlite](https://aiosqlite.omnilib.dev/) — Async SQLite
- Pokémon © Nintendo / Game Freak / The Pokémon Company
