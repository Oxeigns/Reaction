from __future__ import annotations

"""Utilities for sending logs and errors to the configured logs group."""

import traceback
from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.types import Message, User


async def send_log(client: Client, chat_id: int | None, text: str, *, parse_mode: str = "markdown") -> None:
    """Send a log message safely without crashing the main flow."""
    if not chat_id:
        return
    try:
        await client.send_message(chat_id, text, parse_mode=parse_mode)
    except Exception:
        pass


async def log_user_start(client: Client, logs_group: int | None, message: Message) -> None:
    """Log whenever any user starts the bot."""
    if not logs_group or not message.from_user:
        return
    
    user = message.from_user
    username = f" (@{user.username})" if user.username else ""
    
    text = (
        "📥 **New User Interaction**\n"
        f"👤 **Name:** {user.first_name}\n"
        f"🆔 **ID:** `{user.id}`{username}"
    )
    await send_log(client, logs_group, text)


async def log_report_summary(
    client: Client,
    logs_group: int | None,
    user: User,  # Removed the '*' to allow positional args from handlers.py
    target: str,
    elapsed: float,
    success: bool,
) -> None:
    """Send a summary entry after a report completes."""
    if not logs_group:
        return

    username = f" (@{user.username})" if user.username else ""
    duration = round(elapsed, 1)
    status_label = "Success" if success else "Failed"
    status_prefix = "✅" if success else "❌"
    
    text = (
        f"{status_prefix} **Report Job Finished**\n"
        f"👤 **Executor:** {user.first_name}{username}\n"
        f"🔗 **Target:** `{target}`\n"
        f"⏱ **Duration:** `{duration}s`\n"
        f"📊 **Status:** {status_label}"
    )
    await send_log(client, logs_group, text)


async def log_error(client: Client, logs_group: int | None, exc: Exception, owner_id: int | None = None) -> None:
    """Send an error trace to the logs group, tagging the owner."""
    if not logs_group:
        return
        
    mention = f"[Owner](tg://user?id={owner_id})" if owner_id else "Administrator"
    # Use HTML or Markdown code blocks to prevent long traces from formatting weirdly
    trace = traceback.format_exc()
    if len(trace) > 3000:  # Telegram message limit safety
        trace = trace[-3000:] + "\n... (trace truncated)"
        
    text = (
        "⚠️ **Internal Bot Error**\n"
        f"👤 **Notify:** {mention}\n\n"
        f"```python\n{trace}```"
    )
    
    try:
        await client.send_message(logs_group, text, parse_mode="markdown")
    except Exception:
        pass
