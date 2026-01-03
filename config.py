from __future__ import annotations

"""Configuration helpers for the Reaction Reporter Bot.

Fill the values below with your BOT TOKEN, API ID, API HASH, and MONGO URI
before deploying. This avoids mistakes with environment variables.
"""

import os
from typing import Final

# -----------------------------------------------------------
#  🔴 FILL THESE VALUES CAREFULLY BEFORE DEPLOYMENT
# -----------------------------------------------------------

BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "8549633097:AAGeb2iAfIHCiSQJn5uKUqN8IHr7vztl6bU")

API_ID: Final[int | None] = int(os.getenv("API_ID", "27989579")) or None
API_HASH: Final[str] = os.getenv("API_HASH", "64742ebe270a7d202150134d66397839")

MONGO_URI: Final[str] = os.getenv(
    "MONGO_URI",
    "mongodb+srv://annieregain:firstowner8v@anniere.ht2en.mongodb.net/?retryWrites=true&w=majority&appName=AnnieRE",
)

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
