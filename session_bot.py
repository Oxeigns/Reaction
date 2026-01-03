from __future__ import annotations

"""Pyrogram entrypoint implementing the owner/sudo workflows."""

import contextlib
import logging
import traceback
from dataclasses import dataclass, field
from time import monotonic
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from bot.config_store import build_config_store
from bot.report_queue import ReportQueue
from bot.utils import session_strings_from_text, validate_sessions
from report import send_report


logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")

app = Client("reaction-bot", bot_token=config.BOT_TOKEN, api_id=config.API_ID, api_hash=config.API_HASH)
config_store, datastore = build_config_store(config.MONGO_URI)
queue = ReportQueue()


def safe_handler(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Handler error")
            await _send_log(f"⚠️ Error detected:\n{exc}\n{traceback.format_exc()}")

    return wrapper


@dataclass
class ReportState:
    stage: str = "idle"
    report_type: Optional[str] = None
    invite_link: Optional[str] = None
    target_link: Optional[str] = None
    reason: Optional[str] = None
    started_at: float = field(default_factory=monotonic)


user_states: dict[int, ReportState] = {}


def is_owner(user_id: int | None) -> bool:
    return bool(user_id and config.OWNER_ID and user_id == config.OWNER_ID)


def is_sudo(user_id: int | None) -> bool:
    if is_owner(user_id):
        return True
    return bool(user_id and user_id in config.SUDO_USERS)


def _control_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Manage Sessions", callback_data="owner:manage")],
            [InlineKeyboardButton("Set Session Group", callback_data="owner:set_session_group")],
            [InlineKeyboardButton("Set Logs Group", callback_data="owner:set_logs_group")],
        ]
    )


def _sudo_panel(live_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Start Report", callback_data="sudo:start_report")]])


async def _send_log(text: str, *, parse_mode: str | None = None) -> None:
    chat_id = await config_store.logs_group()
    if not chat_id:
        return
    try:
        await app.send_message(chat_id, text, parse_mode=parse_mode)
    except Exception:
        logging.exception("Failed to send log message")


async def _prune_sessions() -> list[str]:
    sessions = await datastore.get_sessions()
    if not sessions or not (config.API_ID and config.API_HASH):
        return []

    valid, invalid = await validate_sessions(config.API_ID, config.API_HASH, sessions)
    if invalid:
        await datastore.remove_sessions(invalid)
    return valid


async def _ensure_admin(chat_id: int) -> bool:
    try:
        member = await app.get_chat_member(chat_id, (await app.get_me()).id)
        return bool(member and getattr(member, "status", "") in {"administrator", "creator"})
    except Exception:
        return False


@app.on_message(filters.command("start"))
@safe_handler
async def start_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_sudo(user_id):
        await message.reply_text("You are unauthorised. Take sudo from owner to use this bot.")
        return

    valid_sessions = await _prune_sessions()
    await config_store.add_known_chat(message.chat.id)

    if is_owner(user_id):
        await message.reply_text("Owner control panel", reply_markup=_control_panel())
        return

    await _log_new_user(message)
    await message.reply_text(
        f"Live sessions: {len(valid_sessions)}", reply_markup=_sudo_panel(len(valid_sessions))
    )


async def _log_new_user(message: Message) -> None:
    if not message.from_user:
        return
    if is_owner(message.from_user.id):
        return
    await _send_log(
        "🔔 New user started bot:\n"
        f"👤 User: [{message.from_user.first_name}](tg://user?id={message.from_user.id})\n"
        f"🆔 ID: {message.from_user.id}",
        parse_mode="markdown",
    )


def _type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Public", callback_data="report:type:public")],
            [InlineKeyboardButton("Private", callback_data="report:type:private")],
        ]
    )


async def _prompt_report_type(message: Message) -> None:
    await message.reply_text("Select report type", reply_markup=_type_keyboard())


@app.on_callback_query(filters.regex("^sudo:start_report$"))
@safe_handler
async def sudo_start_report(_: Client, query: CallbackQuery) -> None:
    user_id = query.from_user.id if query.from_user else 0
    message = query.message
    if not message:
        return

    if queue.is_running(user_id):
        await query.answer("Another report is in progress. Your request is queued. Please wait...", show_alert=True)

    async def _job():
        await _handle_start_report(query, message)

    await queue.enqueue(user_id, _job)


@safe_handler
async def _handle_start_report(query: CallbackQuery, message: Message) -> None:
    await query.answer()
    checking = await message.reply_text("Please wait, checking live sessions...")
    valid_sessions = await _prune_sessions()
    await checking.edit_text(f"Live sessions available: {len(valid_sessions)}")
    if not valid_sessions:
        await message.reply_text("No valid sessions available. Contact the owner to add more.")
        return

    state = user_states.setdefault(query.from_user.id, ReportState())
    state.stage = "type"
    state.report_type = None
    state.invite_link = None
    state.target_link = None
    state.reason = None
    await _prompt_report_type(message)


@app.on_callback_query(filters.regex("^report:type:(public|private)$"))
@safe_handler
async def choose_type(_: Client, query: CallbackQuery) -> None:
    if not is_sudo(query.from_user.id if query.from_user else None):
        await query.answer("Unauthorised", show_alert=True)
        return

    state = user_states.setdefault(query.from_user.id, ReportState())
    state.report_type = query.data.split(":")[-1]
    state.stage = "invite" if state.report_type == "private" else "link"
    await query.answer()
    prompt = (
        "Send private invite link first (https://t.me/+code)." if state.report_type == "private" else "Send the public message link."
    )
    await query.message.reply_text(prompt)


@app.on_message(filters.text & ~filters.command(["start", "broadcast", "set_session", "set_log"]))
@safe_handler
async def text_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_sudo(user_id):
        return

    state = user_states.setdefault(user_id, ReportState())
    if state.stage == "invite":
        state.invite_link = message.text.strip()
        state.stage = "link"
        await message.reply_text("Invite saved. Now send the target message link.")
        return

    if state.stage == "link":
        state.target_link = message.text.strip()
        state.stage = "reason"
        await message.reply_text("Provide a short report reason.")
        return

    if state.stage == "reason":
        state.reason = message.text.strip()
        await _begin_report(message, state)
        return

    await message.reply_text("Use Start Report to begin a new report.")


async def _begin_report(message: Message, state: ReportState) -> None:
    sessions = await _prune_sessions()
    if not sessions:
        await message.reply_text("No valid sessions available.")
        return

    state.stage = "running"
    state.started_at = monotonic()
    await message.reply_text("Report started. Executing sequentially…")
    await queue.enqueue(message.from_user.id, lambda: _run_report_job(message, state, sessions))


@safe_handler
async def _run_report_job(message: Message, state: ReportState, sessions: list[str]) -> None:
    try:
        success = await _perform_report(state, sessions)
        elapsed = monotonic() - state.started_at
        status = "Success" if success else "Failed"
        await message.reply_text(f"Report completed. Status: {status}")
        await _send_log(
            "📄 Report Summary\n"
            f"👤 User: [{message.from_user.first_name}](tg://user?id={message.from_user.id})\n"
            f"🔗 Link: {state.target_link}\n"
            f"⏱️ Time Taken: {int(elapsed)}s\n"
            f"✅ Status: {status if success else '❌ Failed'}",
            parse_mode="markdown",
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Report failed")
        await message.reply_text("Report failed due to an unexpected error.")
        await _send_log(f"⚠️ Error detected:\n{exc}\n{traceback.format_exc()}")
    finally:
        user_states[message.from_user.id] = ReportState()


async def _perform_report(state: ReportState, sessions: list[str]) -> bool:
    if not state.target_link:
        return False

    chat_ref: str | int
    message_id: int
    try:
        if state.report_type == "private":
            chat_ref, message_id = _parse_private_link(state.target_link)
        else:
            chat_ref, message_id = _parse_public_link(state.target_link)
    except Exception:
        return False

    if state.report_type == "private" and state.invite_link:
        # best-effort join with first session
        await _join_with_session(sessions[0], state.invite_link)

    for idx, session in enumerate(sessions):
        client = Client(
            name=f"report_{idx}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session,
            workdir=f"/tmp/report_{idx}",
        )
        try:
            await client.start()
            await send_report(client, chat_ref, message_id, 0, state.reason or "Report")
        except Exception:
            continue
        finally:
            with contextlib.suppress(Exception):
                await client.stop()
    return True


async def _join_with_session(session: str, invite_link: str) -> None:
    client = Client(
        name="joiner",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=session,
        workdir="/tmp/joiner",
    )
    try:
        await client.start()
        await client.join_chat(invite_link)
    finally:
        with contextlib.suppress(Exception):
            await client.stop()


def _parse_public_link(link: str) -> tuple[str | int, int]:
    cleaned = link.replace("https://t.me/", "").strip("/")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Invalid link")

    if parts[0] == "c" and len(parts) >= 3:
        chat_id = int(f"-100{parts[1]}")
        message_id = int(parts[2])
    else:
        chat_id = parts[0].lstrip("@")
        message_id = int(parts[1])
    return chat_id, message_id


def _parse_private_link(link: str) -> tuple[int, int]:
    cleaned = link.replace("https://t.me/c/", "")
    parts = cleaned.split("/")
    if len(parts) < 2:
        raise ValueError("Invalid link")
    chat_id = int(f"-100{parts[0]}")
    return chat_id, int(parts[1])


@app.on_message(filters.command("set_session"))
@safe_handler
async def set_session_group(_: Client, message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    if not await _ensure_admin(message.chat.id):
        await message.reply_text("Make me admin in this group first.")
        return
    await config_store.set_session_group(message.chat.id)
    await message.reply_text("✅ Session group set successfully")


@app.on_message(filters.command("set_log"))
@safe_handler
async def set_log_group(_: Client, message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return
    if not await _ensure_admin(message.chat.id):
        await message.reply_text("Make me admin in this group first.")
        return
    await config_store.set_logs_group(message.chat.id)
    await message.reply_text("✅ Logs group set successfully")


@app.on_message(filters.command("broadcast"))
@safe_handler
async def broadcast(_: Client, message: Message) -> None:
    if not is_owner(message.from_user.id if message.from_user else None):
        return

    logs_group = await config_store.logs_group()
    if not logs_group or message.chat.id != logs_group:
        return

    payload = message.text.split(" ", 1)
    if len(payload) < 2:
        await message.reply_text("Usage: /broadcast <message>")
        return

    text = payload[1]
    chats = await config_store.known_chats()
    user_count = 0
    group_count = 0
    start = monotonic()
    for chat_id in chats:
        try:
            await app.send_message(chat_id, text)
            if chat_id > 0:
                user_count += 1
            else:
                group_count += 1
        except Exception:
            continue
    elapsed = int(monotonic() - start)
    await _send_log(
        "📢 Broadcast Sent\n"
        f"🧑 Users: {user_count}\n"
        f"👥 Groups: {group_count}\n"
        f"⏱️ Time: {elapsed}s"
    )


@app.on_message(filters.group & filters.text)
@safe_handler
async def session_ingestion(_: Client, message: Message) -> None:
    session_group = await config_store.session_group()
    if not session_group or message.chat.id != session_group:
        return
    if not is_owner(message.from_user.id if message.from_user else None):
        return

    sessions = session_strings_from_text(message.text or "")
    if not sessions:
        return

    valid, invalid = await validate_sessions(config.API_ID, config.API_HASH, sessions)
    if not valid:
        await message.reply_text("❌ Invalid session string")
        return

    await datastore.add_sessions(valid, added_by=message.from_user.id if message.from_user else None)
    await message.reply_text("✅ Session added successfully")
    if invalid:
        await message.reply_text("❌ Invalid session string")


@app.on_callback_query(filters.regex("^owner:(manage|set_session_group|set_logs_group)$"))
@safe_handler
async def owner_actions(_: Client, query: CallbackQuery) -> None:
    if not is_owner(query.from_user.id if query.from_user else None):
        await query.answer("Owner only", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":")[-1]
    if action == "manage":
        sessions = await _prune_sessions()
        lines = [f"Total valid sessions: {len(sessions)}"]
        if sessions:
            lines.append("Sessions are added via the configured session group.")
        await query.message.reply_text("\n".join(lines))
    elif action == "set_session_group":
        await query.message.reply_text(
            "Make bot admin in session group and send /set_session command here"
        )
    elif action == "set_logs_group":
        await query.message.reply_text("Make bot admin in logs group and send /set_log command here")


if __name__ == "__main__":
    app.run()
