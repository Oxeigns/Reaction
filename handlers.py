from __future__ import annotations

"""Command and callback handlers for the reporting bot."""

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from datetime import datetime
from io import BytesIO
from time import monotonic
from typing import Callable, Tuple

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError, UserAlreadyParticipant
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from logging_utils import log_error, log_report_summary, log_user_start, send_log
from report import send_report
from session_bot import (
    SessionIdentity,
    extract_sessions_from_text,
    fetch_session_identity,
    prune_sessions,
    validate_session_string,
)
from state import QueueEntry, ReportQueue, StateManager
from sudo import is_owner
from ui import (
    REPORT_REASONS,
    owner_panel,
    queued_message,
    reason_keyboard,
    report_type_keyboard,
    sudo_panel,
)
from bot.utils import resolve_chat_id


def _normalize_chat_id(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def register_handlers(app: Client, persistence, states: StateManager, queue: ReportQueue) -> None:
    """Register all command and callback handlers."""

    async def _ensure_admin(chat_id: int) -> bool:
        try:
            me = await app.get_me()
            member = await app.get_chat_member(chat_id, me.id)
            status = getattr(member, "status", "")
            return status in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
                "administrator",
                "creator",
            }
        except Exception:
            return False

    async def _wrap_errors(func: Callable, *args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Handler error")
            await log_error(app, await persistence.get_logs_group_id(), exc, config.OWNER_ID)

    async def _queue_error(exc: Exception) -> None:
        await log_error(app, await persistence.get_logs_group_id(), exc, config.OWNER_ID)

    queue.set_error_handler(_queue_error)
    session_tokens: dict[str, str] = {}

    async def _log_stage(stage: str, detail: str) -> None:
        await send_log(
            app,
            await persistence.get_logs_group_id(),
            f"🛰 {stage}\n{detail}",
        )

    async def _sessions_available() -> list[str]:
        sessions = await prune_sessions(persistence, announce=True)
        return sessions

    async def _resolve_target_across_sessions(
        target_link: str, sessions: list[str]
    ) -> tuple[int | None, list[str], str | None]:
        """Resolve the target chat id using any available session string.

        This retries across every validated session so private/public links and
        message links can be resolved even when some sessions are missing
        access. Returns the resolved chat id, a list of sessions that could
        access the target, or ``None`` plus the last error message for user
        feedback.
        """

        last_error: str | None = None
        available_sessions: list[str] = []
        resolved_chat_id: int | None = None

        for idx, session in enumerate(sessions):
            client = Client(
                name=f"resolver_{idx}",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=session,
                workdir=f"/tmp/resolver_{idx}",
            )

            try:
                await client.start()
                try:
                    chat_id = await resolve_chat_id(client, target_link)
                    available_sessions.append(session)
                    resolved_chat_id = chat_id
                except Exception as exc:  # noqa: BLE001 - need detailed errors
                    last_error = str(exc)
            finally:
                with contextlib.suppress(Exception):
                    await client.stop()

        if resolved_chat_id is None:
            return None, [], last_error

        return resolved_chat_id, available_sessions, None

    async def _prompt_report_count(message: Message) -> None:
        await message.reply_text(
            (
                "How many reports do you want to send? "
                f"Send a number between {config.MIN_REPORTS} and {config.MAX_REPORTS}."
            )
        )

    async def _apply_report_count(message: Message, state, count: int) -> None:
        if count < config.MIN_REPORTS or count > config.MAX_REPORTS:
            await message.reply_text(
                f"Please choose a value between {config.MIN_REPORTS} and {config.MAX_REPORTS}."
            )
            return

        state.report_count = count
        await message.reply_text(f"✅ Will send {count} reports.")

        next_stage = state.next_stage_after_count or "awaiting_link"
        state.next_stage_after_count = None
        if next_stage == "awaiting_private_join":
            state.stage = "awaiting_private_join"
            await message.reply_text(
                "Send the private group/channel invite link or username so I can join with all sessions."
            )
            return
        if next_stage == "awaiting_link":
            state.stage = "awaiting_link"
            await message.reply_text(
                "Send the target message link (https://t.me/...) to report."
            )
            return
        if next_stage == "begin_report":
            await _begin_report(message, state)
            return

        state.stage = next_stage

    async def _is_sudo_user(user_id: int | None) -> bool:
        if user_id is None:
            return False
        if is_owner(user_id):
            return True
        sudo_users = await persistence.get_sudo_users()
        allowed = sudo_users or set(config.SUDO_USERS)
        return user_id in allowed

    async def _owner_guard(message: Message) -> bool:
        if not message.from_user or not is_owner(message.from_user.id):
            await message.reply_text("Only the owner can manage sudo users.")
            return False
        return True

    @app.on_message(filters.command("start"))
    async def start_handler(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_start, message)

    async def _handle_start(message: Message) -> None:
        if not message.from_user:
            return

        user_id = message.from_user.id
        await persistence.add_known_chat(message.chat.id)
        await log_user_start(app, await persistence.get_logs_group_id(), message)

        if is_owner(user_id):
            await message.reply_text(
                "Welcome, Owner! Choose an action below.", reply_markup=owner_panel()
            )
            await _log_stage("Owner Start", "Owner opened start panel")
            return

        if await _is_sudo_user(user_id):
            await message.reply_text(
                "👋 Ready to report?", reply_markup=sudo_panel(message.from_user.id)
            )
            await _log_stage("Sudo Start", f"Sudo {user_id} opened start panel")
            return

        await message.reply_text(
            "🚫 You are not authorized to use this bot.\n"
            f"Contact the owner (ID: {config.OWNER_ID}) to request access."
        )
        await _log_stage("Unauthorized", f"User {user_id} attempted /start")

    @app.on_message(filters.command("addsudo"))
    async def add_sudo(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_add_sudo, message)

    async def _handle_add_sudo(message: Message) -> None:
        if not await _owner_guard(message):
            return

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply_text("Usage: /addsudo <user_id> [username]")
            return

        user_id = int(parts[1])
        if is_owner(user_id):
            await message.reply_text("Owner already has access.")
            return
        sudo_users = await persistence.get_sudo_users()
        if user_id in sudo_users:
            await message.reply_text("User is already a sudo user.")
            return

        await persistence.add_sudo_user(user_id)
        label = parts[2] if len(parts) > 2 else str(user_id)
        await message.reply_text(f"Added {label} ({user_id}) to sudo users.")
        await _log_stage("Sudo Added", f"Owner added {user_id}")

    async def _handle_sudo_list(message: Message) -> None:
        if not await _owner_guard(message):
            return
        sudo_users = await persistence.get_sudo_users()
        if not sudo_users:
            await message.reply_text("No sudo users are configured.")
            return
        formatted = "\n".join([f"• `{uid}`" for uid in sorted(sudo_users)])
        await message.reply_text(f"Current sudo users:\n{formatted}", parse_mode="markdown")

    @app.on_message(filters.command("rmsudo"))
    async def remove_sudo(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_remove_sudo, message)

    @app.on_message(filters.command("sudolist"))
    async def sudo_list(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_sudo_list, message)

    @app.on_message(filters.command("set_session") & filters.group)
    async def set_session_group(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_set_session_group, message)

    @app.on_message(filters.command("set_log") & filters.group)
    async def set_logs_group(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_set_logs_group, message)

    @app.on_message(filters.command("broadcast"))
    async def broadcast(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_broadcast, message)

    @app.on_message((filters.group) & (filters.text | filters.document))
    async def session_ingest(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_session_ingest, message)

    async def _handle_remove_sudo(message: Message) -> None:
        if not await _owner_guard(message):
            return

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply_text("Usage: /rmsudo <user_id>")
            return

        user_id = int(parts[1])
        sudo_users = await persistence.get_sudo_users()
        if user_id not in sudo_users:
            await message.reply_text("User is not in the sudo list.")
            return

        await persistence.remove_sudo_user(user_id)
        await message.reply_text(f"Removed {user_id} from sudo users.")
        await _log_stage("Sudo Removed", f"Owner removed {user_id}")

    async def _handle_set_session_group(message: Message) -> None:
        if not await _owner_guard(message):
            return
        if not await _ensure_admin(message.chat.id):
            await message.reply_text("Please promote the bot to admin before setting this group.")
            return
        await persistence.save_session_group_id(message.chat.id)
        await message.reply_text(
            "✅ This group is now the session manager. Send session strings here to ingest them."
        )
        await _log_stage("Session Group Set", f"Owner set session group to {message.chat.id}")

    async def _handle_set_logs_group(message: Message) -> None:
        if not await _owner_guard(message):
            return
        if not await _ensure_admin(message.chat.id):
            await message.reply_text("Please promote the bot to admin before setting this group.")
            return
        await persistence.save_logs_group_id(message.chat.id)
        await message.reply_text("📝 Logs will now be sent to this group.")
        await _log_stage("Logs Group Set", f"Owner set logs group to {message.chat.id}")

    async def _handle_broadcast(message: Message) -> None:
        logs_group = await persistence.get_logs_group_id()
        if message.chat.id != logs_group:
            await message.reply_text("Broadcasts can only be sent from the logs group.")
            return
        if not await _is_sudo_user(getattr(message.from_user, "id", None)):
            await message.reply_text("You are not allowed to broadcast.")
            return

        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /broadcast <message>")
            return
        payload = parts[1]
        targets = await persistence.known_chats()
        success = 0
        failed = 0
        for chat_id in targets:
            try:
                await app.send_message(chat_id, payload)
                success += 1
            except Exception:
                failed += 1
        await message.reply_text(f"Broadcast sent. Success: {success}, Failed: {failed}")
        await _log_stage(
            "Broadcast",
            f"Broadcast from {message.from_user.id if message.from_user else 'unknown'} -> {success} ok / {failed} failed",
        )

    async def _handle_session_ingest(message: Message) -> None:
        session_group = await persistence.get_session_group_id()
        if not session_group or message.chat.id != session_group:
            return
        if not message.from_user or not is_owner(message.from_user.id):
            return

        text_parts = []
        if message.text:
            text_parts.append(message.text)
        if message.caption:
            text_parts.append(message.caption)

        if message.document:
            try:
                data = await message.download(in_memory=True)
                if isinstance(data, BytesIO):
                    data.seek(0)
                    text_parts.append(data.read().decode("utf-8", errors="ignore"))
            except Exception:
                await message.reply_text("Unable to read the document. Please send the session strings as text.")

        raw_text = "\n".join(filter(None, text_parts))
        sessions = list({s for s in extract_sessions_from_text(raw_text) if s})
        if not sessions:
            await message.reply_text("No session strings detected in this message.")
            return

        valid: list[str] = []
        invalid: list[str] = []
        for session in sessions:
            if await validate_session_string(session):
                valid.append(session)
            else:
                invalid.append(session)

        added = await persistence.add_sessions(valid, added_by=message.from_user.id) if valid else []
        total_saved = len(await persistence.get_sessions())
        summary = [f"Validated sessions: {len(valid)}"]
        if added:
            summary.append(f"Saved new sessions: {len(added)}")
        if invalid:
            summary.append(f"Invalid sessions: {len(invalid)}")
            await message.reply_text("Some session strings were invalid and were not saved.")

        await message.reply_text("\n".join(summary))
        await _log_stage(
            "Session Ingest",
            f"Owner saved {len(added)} sessions ({len(valid)} valid / {len(invalid)} invalid). Total stored: {total_saved}",
        )

    @app.on_callback_query(filters.regex(r"^sudo:start$"))
    async def start_report(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_start_report, query)

    @app.on_callback_query(filters.regex(r"^owner:manage$"))
    async def manage_sessions(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_owner_manage, query)

    async def _render_session_detail_rows(sessions: list[str]) -> tuple[str, InlineKeyboardMarkup]:
        session_tokens.clear()
        lines: list[str] = []
        buttons: list[list[InlineKeyboardButton]] = []
        for idx, session in enumerate(sessions, start=1):
            identity: SessionIdentity | None = await fetch_session_identity(session)
            name = identity.name if identity else "Unknown"
            username = identity.username if identity else None
            phone = identity.phone_number if identity else None
            parts = [f"{idx}. {name}"]
            if username:
                parts.append(f"@{username}")
            if phone:
                parts.append(phone)
            lines.append(" | ".join(parts))

            token = uuid.uuid4().hex[:12]
            session_tokens[token] = session
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"❌ Remove {idx}", callback_data=f"owner:remove:{token}"
                    )
                ]
            )

        if not lines:
            lines.append("No valid sessions found after validation.")

        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="owner:manage")])
        keyboard = InlineKeyboardMarkup(buttons)
        return "\n".join(lines), keyboard

    @app.on_callback_query(filters.regex(r"^owner:set_session_group$"))
    async def owner_session_hint(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_owner_session_hint, query)

    @app.on_callback_query(filters.regex(r"^owner:set_logs_group$"))
    async def owner_logs_hint(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_owner_logs_hint, query)

    async def _handle_start_report(query: CallbackQuery) -> None:
        if not query.message or not query.from_user:
            return
        if not await _is_sudo_user(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return

        checking = await query.message.reply_text("🔎 Validating sessions, please wait...")
        live_sessions = await _sessions_available()
        if not live_sessions:
            if is_owner(query.from_user.id):
                await checking.edit_text(
                    "No sessions found. Please send session strings in the configured session manager group first."
                )
            else:
                await checking.edit_text("No sessions found. Please contact the bot owner.")
            return

        await _log_stage(
            "Start Report", f"User {query.from_user.id} checking in with {len(live_sessions)} sessions"
        )

        if queue.is_busy() and queue.active_user != query.from_user.id:
            position = queue.expected_position(query.from_user.id)
            notice = queued_message(position)
            if notice:
                await query.message.reply_text(notice)

        await _log_stage("Report Queue", f"User {query.from_user.id} position set")

        state = states.get(query.from_user.id)
        state.reset()
        state.stage = "type"
        await checking.edit_text(f"✅ Live sessions loaded: {len(live_sessions)}")
        await query.message.reply_text("Choose report visibility", reply_markup=report_type_keyboard())
        await query.answer()

    async def _handle_owner_manage(query: CallbackQuery) -> None:
        if not query.from_user or not is_owner(query.from_user.id):
            await query.answer("Owner only", show_alert=True)
            return
        checking = await query.message.reply_text("🔎 Checking saved sessions...")
        sessions = await _sessions_available()
        detail_text, keyboard = await _render_session_detail_rows(sessions)
        await checking.edit_text(
            f"Currently stored sessions: {len(sessions)}\n\n{detail_text}",
            reply_markup=keyboard,
        )
        await _log_stage("Owner Manage", f"Owner checked sessions ({len(sessions)})")
        await query.answer()

    @app.on_callback_query(filters.regex(r"^owner:remove:(?P<token>[A-Za-z0-9]+)$"))
    async def owner_remove_session(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_owner_remove_session, query)

    async def _handle_owner_remove_session(query: CallbackQuery) -> None:
        if not query.from_user or not is_owner(query.from_user.id):
            await query.answer("Owner only", show_alert=True)
            return

        token = query.matches[0].group("token") if query.matches else None
        session = session_tokens.get(token or "")
        if not session:
            await query.answer("Session mapping expired. Refresh the list.", show_alert=True)
            return

        removed = await persistence.remove_sessions([session])
        session_tokens.pop(token, None)
        if removed:
            await query.answer("Session removed", show_alert=True)
            await query.message.reply_text("✅ Session removed from storage.")
            remaining = len(await persistence.get_sessions())
            await _log_stage(
                "Session Removed",
                f"Owner removed a session. Remaining: {remaining}",
            )
        else:
            await query.answer("Session not found", show_alert=True)

    async def _handle_owner_session_hint(query: CallbackQuery) -> None:
        if not query.from_user or not is_owner(query.from_user.id):
            await query.answer("Owner only", show_alert=True)
            return
        await query.message.reply_text(
            "Send /set_session in the target group where you'll drop session strings."
        )
        await query.answer()

    async def _handle_owner_logs_hint(query: CallbackQuery) -> None:
        if not query.from_user or not is_owner(query.from_user.id):
            await query.answer("Owner only", show_alert=True)
            return
        await query.message.reply_text("Send /set_log in the logs group to start receiving updates.")
        await query.answer()

    @app.on_callback_query(filters.regex(r"^report:type:(public|private)$"))
    async def choose_type(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_type, query)

    async def _handle_type(query: CallbackQuery) -> None:
        if not query.from_user:
            return
        if not await _is_sudo_user(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return
        state = states.get(query.from_user.id)
        if state.stage not in {"type", "idle"}:
            await query.answer()
            return
        state.report_type = query.data.split(":")[-1]
        state.next_stage_after_count = (
            "awaiting_private_join" if state.report_type == "private" else "awaiting_link"
        )
        state.stage = "awaiting_count"
        await _prompt_report_count(query.message)
        await _log_stage("Report Type", f"User {query.from_user.id} chose {state.report_type}")
        await query.answer()

    @app.on_callback_query(filters.regex(r"^report:reason:[a-z_]+$"))
    async def choose_reason(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_reason, query)

    @app.on_callback_query(filters.regex(r"^report:count:(\d+)$"))
    async def choose_count(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_count, query)

    async def _handle_reason(query: CallbackQuery) -> None:
        if not query.from_user:
            return
        if not await _is_sudo_user(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return
        key = query.data.split(":")[-1]
        label, code = REPORT_REASONS.get(key, ("Other", 9))
        state = states.get(query.from_user.id)
        if key == "other":
            state.stage = "awaiting_reason_text"
            state.reason_code = 9
            state.reason_text = None
            state.next_stage_after_count = "begin_report"
            await query.message.reply_text("Please type the custom reason to submit with your report.")
            await query.answer()
            return

        state.reason_code = code
        state.reason_text = label
        state.next_stage_after_count = "begin_report"
        await query.answer(f"Reason set to {label}")
        await _log_stage("Report Reason", f"User {query.from_user.id} selected {label}")
        if state.report_count is None:
            state.stage = "awaiting_count"
            await _prompt_report_count(query.message)
        else:
            await _begin_report(query.message, state)

    async def _handle_count(query: CallbackQuery) -> None:
        if not query.from_user or not await _is_sudo_user(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return
        state = states.get(query.from_user.id)
        if state.stage != "awaiting_count":
            await query.answer()
            return
        try:
            count = int(query.data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Invalid selection", show_alert=True)
            return

        await _apply_report_count(query.message, state, count)
        await query.answer()

    @app.on_message(
        filters.private
        & filters.text
        & ~filters.command(["start", "broadcast", "set_session", "set_log"])
    )
    async def text_router(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_text, message)

    async def _handle_text(message: Message) -> None:
        if not message.from_user:
            return
        if not await _is_sudo_user(message.from_user.id):
            await message.reply_text("You are not authorized to use this bot.")
            await _log_stage("Unauthorized", f"User {message.from_user.id} attempted text routing")
            return

        state = states.get(message.from_user.id)

        if state.stage == "awaiting_private_join":
            invite = (message.text or "").strip()
            if not _is_valid_target(invite):
                await message.reply_text("Send a valid invite link or @username to continue.")
                return
            joined = await _join_sessions_to_chat(invite, message)
            if not joined:
                return
            state.stage = "awaiting_link"
            await message.reply_text("✅ Joined! Now send the message link to report.")
            return

        if state.stage == "awaiting_link":
            link = (message.text or "").strip()
            if not _is_valid_link(link):
                await message.reply_text("Send a valid https://t.me/ link.")
                return
            try:
                _parse_link(link, state.report_type == "private")
            except ValueError:
                detail = "private" if state.report_type == "private" else "public"
                await message.reply_text(
                    f"The link is not a valid {detail} message link. Please send a correct t.me link."
                )
                return
            state.target_link = link
            state.stage = "awaiting_reason"
            await message.reply_text("Choose a report reason", reply_markup=reason_keyboard())
            await _log_stage("Target Link", f"User {message.from_user.id} provided link {state.target_link}")
            return

        if state.stage == "awaiting_count":
            try:
                count = int(message.text.strip())
                await _apply_report_count(message, state, count)
            except ValueError:
                await message.reply_text(
                    (
                        "Please enter a valid number of reports "
                        f"between {config.MIN_REPORTS} and {config.MAX_REPORTS}."
                    )
                )
            return

        if state.stage == "awaiting_reason_text":
            state.reason_text = (message.text or "").strip()
            if not state.reason_text:
                await message.reply_text("Please type a custom reason.")
                return
            state.next_stage_after_count = "begin_report"
            await _log_stage("Custom Reason", f"User {message.from_user.id} provided custom reason")
            if state.report_count is None:
                state.stage = "awaiting_count"
                await _prompt_report_count(message)
            else:
                await _begin_report(message, state)
            return

        await message.reply_text("Use Start Report to begin a new report.")

    async def _begin_report(message: Message | None, state) -> None:
        if not message or not message.from_user:
            return
        if not state.target_link:
            await message.reply_text("Send the target link first.")
            return
        if not state.report_type:
            await message.reply_text("Choose Public or Private before proceeding.")
            return
        if state.reason_text is None:
            await message.reply_text("Please choose a reason first.")
            return

        try:
            _parse_link(state.target_link, state.report_type == "private")
        except ValueError:
            await message.reply_text("The link looks invalid. Please send a correct t.me message link.")
            return

        state.stage = "queued"
        state.started_at = monotonic()

        if queue.is_busy() and queue.active_user != message.from_user.id:
            await message.reply_text("⏳ Please wait while another report is in progress.")
            notice = queued_message(queue.expected_position(message.from_user.id))
            if notice:
                await message.reply_text(notice)
                await _log_stage("Queue Notice", f"User {message.from_user.id} queued")

        async def notify_position(position: int) -> None:
            if position > 1:
                notice = queued_message(position)
                if notice:
                    await message.reply_text(notice)
                    await _log_stage("Queue Update", f"User {message.from_user.id} moved to {position}")

        entry = QueueEntry(
            message.from_user.id,
            job=lambda: _run_report_job(message, state),
            notify_position=notify_position,
        )
        await queue.enqueue(entry)
        await _log_stage("Report Enqueued", f"User {message.from_user.id} job queued")

    async def _run_report_job(message: Message, state) -> None:
        try:
            result = await _execute_report(message, state)
            success = result["any_success"]
            elapsed = monotonic() - state.started_at
            status = "Success" if success else "❌ Failed"
            summary_lines = [
                "📊 Report attempt summary:",
                f"- Report type: {'Private' if state.report_type == 'private' else 'Public'}",
                f"- Target link: {state.target_link}",
                f"- Requested attempts: {result['requested']}",
                f"- Total attempts: {result['attempted']}",
                f"- Successful attempts: {result['success_count']}",
                f"- Failed attempts: {result['failure_count']}",
                f"- Sessions available: {result['total_sessions']}",
                f"- Time taken: {elapsed:.1f}s",
            ]
            await message.reply_text("\n".join([f"Report completed. Status: {status}"] + summary_lines))
            await persistence.record_report(
                {
                    "user_id": message.from_user.id,
                    "target": state.target_link,
                    "reason": state.reason_text,
                    "success": success,
                    "elapsed": elapsed,
                }
            )
            await log_report_summary(
                app,
                await persistence.get_logs_group_id(),
                user=message.from_user,
                target=state.target_link or "",
                elapsed=elapsed,
                success=success,
            )
            await _log_stage(
                "Report Completed",
                f"User {message.from_user.id} -> {state.target_link} ({'success' if success else 'fail'})",
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Report failed")
            await message.reply_text("Report failed due to an unexpected error.")
            await log_error(app, await persistence.get_logs_group_id(), exc, config.OWNER_ID)
        finally:
            states.reset(message.from_user.id)

    async def _execute_report(message: Message, state) -> dict:
        sessions = await prune_sessions(persistence)
        total_sessions = len(sessions)
        requested_count = max(
            config.MIN_REPORTS, min(state.report_count or config.MIN_REPORTS, config.MAX_REPORTS)
        )
        if not sessions:
            await message.reply_text("No valid sessions available.")
            return {
                "any_success": False,
                "success_count": 0,
                "failure_count": 0,
                "attempted": 0,
                "total_sessions": 0,
                "requested": requested_count,
            }

        try:
            chat_ref, msg_id = _parse_link(state.target_link, state.report_type == "private")
        except ValueError:
            await message.reply_text("Invalid target link.")
            return {
                "any_success": False,
                "success_count": 0,
                "failure_count": 0,
                "attempted": 0,
                "total_sessions": total_sessions,
                "requested": requested_count,
            }

        resolved_chat_id, available_sessions, resolution_error = await _resolve_target_across_sessions(
            state.target_link, sessions
        )
        if resolved_chat_id is None:
            detail = f" Details: {resolution_error}" if resolution_error else ""
            await message.reply_text(
                "Unable to resolve the target for reporting. Please verify the link and try again." + detail
            )
            return {
                "any_success": False,
                "success_count": 0,
                "failure_count": 0,
                "attempted": 0,
                "total_sessions": total_sessions,
                "requested": requested_count,
            }

        if not available_sessions:
            await message.reply_text(
                "We couldn't find any sessions with access to this target. Please join the chat with your sessions first."
            )
            return {
                "any_success": False,
                "success_count": 0,
                "failure_count": 0,
                "attempted": 0,
                "total_sessions": total_sessions,
                "requested": requested_count,
            }

        sessions = available_sessions
        total_sessions = len(sessions)
        chat_ref = resolved_chat_id

        await _log_stage(
            "Report Started", f"User {message.from_user.id} executing with {len(sessions)} sessions"
        )

        started_at = datetime.utcnow()
        start_label = started_at.strftime("%Y-%m-%d %H:%M:%S UTC")

        reason_code = state.reason_code if state.reason_code is not None else 9
        reason_text = state.reason_text or "Report"
        success_any = False
        success_count = 0
        failure_count = 0
        attempted = 0
        progress_message: Message | None = None
        preparing_message: Message | None = None
        stop_preparing = asyncio.Event()
        spinner_frames = [
            "[■□□□□□□□□□]",
            "[■■□□□□□□□□]",
            "[■■■□□□□□□□]",
            "[■■■■□□□□□□]",
            "[■■■■■□□□□□]",
            "[■■■■■■□□□□]",
            "[■■■■■■■□□□]",
            "[■■■■■■■■□□]",
            "[■■■■■■■■■□]",
            "[■■■■■■■■■■]",
        ]

        async def _animate_preparing() -> None:
            """Show a lightweight spinner while the backend warms up."""

            nonlocal preparing_message
            frame_idx = 0
            if not preparing_message:
                with contextlib.suppress(Exception):
                    preparing_message = await message.reply_text(
                        "⚙️ Processing your report request...\n"
                        "<code>Booting reporting engine</code>"
                    )

            while not stop_preparing.is_set() and preparing_message:
                frame = spinner_frames[frame_idx % len(spinner_frames)]
                frame_idx += 1
                with contextlib.suppress(Exception):
                    await preparing_message.edit_text(
                        "⚙️ Processing your report request...\n"
                        f"<code>{frame} initializing secure uplink</code>"
                    )
                await asyncio.sleep(0.8)

        prepare_task = asyncio.create_task(_animate_preparing())
        failure_logs: deque[str] = deque(maxlen=5)

        def _record_failure(exc: Exception | str) -> None:
            reason = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
            failure_logs.appendleft(f"Attempt {attempted + 1} failed due to {reason}")

        def _render_progress(status: str, end_label: str | None = None) -> str:
            progress_pct = 0 if requested_count == 0 else min(
                100, int((attempted / requested_count) * 100)
            )
            bar_width = 20
            filled = min(bar_width, max(0, int(bar_width * progress_pct / 100)))
            bar = "█" * filled + "░" * (bar_width - filled)
            elapsed = int(monotonic() - state.started_at)
            mode = "Private Group/Channel" if state.report_type == "private" else "Public Group/Channel"
            lines = [
                "💻 Live Attempts Panel",
                "━━━━━━━━━━━━━━━━━━",
                f"🛰️ Status: {status}",
                f"🗂️ Report Type: {reason_text}",
                f"📡 Group Type: {mode}",
                f"🔗 Link: {state.target_link}",
                f"🎯 Target Link: {state.target_link}",
                f"🕒 Start: {start_label}",
                f"⏱️ Elapsed: {elapsed}s",
                f"📦 Sessions: {total_sessions}",
                f"🧮 Requested: {requested_count}",
                f"🚀 Attempts: {attempted}/{requested_count}",
                f"✅ Successful: {success_count}",
                f"❌ Failed: {failure_count}",
                f"🛰️ Progress: [{bar}] {progress_pct}%",
            ]
            if failure_logs:
                lines.append("❗ Recent failures:")
                lines.extend(f"• {entry}" for entry in failure_logs)
            if end_label:
                lines.append(f"🏁 End: {end_label}")
            lines.append("⚡ Keeping it sleek — edits are live and safe.")
            return "\n".join(lines)

        with contextlib.suppress(Exception):
            stop_preparing.set()
            with contextlib.suppress(Exception):
                await prepare_task
            progress_message = await message.reply_text(
                (
                    f"Using all {total_sessions} valid sessions in rotation "
                    f"until {requested_count} report attempts are completed.\n\n"
                    + _render_progress("🛠️ Initializing...")
                )
            )

        if preparing_message and progress_message:
            with contextlib.suppress(Exception):
                await preparing_message.delete()

        async def _update_progress(status: str, end_label: str | None = None) -> None:
            if not progress_message:
                return
            with contextlib.suppress(Exception):
                await progress_message.edit_text(_render_progress(status, end_label=end_label))

        update_interval = 2
        while attempted < requested_count and total_sessions:
            session = sessions[attempted % total_sessions]
            client = Client(
                name=f"report_{attempted}",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=session,
                workdir=f"/tmp/report_{attempted}",
            )
            try:
                await client.start()
                try:
                    # Ensure the chat is cached for this session so Telegram can
                    # resolve the peer during reporting.
                    await client.get_chat(chat_ref)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    failure_count += 1
                    _record_failure(exc)
                    continue

                try:
                    ok = await send_report(
                        client, chat_ref, msg_id, reason_code, reason_text
                    )
                except FloodWait as fw:
                    delay = getattr(fw, "value", 1)
                    _record_failure(f"Flood wait {delay}s; retrying once")
                    await asyncio.sleep(delay)
                    try:
                        ok = await send_report(
                            client, chat_ref, msg_id, reason_code, reason_text
                        )
                    except Exception as exc:  # noqa: BLE001
                        ok = False
                        failure_count += 1
                        _record_failure(exc)
                except RPCError as exc:
                    ok = False
                    failure_count += 1
                    _record_failure(exc)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    failure_count += 1
                    _record_failure(exc)
                else:
                    if ok:
                        success_any = True
                        success_count += 1
                    else:
                        failure_count += 1
                        _record_failure("Send report call returned unsuccessful")
                await asyncio.sleep(1.5)
            finally:
                attempted += 1
                if (
                    attempted == 1
                    or attempted == requested_count
                    or attempted % update_interval == 0
                ):
                    await _update_progress("⚡ Running live...")
                with contextlib.suppress(Exception):
                    await client.stop()

        final_label = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        final_status = "✅ Completed" if success_any else "❌ Completed"
        await _update_progress(final_status, end_label=final_label)
        return {
            "any_success": success_any,
            "success_count": success_count,
            "failure_count": failure_count,
            "attempted": attempted,
            "total_sessions": total_sessions,
            "requested": requested_count,
        }

    async def _join_sessions_to_chat(target: str, message: Message) -> bool:
        sessions = await _sessions_available()
        if not sessions:
            await message.reply_text("No sessions available. Contact the owner to add them first.")
            return False

        joined = 0
        failed = 0
        already_joined = 0
        failure_reasons: deque[str] = deque(maxlen=5)
        for idx, session in enumerate(sessions):
            client = Client(
                name=f"joiner_{idx}",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=session,
                workdir=f"/tmp/joiner_{idx}",
            )
            try:
                await client.start()
                try:
                    member = await client.get_chat_member(target, "me")
                    status = getattr(member, "status", "")
                    if status not in {ChatMemberStatus.KICKED, "kicked", "left"}:
                        already_joined += 1
                        continue
                except RPCError:
                    # If the session cannot access the chat yet, fall back to joining.
                    pass

                await client.join_chat(target)
                joined += 1
                await asyncio.sleep(1)
            except UserAlreadyParticipant:
                already_joined += 1
            except RPCError as exc:
                failed += 1
                failure_reasons.append(
                    f"Session {idx + 1}: {type(exc).__name__}: {exc}"
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                failure_reasons.append(
                    f"Session {idx + 1}: {type(exc).__name__}: {exc}"
                )
            finally:
                with contextlib.suppress(Exception):
                    await client.stop()

        if joined or already_joined:
            total_ready = joined + already_joined
            details = f"(joined: {joined}, already in: {already_joined}, failed: {failed})"
            await message.reply_text(
                f"🤝 Access confirmed for {total_ready}/{len(sessions)} sessions {details}."
            )
            await _log_stage(
                "Private Join",
                (
                    "User "
                    f"{message.from_user.id} joined {target} with {joined} sessions, "
                    f"{already_joined} already present, {failed} failed"
                ),
            )
            return True

        detail_lines = ["Could not join the target with any session. Please verify the link."]
        if failed:
            detail_lines.append(f"❌ Failed joins: {failed}/{len(sessions)} sessions.")
        if failure_reasons:
            detail_lines.append("Recent errors:")
            detail_lines.extend(f"• {reason}" for reason in failure_reasons)

        await message.reply_text("\n".join(detail_lines))
        await _log_stage(
            "Private Join Failed",
            (
                f"User {message.from_user.id} failed to join {target} "
                f"({failed}/{len(sessions)} failed)"
            ),
        )
        return False

def _is_valid_target(text: str) -> bool:
    value = (text or "").strip()
    return value.startswith("https://t.me/") or value.startswith("t.me/") or value.startswith("@")


def _is_valid_link(link: str) -> bool:
    cleaned = (link or "").strip()
    return cleaned.startswith("https://t.me/") or cleaned.startswith("t.me/")

def _parse_link(link: str, is_private: bool) -> Tuple[str | int, int]:
    cleaned = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").strip("/")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Invalid link")

    if is_private:
        if parts[0] == "c":
            if len(parts) < 3:
                raise ValueError("Invalid private link")
            chat_id = int(f"-100{parts[1]}")
            message_id = int(parts[2])
            return chat_id, message_id

        if not parts[0].isdigit():
            raise ValueError("Invalid private link")
        chat_id = int(f"-100{parts[0]}")
        message_id = int(parts[1])
        return chat_id, message_id

    if parts[0] == "c" and len(parts) >= 3:
        chat_id = int(f"-100{parts[1]}")
        message_id = int(parts[2])
    else:
        chat_id = parts[0].lstrip("@")
        message_id = int(parts[1])
    return chat_id, message_id
