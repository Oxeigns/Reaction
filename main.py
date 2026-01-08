#!/usr/bin/env python3
"""Entry point for the MTProto Reporting Bot."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from pyrogram.errors import ApiIdInvalid, AuthKeyUnused, BadRequest

import config
from bot.dependencies import ensure_pyrogram_creds, verify_author_integrity
from handlers import register_handlers
from session_bot import create_bot

# Configure logging to be more descriptive for MTProto events
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Main")

async def shutdown(app: Any, persistence: Any, tasks: list[asyncio.Task]):
    """Gracefully stops the bot and cleans up all background workers."""
    logger.info("Shutdown signal received. Cleaning up...")
    
    # Stop the Pyrogram client first to stop receiving updates
    if app.is_connected:
        await app.stop()
        
    # Cancel all running background tasks (like report queues)
    for task in tasks:
        task.cancel()
    
    # Ensure the database connection is closed safely
    await persistence.close()
    logger.info("Cleanup complete. Goodbye.")

async def start_bot() -> None:
    # 1. Integrity and Credential Guard
    verify_author_integrity(config.AUTHOR_NAME, config.AUTHOR_HASH)
    ensure_pyrogram_creds()

    # 2. Initialize the Core Components
    # Expecting app (Pyrogram Client), persistence (DataStore), states, and queue
    app, persistence, states, queue = create_bot()

    # Sync SUDO users from database to memory config
    sudo_users = await persistence.get_sudo_users()
    config.SUDO_USERS = set(sudo_users)
    
    # 3. Register Conversation and Command Handlers
    register_handlers(app, persistence, states, queue)

    # 4. Start the MTProto Client
    try:
        await app.start()
    except (ApiIdInvalid, AuthKeyUnused):
        logger.critical("Invalid API credentials or Session String. Please check your config.")
        return
    except BadRequest as exc:
        logger.error("Pyrogram failed to start: %s", exc)
        return

    logger.info("Bot is now online (ID: %s)", app.me.id)

    # 5. Signal Handling for Linux/Docker Environments
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    # Background tasks list for cleanup tracking
    background_tasks = []

    def handle_exit():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_exit)

    # 6. Execution Loop
    # We wait for the stop_event (Ctrl+C or Kill) without blocking the loop
    try:
        await stop_event.wait()
    finally:
        await shutdown(app, persistence, background_tasks)

def main() -> None:
    """Main entry point with asyncio execution."""
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception("Fatal error during runtime: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
