#!/usr/bin/env python3
"""Telegram reporting bot with a guided, premium-style chat UI.

The bot coordinates multiple Pyrogram session strings to submit reports against
Telegram profiles, groups, channels, or stories. The conversation flow focuses
on clarity, guardrails, and graceful error handling so users can complete a
full reporting run without surprises.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Iterable, List
from urllib.parse import urlparse

from pyrogram import Client
from pyrogram.errors import BadRequest, FloodWait, RPCError, UsernameNotOccupied
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
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
PRIVATE_INVITE = 11
PRIVATE_MESSAGE = 12
PUBLIC_MESSAGE = 13
STORY_URL = 14

DEFAULT_REPORTS = 5000
MIN_REPORTS = 500
MAX_REPORTS = 7000
MIN_SESSIONS = 1
MAX_SESSIONS = 500

MENU_LIVE_STATUS = "Live"


data_store = DataStore(config.MONGO_URI)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def build_logger() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def ensure_token() -> str:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. Set it as an environment variable.")
    return config.BOT_TOKEN


def ensure_pyrogram_creds() -> None:
    if not (config.API_ID and config.API_HASH):
        raise RuntimeError("API_ID and API_HASH are required for Pyrogram sessions")


def verify_author_integrity(author_name: str, expected_hash: str) -> None:
    """Verify the stored author hash matches the provided author name."""

    computed_hash = hashlib.sha256(author_name.encode("utf-8")).hexdigest()
    if computed_hash != expected_hash:
        print("Integrity check failed: unauthorized modification.")
        raise SystemExit(1)


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
    parsed = urlparse(text if text.startswith("http") else f"https://{text}")
    return parsed.netloc.endswith("t.me") and len(parsed.path.strip("/")) > 0


def parse_telegram_url(url: str) -> dict:
    """Parse a Telegram URL into structured components."""

    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    path_parts = [p for p in parsed.path.split("/") if p]

    if not parsed.netloc.endswith("t.me") or not path_parts:
        raise ValueError("Invalid Telegram URL")

    if path_parts[0].startswith("+"):
        return {"type": "invite", "invite_link": f"https://t.me/{path_parts[0]}"}

    if path_parts[0] == "c" and len(path_parts) >= 3:
        return {
            "type": "private_message",
            "chat_id": int(f"-100{path_parts[1]}"),
            "message_id": int(path_parts[2]),
        }

    if len(path_parts) >= 3 and path_parts[1] in {"s", "story"}:
        return {
            "type": "story",
            "username": path_parts[0],
            "story_id": path_parts[2],
        }

    if len(path_parts) >= 2:
        return {
            "type": "public_message",
            "username": path_parts[0],
            "message_id": int(path_parts[1]),
        }

    if len(path_parts) == 1:
        return {"type": "username", "username": path_parts[0]}

    raise ValueError("Unrecognized Telegram URL format")


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


# ---------------------------------------------------------------------------
# UI builders
# ---------------------------------------------------------------------------

def main_menu_keyboard(saved_sessions: int = 0, active_sessions: int = 0, live_status: str = MENU_LIVE_STATUS) -> InlineKeyboardMarkup:
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
            [InlineKeyboardButton("Private Channel / Private Group", callback_data="kind:private")],
            [InlineKeyboardButton("Public Channel / Public Group", callback_data="kind:public")],
            [InlineKeyboardButton("Story URL (Profile Story)", callback_data="kind:story")],
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


def render_greeting() -> str:
    return (
        "━━━━━━━✦ DARK MODE ONLINE ✦━━━━━━━╮\n"
        "🤖 *Nightfall Reporter* — premium chat cockpit engaged.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n"
        "🖤 Polished bubbles, elevated reply cards, and tactile pill buttons are live.\n"
        "🌙 Start reporting instantly with saved creds or add new sessions on the fly.\n"
        "✨ Dynamic status chips below keep you oriented as you move through each step.\n"
        "\nTap a control to begin."
    )


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def profile_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("profile", {})


def flow_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("flow", {})


def reset_flow_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    context.user_data["flow"] = {}
    return context.user_data["flow"]


def saved_session_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return len(profile_state(context).get("saved_sessions", []))


def active_session_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return len(flow_state(context).get("sessions", []))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = profile_state(context)
    profile.setdefault("saved_sessions", await data_store.get_sessions())

    greeting = render_greeting()

    await update.effective_message.reply_text(
        greeting,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(len(profile["saved_sessions"]), active_session_count(context)),
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
    active = active_session_count(context)
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
        active = active_session_count(context)
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

    profile = profile_state(context)

    if query.data == "session_mode:reuse":
        saved_sessions = profile.get("saved_sessions", [])
        if not saved_sessions:
            await query.edit_message_text(
                friendly_error("No saved sessions available. Please add new sessions to continue."),
                reply_markup=main_menu_keyboard(len(saved_sessions), active_session_count(context)),
            )
            return ConversationHandler.END

        flow = reset_flow_state(context)
        flow["sessions"] = list(saved_sessions)
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
    profile = profile_state(context)
    flow = reset_flow_state(context)

    profile.setdefault("saved_sessions", await data_store.get_sessions())

    saved_api_id = profile.get("api_id") or config.API_ID
    saved_api_hash = profile.get("api_hash") or config.API_HASH

    if saved_api_id and saved_api_hash:
        flow["api_id"] = saved_api_id
        flow["api_hash"] = saved_api_hash
        profile["api_id"] = saved_api_id
        profile["api_hash"] = saved_api_hash

        saved_sessions = profile.get("saved_sessions", [])
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

    flow_state(context)["api_id"] = int(text)
    profile_state(context)["api_id"] = int(text)
    await update.effective_message.reply_text("Enter your API Hash.")
    return API_HASH_STATE


async def handle_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.effective_message.reply_text(friendly_error("API Hash cannot be empty."))
        return API_HASH_STATE

    flow_state(context)["api_hash"] = text
    profile_state(context)["api_hash"] = text
    await update.effective_message.reply_text(
        (
            f"Paste between {MIN_SESSIONS} and {MAX_SESSIONS} Pyrogram session strings (one per line).\n"
            "Type 'use saved' to load everything stored in MongoDB."
        )
    )
    return REPORT_SESSIONS


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    flow = flow_state(context)

    if text.lower() == "use saved":
        sessions = await data_store.get_sessions()
        if len(sessions) < MIN_SESSIONS:
            await update.effective_message.reply_text(
                friendly_error(f"Not enough saved sessions. Add at least {MIN_SESSIONS} with /addsessions or paste them now."),
                reply_markup=main_menu_keyboard(len(sessions), active_session_count(context)),
            )
            return REPORT_SESSIONS
    else:
        sessions = session_strings_from_text(text)
        if not (MIN_SESSIONS <= len(sessions) <= MAX_SESSIONS):
            await update.effective_message.reply_text(
                friendly_error(f"Provide between {MIN_SESSIONS} and {MAX_SESSIONS} session strings (one per line).")
            )
            return REPORT_SESSIONS
        added = await data_store.add_sessions(
            sessions, added_by=update.effective_user.id if update.effective_user else None
        )
        await update.effective_message.reply_text(
            f"Stored {len(added)} new session(s). {len(sessions)} will be used for this run."
        )

    api_id = flow.get("api_id", 0)
    api_hash = flow.get("api_hash", "")
    valid, invalid = await validate_sessions(api_id, api_hash, sessions)
    if not valid:
        saved_count = len(await data_store.get_sessions())
        await update.effective_message.reply_text(
            friendly_error("No valid sessions were found. Please try again with fresh session strings."),
            reply_markup=main_menu_keyboard(saved_count, active_session_count(context)),
        )
        return ConversationHandler.END

    flow["sessions"] = valid
    profile = profile_state(context)
    profile["saved_sessions"] = valid
    if invalid:
        await update.effective_message.reply_text(f"Ignored {len(invalid)} invalid session(s); using {len(valid)} valid ones.")

    await update.effective_message.reply_text(
        "What are you reporting? Choose a category.", reply_markup=target_kind_keyboard()
    )
    return TARGET_KIND


async def handle_target_kind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kind = query.data
    flow_state(context)["target_kind"] = kind

    if kind == "kind:private":
        await query.edit_message_text(
            "Send the private channel/group invite link (e.g., https://t.me/+xxxx)."
        )
        return PRIVATE_INVITE

    if kind == "kind:public":
        await query.edit_message_text(
            "Send the public message link (e.g., https://t.me/channelusername/1234)."
        )
        return PUBLIC_MESSAGE

    await query.edit_message_text(
        "Send the story URL (profile story link)."
    )
    return STORY_URL


async def handle_report_urls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    links = parse_links(update.message.text or "")
    if not links:
        await update.effective_message.reply_text(
            friendly_error("Please share at least one valid Telegram link (max 5).")
        )
        return REPORT_URLS

    flow = flow_state(context)
    sessions = flow.get("sessions", [])
    api_id = flow.get("api_id") or config.API_ID
    api_hash = flow.get("api_hash") or config.API_HASH

    valid, error_text = await validate_targets(links, sessions, api_id, api_hash, flow.get("invite_link"))
    if not valid:
        await update.effective_message.reply_text(friendly_error(error_text or "Unable to validate the provided links."))
        return REPORT_URLS

    flow["targets"] = links
    await update.effective_message.reply_text("Select the report type.", reply_markup=reason_keyboard())
    return REPORT_REASON_TYPE


async def _validate_and_continue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_link: str,
    *,
    next_state_on_failure: int,
) -> int:
    flow = flow_state(context)
    sessions = flow.get("sessions", [])
    api_id = flow.get("api_id") or config.API_ID
    api_hash = flow.get("api_hash") or config.API_HASH
    invite_link = flow.get("invite_link")

    valid, error_text = await validate_targets([target_link], sessions, api_id, api_hash, invite_link)
    if not valid:
        await update.effective_message.reply_text(
            friendly_error(error_text or "Unable to validate the provided link."),
        )
        return next_state_on_failure

    flow["targets"] = [target_link]
    await update.effective_message.reply_text("Select the report type.", reply_markup=reason_keyboard())
    return REPORT_REASON_TYPE


async def handle_private_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = (update.message.text or "").strip()
    try:
        details = parse_telegram_url(link)
    except Exception:
        await update.effective_message.reply_text(
            friendly_error("That does not look like a valid invite link. Please send a link like https://t.me/+xxxx."),
        )
        return PRIVATE_INVITE

    if details.get("type") != "invite":
        await update.effective_message.reply_text(
            friendly_error("Please provide a private invite link that starts with https://t.me/+"),
        )
        return PRIVATE_INVITE

    flow_state(context)["invite_link"] = link
    await update.effective_message.reply_text(
        "Now send the message link from that private chat (e.g., https://t.me/c/123456789/45)."
    )
    return PRIVATE_MESSAGE


async def handle_private_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = (update.message.text or "").strip()
    try:
        details = parse_telegram_url(link)
    except Exception:
        await update.effective_message.reply_text(
            friendly_error("That does not look like a valid private message link."),
        )
        return PRIVATE_MESSAGE

    if details.get("type") != "private_message":
        await update.effective_message.reply_text(
            friendly_error("Please send a private message link in the form https://t.me/c/123456789/45."),
        )
        return PRIVATE_MESSAGE

    return await _validate_and_continue(
        update,
        context,
        link,
        next_state_on_failure=PRIVATE_MESSAGE,
    )


async def handle_public_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = (update.message.text or "").strip()
    try:
        details = parse_telegram_url(link)
    except Exception:
        await update.effective_message.reply_text(
            friendly_error("That is not a valid public message link."),
        )
        return PUBLIC_MESSAGE

    if details.get("type") != "public_message":
        await update.effective_message.reply_text(
            friendly_error("Send a public message link like https://t.me/channelusername/1234."),
        )
        return PUBLIC_MESSAGE

    return await _validate_and_continue(
        update,
        context,
        link,
        next_state_on_failure=PUBLIC_MESSAGE,
    )


async def handle_story_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = (update.message.text or "").strip()
    try:
        details = parse_telegram_url(link)
    except Exception:
        await update.effective_message.reply_text(
            friendly_error("That is not a valid story URL."),
        )
        return STORY_URL

    if details.get("type") not in {"story", "username", "public_message"}:
        await update.effective_message.reply_text(
            friendly_error("Send a profile story link from t.me."),
        )
        return STORY_URL

    return await _validate_and_continue(
        update,
        context,
        link,
        next_state_on_failure=STORY_URL,
    )


async def handle_reason_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, _, code = query.data.partition(":")
    flow_state(context)["reason_code"] = int(code or 5)
    await query.edit_message_text("Add a short reason/message to include in the report.")
    return REPORT_MESSAGE


async def handle_reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reasons = parse_reasons(update.message.text or "")
    if not reasons:
        await update.effective_message.reply_text(friendly_error("Please provide at least one reason line."))
        return REPORT_MESSAGE

    flow_state(context)["reasons"] = reasons
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

    flow_state(context)["count"] = count

    flow = flow_state(context)
    summary = (
        f"Targets: {len(flow.get('targets', []))}\n"
        f"Reasons: {', '.join(flow.get('reasons', []))}\n"
        f"Report type: {flow.get('reason_code')}\n"
        f"Total reports each: {flow.get('count')}\n"
        f"Session count: {len(flow.get('sessions', []))}"
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
    job_data = deepcopy(flow_state(context))

    # Use the application's task helper so the job is cancelled cleanly when the
    # bot shuts down. This avoids stray "Event loop is closed" errors.
    context.application.create_task(run_report_job(query, context, job_data))
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

    await context.bot.send_message(chat_id=chat_id, text="Preparing clients...")

    messages = []

    try:
        for target in targets:
            started = datetime.now(timezone.utc)
            try:
                summary = await perform_reporting(
                    target,
                    reasons,
                    count,
                    sessions,
                    api_id=api_id,
                    api_hash=api_hash,
                    reason_code=reason_code,
                    invite_link=job_data.get("invite_link"),
                )
            except Exception as exc:  # pragma: no cover - runtime safety
                logging.exception("Failed to complete reporting job")
                summary = {"success": 0, "failed": 0, "halted": True, "error": str(exc)}

            ended = datetime.now(timezone.utc)
            sessions_used = summary.get("sessions_started", len(sessions))
            text = (
                f"Target: {target}\n"
                f"Reasons: {', '.join(reasons)}\n"
                f"Requested: {count}\n"
                f"Sessions used: {sessions_used}\n"
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
                    "sessions": sessions_used,
                    "success": summary["success"],
                    "failed": summary["failed"],
                    "started_at": started,
                    "ended_at": ended,
                    "halted": summary.get("halted", False),
                }
            )

            if summary.get("halted"):
                break
    except asyncio.CancelledError:  # pragma: no cover - application shutdown
        logging.info("Report job cancelled during shutdown")
        return

    await context.bot.send_message(chat_id=chat_id, text="\n\n".join(messages))


async def resolve_chat_id(client: Client, target: str, invite_link: str | None = None):
    """Resolve a Telegram link to a numeric chat ID using Pyrogram helpers."""

    details = parse_telegram_url(target)

    if details["type"] == "invite":
        chat = await client.get_chat(details["invite_link"])
        return chat.id

    if details["type"] == "private_message":
        chat_id = details["chat_id"]

        if invite_link:
            # Prefer the chat ID returned from the join to avoid malformed or stale IDs
            chat = await client.join_chat(invite_link)
            chat_id = getattr(chat, "id", chat_id)
        else:
            chat = await client.get_chat(chat_id)
            chat_id = getattr(chat, "id", chat_id)

        await client.get_messages(chat_id, details["message_id"])
        return chat_id

    if details["type"] == "public_message":
        chat = await client.get_chat(details["username"])
        await client.get_messages(chat.id, details["message_id"])
        return chat.id

    if details["type"] == "story":
        chat = await client.get_chat(details["username"])
        return chat.id

    if details["type"] == "username":
        chat = await client.get_chat(details["username"])
        return chat.id

    raise BadRequest("Unsupported Telegram link format")


async def validate_targets(
    targets: list[str],
    sessions: list[str],
    api_id: int | None,
    api_hash: str | None,
    invite_link: str | None = None,
) -> tuple[bool, str | None]:
    """Confirm each target link or username resolves before starting reports."""

    if not targets:
        return False, "No targets provided for validation."

    if not sessions:
        return False, "No sessions available to validate the provided targets."

    if not (api_id and api_hash):
        ensure_pyrogram_creds()
        api_id = config.API_ID
        api_hash = config.API_HASH

    last_error: str | None = None

    for idx, session in enumerate(sessions):
        client = Client(
            name=f"target_validator_{idx}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            workdir=f"/tmp/target_validator_{idx}",
        )

        try:
            await client.start()

            for target in targets:
                try:
                    await resolve_chat_id(client, target, invite_link)
                except UsernameNotOccupied:
                    # The current session may not have access to the target. Try the next
                    # session before surfacing the error back to the user.
                    last_error = f"The username or link '{target}' is not occupied. Please check it."
                    raise
                except BadRequest as exc:
                    last_error = f"The link '{target}' is not valid: {exc}."
                    raise
                except ValueError as exc:
                    last_error = f"The link '{target}' is not valid: {exc}."
                    raise
                except RPCError as exc:
                    last_error = f"Could not resolve '{target}' ({exc})."
                    raise

            # All targets resolved successfully with this session
            return True, None
        except Exception:
            # Move on to the next session if available
            continue
        finally:
            try:
                await client.stop()
            except Exception:
                pass

    return False, last_error or "Unable to validate the provided targets with the available sessions."


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
    invite_link: str | None = None,
) -> dict:
    """Send repeated report requests with bounded concurrency."""
    if not (api_id and api_hash):
        ensure_pyrogram_creds()
        api_id = config.API_ID
        api_hash = config.API_HASH

    clients: list[Client] = []
    failed_sessions = 0
    for idx, session in enumerate(sessions):
        client = Client(
            name=f"reporter_{idx}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            workdir=f"/tmp/report_session_{idx}",
        )
        try:
            await client.start()
            clients.append(client)
        except Exception:
            failed_sessions += 1

    if not clients:
        return {"success": 0, "failed": 0, "halted": True, "error": "No sessions could be started"}

    reason_text = "; ".join(reasons)[:512] or "No reason provided"

    try:
        chat_id: int | None = None
        last_error: str | None = None

        for client in clients:
            try:
                chat_id = await resolve_chat_id(client, target, invite_link)
                break
            except UsernameNotOccupied:
                last_error = (
                    "The username or link appears to be unoccupied or deleted. "
                    "Please verify the target and try again."
                )
                continue
            except BadRequest as exc:
                last_error = f"The link '{target}' is not valid: {exc}."
                break
            except RPCError as exc:
                last_error = f"Could not resolve '{target}' ({exc})."
                continue

        if chat_id is None:
            return {
                "success": 0,
                "failed": 0,
                "halted": True,
                "error": last_error or "Unable to resolve the target with the available sessions.",
            }

        # Ensure every client can access the target when an invite link is supplied.
        if invite_link:
            for client in clients:
                try:
                    await client.join_chat(invite_link)
                except FloodWait as fw:
                    await asyncio.sleep(getattr(fw, "value", 1))
                    try:
                        await client.join_chat(invite_link)
                    except Exception:
                        pass
                except Exception:
                    # Keep going; other sessions may still be able to join/report.
                    pass

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
            nonlocal success, failed, halted
            while True:
                if halted:
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                            queue.task_done()
                        except asyncio.QueueEmpty:
                            break
                    break

                try:
                    client = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                result = await report_once(client)
                if result:
                    success += 1
                else:
                    failed += 1
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await queue.join()
        await asyncio.gather(*workers)

        return {
            "success": success,
            "failed": failed,
            "halted": halted,
            "sessions_started": len(clients),
            "sessions_failed": failed_sessions,
        }

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
            friendly_error(f"Please provide between {MIN_SESSIONS} and {MAX_SESSIONS} sessions.")
        )
        return ADD_SESSIONS

    added = await data_store.add_sessions(
        sessions, added_by=update.effective_user.id if update.effective_user else None
    )
    profile_state(context)["saved_sessions"] = (profile_state(context).get("saved_sessions") or []) + added
    await update.effective_message.reply_text(
        f"Stored {len(added)} new session(s). Total available: {len(await data_store.get_sessions())}."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Canceled. Use /report to begin again.")
    reset_flow_state(context)
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Update %s caused error", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again later.")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

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
            PRIVATE_INVITE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_invite)],
            PRIVATE_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_message_link)],
            PUBLIC_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_public_message_link)],
            STORY_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_story_url)],
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    verify_author_integrity(config.AUTHOR_NAME, config.AUTHOR_HASH)
    build_logger()
    app = build_app()

    logging.info("Bot started and polling.")
    try:
        app.run_polling()
    except NetworkError as exc:
        logging.error("Failed to connect to Telegram: %s", exc)
        raise SystemExit(1) from exc
    finally:
        asyncio.run(data_store.close())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
