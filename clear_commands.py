"""
clear_commands.py
─────────────────
One-time utility to wipe ALL global slash commands from Discord.
Run this ONCE if you see duplicate commands, then delete this file.

Usage:
    python clear_commands.py

After running, restart bot.py normally.
Global command changes can take up to 1 hour to propagate everywhere,
but setting DEV_GUILD_IDS in .env gives instant results in your test servers.
"""

import asyncio
import os

import discord
from dotenv import load_dotenv

load_dotenv()


async def clear():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌  DISCORD_TOKEN not set in .env")
        return

    client = discord.Client(intents=discord.Intents.none())
    tree   = discord.app_commands.CommandTree(client)

    async with client:
        await client.login(token)

        # Wipe global commands
        tree.clear_commands(guild=None)
        await tree.sync()
        print("✅  Global slash commands cleared.")

        # Wipe guild-specific commands if DEV_GUILD_IDS set
        raw = os.getenv("DEV_GUILD_IDS", "").strip()
        guild_ids = [g.strip() for g in raw.split(",") if g.strip()]
        for gid in guild_ids:
            guild = discord.Object(id=int(gid))
            tree.clear_commands(guild=guild)
            await tree.sync(guild=guild)
            print(f"✅  Commands cleared for guild {gid}.")

        print("\nDone. Now run:  python bot.py")
        await client.close()


asyncio.run(clear())
