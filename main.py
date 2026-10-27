import asyncio
import logging
import os

import discord
from discord.ext import commands

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

COGS = [
    "cogs.server-copy",
]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded: %s", cog)
            except Exception:
                log.exception("Failed to load: %s", cog)

        synced = await self.tree.sync()
        log.info("Synced %d commands", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")

    async with MyBot() as bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())