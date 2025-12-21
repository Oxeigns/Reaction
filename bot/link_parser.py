"""Robust parsing of Telegram join and message links."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog", "www.t.me", "www.telegram.me"}


@dataclass
class JoinLink:
    kind: str  # public_username | invite_hash
    value: str
    raw: str


@dataclass
class MessageLink:
    kind: str  # public_msg | private_c_msg
    chat_ref: str | int
    msg_id: int
    raw: str


def normalize_url(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("tg://resolve"):
        parsed = urlparse(cleaned)
        qs = parse_qs(parsed.query)
        domain = qs.get("domain", [""])[0]
        start = qs.get("start", [None])[0]
        if domain:
            cleaned = f"https://t.me/{domain}"
            if start:
                cleaned = f"{cleaned}/{start}"
    if not re.match(r"^[a-z]+://", cleaned, re.I):
        cleaned = "https://" + cleaned
    parsed = urlparse(cleaned)
    normalized = parsed._replace(query="", fragment="")
    return normalized.geturl()


def _match_host(parsed) -> bool:
    return parsed.netloc.lower() in TELEGRAM_HOSTS


def parse_join_link(text: str) -> Optional[JoinLink]:
    url = normalize_url(text)
    parsed = urlparse(url)
    if not _match_host(parsed):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None

    if parts[0].startswith("+"):
        return JoinLink("invite_hash", parts[0].lstrip("+"), raw=url)
    if parts[0].lower() == "joinchat" and len(parts) >= 2:
        return JoinLink("invite_hash", parts[1], raw=url)

    # Public username join
    if len(parts) == 1 and re.match(r"^[A-Za-z0-9_]{5,}$", parts[0]):
        return JoinLink("public_username", parts[0], raw=url)
    return None


def parse_message_link(text: str) -> Optional[MessageLink]:
    url = normalize_url(text)
    parsed = urlparse(url)
    if not _match_host(parsed):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None

    # Private/supergroup messages t.me/c/<internal>/<msg>
    if parts[0] == "c" and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        chat_id = int(f"-100{parts[1]}")
        return MessageLink("private_c_msg", chat_id, int(parts[2]), raw=url)

    # Public message link
    if re.match(r"^[A-Za-z0-9_]{5,}$", parts[0]) and parts[1].isdigit():
        return MessageLink("public_msg", parts[0], int(parts[1]), raw=url)

    return None


__all__ = [
    "JoinLink",
    "MessageLink",
    "normalize_url",
    "parse_join_link",
    "parse_message_link",
]
