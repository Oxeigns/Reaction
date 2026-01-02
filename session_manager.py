from __future__ import annotations

"""Session management utilities for the reporting worker.

This module discovers Pyrogram ``.session`` files on disk and prepares client
instances that can be started on demand. Only lightweight construction happens
here so callers can decide how and when to connect.
"""

from pathlib import Path
from typing import Iterable, List

from pyrogram import Client

import config


def scan_session_files(session_dir: str | Path = "sessions") -> List[Path]:
    """Return a list of available ``.session`` files sorted by name."""

    path = Path(session_dir)
    if not path.exists():
        return []

    return sorted([p for p in path.iterdir() if p.suffix == ".session" and p.is_file()])


def build_clients(session_files: Iterable[Path], api_id: int | None = None, api_hash: str | None = None) -> List[Client]:
    """Build Pyrogram :class:`Client` instances for the given session files."""

    clients: list[Client] = []
    api_id = api_id or config.API_ID
    api_hash = api_hash or config.API_HASH

    for session_file in session_files:
        clients.append(
            Client(
                name=session_file.stem,
                api_id=api_id,
                api_hash=api_hash,
                workdir=str(session_file.parent),
                no_updates=True,
            )
        )

    return clients
