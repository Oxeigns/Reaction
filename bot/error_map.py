"""Structured mapping from Pyrogram exceptions to user-facing codes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pyrogram import errors


@dataclass
class ErrorInfo:
    code: str
    detail: str
    retry_after: Optional[int] = None


def _extract_message(exc: BaseException) -> str:
    msg = str(exc) or exc.__class__.__name__
    return msg.replace("\n", " ").strip()


def map_exc(exc: BaseException) -> ErrorInfo:
    """Convert a Pyrogram exception into a consistent :class:`ErrorInfo`.

    All error codes are upper-case and stable so the UI can render them without
    losing context. Unknown exceptions are preserved with the class name and a
    shortened message to avoid hiding important details.
    """

    if isinstance(exc, errors.FloodWait):
        return ErrorInfo("FLOOD_WAIT", "Too many requests", retry_after=int(getattr(exc, "value", 0)))
    if isinstance(exc, errors.InviteHashExpired):
        return ErrorInfo("INVITE_EXPIRED", "Invite link expired")
    if isinstance(exc, errors.InviteHashInvalid):
        return ErrorInfo("INVITE_INVALID_HASH", "Invite hash invalid")
    if isinstance(exc, errors.UserAlreadyParticipant):
        return ErrorInfo("ALREADY_MEMBER", "Already a participant")
    if isinstance(exc, errors.ChannelPrivate):
        return ErrorInfo("NO_ACCESS_OR_NOT_JOINED", "Chat is private or not joined")
    if isinstance(exc, errors.ChatAdminRequired):
        return ErrorInfo("ADMIN_REQUIRED", "Admin privileges required")
    if isinstance(exc, errors.MessageIdInvalid):
        return ErrorInfo("MESSAGE_ID_INVALID", "Message id invalid")
    if isinstance(exc, errors.MessageNotModified):
        return ErrorInfo("MESSAGE_NOT_MODIFIED", "Message not modified")
    if isinstance(exc, errors.MessageEmpty):
        return ErrorInfo("MESSAGE_EMPTY", "Message is empty")
    if isinstance(exc, errors.MessageAuthorRequired):
        return ErrorInfo("MESSAGE_AUTHOR_REQUIRED", "Author required to perform this action")
    if isinstance(exc, errors.PeerIdInvalid):
        return ErrorInfo("PEER_ID_INVALID", "Invalid peer id")
    if isinstance(exc, errors.UserDeactivated):
        return ErrorInfo("USER_DEACTIVATED", "User deactivated")
    if isinstance(exc, errors.SessionExpired):
        return ErrorInfo("SESSION_EXPIRED", "Session expired")
    if isinstance(exc, errors.AuthKeyUnregistered):
        return ErrorInfo("SESSION_INVALID", "Auth key unregistered")
    if isinstance(exc, errors.UserBannedInChannel):
        return ErrorInfo("BANNED_IN_CHANNEL", "User banned in channel")
    if isinstance(exc, errors.MessageNotModified):
        return ErrorInfo("MESSAGE_NOT_MODIFIED", "Message not modified")
    return ErrorInfo("UNKNOWN_ERROR", _extract_message(exc))


__all__ = ["ErrorInfo", "map_exc"]
