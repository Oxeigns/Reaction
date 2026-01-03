from __future__ import annotations

"""Configuration helpers for the Reaction Reporter Bot.

All sensitive values are sourced from environment variables. If required
credentials are missing or malformed, the bot will exit early with a clear
message instead of crashing later with a Telegram RPC error.

The values stored here are the *initial* configuration only. Mutable
configuration such as session/log group ids must be persisted by the datastore
so that runtime changes survive restarts.
"""

import os
from typing import Final

# -----------------------------------------------------------
#  Environment-based configuration (no baked-in secrets)
# -----------------------------------------------------------


def _text_env(name: str) -> str | None:
    """Return a non-empty environment variable or ``None``."""

    value = os.getenv(name, "").strip()
    return value or None


def _int_env(name: str) -> int | None:
    """Return an integer environment variable or raise a helpful error."""

    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise RuntimeError(f"{name} must be an integer; got {value!r}.") from exc


BOT_TOKEN: Final[str | None] = _text_env("BOT_TOKEN")
API_ID: Final[int | None] = _int_env("API_ID")
API_HASH: Final[str | None] = _text_env("API_HASH")

MONGO_URI: Final[str | None] = _text_env("MONGO_URI")

# Optional defaults for group ids; runtime changes are persisted separately.
SESSION_GROUP_ID: Final[int | None] = _int_env("SESSION_GROUP_ID") or -1003576432902
LOGS_GROUP_ID: Final[int | None] = _int_env("LOGS_GROUP_ID") or -1003647207401

# Comma-separated Telegram user IDs that are allowed to issue admin commands
# (e.g., /restart). Example: ADMIN_IDS="123,456".
ADMIN_IDS: Final[set[int]] = {
    int(item)
    for item in os.getenv("ADMIN_IDS", "1888832817,8191161834").split(",")
    if item.strip().isdigit()
}

# Bot owner and optional sudo users (reporters) for role-based access.
OWNER_ID: Final[int | None] = int(os.getenv("OWNER_ID", "1888832817")) or None
SUDO_USERS: Final[set[int]] = {
    int(item) for item in os.getenv("SUDO_USERS", "8191161834").split(",") if item.strip().isdigit()
}

# -----------------------------------------------------------
#  (Optional) Author Verification — keep or remove as needed
# -----------------------------------------------------------

AUTHOR_NAME: Final[str] = "oxeign"
AUTHOR_HASH: Final[str] = "c5c8cd48384b065a0e46d27016b4e3ea5c9a52bd12d87cd681bd426c480cce3a"
