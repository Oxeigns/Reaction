from __future__ import annotations

"""Configuration helpers for the Reaction Reporter Bot.

Fill the values below with your BOT TOKEN, API ID, API HASH, and MONGO URI
before deploying. This avoids mistakes with environment variables.
"""

from typing import Final

# -----------------------------------------------------------
#  🔴 FILL THESE VALUES CAREFULLY BEFORE DEPLOYMENT
# -----------------------------------------------------------

BOT_TOKEN: Final[str] = "8549633097:AAGeb2iAfIHCiSQJn5uKUqN8IHr7vztl6bU"

API_ID: Final[int] = 27989579        # ← Enter your API ID (integer)
API_HASH: Final[str] = "64742ebe270a7d202150134d66397839"

MONGO_URI: Final[str] = "mongodb+srv://annieregain:firstowner8v@anniere.ht2en.mongodb.net/?retryWrites=true&w=majority&appName=AnnieRE"

# -----------------------------------------------------------
#  (Optional) Author Verification — keep or remove as needed
# -----------------------------------------------------------

AUTHOR_NAME: Final[str] = "oxeign"
AUTHOR_HASH: Final[str] = "c5c8cd48384b065a0e46d27016b4e3ea5c9a52bd12d87cd681bd426c480cce3a"
