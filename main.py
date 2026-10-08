"""Entry point: python main.py"""
import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler

import updater

UPDATE = updater.update()  # before importing the bot, so a fresh download is what runs

import discord  # noqa: E402

from bot.cogs.health import ERRORS  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.core import TradingBot  # noqa: E402


class IntentHint(logging.Filter):
    """discord.py can hide Discord's 4014 close behind a 1006 reconnect loop; say plainly what to fix."""

    def filter(self, record: logging.LogRecord) -> bool:
        if "Disallowed intent" in record.getMessage() or "session has been invalidated" in record.getMessage():
            logging.getLogger("bot").error(
                "Discord may be refusing the bot's intents. Developer Portal > your app > Bot > turn ON "
                "Server Members Intent and Message Content Intent > Save Changes, then restart."
            )
        return True


def setup_logs(log_dir: str) -> None:
    # Console output goes to the systemd journal (journalctl -u tradingbot).
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    discord.utils.setup_logging(level=level)
    root = logging.getLogger()
    os.makedirs(log_dir, exist_ok=True)
    # Plus a rotating file (5 files x 5 MB max) that /logs can read from Discord.
    fh = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    root.addHandler(ERRORS)
    logging.getLogger("discord.gateway").addFilter(IntentHint())


async def run(config: Config) -> None:
    bot = TradingBot(config)
    loop = asyncio.get_running_loop()
    # systemd sends SIGTERM on stop/reboot: close cleanly so the next start isn't reported as a crash.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.close()))
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    async with bot:
        # discord.py handles rate limits (429s) and reconnects automatically.
        await bot.start(config.token)


def main() -> None:
    config = Config.from_env()
    setup_logs(config.log_dir)
    logging.getLogger("updater").info("Self-update: %s", UPDATE)
    asyncio.run(run(config))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
