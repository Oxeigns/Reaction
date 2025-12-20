#!/usr/bin/env python3
"""Telegram reporting bot entrypoint.

This module initializes logging, validates configuration integrity, and wires the
Telegram application together. The previous monolithic implementation has been
split into focused modules under ``bot/`` for clarity and testability.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import config
from bot.app_builder import build_app, run_polling
from bot.dependencies import data_store, verify_author_integrity
from bot.logging_utils import build_logger
from bot.scheduler import SchedulerManager, log_heartbeat


def _setup_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    """Register SIGTERM/SIGINT handlers to trigger graceful shutdown."""

    def _signal_handler(signame: str) -> None:
        logging.info("Received %s; shutting down gracefully.", signame)
        shutdown_event.set()

    for signame in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, signame), lambda s=signame: _signal_handler(s))
        except NotImplementedError:
            # add_signal_handler isn't available on Windows event loops.
            signal.signal(getattr(signal, signame), lambda *_: shutdown_event.set())


def _restart_process() -> None:
    logging.info("Restart requested; re-executing process with same args.")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def main_async() -> None:
    """Entrypoint used by asyncio.run."""

    verify_author_integrity(config.AUTHOR_NAME, config.AUTHOR_HASH)
    build_logger()

    if data_store.is_persistent:
        logging.info("MongoDB persistence enabled; sessions and reports will survive dyno restarts.")
    else:
        logging.info(
            "Using in-memory storage; set the MONGO_URI environment variable to persist sessions and reports.")

    app = build_app()
    shutdown_event = asyncio.Event()

    app.bot_data["shutdown_event"] = shutdown_event
    app.bot_data.setdefault("restart_requested", False)

    loop = asyncio.get_running_loop()
    SchedulerManager.set_event_loop(loop)
    SchedulerManager.ensure_job("heartbeat", log_heartbeat, trigger="interval", seconds=300)
    _setup_signal_handlers(loop, shutdown_event)

    try:
        await run_polling(app, shutdown_event)
    finally:
        SchedulerManager.shutdown()
        await data_store.close()

    if app.bot_data.get("restart_requested"):
        _restart_process()


def main() -> None:
    # asyncio.run owns the single event loop for the process; avoid creating or
    # closing additional loops elsewhere to keep startup/shutdown predictable.
    asyncio.run(main_async())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
