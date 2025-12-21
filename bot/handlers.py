from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from copy import deepcopy
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

import config
from bot.constants import (
    ADD_SESSIONS,
    API_HASH_STATE,
    API_ID_STATE,
    DEFAULT_REPORTS,
    MAX_REPORTS,
    MAX_SESSIONS,
    MIN_REPORTS,
    MIN_SESSIONS,
    PRIVATE_INVITE,
    PRIVATE_MESSAGE,
    PUBLIC_MESSAGE,
    REPORT_COUNT,
    REPORT_MESSAGE,
    REPORT_REASON_TYPE,
    REPORT_SESSIONS,
    REPORT_URLS,
    REASON_LABELS,
    SESSION_MODE,
    STORY_URL,
    TARGET_KIND,
)
from bot.dependencies import API_HASH, API_ID, data_store
from bot.link_parser import parse_join_target
from bot.target_resolver import ensure_join_if_needed, fetch_target_details, parse_target, resolve_entity
from bot.health import format_duration, process_health
from bot.scheduler import SchedulerManager
from bot.reporting import run_report_job
from bot.state import (
    active_session_count,
    clear_report_state,
    flow_state,
    profile_state,
    reset_flow_state,
    reset_user_context,
    saved_session_count,
)
from bot.ui import (
    add_restart_button,
    main_menu_keyboard,
    reason_keyboard,
    render_card,
    render_greeting,
    session_mode_keyboard,
    target_kind_keyboard,
    navigation_keyboard,
)
from bot.utils import (
    friendly_error,
    is_valid_link,
    parse_links,
    parse_reasons,
    parse_telegram_url,
    session_strings_from_text,
    validate_sessions,
)

HELP_MESSAGE = (
    "ℹ️ *How to use the reporter*\n"
    "1) Run /report or tap Start report.\n"
    "2) Provide your API ID and API Hash.\n"
    "3) Add 1-500 Pyrogram session strings (or type 'use saved').\n"
    "4) Pick what you are reporting (private group, public group/channel, or profile/story).\n"
    "5) Send up to 5 Telegram URLs, choose a report type, and write a short reason.\n"
    "6) Choose 500-7000 report attempts (default 5000).\n"
    "I will show successes, failures, time taken, and stop automatically if the content disappears."
)


async def safe_edit_message(query, text: str, *, reply_markup=None, parse_mode=None, **kwargs):
    current = query.message
    html_text = getattr(current, "text_html", None)
    current_text = (html_text if parse_mode == ParseMode.HTML else current.text) or ""
    current_markup = current.reply_markup
    current_markup_dict = current_markup.to_dict() if current_markup else None
    new_markup_dict = reply_markup.to_dict() if reply_markup else None

    if current_text == text and current_markup_dict == new_markup_dict:
        return current

    try:
        return await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
        )
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return current
        raise


def _format_sessions_for_copy(sessions: list[str], *, max_items: int = 10) -> str:
    lines = [f"<code>{escape(session)}</code>" for session in sessions[:max_items]]
    remaining = len(sessions) - max_items
    if remaining > 0:
        lines.append(f"…and {remaining} more session(s).")
    return "\n".join(lines)


async def _ensure_active_session(query, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = flow_state(context)
    if flow.get("sessions"):
        return True

    profile = profile_state(context)
    profile["saved_sessions"] = await data_store.get_sessions()
    saved_sessions = profile.get("saved_sessions") or []

    if saved_sessions:
        flow["sessions"] = list(saved_sessions)
        return True

    await safe_edit_message(
        query,
        friendly_error("Please add a new session"),
        reply_markup=main_menu_keyboard(len(saved_sessions), active_session_count(context)),
    )
    return False


async def _validate_sessions_with_feedback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    sessions: list[str],
    *,
    api_id: int | None,
    api_hash: str | None,
    fallback_markup=None,
) -> list[str]:
    if not sessions:
        await _notify_user(
            update,
            friendly_error("No sessions provided. Please add sessions to continue."),
            reply_markup=fallback_markup,
        )
        return []

    if not (api_id and api_hash):
        await _notify_user(
            update,
            friendly_error("API credentials are missing. Please restart and set API ID/API Hash."),
            reply_markup=fallback_markup,
        )
        return []

    try:
        valid, invalid = await validate_sessions(api_id, api_hash, sessions)
    except Exception:
        logging.exception("Session validation failed")
        await _notify_user(
            update,
            friendly_error("Unable to validate sessions right now. Please try again."),
            reply_markup=fallback_markup,
        )
        return []

    if invalid:
        removed = await data_store.remove_sessions(invalid)
        card = render_card(
            "Session validation",
            [
                f"Removed {len(invalid)} invalid session(s).",
                f"{len(valid)} valid session(s) remain.",
            ],
            [f"Pruned from storage: {removed}"],
        )
        await _notify_user(
            update,
            f"<pre>{card}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=fallback_markup,
        )

    if not valid:
        await _notify_user(
            update,
            friendly_error("All provided sessions are invalid. Please add new sessions."),
            reply_markup=fallback_markup,
        )
        return []

    return valid


async def _with_resolver_client(context: ContextTypes.DEFAULT_TYPE, callback):
    """Start a lightweight Pyrogram client for target resolution and cleanup automatically."""

    from pyrogram import Client

    flow = flow_state(context)
    profile = profile_state(context)

    api_id = flow.get("api_id") or profile.get("api_id") or config.API_ID
    api_hash = flow.get("api_hash") or profile.get("api_hash") or config.API_HASH
    sessions = flow.get("sessions") or []

    if not api_id or not api_hash or not sessions:
        raise RuntimeError("Missing API credentials or sessions for resolution")

    session = sessions[0]
    client = Client(
        name=f"resolver_{abs(hash(session)) % 10_000}",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session,
        workdir=f"/tmp/resolver_{abs(hash(session)) % 10_000}",
    )
    try:
        await client.start()
        return await callback(client)
    finally:
        try:
            await client.stop()
        except Exception:
            pass


async def _join_target_with_client(client, parsed_link, status_callback, *, max_attempts: int = 3):
    from pyrogram.errors import FloodWait, RPCError, UserAlreadyParticipant

    me = await client.get_me()
    join_target = parsed_link.normalized_url if parsed_link.type == "invite" else parsed_link.username or parsed_link.normalized_url
    join_target = join_target.lstrip("@") if isinstance(join_target, str) else join_target

    for attempt in range(1, max_attempts + 1):
        try:
            await client.join_chat(join_target)
            chat = await client.get_chat(join_target)
            member = await client.get_chat_member(chat.id, me.id)
            return {"ok": True, "chat": chat, "member": member, "attempt": attempt}
        except UserAlreadyParticipant:
            chat = await client.get_chat(join_target)
            member = await client.get_chat_member(chat.id, me.id)
            return {"ok": True, "chat": chat, "member": member, "attempt": attempt, "already": True}
        except FloodWait as exc:
            wait_seconds = int(getattr(exc, "value", 0) or 0)
            jitter = random.uniform(1, 3)
            await status_callback(
                f"⚠️ FloodWait: Telegram rate limited this client. Wait {wait_seconds} seconds, then I'll retry automatically. "
                f"(attempt {attempt}/{max_attempts})"
            )
            if attempt >= max_attempts:
                return {
                    "ok": False,
                    "error": "flood_wait",
                    "wait_seconds": wait_seconds,
                    "detail": str(exc),
                }
            await asyncio.sleep(wait_seconds + jitter)
            continue
        except RPCError as exc:
            return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": exc.__class__.__name__, "detail": str(exc)}

    return {"ok": False, "error": "exhausted"}


def _attach_invite(spec, invite_link: str | None):
    if not invite_link or spec.invite_link:
        return spec

    invite_hash_match = None
    if "+" in invite_link:
        invite_hash_match = invite_link.split("+")[-1]
    else:
        invite_hash_match = invite_link.rsplit("/", 1)[-1]

    return spec.__class__(
        raw=spec.raw,
        normalized=spec.normalized,
        kind="invite" if spec.kind == "username" else spec.kind,
        username=spec.username,
        numeric_id=spec.numeric_id,
        invite_hash=invite_hash_match,
        invite_link=invite_link,
        message_id=spec.message_id,
        internal_id=spec.internal_id,
    )


async def _join_and_report(update: Update, context: ContextTypes.DEFAULT_TYPE, link_text: str):
    try:
        parsed_link = parse_join_target(link_text)
    except Exception:
        await update.effective_message.reply_text(
            "Please send a valid invite link or public @username (https://t.me/+code or @channel)",
            reply_markup=navigation_keyboard(),
        )
        return None

    async def _status(message: str):
        await update.effective_message.reply_text(message, reply_markup=navigation_keyboard())

    async def _runner(client):
        return await _join_target_with_client(client, parsed_link, _status)

    try:
        result = await _with_resolver_client(context, _runner)
    except Exception:
        logging.exception("Join flow failed")
        await update.effective_message.reply_text(
            friendly_error("Unable to join this link with the provided session."),
            reply_markup=navigation_keyboard(),
        )
        return None

    if not result or not result.get("ok"):
        wait_seconds = result.get("wait_seconds") if result else None
        detail = result.get("detail") if result else None
        await update.effective_message.reply_text(
            friendly_error(
                "Join failed. "
                + (
                    f"FloodWait {wait_seconds}s. Please wait and resend." if result and result.get("error") == "flood_wait" else "Please verify the link or try another session."
                )
            ),
            reply_markup=navigation_keyboard(),
        )
        if detail:
            logging.warning("Join failure detail: %s", detail)
        return None

    chat = result.get("chat")
    if chat:
        flow_state(context)["invite_link"] = parsed_link.normalized_url if parsed_link.type == "invite" else None
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or parsed_link.username or "chat"
        await update.effective_message.reply_text(
            f"✅ Joined successfully: {title} ({getattr(chat, 'id', 'unknown')})",
            reply_markup=navigation_keyboard(),
        )

    return parsed_link


def _format_target_details(details) -> str:
    lines = ["🎯 <b>Target Details</b>"]
    if details.title:
        lines.append(f"Name: {escape(details.title)}")
    if details.username:
        lines.append(f"Username: @{escape(details.username)}")
    if details.id:
        lines.append(f"ID: <code>{details.id}</code>")
    if details.type:
        lines.append(f"Type: {escape(details.type)}{' (private)' if details.private else ''}")
    if details.members:
        lines.append(f"Members/Subscribers: {details.members}")
    if details.description:
        lines.append(f"About: {escape(details.description[:140])}")
    flags = []
    for flag, label in (
        (details.is_bot, "bot"),
        (details.is_verified, "verified"),
        (details.is_scam, "scam"),
        (details.is_fake, "fake"),
    ):
        if flag:
            flags.append(label)
    if flags:
        lines.append(f"Flags: {', '.join(flags)}")
    return "\n".join(lines)


async def _resolve_and_preview_target(update: Update, context: ContextTypes.DEFAULT_TYPE, target_text: str) -> bool:
    """Join private chats when needed and show target details."""

    invite_link = flow_state(context).get("invite_link")
    spec = _attach_invite(parse_target(target_text), invite_link)

    async def _runner(client):
        join_info = await ensure_join_if_needed(client, spec)
        resolution = await resolve_entity(client, spec)
        details = await fetch_target_details(client, resolution)
        return join_info, resolution, details

    try:
        join_info, resolution, details = await _with_resolver_client(context, _runner)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Target preview failed")
        await update.effective_message.reply_text(
            friendly_error("Unable to reach this target right now. Please verify the link and try again."),
            reply_markup=navigation_keyboard(),
        )
        return False

    if join_info and not join_info.ok:
        await update.effective_message.reply_text(
            friendly_error("I could not join the invite link. Please provide a valid invite or try another session."),
            reply_markup=navigation_keyboard(),
        )
        return False

    if not resolution.ok:
        logging.error(
            "TargetResolver failed resolution for %s (kind=%s, error=%s)",
            spec.raw,
            spec.kind,
            resolution.error,
            stack_info=True,
        )
        await update.effective_message.reply_text(
            friendly_error("Could not resolve link/chat. Ensure I have access and the link is correct."),
            reply_markup=navigation_keyboard(),
        )
        return False

    await update.effective_message.reply_text(
        _format_target_details(details), parse_mode=ParseMode.HTML, reply_markup=navigation_keyboard(show_back=False)
    )
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_user_context(context, update.effective_user.id if update.effective_user else None)
    profile = profile_state(context)
    profile["saved_sessions"] = await data_store.get_sessions()

    greeting = render_greeting()

    await update.effective_message.reply_text(
        f"<pre>{greeting}</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(len(profile["saved_sessions"]), active_session_count(context)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.MARKDOWN)

async def _notify_user(
    update: Update,
    text: str,
    *,
    reply_markup=None,
    parse_mode=None,
):
    if update.callback_query:
        return await safe_edit_message(
            update.callback_query, text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    return await update.effective_message.reply_text(
        text, reply_markup=reply_markup, parse_mode=parse_mode
    )


async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    snapshot = process_health()
    uptime_text = format_duration(snapshot["uptime_seconds"])
    message = (
        f"⏱ Uptime: {uptime_text}\n"
        f"🕒 Server time: {snapshot['server_time']}\n"
        f"🔖 Version: {snapshot['version']}\n"
        f"⚙️ CPU: {snapshot['cpu_percent']:.1f}%\n"
        f"🧠 Memory: {snapshot['memory_mb']:.1f} MB"
    )
    await update.effective_message.reply_text(message)


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sent_at = time.perf_counter()
    message = await update.effective_message.reply_text("Pinging Telegram…")
    latency_ms = (time.perf_counter() - sent_at) * 1000

    snapshot = process_health()
    await message.edit_text(
        "\n".join(
            [
                f"🏓 Pong! {latency_ms:.0f} ms",
                f"⚙️ CPU: {snapshot['cpu_percent']:.1f}%",
                f"🧠 Memory: {snapshot['memory_mb']:.1f} MB",
                f"⏱ Uptime: {format_duration(snapshot['uptime_seconds'])}",
            ]
        )
    )


async def _send_restart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = profile_state(context)
    profile["saved_sessions"] = await data_store.get_sessions()
    greeting = render_greeting()
    markup = main_menu_keyboard(len(profile.get("saved_sessions", [])), active_session_count(context))

    if update.callback_query:
        query = update.callback_query
        await query.answer("Restarted.")
        await safe_edit_message(query, f"<pre>{greeting}</pre>", parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.effective_message.reply_text(
            f"<pre>{greeting}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Any user can request a full process restart via "/restart bot" for reliability.
    if getattr(context, "args", None) and context.args and context.args[0].lower() == "bot":
        logging.info("Restart requested by %s (%s)", update.effective_user, update.effective_user.id if update.effective_user else "unknown")
        await update.effective_message.reply_text("Restarting… I will be back shortly for everyone.")
        SchedulerManager.shutdown()
        context.bot_data["restart_requested"] = True
        shutdown_event = context.bot_data.get("shutdown_event")
        if shutdown_event:
            shutdown_event.set()
        else:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        return

    reset_user_context(context, update.effective_user.id if update.effective_user else None)
    await _send_restart_menu(update, context)


async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reset_user_context(context, update.effective_user.id if update.effective_user else None)
    await _send_restart_menu(update, context)
    return ConversationHandler.END


REASON_PROMPT = (
    "Select a report type via the buttons below (Spam, Child abuse, Pornography,"
    " Violence, Illegal content, Copyright, Other)."
)


def _reason_label(reason_code: int | None) -> str:
    if reason_code is None:
        return "Not set"
    return REASON_LABELS.get(reason_code, str(reason_code))


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
        await safe_edit_message(query, f"Send {MIN_SESSIONS}-{MAX_SESSIONS} Pyrogram session strings, one per line.")
        return ADD_SESSIONS
    if query.data == "action:help":
        await safe_edit_message(
            query,
            HELP_MESSAGE,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(saved_session_count(context), active_session_count(context)),
        )
        return ConversationHandler.END
    if query.data == "action:sessions":
        saved = len(await data_store.get_sessions())
        active = active_session_count(context)
        await safe_edit_message(
            query,
            f"Saved sessions: {saved}\nCurrently loaded for this chat: {active}",
            reply_markup=main_menu_keyboard(saved, active),
        )
        return ConversationHandler.END
    return ConversationHandler.END


async def handle_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "nav:back":
        await safe_edit_message(query, "What are you reporting?", reply_markup=target_kind_keyboard())
        return TARGET_KIND

    reset_user_context(context, update.effective_user.id if update.effective_user else None)
    profile = profile_state(context)
    profile["saved_sessions"] = await data_store.get_sessions()
    await safe_edit_message(
        query,
        "Canceled. Use /report to start again.",
        reply_markup=main_menu_keyboard(len(profile.get("saved_sessions", [])), active_session_count(context)),
    )
    return ConversationHandler.END


async def handle_report_again(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    last_config = context.user_data.get("last_report_config") or {}
    profile = profile_state(context)
    profile["saved_sessions"] = await data_store.get_sessions()

    if not last_config.get("sessions") and not profile.get("saved_sessions"):
        await safe_edit_message(
            query,
            friendly_error("No previous sessions available. Please add sessions to start reporting."),
            reply_markup=main_menu_keyboard(saved_session_count(context), active_session_count(context)),
        )
        return ConversationHandler.END

    flow = reset_flow_state(context)
    flow["sessions"] = list(last_config.get("sessions") or profile.get("saved_sessions") or [])

    api_id = last_config.get("api_id") or profile.get("api_id") or config.API_ID
    api_hash = last_config.get("api_hash") or profile.get("api_hash") or config.API_HASH

    if api_id:
        flow["api_id"] = api_id
    if api_hash:
        flow["api_hash"] = api_hash

    valid_sessions = await _validate_sessions_with_feedback(
        update,
        context,
        flow["sessions"],
        api_id=api_id,
        api_hash=api_hash,
        fallback_markup=main_menu_keyboard(saved_session_count(context), active_session_count(context)),
    )
    if not valid_sessions:
        return ConversationHandler.END

    flow["sessions"] = valid_sessions

    await safe_edit_message(
        query,
        "Reusing your previous sessions. What are you reporting?",
        reply_markup=target_kind_keyboard(),
    )
    return TARGET_KIND


async def handle_status_chip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Live status indicators — you are already in the dark UI.", show_alert=False)


async def handle_session_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    profile = profile_state(context)
    profile["saved_sessions"] = await data_store.get_sessions()
    flow = flow_state(context)

    if "api_id" not in flow and profile.get("api_id"):
        flow["api_id"] = profile.get("api_id")
    if "api_hash" not in flow and profile.get("api_hash"):
        flow["api_hash"] = profile.get("api_hash")

    if query.data == "session_mode:reuse":
        saved_sessions = profile.get("saved_sessions", [])
        if not saved_sessions:
            await safe_edit_message(
                query,
                friendly_error("No saved sessions available. Please add new sessions to continue."),
                reply_markup=main_menu_keyboard(len(saved_sessions), active_session_count(context)),
            )
            return ConversationHandler.END

        flow["sessions"] = list(saved_sessions)
        valid_sessions = await _validate_sessions_with_feedback(
            update,
            context,
            flow["sessions"],
            api_id=flow.get("api_id") or profile.get("api_id") or config.API_ID,
            api_hash=flow.get("api_hash") or profile.get("api_hash") or config.API_HASH,
            fallback_markup=main_menu_keyboard(len(saved_sessions), active_session_count(context)),
        )
        if not valid_sessions:
            return ConversationHandler.END

        flow["sessions"] = valid_sessions
        profile["saved_sessions"] = list(valid_sessions)
        session_preview = _format_sessions_for_copy(valid_sessions)
        await safe_edit_message(
            query,
            f"Using your saved sessions:\n\n{session_preview}\n\nWhat are you reporting?",
            reply_markup=target_kind_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return TARGET_KIND

    flow["sessions"] = []
    await safe_edit_message(
        query,
        f"Send between {MIN_SESSIONS} and {MAX_SESSIONS} Pyrogram session strings (one per line).",
    )
    return REPORT_SESSIONS


async def start_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = profile_state(context)
    flow = reset_flow_state(context)

    profile["saved_sessions"] = await data_store.get_sessions()

    saved_api_id = profile.get("api_id") or config.API_ID
    saved_api_hash = profile.get("api_hash") or config.API_HASH

    if saved_api_id and saved_api_hash:
        flow["api_id"] = saved_api_id
        flow["api_hash"] = saved_api_hash
        profile["api_id"] = saved_api_id
        profile["api_hash"] = saved_api_hash
        await update.effective_message.reply_text(
            "Using your saved API credentials. Select a session mode to continue.",
            reply_markup=session_mode_keyboard(),
        )
        return SESSION_MODE

    await update.effective_message.reply_text("Enter your API ID (integer).")
    return API_ID_STATE


async def handle_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("Please provide a valid integer API ID.")
        return API_ID_STATE

    api_id = int(text)
    flow_state(context)["api_id"] = api_id
    profile_state(context)["api_id"] = api_id

    await update.effective_message.reply_text("Enter your API Hash (keep it secret).")
    return API_HASH_STATE


async def handle_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    api_hash = (update.message.text or "").strip()
    if len(api_hash) < 10:
        await update.effective_message.reply_text("API Hash seems too short. Please re-enter it.")
        return API_HASH_STATE

    flow_state(context)["api_hash"] = api_hash
    profile_state(context)["api_hash"] = api_hash

    await update.effective_message.reply_text(
        f"Send between {MIN_SESSIONS} and {MAX_SESSIONS} Pyrogram session strings (one per line), or type 'use saved' to reuse stored ones."
    )
    return REPORT_SESSIONS


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_text = (update.message.text or "").strip()
    profile = profile_state(context)

    if raw_text.lower() in {"use saved", "use_saved"}:
        saved_sessions = profile.get("saved_sessions", [])
        if not saved_sessions:
            await update.effective_message.reply_text(
                friendly_error("No saved sessions available. Please enter new sessions."),
                reply_markup=main_menu_keyboard(len(saved_sessions), active_session_count(context)),
            )
            return ConversationHandler.END

        flow = flow_state(context)
        flow["sessions"] = list(saved_sessions)

        valid_sessions = await _validate_sessions_with_feedback(
            update,
            context,
            flow["sessions"],
            api_id=flow.get("api_id") or profile.get("api_id") or config.API_ID,
            api_hash=flow.get("api_hash") or profile.get("api_hash") or config.API_HASH,
            fallback_markup=main_menu_keyboard(len(saved_sessions), active_session_count(context)),
        )
        if not valid_sessions:
            return ConversationHandler.END

        flow["sessions"] = valid_sessions
        session_preview = _format_sessions_for_copy(valid_sessions)
        await update.effective_message.reply_text(
            f"Using your saved sessions:\n\n{session_preview}\n\nWhat are you reporting?",
            reply_markup=target_kind_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return TARGET_KIND

    sessions = session_strings_from_text(raw_text)
    if not (MIN_SESSIONS <= len(sessions) <= MAX_SESSIONS):
        await update.effective_message.reply_text(
            friendly_error(f"Please provide between {MIN_SESSIONS} and {MAX_SESSIONS} sessions."),
            reply_markup=main_menu_keyboard(saved_session_count(context), active_session_count(context)),
        )
        return REPORT_SESSIONS

    flow = flow_state(context)

    valid_sessions = await _validate_sessions_with_feedback(
        update,
        context,
        sessions,
        api_id=flow.get("api_id") or profile.get("api_id") or config.API_ID,
        api_hash=flow.get("api_hash") or profile.get("api_hash") or config.API_HASH,
        fallback_markup=main_menu_keyboard(saved_session_count(context), active_session_count(context)),
    )

    if not valid_sessions:
        return REPORT_SESSIONS

    added = await data_store.add_sessions(
        valid_sessions, added_by=update.effective_user.id if update.effective_user else None
    )
    if added:
        profile["saved_sessions"] = list({*(profile.get("saved_sessions") or []), *valid_sessions})

    flow["sessions"] = valid_sessions
    await update.effective_message.reply_text("What are you reporting?", reply_markup=target_kind_keyboard())
    return TARGET_KIND


async def handle_target_kind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not await _ensure_active_session(query, context):
        return ConversationHandler.END

    if query.data == "kind:private":
        await safe_edit_message(
            query,
            "Send the private invite link (https://t.me/+code)",
            reply_markup=navigation_keyboard(show_back=False),
        )
        return PRIVATE_INVITE

    if query.data == "kind:public":
        await safe_edit_message(
            query,
            "Send the public message link (https://t.me/username/1234)",
            reply_markup=navigation_keyboard(show_back=False),
        )
        return PUBLIC_MESSAGE

    await safe_edit_message(query, "Send the story URL or username.", reply_markup=navigation_keyboard(show_back=False))
    return STORY_URL


async def handle_private_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    parsed_link = await _join_and_report(update, context, text)
    if not parsed_link:
        return PRIVATE_INVITE

    await update.effective_message.reply_text(
        "Joined successfully. Now send the private message link (https://t.me/c/123456789/45)",
        reply_markup=navigation_keyboard(),
    )
    return PRIVATE_MESSAGE


async def handle_private_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        parsed = parse_telegram_url(text)
    except Exception:
        await update.effective_message.reply_text(
            "Please send a valid private message link (https://t.me/c/123456789/45)",
            reply_markup=navigation_keyboard(),
        )
        return PRIVATE_MESSAGE

    if parsed.get("type") != "private_message":
        await update.effective_message.reply_text(
            "Please send a valid private message link (https://t.me/c/123456789/45)",
            reply_markup=navigation_keyboard(),
        )
        return PRIVATE_MESSAGE

    flow = flow_state(context)
    flow["targets"] = [text]
    flow["target_kind"] = "private"

    if not await _resolve_and_preview_target(update, context, text):
        return PRIVATE_MESSAGE

    await update.effective_message.reply_text(
        REASON_PROMPT, reply_markup=reason_keyboard()
    )
    return REPORT_REASON_TYPE


async def handle_public_message_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.effective_message.reply_text(
            "Send a valid public link or @username (https://t.me/username/1234)", reply_markup=navigation_keyboard()
        )
        return PUBLIC_MESSAGE

    flow = flow_state(context)
    flow["targets"] = [text]
    flow["target_kind"] = "public"

    if not await _resolve_and_preview_target(update, context, text):
        return PUBLIC_MESSAGE

    await update.effective_message.reply_text(
        REASON_PROMPT, reply_markup=reason_keyboard()
    )
    return REPORT_REASON_TYPE


async def handle_story_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.effective_message.reply_text(
            "Send a valid story URL or username.", reply_markup=navigation_keyboard()
        )
        return STORY_URL
    await update.effective_message.reply_text(
        "NOT_SUPPORTED: profile/story URLs are not supported for reporting.",
        reply_markup=navigation_keyboard(),
    )
    return ConversationHandler.END


async def handle_report_urls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    targets = parse_links(update.message.text or "")
    if not targets:
        await update.effective_message.reply_text("Please send at least one valid Telegram URL.")
        return REPORT_URLS

    flow_state(context)["targets"] = targets
    await update.effective_message.reply_text(
        REASON_PROMPT, reply_markup=reason_keyboard()
    )
    return REPORT_REASON_TYPE


async def handle_reason_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not await _ensure_active_session(query, context):
        return ConversationHandler.END
    reason_code = int(query.data.split(":")[1])
    flow_state(context)["reason_code"] = reason_code

    await safe_edit_message(query, "Send a short reason for reporting (up to 5 lines).")
    return REPORT_MESSAGE


async def handle_reason_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reasons = parse_reasons(update.message.text or "")
    if not reasons:
        await update.effective_message.reply_text("Please send at least one reason.")
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
        f"Report type: {_reason_label(flow.get('reason_code'))}\n"
        f"Total reports each: {flow.get('count')}\n"
        f"Session count: {len(flow.get('sessions', []))}"
    )

    await update.effective_message.reply_text(
        f"Confirm the report run?\n\n{summary}",
        reply_markup=add_restart_button(
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Start", callback_data="confirm:start")],
                    [InlineKeyboardButton("Cancel", callback_data="confirm:cancel")],
                ]
            )
        ),
    )
    return ConversationHandler.WAITING


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not await _ensure_active_session(query, context):
        return ConversationHandler.END
    if query.data == "confirm:cancel":
        await safe_edit_message(query, "Canceled. Use /report to start over.")
        return ConversationHandler.END

    await safe_edit_message(query, "Reporting has started. I'll send updates when done.")

    job_data = deepcopy(flow_state(context))

    context.user_data["last_report_config"] = deepcopy(job_data)

    task = context.application.create_task(run_report_job(query, context, job_data))
    context.user_data["active_report_task"] = task
    clear_report_state(context)
    return ConversationHandler.END


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
        card = render_card("Unexpected error", ["Something went wrong. Please try again later."], [])
        await update.effective_message.reply_text(
            f"<pre>{card}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=add_restart_button(None),
        )


__all__ = [
    "start",
    "help_command",
    "show_sessions",
    "handle_action_buttons",
    "handle_status_chip",
    "handle_session_mode",
    "handle_report_again",
    "start_report",
    "handle_api_id",
    "handle_api_hash",
    "handle_sessions",
    "handle_target_kind",
    "handle_navigation",
    "handle_private_invite",
    "handle_private_message_link",
    "handle_public_message_link",
    "handle_story_url",
    "handle_report_urls",
    "handle_reason_type",
    "handle_reason_message",
    "handle_report_count",
    "handle_confirmation",
    "handle_add_sessions",
    "receive_added_sessions",
    "cancel",
    "error_handler",
    "uptime_command",
    "ping_command",
    "restart_command",
    "restart_callback",
]
