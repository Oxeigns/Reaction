#!/usr/bin/env python3
"""Entry point for the MTProto Reporting Bot."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

# Yahan 'AuthKeyUnused' ko hatakar 'AuthKeyInvalid' kar diya gaya hai
from pyrogram.errors import ApiIdInvalid, AuthKeyInvalid, BadRequest

import config
from bot.dependencies import ensure_pyrogram_creds, verify_author_integrity
from handlers import register_handlers
from session_bot import create_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Main")

async def shutdown(app: Any, persistence: Any, tasks: list[asyncio.Task]):
    logger.info("Shutdown signal received. Cleaning up...")
    if app.is_connected:
        await app.stop()
    for task in tasks:
        task.cancel()
    await persistence.close()
    logger.info("Cleanup complete.")

async def start_bot() -> None:
    verify_author_integrity(config.AUTHOR_NAME, config.AUTHOR_HASH)
    ensure_pyrogram_creds()

    app, persistence, states, queue = create_bot()

    sudo_users = await persistence.get_sudo_users()
    config.SUDO_USERS = set(sudo_users)
    
    register_handlers(app, persistence, states, queue)

    try:
        await app.start()
    except (ApiIdInvalid, AuthKeyInvalid): # Fixed: Yahan bhi update kiya hai
        logger.critical("Invalid API credentials or Session String. Check your config.")
        return
    except BadRequest as exc:
        logger.error("Pyrogram failed to start: %s", exc)
        return

    logger.info("Bot is now online (ID: %s)", app.me.id)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    background_tasks = []

    def handle_exit():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_exit)

    try:
        await stop_event.wait()
    finally:
        await shutdown(app, persistence, background_tasks)

def main() -> None:
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
