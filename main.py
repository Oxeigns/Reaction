#!/usr/bin/env python3
"""Telegram reporting bot that coordinates multiple Pyrogram sessions."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Iterable, List
from urllib.parse import urlparse

from pyrogram import Client
from pyrogram.errors import BadRequest, FloodWait, RPCError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from report import report_profile_photo
from storage import DataStore

# Conversation states
REPORT_URL, REPORT_REASONS, REPORT_COUNT, REPORT_SESSIONS = range(4)
ADD_SESSIONS = 10

DEFAULT_REPORTS = 5000
MIN_SESSIONS = 5
MAX_SESSIONS = 500

data_store = DataStore(config.MONGO_URI)


def build_logger() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def ensure_token() -> str:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. Set it in the environment or config.py")
    return config.BOT_TOKEN


def ensure_pyrogram_creds() -> None:
    if not (config.API_ID and config.API_HASH):
        raise RuntimeError("API_ID and API_HASH are required for Pyrogram sessions")


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Start report", callback_data="action:start")],
            [InlineKeyboardButton("Add sessions", callback_data="action:add")],
        ]
    )


def friendly_error(message: str) -> str:
    return f"⚠️ {message}\nUse the menu below or try again."


def parse_reasons(text: str) -> List[str]:
    reasons = [line.strip() for line in text.replace(";", "\n").splitlines() if line.strip()]
    return reasons[:5]


def is_valid_link(text: str) -> bool:
    text = text.strip()
    return text.startswith("https://t.me/") or text.startswith("t.me/") or text.startswith("@")


def extract_target_identifier(text: str) -> str:
    text = text.strip()
    if text.startswith("@"):  # username
        return text[1:]

    parsed = urlparse(text if text.startswith("http") else f"https://{text}")
    path = parsed.path.lstrip("/")
    return path.split("/", maxsplit=1)[0]


def session_strings_from_text(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def send_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not config.LOG_CHAT_ID:
        logging.info("Skipping log send; LOG_CHAT_ID not configured: %s", text)
        return
    asyncio.create_task(
        context.bot.send_message(chat_id=config.LOG_CHAT_ID, text=text, disable_notification=True)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    greeting = (
        "👋 Welcome! I coordinate Pyrogram sessions to report illegal content.\n"
        "• Share a Telegram link and up to 5 reasons.\n"
        "• Minimum sessions: 5. Maximum: 500.\n"
        "• Default report count: 5000 requests.\n"
        "Use /report to begin or buttons below."
    )
    await update.effective_message.reply_text(greeting, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "ℹ️ *How to use the reporter*\n"
        "1) Run /report or tap Start report.\n"
        "2) Send the profile, group, or channel link.\n"
        "3) List up to 5 reasons (one per line).\n"
        "4) Accept 5000 reports or enter a custom count.\n"
        "5) Paste 5-500 Pyrogram session strings or type 'use saved'.\n"
        "I will track successes, failures, and times, then store the run in MongoDB."
    )
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def handle_action_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "action:start":
        return await start_report(update, context)
    if query.data == "action:add":
        await query.edit_message_text("Send 5-500 Pyrogram session strings, one per line.")
        return ADD_SESSIONS
    return ConversationHandler.END


async def start_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Send the Telegram link you want to report (https://t.me/... or @username)."
    )
    return REPORT_URL


async def handle_report_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not is_valid_link(text):
        await update.effective_message.reply_text(
            friendly_error("Invalid link. Use https://t.me/... or @username."),
            reply_markup=main_menu_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["target"] = text
    await update.effective_message.reply_text(
        "List up to 5 reasons for reporting (one per line or separated by semicolons)."
    )
    return REPORT_REASONS


async def handle_reasons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reasons = parse_reasons(update.message.text or "")
    if not reasons:
        await update.effective_message.reply_text(friendly_error("Please provide at least one reason."))
        return REPORT_REASONS

    context.user_data["reasons"] = reasons
    await update.effective_message.reply_text(
        f"How many report requests? Send a number or type 'default' for {DEFAULT_REPORTS}."
    )
    return REPORT_COUNT


async def handle_report_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().lower()
    if text in {"", "default"}:
        count = DEFAULT_REPORTS
    else:
        if not text.isdigit() or int(text) <= 0:
            await update.effective_message.reply_text(friendly_error("Please send a positive number."))
            return REPORT_COUNT
        count = int(text)

    context.user_data["count"] = count
    await update.effective_message.reply_text(
        (
            "Paste 5-500 Pyrogram session strings (one per line).\n"
            "Type 'use saved' to run with all stored sessions."
        )
    )
    return REPORT_SESSIONS


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    sessions: list[str] = []

    if text.lower() == "use saved":
        sessions = await data_store.get_sessions()
        if len(sessions) < MIN_SESSIONS:
            await update.effective_message.reply_text(
                friendly_error(
                    f"Not enough saved sessions. Add at least {MIN_SESSIONS} with /addsessions or paste them now."
                )
            )
            return REPORT_SESSIONS
    else:
        sessions = session_strings_from_text(text)
        if not (MIN_SESSIONS <= len(sessions) <= MAX_SESSIONS):
            await update.effective_message.reply_text(
                friendly_error(
                    f"Provide between {MIN_SESSIONS} and {MAX_SESSIONS} session strings (one per line)."
                )
            )
            return REPORT_SESSIONS
        added = await data_store.add_sessions(
            sessions, added_by=update.effective_user.id if update.effective_user else None
        )
        await update.effective_message.reply_text(
            f"Stored {len(added)} new session(s). {len(sessions)} will be used for this run."
        )

    context.user_data["sessions"] = sessions

    summary = (
        f"Target: {context.user_data.get('target')}\n"
        f"Reasons: {', '.join(context.user_data.get('reasons', []))}\n"
        f"Total reports: {context.user_data.get('count')}\n"
        f"Session count: {len(sessions)}"
    )

    await update.effective_message.reply_text(
        f"Confirm the report?\n\n{summary}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Start", callback_data="confirm:start")],
                [InlineKeyboardButton("Cancel", callback_data="confirm:cancel")],
            ]
        ),
    )
    return ConversationHandler.WAITING


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm:cancel":
        await query.edit_message_text("Canceled. Use /report to start over.")
        return ConversationHandler.END

    await query.edit_message_text("Reporting has started. I'll send updates when done.")
    asyncio.create_task(run_report_job(query, context))
    return ConversationHandler.END


async def run_report_job(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = query.from_user
    chat_id = query.message.chat_id

    target = context.user_data.get("target")
    reasons = context.user_data.get("reasons", [])
    count = context.user_data.get("count", DEFAULT_REPORTS)
    sessions = context.user_data.get("sessions", [])

    started = datetime.now(timezone.utc)
    await context.bot.send_message(chat_id=chat_id, text="Preparing clients...")

    summary = await perform_reporting(target, reasons, count, sessions)
    ended = datetime.now(timezone.utc)

    summary_text = (
        "✅ Reporting finished.\n"
        f"Target: {target}\n"
        f"Reasons: {', '.join(reasons)}\n"
        f"Requested: {count}\n"
        f"Sessions used: {len(sessions)}\n"
        f"Success: {summary['success']} | Failed: {summary['failed']}\n"
        f"Started: {started.isoformat()}\n"
        f"Ended: {ended.isoformat()}"
    )

    await context.bot.send_message(chat_id=chat_id, text=summary_text)

    await data_store.record_report(
        {
            "user_id": user.id if user else None,
            "target": target,
            "reasons": reasons,
            "requested": count,
            "sessions": len(sessions),
            "success": summary["success"],
            "failed": summary["failed"],
            "started_at": started,
            "ended_at": ended,
        }
    )

    send_log(
        context,
        (
            f"Report run finished: user_id={user.id if user else 'unknown'} | target={target} | "
            f"success={summary['success']} | failed={summary['failed']}"
        ),
    )


async def resolve_chat_id(client: Client, target: str):
    identifier = extract_target_identifier(target)
    chat = await client.get_chat(identifier)
    return chat.id


async def perform_reporting(target: str, reasons: Iterable[str], total: int, sessions: list[str]) -> dict:
    ensure_pyrogram_creds()

    clients = [
        Client(
            name=f"reporter_{idx}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session,
            workdir=f"/tmp/report_session_{idx}",
        )
        for idx, session in enumerate(sessions)
    ]

    reason_text = "; ".join(reasons)[:512] or "No reason provided"

    try:
        for client in clients:
            await client.start()

        chat_id = await resolve_chat_id(clients[0], target)

        success = 0
        failed = 0

        async def report_once(client: Client) -> bool:
            try:
                return await report_profile_photo(client, chat_id, reason=5, reason_text=reason_text)
            except FloodWait as fw:
                await asyncio.sleep(getattr(fw, "value", 1))
                try:
                    return await report_profile_photo(client, chat_id, reason=5, reason_text=reason_text)
                except Exception:
                    return False
            except (BadRequest, RPCError):
                return False

        tasks = []
        for i in range(total):
            client = clients[i % len(clients)]
            tasks.append(asyncio.create_task(report_once(client)))

        if tasks:
            for result in await asyncio.gather(*tasks):
                if result:
                    success += 1
                else:
                    failed += 1

        return {"success": success, "failed": failed}

    finally:
        for client in clients:
            try:
                await client.stop()
            except Exception:
                pass


async def handle_add_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        f"Send between {MIN_SESSIONS} and {MAX_SESSIONS} Pyrogram session strings (one per line)."
    )
    return ADD_SESSIONS


async def receive_added_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sessions = session_strings_from_text(update.message.text or "")
    if not (MIN_SESSIONS <= len(sessions) <= MAX_SESSIONS):
        await update.effective_message.reply_text(
            friendly_error(
                f"Please provide between {MIN_SESSIONS} and {MAX_SESSIONS} sessions."
            )
        )
        return ADD_SESSIONS

    added = await data_store.add_sessions(
        sessions, added_by=update.effective_user.id if update.effective_user else None
    )
    await update.effective_message.reply_text(
        f"Stored {len(added)} new session(s). Total available: {len(await data_store.get_sessions())}."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Canceled. Use /report to begin again.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Update %s caused error", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again later.")


def build_app() -> Application:
    application = (
        ApplicationBuilder()
        .token(ensure_token())
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )

    report_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("report", start_report),
            CallbackQueryHandler(handle_action_buttons, pattern=r"^action:"),
        ],
        states={
            REPORT_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_url)],
            REPORT_REASONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reasons)],
            REPORT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_count)],
            REPORT_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sessions)],
            ADD_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_added_sessions)],
            ConversationHandler.WAITING: [CallbackQueryHandler(handle_confirmation, pattern=r"^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    add_sessions_conv = ConversationHandler(
        entry_points=[CommandHandler("addsessions", handle_add_sessions)],
        states={ADD_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_added_sessions)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(add_sessions_conv)
    application.add_handler(report_conversation)
    application.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^confirm:"))

    application.add_error_handler(error_handler)
    return application


async def main() -> None:
    build_logger()
    app = build_app()
    await app.initialize()
    await app.start()
    logging.info("Bot started and polling.")
    await app.updater.start_polling()
    await app.updater.idle()
    await app.stop()
    await app.shutdown()
    await data_store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
