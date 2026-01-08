from __future__ import annotations

import hashlib
import logging
from typing import Final

import config
from storage import build_datastore

# Extract constants from config
BOT_TOKEN: Final[str | None] = getattr(config, "BOT_TOKEN", None)
API_ID: Final[int | None] = getattr(config, "API_ID", None)
API_HASH: Final[str | None] = getattr(config, "API_HASH", None)

def ensure_token() -> str:
    """Ensures the Bot Token exists, preventing startup without a gateway."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Please set the BOT_TOKEN environment variable."
        )
    return BOT_TOKEN

def ensure_pyrogram_creds() -> None:
    """
    Validates API_ID and API_HASH. 
    Crucial because Pyrogram workers require these to sign every MTProto request.
    """
    if not API_ID or not API_HASH:
        raise RuntimeError(
            "API_ID and API_HASH are required for MTProto reporting. "
            "Get them from https://my.telegram.org"
        )

def verify_author_integrity(author_name: str, expected_hash: str) -> None:
    """Security check to ensure the core attribution hasn't been tampered with."""
    computed_hash = hashlib.sha256(author_name.encode("utf-8")).hexdigest()
    if computed_hash != expected_hash:
        logging.critical("Integrity check failed: unauthorized modification of author credits.")
        raise SystemExit(1)

class _LazyDataStore:
    """
    Proxy pattern: Delays MongoDB connection until the first time 
    the data_store is actually accessed.
    """
    def __init__(self) -> None:
        self._instance = None

    def get(self):
        if self._instance is None:
            # We use the factory we built in persistence.py
            self._instance = build_datastore(config.MONGO_URI)
        return self._instance

    def __getattr__(self, item):
        """Delegates all calls (like .add_sessions) to the actual DataStore instance."""
        return getattr(self.get(), item)

# Global proxy instance
_data_store_proxy = _LazyDataStore()

def get_data_store():
    """Function-based access to the datastore."""
    return _data_store_proxy.get()

# Object-based access to the datastore
data_store = _data_store_proxy

__all__ = [
    "BOT_TOKEN",
    "API_ID",
    "API_HASH",
    "ensure_token",
    "ensure_pyrogram_creds",
    "verify_author_integrity",
    "get_data_store",
    "data_store",
]
