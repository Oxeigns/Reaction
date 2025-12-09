"""Reporting helpers and pyrogram monkey patching.

This module centralizes report submission for messages and profiles/chats.
It also monkey-patches :class:`pyrogram.Client` with a `send_report` helper
that wraps the raw MTProto ``messages.Report`` call. The functions are
structured so the caller can centralize concurrency and retry logic.
"""

from __future__ import annotations

import asyncio
from pyrogram import Client
from pyrogram.errors import BadRequest, FloodWait, MessageIdInvalid, RPCError


async def send_report(client: Client, chat_id, message_id: int, reason: int, reason_text: str) -> bool:
    """Send a report against a specific message.

    The function re-raises FloodWait, BadRequest, and RPCError so upstream
    loops can coordinate throttling and retries. MessageIdInvalid is treated
    as a soft success, allowing workers to skip deleted messages gracefully.
    """
    try:
        await client.send_report(
            chat_id=chat_id, message_id=message_id, reason=int(reason), message=reason_text
        )
        return True

    except MessageIdInvalid:
        print(
            f"[{getattr(client, 'name', 'unknown')}] Message ID {message_id} is invalid or deleted. Skipping this message."
        )
        return True
    except (FloodWait, BadRequest, RPCError):
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"Report API Error (Session {getattr(client, 'name', 'unknown')}): {exc}")
        return False


async def report_profile_photo(client: Client, entity_id, reason: int, reason_text: str) -> bool:
    """Report a user profile, chat, or generic entity.

    The function delegates to ``client.send_report`` with ``message_id=None``
    when available. FloodWait, BadRequest, and RPCError bubble up for callers
    to manage shared backoff logic.
    """
    try:
        await client.send_report(chat_id=entity_id, message_id=None, reason=int(reason), message=reason_text)
        return True

    except (FloodWait, BadRequest, RPCError):
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"Profile/Chat Report API Error (Session {getattr(client, 'name', 'unknown')}): {exc}")
        return False


if not hasattr(Client, "send_report"):
    # Lazy imports so users without reporting needs avoid pulling raw types prematurely.
    from pyrogram.raw.functions.messages import Report
    from pyrogram.raw.types import (
        InputReportReasonChildAbuse,
        InputReportReasonCopyright,
        InputReportReasonOther,
        InputReportReasonPornography,
        InputReportReasonSpam,
        InputReportReasonViolence,
    )

    async def _client_send_report(
        self,
        chat_id,
        message_id: int | None = None,
        reason: int = 0,
        message: str = "",
    ) -> None:
        """High-level wrapper for the raw ``messages.Report`` call."""

        reason_map = {
            0: InputReportReasonSpam,
            1: InputReportReasonViolence,
            2: InputReportReasonPornography,
            3: InputReportReasonChildAbuse,
            4: InputReportReasonCopyright,
            5: InputReportReasonOther,
        }

        try:
            peer = None
            if hasattr(self, "resolve_peer"):
                try:
                    resolved = self.resolve_peer(chat_id)
                    peer = await resolved if asyncio.iscoroutine(resolved) else resolved
                except Exception:
                    peer = chat_id
            else:
                peer = chat_id

            reason_cls = reason_map.get(int(reason), InputReportReasonOther)
            if reason_cls is InputReportReasonOther:
                reason_obj = reason_cls(text=message[:512] if message else "")
            else:
                reason_obj = reason_cls()

            ids = [int(message_id)] if message_id is not None else []

            await self.invoke(Report(peer=peer, id=ids, reason=reason_obj, message=message or ""))

        except MessageIdInvalid:
            raise
        except (FloodWait, BadRequest, RPCError):
            raise

    setattr(Client, "send_report", _client_send_report)
