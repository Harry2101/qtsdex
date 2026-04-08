"""
King's Dex — Main entry point.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qtsdex")

COGS = [
    # ── Pokédex ───────────────────────────────────────────────────────────────
    "cogs.pokemon",
    "cogs.moves",
    "cogs.move_lookup",
    "cogs.ability",
    "cogs.type_lookup",
    "cogs.weakness",
    "cogs.typechart",
    "cogs.item",
    "cogs.sprite",
    "cogs.events",
    # ── Utilities ─────────────────────────────────────────────────────────────
    "cogs.extract",
    # ── Incense Manager ───────────────────────────────────────────────────────
    "cogs.incense",
    "cogs.channels",
    "cogs.changelog",
    "cogs.help",
    # ── Starboard ─────────────────────────────────────────────────────────────
    "cogs.starboard",
    # ── Catch Tracker ─────────────────────────────────────────────────────────
    "cogs.catch_tracker",
    # ── Owner tools ───────────────────────────────────────────────────────────
    "cogs.inspect",
]


class QTsDex(commands.Bot):
    def __init__(self):
        # message_content intent required to read prefix command args
        # and to detect Operation Dex bot's messages
        intents                 = discord.Intents.default()
        intents.message_content = True
        intents.members         = True   # needed to resolve member objects for permission checks
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # ── Initialise databases ──────────────────────────────────────────────
        from services import events_db, incense_db, guild_settings_db, starboard_db, catch_db
        await events_db.init_db()
        await incense_db.init_db()
        await guild_settings_db.init_db()
        await starboard_db.init_db()
        await catch_db.init_db()
        log.info("✅ Databases initialised")

        # ── Load cogs ─────────────────────────────────────────────────────────
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"✅ Loaded {cog}")
            except Exception as e:
                log.error(f"❌ Failed to load {cog}: {e}")

        # ── Sync slash commands ───────────────────────────────────────────────
        raw       = os.getenv("DEV_GUILD_IDS", "").strip()
        guild_ids = [g.strip() for g in raw.split(",") if g.strip()]

        if not guild_ids:
            synced = await self.tree.sync()
            log.info(f"🌐 Synced {len(synced)} commands globally")
            return

        for gid in guild_ids:
            guild_obj = discord.Object(id=int(gid))
            try:
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                log.info(f"🔄 Synced {len(synced)} commands to guild {gid}")
            except discord.Forbidden:
                log.warning(
                    f"⚠️  Guild {gid} — SKIPPED (bot not in server or missing "
                    f"'applications.commands' scope)."
                )
            except Exception as e:
                log.error(f"❌ Failed to sync to guild {gid}: {e}")

    async def on_ready(self):
        log.info(f"\n{'='*52}")
        log.info(f"  🤖  King's Dex  •  {self.user}  (ID: {self.user.id})")
        log.info(f"{'='*52}")

        raw       = os.getenv("DEV_GUILD_IDS", "").strip()
        guild_ids = [g.strip() for g in raw.split(",") if g.strip()]

        log.info(f"  Connected to {len(self.guilds)} guild(s):\n")
        for g in sorted(self.guilds, key=lambda x: x.name):
            owner     = g.owner
            owner_str = f"{owner} (ID: {owner.id})" if owner else "Unknown"
            in_env    = str(g.id) in guild_ids
            tag       = "✅ syncing" if in_env else "⚪ no sync"
            log.info(f"  {tag}  |  {g.name!r}  (ID: {g.id})")
            log.info(f"           Owner: {owner_str}  |  Members: {g.member_count}")

        log.info(f"{'='*52}\n")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Pokémon battles | /pokemon  •  !pause / !resume",
            )
        )

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        log.error(f"Command error in {interaction.command}: {error}")
        msg = "Something went wrong. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not set in .env")
    bot = QTsDex()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())