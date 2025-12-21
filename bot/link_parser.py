from __future__ import annotations

"""Utilities for validating and normalizing Telegram links/usernames for joins."""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class ParsedTelegramLink:
    type: Literal["invite", "public"]
    normalized_url: str
    invite_hash: str | None = None
    username: str | None = None


@dataclass(frozen=True)
class ParsedMessageLink:
    type: Literal["public_message", "private_message"]
    chat_ref: str | int
    message_id: int
    internal_id: int | None = None
    username: str | None = None


_TRAILING_PUNCTUATION = ",.;)]}>'\""


def _clean_input(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = cleaned.rstrip(_TRAILING_PUNCTUATION)
    return cleaned


def parse_message_link(raw: str) -> ParsedMessageLink:
    """Parse Telegram message links in both public and private (/c) forms.

    Supports:
    - https://t.me/<username>/<msg_id>
    - https://t.me/c/<internal_id>/<msg_id>
    - Links without scheme (t.me/...) or with query strings.
    """

    cleaned = _clean_input(raw)
    if not cleaned:
        raise ValueError("Empty link")

    parsed = urlparse(cleaned if cleaned.startswith("http") else f"https://{cleaned}")
    path_parts = [p for p in parsed.path.split("/") if p]

    if not parsed.netloc.endswith("t.me") or len(path_parts) < 2:
        raise ValueError("Not a Telegram message link")

    if path_parts[0].lower() == "c":
        if len(path_parts) < 3 or not path_parts[1].isdigit() or not path_parts[2].isdigit():
            raise ValueError("Invalid private message link")
        internal_id = int(path_parts[1])
        message_id = int(path_parts[2])
        return ParsedMessageLink(
            type="private_message",
            internal_id=internal_id,
            chat_ref=int(f"-100{internal_id}"),
            message_id=message_id,
        )

    if not path_parts[1].isdigit():
        raise ValueError("Invalid public message link")

    username = path_parts[0].lstrip("@")
    if not username:
        raise ValueError("Username is missing")

    return ParsedMessageLink(
        type="public_message",
        username=username,
        chat_ref=username,
        message_id=int(path_parts[1]),
    )


def _parse_invite_hash_from_url(parsed) -> str | None:
    path_parts = [p for p in parsed.path.split("/") if p]
    if parsed.scheme == "tg" and parsed.netloc.startswith("join"):
        invite = parse_qs(parsed.query).get("invite")
        if invite and invite[0]:
            return invite[0]
    if path_parts:
        first = path_parts[0]
        if first.startswith("+"):
            return first.lstrip("+") or None
        if first.lower() == "joinchat" and len(path_parts) >= 2:
            return path_parts[1] or None
    return None


def parse_join_target(raw: str) -> ParsedTelegramLink:
    """Parse a user supplied link/username into a joinable target.

    Accepted inputs:
    - https://t.me/+hash
    - https://t.me/joinchat/hash
    - tg://join?invite=hash
    - +hash (invite hash)
    - @username, username, https://t.me/username
    - Message links (https://t.me/username/123) are treated as public usernames
    """

    cleaned = _clean_input(raw)
    if not cleaned:
        raise ValueError("Empty link or username")
    if " " in cleaned:
        raise ValueError("Usernames or invites cannot contain spaces")

    if cleaned.startswith("+"):
        invite_hash = cleaned.lstrip("+")
        if not invite_hash:
            raise ValueError("Invite hash is missing")
        return ParsedTelegramLink(
            type="invite",
            normalized_url=f"https://t.me/+{invite_hash}",
            invite_hash=invite_hash,
        )

    if cleaned.startswith("@"):
        username = cleaned.lstrip("@")
        if not username:
            raise ValueError("Username is missing")
        return ParsedTelegramLink(
            type="public",
            normalized_url=f"https://t.me/{username}",
            username=username,
        )

    parsed = urlparse(cleaned if cleaned.startswith("http") or cleaned.startswith("tg") else f"https://{cleaned}")
    invite_hash = _parse_invite_hash_from_url(parsed)
    if invite_hash:
        return ParsedTelegramLink(
            type="invite",
            normalized_url=f"https://t.me/+{invite_hash}",
            invite_hash=invite_hash,
        )

    if parsed.netloc and parsed.netloc.endswith("t.me"):
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            raise ValueError("The t.me link is missing a username")
        username = path_parts[0].lstrip("@")
        if not username:
            raise ValueError("Username is missing")
        # Allow message/story suffixes; we only care about the username for joins
        username = re.sub(r"[^A-Za-z0-9_]+$", "", username)
        return ParsedTelegramLink(
            type="public",
            normalized_url=f"https://t.me/{username}",
            username=username,
        )

    if cleaned:
        return ParsedTelegramLink(
            type="public",
            normalized_url=f"https://t.me/{cleaned.lstrip('@')}",
            username=cleaned.lstrip("@"),
        )

    raise ValueError("Unsupported link or username")


def maybe_parse_join_target(raw: str) -> ParsedTelegramLink | None:
    try:
        return parse_join_target(raw)
    except Exception:
        return None


__all__ = [
    "ParsedTelegramLink",
    "ParsedMessageLink",
    "parse_join_target",
    "parse_message_link",
    "maybe_parse_join_target",
]
