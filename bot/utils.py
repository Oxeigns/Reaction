from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from pyrogram import Client

from bot.dependencies import API_HASH, API_ID


def friendly_error(message: str) -> str:
    return f"⚠️ {message}\nUse the menu below or try again."


def parse_reasons(text: str) -> list[str]:
    reasons = [line.strip() for line in text.replace(";", "\n").splitlines() if line.strip()]
    return reasons[:5]


def parse_links(text: str) -> list[str]:
    links: list[str] = []
    for chunk in text.replace(";", "\n").split():
        if is_valid_link(chunk):
            links.append(chunk)
    return links[:5]


def is_valid_link(text: str) -> bool:
    text = text.strip()
    parsed = urlparse(text if text.startswith("http") else f"https://{text}")
    return parsed.netloc.endswith("t.me") and len(parsed.path.strip("/")) > 0


def parse_telegram_url(url: str) -> dict:
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    path_parts = [p for p in parsed.path.split("/") if p]

    if not parsed.netloc.endswith("t.me") or not path_parts:
        raise ValueError("Invalid Telegram URL")

    if path_parts[0].startswith("+"):
        return {"type": "invite", "invite_link": f"https://t.me/{path_parts[0]}"}

    if path_parts[0] == "c" and len(path_parts) >= 3:
        return {
            "type": "private_message",
            "chat_id": int(f"-100{path_parts[1]}"),
            "message_id": int(path_parts[2]),
        }

    if len(path_parts) >= 3 and path_parts[1] in {"s", "story"}:
        return {
            "type": "story",
            "username": path_parts[0],
            "story_id": path_parts[2],
        }

    if len(path_parts) >= 2:
        return {
            "type": "public_message",
            "username": path_parts[0],
            "message_id": int(path_parts[1]),
        }

    if len(path_parts) == 1:
        return {"type": "username", "username": path_parts[0]}

    raise ValueError("Unrecognized Telegram URL format")


def extract_target_identifier(text: str) -> str:
    text = text.strip()
    if text.startswith("@"):  # username
        return text[1:]

    parsed = urlparse(text if text.startswith("http") else f"https://{text}")
    path = parsed.path.lstrip("/")
    return path.split("/", maxsplit=1)[0]


def session_strings_from_text(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


async def validate_sessions(api_id: int, api_hash: str, sessions: list[str]) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    invalid: list[str] = []

    for idx, session in enumerate(sessions):
        client = Client(
            name=f"validation_{idx}", api_id=api_id, api_hash=api_hash, session_string=session, workdir=f"/tmp/validate_{idx}"
        )
        try:
            await client.start()
            valid.append(session)
        except Exception:
            invalid.append(session)
        finally:
            try:
                await client.stop()
            except Exception:
                pass

    return valid, invalid


async def resolve_chat_id(client: Client, target: str, invite_link: str | None = None):
    details = parse_telegram_url(target)

    if details["type"] == "invite":
        chat = await client.get_chat(details["invite_link"])
        return chat.id

    if details["type"] == "private_message":
        chat_id = details["chat_id"]

        if invite_link:
            chat = await client.join_chat(invite_link)
            chat_id = getattr(chat, "id", chat_id)
        else:
            chat = await client.get_chat(chat_id)
            chat_id = getattr(chat, "id", chat_id)

        await client.get_messages(chat_id, details["message_id"])
        return chat_id

    if details["type"] == "public_message":
        chat = await client.get_chat(details["username"])
        await client.get_messages(chat.id, details["message_id"])
        return chat.id

    if details["type"] == "story":
        chat = await client.get_chat(details["username"])
        return chat.id

    if details["type"] == "username":
        chat = await client.get_chat(details["username"])
        return chat.id

    raise ValueError("Unsupported Telegram link format")


async def validate_targets(
    targets: list[str],
    sessions: list[str],
    api_id: int | None,
    api_hash: str | None,
    invite_link: str | None = None,
) -> tuple[bool, str | None]:
    if not targets:
        return False, "No targets provided for validation."

    if not sessions:
        return False, "No sessions available to validate the provided targets."

    if not (api_id and api_hash):
        api_id = API_ID
        api_hash = API_HASH

    last_error: str | None = None

    for idx, session in enumerate(sessions):
        client = Client(
            name=f"target_validator_{idx}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            workdir=f"/tmp/target_validator_{idx}",
        )

        try:
            await client.start()

            for target in targets:
                try:
                    await resolve_chat_id(client, target, invite_link)
                except Exception as exc:  # noqa: BLE001 - allow detailed error messaging
                    last_error = f"The link '{target}' is not valid: {exc}."
                    raise

            return True, None
        except Exception:
            continue
        finally:
            try:
                await client.stop()
            except Exception:
                pass

    return False, last_error

__all__ = [
    "friendly_error",
    "parse_reasons",
    "parse_links",
    "is_valid_link",
    "parse_telegram_url",
    "extract_target_identifier",
    "session_strings_from_text",
    "validate_sessions",
    "resolve_chat_id",
    "validate_targets",
]
