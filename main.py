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
(
    API_ID_STATE,
    API_HASH_STATE,
    REPORT_SESSIONS,
    TARGET_KIND,
    REPORT_URLS,
    REPORT_REASON_TYPE,
    REPORT_MESSAGE,
    REPORT_COUNT,
    SESSION_MODE,
) = range(9)
ADD_SESSIONS = 10

DEFAULT_REPORTS = 5000
MIN_REPORTS = 500
MAX_REPORTS = 7000
MIN_SESSIONS = 1
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


def main_menu_keyboard(saved_sessions: int = 0, active_sessions: int = 0, live_status: str = "Live") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Start report", callback_data="action:start")],
            [InlineKeyboardButton("🧩 Add sessions", callback_data="action:add")],
            [InlineKeyboardButton("💾 Saved sessions", callback_data="action:sessions")],
            [
                InlineKeyboardButton(f"🟢 {live_status} · Dark UI", callback_data="status:live"),
                InlineKeyboardButton(f"🎯 Loaded: {active_sessions}", callback_data="status:active"),
                InlineKeyboardButton(f"📦 Saved: {saved_sessions}", callback_data="status:saved"),
            ],
        ]
    )


def target_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Private group", callback_data="kind:private")],
            [InlineKeyboardButton("Public group / channel", callback_data="kind:public")],
            [InlineKeyboardButton("Profile / story", callback_data="kind:profile")],
        ]
    )


def reason_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Spam", callback_data="reason:0"), InlineKeyboardButton("Violence", callback_data="reason:1")],
            [InlineKeyboardButton("Pornography", callback_data="reason:2"), InlineKeyboardButton("Child abuse", callback_data="reason:3")],
            [InlineKeyboardButton("Copyright", callback_data="reason:4"), InlineKeyboardButton("Other", callback_data="reason:5")],
        ]
    )


def session_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Report with saved sessions", callback_data="session_mode:reuse")],
            [InlineKeyboardButton("Add new sessions", callback_data="session_mode:new")],
        ]
    )


def friendly_error(message: str) -> str:
    return f"⚠️ {message}\nUse the menu below or try again."


def parse_reasons(text: str) -> List[str]:
    reasons = [line.strip() for line in text.replace(";", "\n").splitlines() if line.strip()]
    return reasons[:5]


def parse_links(text: str) -> list[str]:
    links: list[str] = []
    for chunk in text.replace(";", "\n").split():
        if is_valid_link(chunk):
            links.append(chunk)
    return links[:5]


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


async def validate_sessions(api_id: int, api_hash: str, sessions: list[str]) -> tuple[list[str], list[str]]:
    """Start/stop each session to confirm validity."""

    valid: list[str] = []
    invalid: list[str] = []

    for idx, session in enumerate(sessions):
        client = Client(
            name=f"validation_{idx}", api_id=api_id, api_hash=api_hash, session_string=session, workdir=f"/tmp/validate_{idx}"
        )
        try:
            await client.start()
            valid.append(session)
        except Exception:
            invalid.append(session)
        finally:
            try:
                await client.stop()
            except Exception:
                pass

    return valid, invalid


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved_sessions = len(await data_store.get_sessions())
    active_sessions = len(context.user_data.get("sessions", []))

    greeting = (
        "╭━━━━━━━✦ DARK MODE ONLINE ✦━━━━━━━╮\n"
        "🤖 *Nightfall Reporter* — premium chat cockpit engaged.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n"
        "🖤 Polished bubbles, elevated reply cards, and tactile pill buttons are live.\n"
        "🌙 Start reporting instantly with saved creds or add new sessions on the fly.\n"
        "✨ Dynamic status chips below keep you oriented as you move through each step.\n"
        "\nTap a control to begin."
    )

    await update.effective_message.reply_text(
        greeting,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(saved_sessions, active_sessions),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "ℹ️ *How to use the reporter*\n"
        "1) Run /report or tap Start report.\n"
        "2) Provide your API ID and API Hash.\n"
        "3) Add 1-500 Pyrogram session strings (or type 'use saved').\n"
        "4) Pick what you are reporting (private group, public group/channel, or profile/story).\n"
        "5) Send up to 5 Telegram URLs, choose a report type, and write a short reason.\n"
        "6) Choose 500-7000 report attempts (default 5000).\n"
        "I will show successes, failures, time taken, and stop automatically if the content disappears."
    )
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def show_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = len(await data_store.get_sessions())
    active = len(context.user_data.get("sessions", []))
    await update.effective_message.reply_text(
        f"Saved sessions: {saved}\nCurrently loaded for this chat: {active}",
        reply_markup=main_menu_keyboard(saved, active),
    )


async def handle_action_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "action:start":
        return await start_report(update, context)
    if query.data == "action:add":
        await query.edit_message_text(f"Send {MIN_SESSIONS}-{MAX_SESSIONS} Pyrogram session strings, one per line.")
        return ADD_SESSIONS
    if query.data == "action:sessions":
        saved = len(await data_store.get_sessions())
        active = len(context.user_data.get("sessions", []))
        await query.edit_message_text(
            f"Saved sessions: {saved}\nCurrently loaded for this chat: {active}",
            reply_markup=main_menu_keyboard(saved, active),
        )
        return ConversationHandler.END
    return ConversationHandler.END


async def handle_status_chip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Live status indicators — you are already in the dark UI.", show_alert=False)


async def handle_session_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    saved_sessions = context.user_data.get("saved_sessions", [])

    if query.data == "session_mode:reuse":
        if not saved_sessions:
            await query.edit_message_text(
                friendly_error("No saved sessions available. Please add new sessions to continue."),
                reply_markup=main_menu_keyboard(len(saved_sessions), len(context.user_data.get("sessions", []))),
            )
            return ConversationHandler.END

        context.user_data["sessions"] = list(saved_sessions)
        await query.edit_message_text(
            "Using your saved sessions. What are you reporting?",
            reply_markup=target_kind_keyboard(),
        )
        return TARGET_KIND

    await query.edit_message_text(
        f"Send between {MIN_SESSIONS} and {MAX_SESSIONS} Pyrogram session strings (one per line)."
    )
    return REPORT_SESSIONS


async def start_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    saved_api_id = context.user_data.get("saved_api_id") or config.API_ID
    saved_api_hash = context.user_data.get("saved_api_hash") or config.API_HASH
    saved_sessions = context.user_data.get("saved_sessions")
    if saved_sessions is None:
        saved_sessions = await data_store.get_sessions()

    # Reset conversation-specific values while keeping any previously stored credentials/sessions
    context.user_data.clear()
    if saved_api_id and saved_api_hash:
        context.user_data["saved_api_id"] = saved_api_id
        context.user_data["saved_api_hash"] = saved_api_hash
    if saved_sessions:
        context.user_data["saved_sessions"] = list(saved_sessions)

    if saved_api_id and saved_api_hash:
        context.user_data["api_id"] = saved_api_id
        context.user_data["api_hash"] = saved_api_hash
        if saved_sessions:
            await update.effective_message.reply_text(
                (
                    f"Using your saved API credentials. {len(saved_sessions)} active session(s) ready.\n"
                    "Do you want to reuse them or add new sessions?"
                ),
                reply_markup=session_mode_keyboard(),
            )
            return SESSION_MODE

        await update.effective_message.reply_text(
            (
                "Using your saved API credentials. Send between "
                f"{MIN_SESSIONS}-{MAX_SESSIONS} Pyrogram session strings (one per line)."
            )
        )
        return REPORT_SESSIONS

    await update.effective_message.reply_text("Enter your Telegram API ID (numeric).")
    return API_ID_STATE


async def handle_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text(friendly_error("API ID must be numeric."))
        return API_ID_STATE

    context.user_data["api_id"] = int(text)
    context.user_data["saved_api_id"] = int(text)
    await update.effective_message.reply_text("Enter your API Hash.")
    return API_HASH_STATE


async def handle_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.effective_message.reply_text(friendly_error("API Hash cannot be empty."))
        return API_HASH_STATE

    context.user_data["api_hash"] = text
    context.user_data["saved_api_hash"] = text
    await update.effective_message.reply_text(
        (
            f"Paste between {MIN_SESSIONS} and {MAX_SESSIONS} Pyrogram session strings (one per line).\n"
            "Type 'use saved' to load everything stored in MongoDB."
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

    valid, invalid = await validate_sessions(
        context.user_data.get("api_id", 0), context.user_data.get("api_hash", ""), sessions
    )
    if not valid:
        saved_count = len(await data_store.get_sessions())
        await update.effective_message.reply_text(
            friendly_error("No valid sessions were found. Please try again with fresh session strings."),
            reply_markup=main_menu_keyboard(saved_count, len(context.user_data.get("sessions", []))),
        )
        return ConversationHandler.END

    context.user_data["sessions"] = valid
    context.user_data["saved_sessions"] = valid
    if invalid:
        await update.effective_message.reply_text(f"Ignored {len(invalid)} invalid session(s); using {len(valid)} valid ones.")

    await update.effective_message.reply_text(
        "What are you reporting? Choose a category.", reply_markup=target_kind_keyboard()
    )
    return TARGET_KIND


async def handle_target_kind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["target_kind"] = query.data
    await query.edit_message_text(
        "Send up to 5 Telegram URLs or @usernames to report (separated by spaces or new lines)."
    )
    return REPORT_URLS


async def handle_report_urls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = parse_links(update.message.text or "")
    if not links:
        await update.effective_message.reply_text(
            friendly_error("Please share at least one valid Telegram link or @username (max 5).")
        )
        return REPORT_URLS

    context.user_data["targets"] = links
    await update.effective_message.reply_text("Select the report type.", reply_markup=reason_keyboard())
    return REPORT_REASON_TYPE


async def handle_reason_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, _, code = query.data.partition(":")
    context.user_data["reason_code"] = int(code or 5)
    await query.edit_message_text("Add a short reason/message to include in the report.")
    return REPORT_MESSAGE


async def handle_reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reasons = parse_reasons(update.message.text or "")
    if not reasons:
        await update.effective_message.reply_text(friendly_error("Please provide at least one reason line."))
        return REPORT_MESSAGE

    context.user_data["reasons"] = reasons
    await update.effective_message.reply_text(
        f"How many report requests? (min {MIN_REPORTS}, max {MAX_REPORTS}, or 'default' for {DEFAULT_REPORTS})"
    )
    return REPORT_COUNT


async def handle_report_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().lower()
    if text in {"", "default"}:
        count = DEFAULT_REPORTS
    elif text.isdigit():
        count = int(text)
        if not (MIN_REPORTS <= count <= MAX_REPORTS):
            await update.effective_message.reply_text(
                friendly_error(f"Enter a number between {MIN_REPORTS} and {MAX_REPORTS}, or 'default'.")
            )
            return REPORT_COUNT
    else:
        await update.effective_message.reply_text(
            friendly_error(f"Enter a number between {MIN_REPORTS} and {MAX_REPORTS}, or 'default'.")
        )
        return REPORT_COUNT

    context.user_data["count"] = count

    summary = (
        f"Targets: {len(context.user_data.get('targets', []))}\n"
        f"Reasons: {', '.join(context.user_data.get('reasons', []))}\n"
        f"Report type: {context.user_data.get('reason_code')}\n"
        f"Total reports each: {context.user_data.get('count')}\n"
        f"Session count: {len(context.user_data.get('sessions', []))}"
    )

    await update.effective_message.reply_text(
        f"Confirm the report run?\n\n{summary}",
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

    # Capture a snapshot of the current conversation data so that subsequent
    # /report runs do not erase the information needed by this background task.
    job_data = {
        "targets": list(context.user_data.get("targets", [])),
        "reasons": list(context.user_data.get("reasons", [])),
        "count": context.user_data.get("count", DEFAULT_REPORTS),
        "sessions": list(context.user_data.get("sessions", [])),
        "api_id": context.user_data.get("api_id"),
        "api_hash": context.user_data.get("api_hash"),
        "reason_code": context.user_data.get("reason_code", 5),
    }

    asyncio.create_task(run_report_job(query, context, job_data))
    return ConversationHandler.END


async def run_report_job(query, context: ContextTypes.DEFAULT_TYPE, job_data: dict) -> None:
    user = query.from_user
    chat_id = query.message.chat_id

    targets = job_data.get("targets", [])
    reasons = job_data.get("reasons", [])
    count = job_data.get("count", DEFAULT_REPORTS)
    sessions = job_data.get("sessions", [])
    api_id = job_data.get("api_id")
    api_hash = job_data.get("api_hash")
    reason_code = job_data.get("reason_code", 5)

    started = datetime.now(timezone.utc)
    await context.bot.send_message(chat_id=chat_id, text="Preparing clients...")

    messages = []

    for target in targets:
        summary = await perform_reporting(target, reasons, count, sessions, api_id=api_id, api_hash=api_hash, reason_code=reason_code)
        ended = datetime.now(timezone.utc)
        text = (
            f"Target: {target}\n"
            f"Reasons: {', '.join(reasons)}\n"
            f"Requested: {count}\n"
            f"Sessions used: {len(sessions)}\n"
            f"Success: {summary['success']} | Failed: {summary['failed']}\n"
            f"Stopped early: {'Yes' if summary.get('halted') else 'No'}\n"
            f"Error: {summary.get('error', 'None')}\n"
            f"Started: {started.isoformat()}\n"
            f"Ended: {ended.isoformat()}"
        )
        messages.append(text)

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
                "halted": summary.get("halted", False),
            }
        )

        if summary.get("halted"):
            break

    await context.bot.send_message(chat_id=chat_id, text="\n\n".join(messages))


async def resolve_chat_id(client: Client, target: str):
    identifier = extract_target_identifier(target)
    chat = await client.get_chat(identifier)
    return chat.id


async def perform_reporting(
    target: str,
    reasons: Iterable[str],
    total: int,
    sessions: list[str],
    *,
    api_id: int | None,
    api_hash: str | None,
    reason_code: int = 5,
    max_concurrency: int = 25,
) -> dict:
    """Send repeated report requests with bounded concurrency."""
    if not (api_id and api_hash):
        ensure_pyrogram_creds()
        api_id = config.API_ID
        api_hash = config.API_HASH

    clients = [
        Client(
            name=f"reporter_{idx}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            workdir=f"/tmp/report_session_{idx}",
        )
        for idx, session in enumerate(sessions)
    ]

    reason_text = "; ".join(reasons)[:512] or "No reason provided"

    try:
        for client in clients:
            await client.start()

        try:
            chat_id = await resolve_chat_id(clients[0], target)
        except (BadRequest, RPCError) as exc:
            return {"success": 0, "failed": 0, "halted": True, "error": str(exc)}

        success = 0
        failed = 0

        halted = False

        async def report_once(client: Client) -> bool:
            nonlocal halted
            try:
                return await report_profile_photo(client, chat_id, reason=reason_code, reason_text=reason_text)
            except FloodWait as fw:
                await asyncio.sleep(getattr(fw, "value", 1))
                try:
                    return await report_profile_photo(client, chat_id, reason=reason_code, reason_text=reason_text)
                except Exception:
                    return False
            except (BadRequest, RPCError):
                halted = True
                return False

        # Avoid spawning thousands of concurrent tasks by capping worker count.
        worker_count = max(1, min(max_concurrency, total, len(clients)))
        queue: asyncio.Queue[Client] = asyncio.Queue()

        for _ in range(total):
            queue.put_nowait(clients[_ % len(clients)])

        async def worker() -> None:
            nonlocal success, failed
            while not queue.empty():
                client = await queue.get()
                result = await report_once(client)
                if result:
                    success += 1
                else:
                    failed += 1
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await queue.join()
        await asyncio.gather(*workers)

        return {"success": success, "failed": failed, "halted": halted}

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
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_id)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_hash)],
            REPORT_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sessions)],
            SESSION_MODE: [CallbackQueryHandler(handle_session_mode, pattern=r"^session_mode:")],
            TARGET_KIND: [CallbackQueryHandler(handle_target_kind, pattern=r"^kind:")],
            REPORT_URLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_urls)],
            REPORT_REASON_TYPE: [CallbackQueryHandler(handle_reason_type, pattern=r"^reason:")],
            REPORT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reason_message)],
            REPORT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_count)],
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
    application.add_handler(CommandHandler("sessions", show_sessions))
    application.add_handler(add_sessions_conv)
    application.add_handler(report_conversation)
    application.add_handler(CallbackQueryHandler(handle_status_chip, pattern=r"^status:"))
    application.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^confirm:"))

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    build_logger()
    app = build_app()

    # Application.run_polling takes care of initialization, startup, and shutdown
    # logic. Running it directly avoids calling the deprecated Updater APIs that
    # no longer provide an `idle` helper in PTB 21+.
    logging.info("Bot started and polling.")
    try:
        app.run_polling()
    finally:
        asyncio.run(data_store.close())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
