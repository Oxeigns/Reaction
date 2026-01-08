from __future__ import annotations

import logging
from typing import Any

def map_pyrogram_error(exc: Exception | None) -> tuple[str, str, int | None]:
    """
    Translate Pyrogram exceptions into scannable user codes.
    Returns: (Error Code, Human Detail, Wait Time in Seconds)
    """
    if exc is None:
        return "UNKNOWN_ERROR", "No exception provided", None

    detail = str(exc)
    # Extract retry_after if available (standard in FloodWait)
    retry_after = getattr(exc, "value", None)

    try:
        from pyrogram.errors import (
            FloodWait, InviteHashExpired, InviteHashInvalid,
            UserAlreadyParticipant, ChannelPrivate, ChatAdminRequired,
            MessageIdInvalid, PeerIdInvalid, UsernameInvalid
        )
    except ImportError:
        # Fallback if types aren't available
        FloodWait = InviteHashExpired = InviteHashInvalid = \
        UserAlreadyParticipant = ChannelPrivate = ChatAdminRequired = \
        MessageIdInvalid = PeerIdInvalid = UsernameInvalid = type("Dummy", (), {})

    # 1. Handling Rate Limits
    if isinstance(exc, FloodWait) or exc.__class__.__name__ == "FloodWait":
        return "FLOOD_WAIT", "Telegram rate limit reached.", int(retry_after or 0)

    # 2. Link & Access Issues
    if isinstance(exc, InviteHashExpired):
        return "INVITE_EXPIRED", "The invite link has expired.", None
    if isinstance(exc, InviteHashInvalid):
        return "INVITE_INVALID", "The invite link hash is malformed.", None
    if isinstance(exc, UserAlreadyParticipant):
        return "ALREADY_MEMBER", "Session is already in this chat.", None
    if isinstance(exc, (ChannelPrivate, ChatAdminRequired)):
        return "NO_ACCESS", "Private chat or admin rights required.", None

    # 3. Targeted Item Issues
    if isinstance(exc, MessageIdInvalid) or "MESSAGE_ID_INVALID" in detail:
        return "MESSAGE_ID_INVALID", "The message ID no longer exists.", None
    if isinstance(exc, (PeerIdInvalid, UsernameInvalid)):
        return "TARGET_NOT_FOUND", "The user or chat could not be resolved.", None

    # 4. Fallback for generic MTProto errors
    if "MESSAGE_NOT_FOUND" in detail.upper():
        return "MESSAGE_NOT_FOUND", "Target message is missing.", None

    return "UNKNOWN_ERROR", f"{exc.__class__.__name__}: {detail}", None

__all__ = ["map_pyrogram_error"]
