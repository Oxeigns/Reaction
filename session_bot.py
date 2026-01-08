from __future__ import annotations

"""Pyrogram client builder and session utilities."""

import contextlib
import logging
from dataclasses import dataclass
import re
import uuid
from typing import Iterable, Tuple

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError

import config
from state import ReportQueue, StateManager
from storage import build_datastore

# Regex for URL-safe base64 strings common in Pyrogram
_SESSION_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_-]+$")

def is_session_string(session: str) -> bool:
    """Heuristically determine if text looks like a Pyrogram session string."""
    s = (session or "").strip()
    # Pyrogram String Sessions are typically > 200 chars. 
    # v2 strings are even longer. 80 is a safe minimum.
    if len(s) < 80:
        return False
    return bool(_SESSION_ALLOWED_RE.match(s))

async def validate_session_string(session: str) -> bool:
    """
    Ensure a Pyrogram session string can start and access basic info.
    """
    if not is_session_string(session):
        return False

    # name must be unique to avoid SQLite 'database is locked' errors
    client = Client(
        name=f"val_{uuid.uuid4().hex[:8]}",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=session.strip(),
        in_memory=True,
        no_updates=True,
    )

    try:
        await client.start()
        await client.get_me()
        return True
    except FloodWait as e:
        logging.warning(f"Session validation hit FloodWait ({e.value}s); assuming valid.")
        return True
    except RPCError:
        return False
    except Exception as e:
        logging.error(f"Unexpected validation error: {e}")
        return False
    finally:
        with contextlib.suppress(Exception):
            await client.stop()

async def validate_sessions(sessions: Iterable[str]) -> Tuple[list[str], list[str]]:
    """Batch validate sessions and return (valid_list, invalid_list)."""
    valid, invalid = [], []
    for s in sessions:
        if await validate_session_string(s):
            valid.append(s)
        else:
            invalid.append(s)
    return valid, invalid

async def prune_sessions(persistence, *, announce: bool = False) -> list[str]:
    """Scans DB, removes dead sessions, returns the healthy ones."""
    sessions = await persistence.get_sessions()
    if not sessions:
        return []
    
    valid, invalid = await validate_sessions(sessions)
    if invalid:
        await persistence.remove_sessions(invalid)
        if announce:
            logging.info(f"Pruned {len(invalid)} dead sessions from database.")
    return valid

def extract_sessions_from_text(text: str) -> list[str]:
    """Extracts strings that look like Pyrogram sessions from a block of text."""
    # Split by any whitespace or newlines
    parts = re.split(r'\s+', text or "")
    return [p.strip() for p in parts if is_session_string(p)]

def create_bot() -> tuple[Client, any, StateManager, ReportQueue]:
    """Initializes the main bot instance and its dependencies."""
    persistence = build_datastore(config.MONGO_URI)
    queue = ReportQueue()
    states = StateManager()

    app = Client(
        "reaction-reporter",
        bot_token=config.BOT_TOKEN,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        in_memory=True
    )

    return app, persistence, states, queue
