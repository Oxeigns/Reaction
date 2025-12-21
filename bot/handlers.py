from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import Client
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

import config
from .join_coordinator import JoinResult, join_all_clients
from .link_parser import JoinLink, MessageLink, normalize_url, parse_join_link, parse_message_link
from .reporting import start_reporting
from .target_resolver import resolve_target

WAIT_JOIN_LINK = "WAIT_JOIN_LINK"
JOINING = "JOINING"
SHOW_CHAT_DETAILS = "SHOW_CHAT_DETAILS"
WAIT_TARGET_LINK = "WAIT_TARGET_LINK"
VALIDATING_TARGET = "VALIDATING_TARGET"
READY = "READY"
REPORTING = "REPORTING"


def _user_ctx(context: CallbackContext) -> Dict[str, Any]:
    return context.user_data.setdefault(
        "flow",
        {
            "state": WAIT_JOIN_LINK,
            "status_message": None,
            "join_link": None,
            "target_link": None,
            "target_preview": None,
            "report_stop": None,
        },
    )


async def _ensure_clients(context: CallbackContext) -> List[Tuple[str, Client]]:
    clients: List[Tuple[str, Client]] = context.application.bot_data.get("pyro_clients", [])
    if clients:
        return clients

    sessions_env = (config.__dict__.get("SESSION_STRINGS") or "") if hasattr(config, "SESSION_STRINGS") else ""
    if not sessions_env:
        sessions_env = ""
    session_strings = [s for s in sessions_env.split("\n") if s.strip()]
    clients = []
    for idx, session in enumerate(session_strings, start=1):
        client = Client(
            name=f"client_{idx}",
            session_string=session.strip(),
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True,
        )
        await client.start()
        clients.append((f"client_{idx}", client))
    context.application.bot_data["pyro_clients"] = clients
    return clients


async def start(update: Update, context: CallbackContext) -> None:
    ctx = _user_ctx(context)
    ctx.update({"state": WAIT_JOIN_LINK, "join_link": None, "target_link": None, "target_preview": None, "report_stop": None})
    await update.message.reply_text(
        "Send a chat link to join (public username or invite link). Supported: https://t.me/+hash, https://t.me/joinchat/hash, https://t.me/username",
    )


async def stop(update: Update, context: CallbackContext) -> None:
    ctx = _user_ctx(context)
    stop_event: asyncio.Event | None = ctx.get("report_stop")
    if stop_event:
        stop_event.set()
    ctx["state"] = WAIT_JOIN_LINK
    await update.message.reply_text("Stopped. Send a join link to start again.")


async def status(update: Update, context: CallbackContext) -> None:
    ctx = _user_ctx(context)
    state = ctx.get("state", WAIT_JOIN_LINK)
    await update.message.reply_text(f"Current state: {state}")


def _format_join_status(results: Dict[str, JoinResult], total: int) -> str:
    lines = ["**Joining…**"]
    joined = sum(1 for r in results.values() if r.status in {"joined", "already"})
    for name, res in results.items():
        if res.status in {"joined", "already"}:
            icon = "✅" if res.status == "joined" else "ℹ️"
            lines.append(f"- {name}: {icon} {res.code.lower().replace('_', ' ')}")
        elif res.status == "floodwait":
            lines.append(
                f"- {name}: ⏳ FLOOD_WAIT {res.retry_after}s (retry #{res.attempts} in {res.retry_after}s)"
            )
        else:
            lines.append(f"- {name}: ❌ failed: {res.code}")
    pending_retries = sum(1 for r in results.values() if r.status == "floodwait")
    lines.append(f"joined: {joined}/{total}")
    lines.append(f"pending retries: {pending_retries}")
    lines.append(f"last update: {dt.datetime.utcnow().isoformat()}Z")
    return "\n".join(lines)


def _format_validation_status(status: Dict[str, str]) -> str:
    lines = ["**Reporting…**"]
    for name, code in status.items():
        icon = "✅" if code == "OK" else ("⏳" if code == "FLOOD_WAIT" else "⚠️")
        lines.append(f"- {name}: {icon} {code}")
    lines.append(f"last update: {dt.datetime.utcnow().isoformat()}Z")
    return "\n".join(lines)


def _detect_not_supported(text: str) -> Optional[str]:
    url = normalize_url(text)
    parts = [p for p in url.split("/") if p]
    if not parts:
        return None
    if parts[-1] in {"s", "story"} or "/s/" in url or "/story/" in url:
        return "NOT_SUPPORTED:story urls"
    if len(parts) == 3 and parts[-2] in {"s", "story"}:
        return "NOT_SUPPORTED:story urls"
    parsed_parts = [p for p in url.split("/") if p]
    if parsed_parts and len(parsed_parts) == 3 and parsed_parts[-2].isalpha() and not parsed_parts[-1].isdigit():
        return "NOT_SUPPORTED:profile url"
    if len(parsed_parts) == 2 and parsed_parts[-1].isalpha():
        return "NOT_SUPPORTED:profile url"
    return None


async def _update_status_message(context: CallbackContext, chat_id: int, message_id: int, text: str) -> None:
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logging.debug("status update failed", exc_info=True)


async def _handle_join(update: Update, context: CallbackContext, join_link: JoinLink) -> None:
    ctx = _user_ctx(context)
    ctx["state"] = JOINING
    clients = await _ensure_clients(context)
    if not clients:
        await update.message.reply_text("NOT_SUPPORTED: no client sessions configured")
        ctx["state"] = WAIT_JOIN_LINK
        return
    status_message = await update.message.reply_text("Starting join…")
    ctx["status_message"] = (status_message.chat_id, status_message.message_id)

    semaphore = asyncio.Semaphore(3)

    async def progress(res: Dict[str, JoinResult]):
        text = _format_join_status(res, len(clients))
        await _update_status_message(context, status_message.chat_id, status_message.message_id, text)

    results = await join_all_clients(join_link, clients, progress, semaphore)
    failures = [r for r in results.values() if r.status == "failed"]
    incomplete = [r for r in results.values() if r.status == "floodwait"]
    if failures or incomplete:
        await _update_status_message(
            context,
            status_message.chat_id,
            status_message.message_id,
            _format_join_status(results, len(clients)),
        )
        ctx["state"] = WAIT_JOIN_LINK
        return

    # joined successfully
    joined_client = clients[0][1] if clients else None
    chat_details = None
    if joined_client:
        try:
            if join_link.kind == "public_username":
                chat_details = await joined_client.get_chat(join_link.value)
            else:
                chat_details = await joined_client.get_chat(join_link.raw)
        except Exception:
            logging.debug("unable to fetch chat details", exc_info=True)

    details_lines = ["Join complete."]
    if chat_details:
        details_lines.append(f"title: {getattr(chat_details, 'title', '(unknown)')}")
        details_lines.append(f"id: {chat_details.id}")
        username = getattr(chat_details, "username", None)
        if username:
            details_lines.append(f"username: @{username}")
        details_lines.append(f"type: {getattr(chat_details, 'type', '')}")
    details_lines.append("Send a target message link to validate.")
    await _update_status_message(context, status_message.chat_id, status_message.message_id, "\n".join(details_lines))
    ctx["state"] = WAIT_TARGET_LINK
    ctx["join_link"] = join_link


async def _handle_target(update: Update, context: CallbackContext, msg_link: MessageLink) -> None:
    ctx = _user_ctx(context)
    ctx["state"] = VALIDATING_TARGET
    status_message = await update.message.reply_text("Validating target…")
    ctx["status_message"] = (status_message.chat_id, status_message.message_id)
    clients = await _ensure_clients(context)
    if not clients:
        await _update_status_message(context, status_message.chat_id, status_message.message_id, "NOT_SUPPORTED: no client sessions configured")
        ctx["state"] = WAIT_TARGET_LINK
        return

    async def progress(status: Dict[str, str]):
        text = _format_validation_status(status)
        await _update_status_message(context, status_message.chat_id, status_message.message_id, text)

    preview, error, client_name = await resolve_target(msg_link, clients)
    if not preview:
        reason = error.code if error else "UNKNOWN_ERROR"
        await _update_status_message(
            context,
            status_message.chat_id,
            status_message.message_id,
            f"Validation failed: {reason}",
        )
        ctx["state"] = WAIT_TARGET_LINK
        return

    ctx["state"] = READY
    ctx["target_preview"] = preview
    preview_lines = [
        "Target validated:",
        f"chat: {preview.chat_title} ({preview.chat_id})",
        f"msg id: {preview.msg_id}",
    ]
    if preview.snippet:
        preview_lines.append(f"snippet: {preview.snippet}")
    await _update_status_message(context, status_message.chat_id, status_message.message_id, "\n".join(preview_lines))

    ctx["state"] = REPORTING
    stop_event = asyncio.Event()
    ctx["report_stop"] = stop_event
    await start_reporting(clients, preview, progress_cb=progress, stop_event=stop_event)


async def handle_message(update: Update, context: CallbackContext) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    ctx = _user_ctx(context)
    state = ctx.get("state", WAIT_JOIN_LINK)

    not_supported = _detect_not_supported(text)
    if not_supported:
        await update.message.reply_text(not_supported)
        return

    if state == WAIT_JOIN_LINK:
        join_link = parse_join_link(text)
        if not join_link:
            await update.message.reply_text("INVALID_FORMAT: send a join link like https://t.me/+hash or https://t.me/username")
            return
        await _handle_join(update, context, join_link)
        return

    if state == WAIT_TARGET_LINK:
        msg_link = parse_message_link(text)
        if not msg_link:
            await update.message.reply_text("INVALID_FORMAT: send a message link like https://t.me/username/123")
            return
        await _handle_target(update, context, msg_link)
        return

    await update.message.reply_text("Currently busy. Use /stop to reset.")


async def error_handler(update: object, context: CallbackContext) -> None:
    logging.exception("Unhandled error", exc_info=context.error)


__all__ = [
    "start",
    "stop",
    "status",
    "handle_message",
    "error_handler",
    "WAIT_JOIN_LINK",
    "WAIT_TARGET_LINK",
    "READY",
]
