from __future__ import annotations

"""Pyrogram client builder and session utilities."""

import contextlib
import logging
from typing import Iterable, Tuple

from pyrogram import Client

import config
from state import ReportQueue, StateManager
from storage import build_datastore


async def validate_session_string(session: str) -> bool:
    """Ensure a Pyrogram session string can start and access basic info."""

    client = Client(
        name="validator",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=session,
        workdir="/tmp/validator",
    )
    try:
        await client.start()
        await client.get_me()
        return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.stop()


async def validate_sessions(sessions: Iterable[str]) -> Tuple[list[str], list[str]]:
    valid: list[str] = []
    invalid: list[str] = []
    for session in sessions:
        if await validate_session_string(session):
            valid.append(session)
        else:
            invalid.append(session)
    return valid, invalid


async def prune_sessions(persistence, *, announce: bool = False) -> list[str]:
    """Remove invalid sessions and return the surviving ones."""

    sessions = await persistence.get_sessions()
    if not sessions:
        return []
    valid, invalid = await validate_sessions(sessions)
    if invalid:
        await persistence.remove_sessions(invalid)
        if announce:
            logging.warning("Removed %s invalid sessions", len(invalid))
    return valid


def extract_sessions_from_text(text: str) -> list[str]:
    """Parse potential session strings from raw text."""

    candidates = [part.strip() for part in text.split() if len(part.strip()) > 50]
    return [candidate for candidate in candidates if ":" in candidate or len(candidate) > 80]


def create_bot() -> tuple[Client, object, StateManager, ReportQueue]:
    persistence = build_datastore(config.MONGO_URI)
    queue = ReportQueue()
    states = StateManager()

    app = Client(
        "reaction-reporter",
        bot_token=config.BOT_TOKEN,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
    )

    return app, persistence, states, queue

