#!/usr/bin/env python3
from __future__ import annotations

"""Minimal Telegram bot entrypoint for concurrent MTProto reporting."""

import asyncio
import logging
import time
from typing import Dict

from pyrogram import Client, filters
from pyrogram.types import Message

import config
from reporter import report_user
from session_manager import build_clients, scan_session_files

BOT_NAME = "reaction_reporter_bot"
MAX_REPORTS = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class BotState:
    def __init__(self) -> None:
        self.awaiting_username: Dict[int, bool] = {}

    def set_waiting(self, user_id: int, waiting: bool) -> None:
        self.awaiting_username[user_id] = waiting

    def is_waiting(self, user_id: int) -> bool:
        return self.awaiting_username.get(user_id, False)


state = BotState()


def is_authorized(message: Message) -> bool:
    if not config.ADMIN_IDS:
        return True
    return message.from_user and message.from_user.id in config.ADMIN_IDS


app = Client(
    BOT_NAME,
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    in_memory=True,
)


@app.on_message(filters.command("start"))
async def start_handler(_: Client, message: Message) -> None:
    if not is_authorized(message):
        await message.reply_text("🚫 You are not allowed to run reports.")
        return

    state.set_waiting(message.from_user.id, True)
    await message.reply_text(
        "Send the @username or numeric ID of the target account to start reporting.\n"
        "Sessions will be loaded from the sessions/ folder and run concurrently."
    )


@app.on_message(filters.text & filters.private)
async def username_handler(_: Client, message: Message) -> None:
    if not is_authorized(message) or not state.is_waiting(message.from_user.id):
        return

    username = message.text.strip()
    session_files = scan_session_files()
    if not session_files:
        await message.reply_text("No .session files found in the sessions/ folder.")
        state.set_waiting(message.from_user.id, False)
        return

    clients = build_clients(session_files)
    await message.reply_text(
        f"🚀 Starting reports for {username} using {len(clients)} sessions."
    )

    start_time = time.perf_counter()
    stats = await report_user(username, clients, max_reports=MAX_REPORTS)
    duration = time.perf_counter() - start_time

    await message.reply_text(
        "✅ Reporting complete\n"
        f"Target: {username}\n"
        f"Sessions used: {len(clients)}\n"
        f"Attempts: {stats['attempts']} / {MAX_REPORTS}\n"
        f"Success: {stats['success']} | Failed: {stats['failed']}\n"
        f"Duration: {duration:.2f}s"
    )
    state.set_waiting(message.from_user.id, False)


async def main() -> None:
    await app.start()
    logging.info("Bot started. Waiting for commands...")
    await idle()
    await app.stop()


async def idle() -> None:
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
