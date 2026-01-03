from __future__ import annotations

"""Utilities for sending logs and errors to the configured logs group."""

import traceback

from pyrogram import Client
from pyrogram.errors import RPCError

from sudo import is_owner


async def send_log(client: Client, chat_id: int | None, text: str, *, parse_mode: str | None = None) -> None:
    """Send a log message safely."""

    if not chat_id:
        return
    try:
        await client.send_message(chat_id, text, parse_mode=parse_mode)
    except Exception:
        # Avoid crashing the bot on log errors
        pass


async def log_new_user(client: Client, logs_group: int | None, message) -> None:
    """Log when a non-owner user starts the bot."""

    if not logs_group or not message.from_user or is_owner(message.from_user.id):
        return
    text = (
        "📥 New user started bot\n"
        f"👤 User: [{message.from_user.first_name}](tg://user?id={message.from_user.id})\n"
        f"🆔 ID: {message.from_user.id}"
    )
    await send_log(client, logs_group, text, parse_mode="markdown")


async def log_report_summary(
    client: Client,
    logs_group: int | None,
    *,
    user,
    target: str,
    elapsed: float,
    success: bool,
) -> None:
    """Send a summary entry after a report completes."""

    status = "Success" if success else "❌ Failed"
    text = (
        "📄 Report Summary\n"
        f"👤 User: [{user.first_name}](tg://user?id={user.id})\n"
        f"🔗 Target: {target}\n"
        f"⏱ Time Taken: {int(elapsed)}s\n"
        f"✅ Status: {status}"
    )
    await send_log(client, logs_group, text, parse_mode="markdown")


async def log_error(client: Client, logs_group: int | None, exc: Exception, owner_id: int | None = None) -> None:
    """Send an error trace to the logs group, tagging the owner when known."""

    if not logs_group:
        return
    mention = f"[Owner](tg://user?id={owner_id})" if owner_id else "Owner"
    text = "⚠️ Bot Error\n" f"{mention}, attention needed.\n" f"``{traceback.format_exc()}``"
    try:
        await client.send_message(logs_group, text, parse_mode="markdown")
    except RPCError:
        pass

