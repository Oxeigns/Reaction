from __future__ import annotations

"""Command and callback handlers for the reporting bot."""

import contextlib
import logging
from time import monotonic
from typing import Callable, Tuple

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import RPCError
from pyrogram.types import CallbackQuery, Message

import config
from logging_utils import log_error, log_report_summary, log_user_start, send_log
from report import send_report
from session_bot import extract_sessions_from_text, prune_sessions, validate_sessions
from state import QueueEntry, ReportQueue, StateManager
from sudo import is_owner, is_sudo
from ui import REPORT_REASONS, owner_panel, queued_message, reason_keyboard, report_type_keyboard, sudo_panel


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
            await log_error(app, await persistence.logs_group(), exc, config.OWNER_ID)

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
        await log_user_start(app, await persistence.logs_group(), message)

        if not is_sudo(user_id):
            await message.reply_text("Access Denied: This bot is not for public use.")
            return

        live_sessions = await prune_sessions(persistence)

        if is_owner(user_id):
            if not live_sessions:
                await message.reply_text(
                    "You need to set sessions first.\nPlease send valid Pyrogram session strings in the Session Manager Group.",
                )
                await message.reply_text(
                    "Owner Control Panel",
                    reply_markup=owner_panel(len(live_sessions)),
                )
                return

            await message.reply_text(
                f"Owner Control Panel\nLive sessions: {len(live_sessions)}",
                reply_markup=owner_panel(len(live_sessions)),
            )
        else:
            if not live_sessions:
                await message.reply_text(
                    "No sessions available.\nPlease contact the bot owner to add valid sessions.",
                )
                return
            await message.reply_text(
                f"Live sessions: {len(live_sessions)}",
                reply_markup=sudo_panel(len(live_sessions)),
            )

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
        if user_id in config.SUDO_USERS:
            await message.reply_text("User is already a sudo user.")
            return

        config.SUDO_USERS.add(user_id)
        label = parts[2] if len(parts) > 2 else str(user_id)
        await message.reply_text(f"Added {label} ({user_id}) to sudo users.")

    @app.on_message(filters.command("rmsudo"))
    async def remove_sudo(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_remove_sudo, message)

    async def _handle_remove_sudo(message: Message) -> None:
        if not await _owner_guard(message):
            return

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply_text("Usage: /rmsudo <user_id>")
            return

        user_id = int(parts[1])
        if user_id not in config.SUDO_USERS:
            await message.reply_text("User is not in the sudo list.")
            return

        config.SUDO_USERS.discard(user_id)
        await message.reply_text(f"Removed {user_id} from sudo users.")

    @app.on_message(filters.command("sudolist"))
    async def sudo_list(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_sudo_list, message)

    async def _handle_sudo_list(message: Message) -> None:
        if not await _owner_guard(message):
            return

        sudo_users = sorted(config.SUDO_USERS)
        if not sudo_users:
            await message.reply_text("No sudo users configured.")
            return

        entries = [f"- {user_id}" for user_id in sudo_users]
        text = "Current sudo users:\n" + "\n".join(entries)
        await message.reply_text(text)

    @app.on_callback_query(filters.regex(r"^sudo:start$"))
    async def start_report(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_start_report, query)

    async def _handle_start_report(query: CallbackQuery) -> None:
        if not query.message or not query.from_user:
            return
        if not is_sudo(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return

        checking = await query.message.reply_text("Please wait... validating sessions")
        live_sessions = await prune_sessions(persistence, announce=True)
        if not live_sessions:
            await checking.edit_text("No sessions available.\nPlease contact the bot owner to add valid sessions.")
            return

        if queue.is_busy() and queue.active_user != query.from_user.id:
            position = queue.expected_position(query.from_user.id)
            notice = queued_message(position)
            if notice:
                await query.message.reply_text(notice)

        state = states.get(query.from_user.id)
        state.reset()
        state.stage = "type"
        await checking.edit_text(f"Live sessions: {len(live_sessions)}")
        await query.message.reply_text("Select Report Type", reply_markup=report_type_keyboard())
        await query.answer()

    @app.on_callback_query(filters.regex(r"^report:type:(public|private)$"))
    async def choose_type(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_type, query)

    async def _handle_type(query: CallbackQuery) -> None:
        if not query.from_user:
            return
        if not is_sudo(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return
        state = states.get(query.from_user.id)
        if state.stage not in {"type", "idle"}:
            await query.answer()
            return
        state.report_type = query.data.split(":")[-1]
        state.stage = "awaiting_link"
        await query.message.reply_text("Send the target message link (https://t.me/...) to report.")
        await query.answer()

    @app.on_callback_query(filters.regex(r"^report:reason:[a-z_]+$"))
    async def choose_reason(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_reason, query)

    async def _handle_reason(query: CallbackQuery) -> None:
        if not query.from_user:
            return
        if not is_sudo(query.from_user.id):
            await query.answer("Unauthorized", show_alert=True)
            return
        key = query.data.split(":")[-1]
        label, code = REPORT_REASONS.get(key, ("Other", 9))
        state = states.get(query.from_user.id)
        state.reason_code = code
        state.reason_text = label
        await query.answer(f"Reason set to {label}")
        await _begin_report(query.message, state)

    @app.on_message(filters.text & ~filters.command(["start", "broadcast", "set_session", "set_log"]))
    async def text_router(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_text, message)

    async def _handle_text(message: Message) -> None:
        if not message.from_user or not is_sudo(message.from_user.id):
            return
        state = states.get(message.from_user.id)
        if state.stage == "awaiting_link":
            if not _is_valid_link(message.text):
                await message.reply_text("Send a valid https://t.me/ link.")
                return
            state.target_link = message.text.strip()
            state.stage = "awaiting_reason"
            await message.reply_text("Choose a report reason", reply_markup=reason_keyboard())
            return
        if state.stage == "awaiting_reason":
            state.reason_text = message.text.strip()
            state.reason_code = 9
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

        state.stage = "queued"
        state.started_at = monotonic()

        if queue.is_busy() and queue.active_user != message.from_user.id:
            notice = queued_message(queue.expected_position(message.from_user.id))
            if notice:
                await message.reply_text(notice)

        async def notify_position(position: int) -> None:
            if position > 1:
                notice = queued_message(position)
                if notice:
                    await message.reply_text(notice)

        entry = QueueEntry(
            message.from_user.id,
            job=lambda: _run_report_job(message, state),
            notify_position=notify_position,
        )
        await queue.enqueue(entry)

    async def _run_report_job(message: Message, state) -> None:
        try:
            success = await _execute_report(message, state)
            elapsed = monotonic() - state.started_at
            status = "Success" if success else "❌ Failed"
            await message.reply_text(f"Report completed. Status: {status}")
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
                await persistence.logs_group(),
                user=message.from_user,
                target=state.target_link or "",
                elapsed=elapsed,
                success=success,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Report failed")
            await message.reply_text("Report failed due to an unexpected error.")
            await log_error(app, await persistence.logs_group(), exc, config.OWNER_ID)
        finally:
            states.reset(message.from_user.id)

    async def _execute_report(message: Message, state) -> bool:
        sessions = await prune_sessions(persistence)
        if not sessions:
            await message.reply_text("No valid sessions available.")
            return False

        try:
            chat_ref, msg_id = _parse_link(state.target_link, state.report_type == "private")
        except ValueError:
            await message.reply_text("Invalid target link.")
            return False

        reason_code = state.reason_code if state.reason_code is not None else 9
        reason_text = state.reason_text or "Report"
        success_any = False

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
                await send_report(client, chat_ref, msg_id, reason_code, reason_text)
                success_any = True
            except RPCError:
                continue
            except Exception:
                continue
            finally:
                with contextlib.suppress(Exception):
                    await client.stop()
        return success_any

    @app.on_message(filters.command("set_session"))
    async def set_session(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_set_session, message)

    async def _handle_set_session(message: Message) -> None:
        if not message.from_user or not is_owner(message.from_user.id):
            return
        if not await _ensure_admin(message.chat.id):
            await message.reply_text("Make me admin in this group first.")
            return
        await persistence.set_session_group(message.chat.id)
        await persistence.add_known_chat(message.chat.id)
        await message.reply_text("Session Manager Group set successfully.")

    @app.on_message(filters.command("set_log"))
    async def set_log(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_set_log, message)

    async def _handle_set_log(message: Message) -> None:
        if not message.from_user or not is_owner(message.from_user.id):
            return
        if not await _ensure_admin(message.chat.id):
            await message.reply_text("Make me admin in this group first.")
            return
        await persistence.set_logs_group(message.chat.id)
        await persistence.add_known_chat(message.chat.id)
        await message.reply_text("Logs group set successfully")

    @app.on_message(filters.command("broadcast"))
    async def broadcast(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_broadcast, message)

    async def _handle_broadcast(message: Message) -> None:
        if not message.from_user or not is_owner(message.from_user.id):
            return
        logs_group = await persistence.logs_group()
        if not logs_group or message.chat.id != logs_group:
            return
        payload = message.text.split(" ", 1)
        if len(payload) < 2:
            await message.reply_text("Usage: /broadcast <message>")
            return
        text = payload[1]
        chats = await persistence.known_chats()
        user_count = 0
        group_count = 0
        start_time = monotonic()
        for chat_id in chats:
            try:
                await app.send_message(chat_id, text)
                if chat_id > 0:
                    user_count += 1
                else:
                    group_count += 1
            except Exception:
                continue
        elapsed = round(monotonic() - start_time, 2)
        summary = (
            "📢 Broadcast Completed\n"
            f"👤 Users: {user_count}\n"
            f"👥 Groups: {group_count}\n"
            f"⏱ Duration: {elapsed}s"
        )
        await send_log(app, logs_group, summary)

    @app.on_message(filters.group & (filters.text | filters.caption))
    async def session_ingestion(_: Client, message: Message) -> None:
        await _wrap_errors(_handle_session_ingestion, message)

    async def _handle_session_ingestion(message: Message) -> None:
        session_group = await persistence.session_group()
        if not session_group or message.chat.id != session_group:
            return
        if not message.from_user:
            return

        text_content = message.text or message.caption or ""
        sessions = extract_sessions_from_text(text_content)
        if not sessions:
            await message.reply_text("❌ Invalid session string")
            return

        valid, invalid = await validate_sessions(sessions)
        logs_group = await persistence.logs_group()

        response_parts: list[str] = []
        if valid:
            added = await persistence.add_sessions(valid, added_by=message.from_user.id)
            total_sessions = len(await persistence.get_sessions())
            response_parts.append(
                f"✅ Valid sessions: {len(valid)} (added {len(added)})\n📦 Total stored: {total_sessions}"
            )
            await send_log(
                app,
                logs_group,
                f"{len(valid)} session(s) added from the session manager group.",
            )
        if invalid:
            response_parts.append(f"❌ Invalid session(s): {len(invalid)}")
            await send_log(app, logs_group, f"Ignored {len(invalid)} invalid session strings.")

        if response_parts:
            await message.reply_text("\n".join(response_parts))

    @app.on_callback_query(filters.regex(r"^owner:(manage|set_session_group|set_logs_group)$"))
    async def owner_actions(_: Client, query: CallbackQuery) -> None:
        await _wrap_errors(_handle_owner_action, query)

    async def _handle_owner_action(query: CallbackQuery) -> None:
        if not query.from_user or not is_owner(query.from_user.id):
            await query.answer("Owner only", show_alert=True)
            return
        await query.answer()
        action = query.data.split(":")[-1]
        if action == "manage":
            sessions = await prune_sessions(persistence)
            await query.message.reply_text(f"Total valid sessions: {len(sessions)}")
        elif action == "set_session_group":
            await query.message.reply_text("Run /set_session in the target session group where I am admin.")
        elif action == "set_logs_group":
            await query.message.reply_text("Run /set_log in the logs group where I am admin.")


def _is_valid_link(link: str) -> bool:
    return link.startswith("https://t.me/")


def _parse_link(link: str, is_private: bool) -> Tuple[str | int, int]:
    cleaned = link.replace("https://t.me/", "").strip("/")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Invalid link")
    if is_private:
        # Private links are typically t.me/c/<internal_id>/<message_id>
        if parts[0] != "c" and not parts[0].isdigit():
            raise ValueError("Invalid private link")
        if parts[0] == "c":
            chat_id = int(f"-100{parts[1]}") if len(parts) > 2 else int(f"-100{parts[0]}")
            message_id = int(parts[-1])
        else:
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

