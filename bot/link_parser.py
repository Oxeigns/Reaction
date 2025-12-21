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


_TRAILING_PUNCTUATION = ",.;)]}>'\""


def _clean_input(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = cleaned.rstrip(_TRAILING_PUNCTUATION)
    return cleaned


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


__all__ = ["ParsedTelegramLink", "parse_join_target", "maybe_parse_join_target"]
