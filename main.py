#!/usr/bin/env python3
"""Telegram reporting bot entrypoint.

This module initializes logging, validates configuration integrity, and wires the
Telegram application together. The previous monolithic implementation has been
split into focused modules under ``bot/`` for clarity and testability.
"""
from __future__ import annotations

import asyncio
import logging
import signal

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


async def main_async() -> None:
    """Entrypoint used by asyncio.run."""

    verify_author_integrity(config.AUTHOR_NAME, config.AUTHOR_HASH)
    build_logger()

    SchedulerManager.ensure_job("heartbeat", log_heartbeat, trigger="interval", seconds=300)

    app = build_app()
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    _setup_signal_handlers(loop, shutdown_event)

    try:
        await run_polling(app, shutdown_event)
    finally:
        SchedulerManager.shutdown()
        await data_store.close()


def main() -> None:
    # asyncio.run owns the single event loop for the process; avoid creating or
    # closing additional loops elsewhere to keep startup/shutdown predictable.
    asyncio.run(main_async())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
