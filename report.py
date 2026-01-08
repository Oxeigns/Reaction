"""Reporting helpers built on top of :class:`pyrogram.Client`.

The module adds a small `send_report` helper so the bot can call the raw
MTProto ``messages.Report`` RPC with clean ergonomics. The functions here keep
networking concerns centralized: concurrency, retries, and basic resilience are
handled by higher-level flows in ``main.py``.
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Sequence

from pyrogram import Client
from pyrogram.errors import BadRequest, FloodWait, MessageIdInvalid, RPCError, PeerIdInvalid
from pyrogram.raw.types import (
    InputReportReasonChildAbuse,
    InputReportReasonCopyright,
    InputReportReasonFake,
    InputReportReasonGeoIrrelevant,
    InputReportReasonIllegalDrugs,
    InputReportReasonOther,
    InputReportReasonPersonalDetails,
    InputReportReasonPornography,
    InputReportReasonSpam,
    InputReportReasonViolence,
)


def _build_reason(reason: int | object, message: str) -> object:
    """Return a Pyrogram InputReportReason object for the given code or instance."""

    reason_map = {
        0: InputReportReasonSpam,
        1: InputReportReasonViolence,
        2: InputReportReasonPornography,
        3: InputReportReasonChildAbuse,
        4: InputReportReasonCopyright,
        5: InputReportReasonGeoIrrelevant,
        6: InputReportReasonFake,
        7: InputReportReasonIllegalDrugs,
        8: InputReportReasonPersonalDetails,
        9: InputReportReasonOther,
    }

    if hasattr(reason, "write"):
        return reason

    try:
        reason_int = int(reason)
    except Exception:
        return InputReportReasonOther()

    reason_cls = reason_map.get(reason_int, InputReportReasonOther)
    if reason_cls is InputReportReasonOther:
        return reason_cls()

    return reason_cls()


async def _resolve_peer_for_report(client: Client, chat_id):
    """Resolve usernames or numeric ids into raw peers for reporting.

    Pyrogram's async helper resolves numeric IDs easily but can return
    ``UsernameInvalid`` for plain strings if they are not cached. To keep the
    reporting surface robust we fall back to ``contacts.ResolveUsername`` and
    build the appropriate ``InputPeer*`` instance manually.
    """

    from pyrogram.errors import UsernameInvalid
    from pyrogram.raw.functions.contacts import ResolveUsername
    from pyrogram.raw.types import InputPeerChannel, InputPeerChat, InputPeerUser

    if hasattr(chat_id, "write"):
        return chat_id

    # Allow numeric strings so callers can pass ``-100…`` chat IDs directly
    # without hitting the username resolution fallback below.
    if isinstance(chat_id, str) and chat_id.lstrip("-+").isdigit():
        chat_id = int(chat_id)

    try:
        peer = client.resolve_peer(chat_id) if hasattr(client, "resolve_peer") else chat_id
        return await peer if asyncio.iscoroutine(peer) else peer
    except UsernameInvalid:
        # Continue to raw username resolution below
        pass
    except ValueError as exc:
        # Pyrogram raises ``ValueError(Peer id invalid: ...)`` for bad numeric inputs.
        # Surface a consistent API error so callers can show a friendly message.
        raise BadRequest(f"Invalid target for reporting: {exc}") from exc
    except Exception:
        # For non-username inputs we want to bubble up the original error
        # instead of masking it with a generic "Unable to resolve" message.
        raise

    username = str(chat_id).lstrip("@")
    try:
        resolved = await client.invoke(ResolveUsername(username=username))
        if resolved.users:
            user = resolved.users[0]
            return InputPeerUser(user_id=user.id, access_hash=user.access_hash)
        if resolved.chats:
            chat = resolved.chats[0]
            if hasattr(chat, "access_hash"):
                return InputPeerChannel(channel_id=chat.id, access_hash=chat.access_hash)
            return InputPeerChat(chat_id=chat.id)
    except Exception:
        pass

    raise BadRequest("Unable to resolve the target for reporting.")


async def send_report(client: Client, chat_id, message_id: int, reason: int | object, reason_text: str) -> bool:
    """Send a report against a specific message.

    This helper resolves the target first so resolution errors (peer id invalid,
    unable to resolve username) are surfaced consistently as BadRequest/PeerIdInvalid
    and can be handled by callers.
    """
    try:
        reason_obj = _build_reason(reason, reason_text)

        # Pre-resolve the peer so errors like "Peer id invalid" or "Unable to resolve"
        # are raised here and can be handled in a consistent way.
        try:
            resolved_peer = await _resolve_peer_for_report(client, chat_id)
        except (BadRequest, PeerIdInvalid):
            # Bubble up resolution-related API errors so callers can decide how to react.
            raise
        except Exception as exc:
            # Wrap unexpected resolution errors into a BadRequest for consistency.
            raise BadRequest(f"Unable to resolve the target for reporting: {exc}") from exc

        await client.send_report(chat_id=resolved_peer, message_id=message_id, reason=reason_obj, message=reason_text)
        return True

    except MessageIdInvalid:
        print(
            f"[{getattr(client, 'name', 'unknown')}] Message ID {message_id} is invalid or deleted. Skipping this message."
        )
        return True
    except (FloodWait, BadRequest, RPCError):
        # Let callers or higher-level flows handle these (flood waits, API errors).
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        # Defensive logging for unexpected errors — do not crash the entire run.
        print(f"[{getattr(client, 'name', 'unknown')}] Unexpected error while reporting: {exc}")
        return False
