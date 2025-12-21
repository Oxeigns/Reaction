"""Resolve target message links using the first accessible Pyrogram client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from pyrogram import Client

from .error_map import ErrorInfo, map_exc
from .link_parser import MessageLink


@dataclass
class TargetPreview:
    chat_title: str
    chat_id: int
    msg_id: int
    date: Optional[str]
    snippet: str


async def _fetch_message(client: Client, link: MessageLink):
    if link.kind == "public_msg":
        return await client.get_messages(link.chat_ref, link.msg_id)
    return await client.get_messages(link.chat_ref, link.msg_id)


async def resolve_target(
    message_link: MessageLink,
    clients: Iterable[tuple[str, Client]],
) -> Tuple[Optional[TargetPreview], Optional[ErrorInfo], Optional[str]]:
    """Attempt to fetch a target message with available clients.

    Returns ``(preview, error, client_name)`` where ``preview`` is set on
    success. ``error`` is set to the mapped error when all clients fail.
    """

    last_error: Optional[ErrorInfo] = None
    for name, client in clients:
        try:
            msg = await _fetch_message(client, message_link)
            if not msg:
                last_error = ErrorInfo("MESSAGE_NOT_FOUND", "Message not found")
                continue
            chat = msg.chat
            preview = TargetPreview(
                chat_title=getattr(chat, "title", getattr(chat, "first_name", "")) or "(no title)",
                chat_id=chat.id,
                msg_id=msg.id,
                date=str(msg.date) if getattr(msg, "date", None) else None,
                snippet=(msg.text or msg.caption or "").strip()[:200],
            )
            return preview, None, name
        except Exception as exc:  # noqa: BLE001
            last_error = map_exc(exc)
    return None, last_error, None


__all__ = ["TargetPreview", "resolve_target"]
