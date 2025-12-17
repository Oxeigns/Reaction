#!/usr/bin/env python3
"""Telegram reporting bot entrypoint.

This module initializes logging, validates configuration integrity, and wires the
Telegram application together. The previous monolithic implementation has been
split into focused modules under ``bot/`` for clarity and testability.
"""
from __future__ import annotations

import asyncio
import logging

# Ensure Pyrogram has an event loop at import time on Python 3.12+
asyncio.set_event_loop(asyncio.new_event_loop())

import config
from bot.app_builder import build_app, run_polling
from bot.dependencies import data_store, verify_author_integrity
from bot.logging_utils import build_logger
from bot.scheduler import SchedulerManager, log_heartbeat


async def main_async() -> None:
    """Entrypoint used by asyncio.run."""

    verify_author_integrity(config.AUTHOR_NAME, config.AUTHOR_HASH)
    build_logger()

    SchedulerManager.ensure_job("heartbeat", log_heartbeat, trigger="interval", seconds=300)

    app = build_app()

    try:
        await run_polling(app)
    finally:
        SchedulerManager.shutdown()
        await data_store.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
