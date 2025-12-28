"""Async storage helpers for session strings and report summaries."""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Iterable

try:  # pragma: no cover - optional dependency
    import motor.motor_asyncio as motor_asyncio
    _motor_import_error = None
except Exception as exc:  # pragma: no cover - defensive fallback
    motor_asyncio = None
    _motor_import_error = exc


class DataStore:
    """Persist session strings and report audit records."""

    def __init__(
        self,
        mongo_uri: str | None = None,
        *,
        db_name: str = "reporter",
        mongo_env_var: str = "MONGO_URI",
    ) -> None:
        # Resolve the URI from config or environment so Heroku users can enable
        # persistence without code changes.
        self.mongo_env_var = mongo_env_var
        self.mongo_uri = mongo_uri or os.getenv(self.mongo_env_var, "")
        self._in_memory_sessions: set[str] = set()
        self._in_memory_reports: list[dict] = []

        self.client = None
        self.db = None
        if self.mongo_uri:
            if motor_asyncio is None:
                raise RuntimeError(
                    "MongoDB URI provided but Motor is unavailable; install 'motor' to enable persistence. "
                    f"Import error: {_motor_import_error}"
                )
            else:
                try:
                    self.client = motor_asyncio.AsyncIOMotorClient(self.mongo_uri)
                    self.db = self.client.get_default_database() or self.client[db_name]
                    logging.info("Connected to MongoDB for session persistence.")
                except Exception:
                    logging.exception(
                        "Failed to initialize MongoDB client with %s; falling back to in-memory storage.",
                        self.mongo_env_var,
                    )
                    self.client = None
                    self.db = None
        else:
            logging.info(
                "MongoDB persistence disabled; set %s to a MongoDB connection URI to enable it.",
                self.mongo_env_var,
            )

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

    async def close(self) -> None:
        if self.client:
            self.client.close()

    @property
    def is_persistent(self) -> bool:
        """Expose whether MongoDB is available for callers that want to log mode."""

        return bool(self.db)
