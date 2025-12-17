from __future__ import annotations

import hashlib
from typing import Final

import config
from storage import DataStore

BOT_TOKEN: Final[str] = config.BOT_TOKEN
API_ID: Final[int | None] = getattr(config, "API_ID", None)
API_HASH: Final[str | None] = getattr(config, "API_HASH", None)


def ensure_token() -> str:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. Set it as an environment variable.")
    return BOT_TOKEN


def ensure_pyrogram_creds() -> None:
    if not (API_ID and API_HASH):
        raise RuntimeError("API_ID and API_HASH are required for Pyrogram sessions")


def verify_author_integrity(author_name: str, expected_hash: str) -> None:
    computed_hash = hashlib.sha256(author_name.encode("utf-8")).hexdigest()
    if computed_hash != expected_hash:
        print("Integrity check failed: unauthorized modification.")
        raise SystemExit(1)


data_store = DataStore(config.MONGO_URI)

__all__ = [
    "BOT_TOKEN",
    "API_ID",
    "API_HASH",
    "ensure_token",
    "ensure_pyrogram_creds",
    "verify_author_integrity",
    "data_store",
]
