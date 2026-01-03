"""Async storage helpers for sessions, configuration, and audit logs."""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Iterable


class DataStore:
    """Persist session strings, chat configuration, and report audit records."""

    def __init__(
        self,
        client,
        db,
        *,
        mongo_uri: str | None = None,
        db_name: str = "reporter",
        mongo_env_var: str = "MONGO_URI",
    ) -> None:
        self.mongo_env_var = mongo_env_var
        self.mongo_uri = mongo_uri or os.getenv(self.mongo_env_var, "")
        self._in_memory_sessions: set[str] = set()
        self._in_memory_reports: list[dict] = []
        self._in_memory_config: dict[str, int | None] = {"session_group": None, "logs_group": None}
        self._in_memory_chats: set[int] = set()

        self.client = client
        self.db = db or (self.client.get_default_database() if self.client else None)
        if not self.db and self.client:
            self.db = self.client[db_name]

    # ------------------- Session storage -------------------
    async def add_sessions(self, sessions: Iterable[str], added_by: int | None = None) -> list[str]:
        """Add unique session strings and return the list that were newly stored."""

        added: list[str] = []
        normalized = [s.strip() for s in sessions if s and s.strip()]

        if self.db:
            for session in normalized:
                result = await self.db.sessions.update_one(
                    {"session": session},
                    {
                        "$setOnInsert": {
                            "created_at": dt.datetime.utcnow(),
                            "added_by": added_by,
                        }
                    },
                    upsert=True,
                )
                if result.upserted_id:
                    added.append(session)
        else:
            for session in normalized:
                if session not in self._in_memory_sessions:
                    self._in_memory_sessions.add(session)
                    added.append(session)

        return added

    async def get_sessions(self) -> list[str]:
        """Return all known session strings."""

        if self.db:
            cursor = self.db.sessions.find({}, {"_id": False, "session": True})
            return [doc["session"] async for doc in cursor]

        return list(self._in_memory_sessions)

    async def remove_sessions(self, sessions: Iterable[str]) -> int:
        """Remove sessions from persistence, returning the count removed."""

        targets = {s for s in sessions if s}
        if not targets:
            return 0

        removed = 0
        if self.db:
            result = await self.db.sessions.delete_many({"session": {"$in": list(targets)}})
            removed = getattr(result, "deleted_count", 0)
        else:
            for session in list(targets):
                if session in self._in_memory_sessions:
                    self._in_memory_sessions.discard(session)
                    removed += 1

        return removed

    # ------------------- Report records -------------------
    async def record_report(self, payload: dict) -> None:
        """Persist a report summary payload."""

        payload = {
            **payload,
            "stored_at": dt.datetime.utcnow(),
        }
        if self.db:
            await self.db.reports.insert_one(payload)
        else:
            self._in_memory_reports.append(payload)

    # ------------------- Config storage -------------------
    async def set_session_group(self, chat_id: int) -> None:
        await self._set_config_value("session_group", chat_id)

    async def session_group(self) -> int | None:
        return await self._get_config_value("session_group")

    async def set_logs_group(self, chat_id: int) -> None:
        await self._set_config_value("logs_group", chat_id)

    async def logs_group(self) -> int | None:
        return await self._get_config_value("logs_group")

    async def _set_config_value(self, key: str, value: int | None) -> None:
        if self.db:
            await self.db.config.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)
        else:
            self._in_memory_config[key] = value

    async def _get_config_value(self, key: str) -> int | None:
        if self.db:
            doc = await self.db.config.find_one({"_id": "config"}, {"_id": False, key: True})
            return doc.get(key) if doc else None
        return self._in_memory_config.get(key)

    # ------------------- Known chats -------------------
    async def add_known_chat(self, chat_id: int) -> None:
        if self.db:
            await self.db.chats.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True)
        else:
            self._in_memory_chats.add(chat_id)

    async def known_chats(self) -> list[int]:
        if self.db:
            cursor = self.db.chats.find({}, {"_id": False, "chat_id": True})
            return [doc["chat_id"] async for doc in cursor]
        return list(self._in_memory_chats)

    # ------------------- Lifecycle -------------------
    async def close(self) -> None:
        if self.client:
            self.client.close()

    @property
    def is_persistent(self) -> bool:
        """Expose whether MongoDB is available for callers that want to log mode."""

        return bool(self.db)


class FallbackDataStore(DataStore):
    """In-memory persistence used when MongoDB is unavailable."""

    def __init__(self) -> None:
        super().__init__(client=None, db=None)

    async def close(self) -> None:
        return None

    @property
    def is_persistent(self) -> bool:  # pragma: no cover - small override
        return False


def build_datastore(
    mongo_uri: str | None,
    *,
    db_name: str = "reporter",
    mongo_env_var: str = "MONGO_URI",
) -> DataStore:
    """Build a datastore safely, falling back when MongoDB is unavailable."""

    resolved_uri = mongo_uri or os.getenv(mongo_env_var, "")
    if not resolved_uri:
        logging.warning(
            "MongoDB persistence disabled; set %s to a MongoDB connection URI to enable it.",
            mongo_env_var,
        )
        return FallbackDataStore()

    try:  # pragma: no cover - optional dependency
        import motor.motor_asyncio as motor_asyncio
    except Exception as exc:
        logging.warning(
            "MongoDB URI provided but Motor is unavailable; falling back to in-memory storage. Import error: %s",
            exc,
        )
        return FallbackDataStore()

    try:
        client = motor_asyncio.AsyncIOMotorClient(resolved_uri)
        db = client.get_default_database() or client[db_name]
        logging.info("Connected to MongoDB for session persistence.")
        return DataStore(client, db, mongo_uri=resolved_uri, db_name=db_name, mongo_env_var=mongo_env_var)
    except Exception as exc:
        logging.warning(
            "Failed to initialize MongoDB client with %s; falling back to in-memory storage: %s",
            mongo_env_var,
            exc,
        )
        return FallbackDataStore()

