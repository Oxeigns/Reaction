from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Iterable, Any

import config

class DataStore:
    """Persist session strings, chat configuration, and report audit records."""

    def __init__(
        self,
        client: Any = None,
        db: Any = None,
        *,
        mongo_uri: str | None = None,
        db_name: str = "reporter",
        mongo_env_var: str = "MONGO_URI",
    ) -> None:
        self.mongo_uri = mongo_uri or os.getenv(mongo_env_var, "")
        self._in_memory_sessions: set[str] = set()
        self._in_memory_reports: list[dict] = []
        self._config_defaults: dict[str, int | None] = {
            "session_group": config.SESSION_GROUP_ID,
            "logs_group": config.LOGS_GROUP_ID,
        }
        self._in_memory_config: dict[str, int | None] = dict(self._config_defaults)
        self._in_memory_chats: set[int] = set()
        self._sudo_users: set[int] = set(config.SUDO_USERS)
        self._snapshot_path: Path | None = None

        self.client = client
        self.db = db

    def _persist_snapshot(self) -> None:
        """Hook for subclasses to write the current in-memory state."""
        pass

    def _update_from_snapshot(self, payload: dict) -> None:
        self._in_memory_sessions = set(payload.get("sessions", []))
        self._in_memory_reports = payload.get("reports", [])
        self._in_memory_config.update(payload.get("config", {}))
        self._in_memory_chats = set(payload.get("chats", []))
        self._sudo_users = set(payload.get("sudo", [])) or set(config.SUDO_USERS)

    # ------------------- Session storage -------------------
    async def add_sessions(self, sessions: Iterable[str], added_by: int | None = None) -> list[str]:
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

        self._persist_snapshot()
        return added

    async def get_sessions(self) -> list[str]:
        if self.db:
            cursor = self.db.sessions.find({}, {"_id": False, "session": True})
            return [doc["session"] async for doc in cursor]
        return list(self._in_memory_sessions)

    async def remove_sessions(self, sessions: Iterable[str]) -> int:
        targets = {s for s in sessions if s}
        if not targets: return 0

        removed = 0
        if self.db:
            result = await self.db.sessions.delete_many({"session": {"$in": list(targets)}})
            removed = result.deleted_count
        else:
            for session in list(targets):
                if session in self._in_memory_sessions:
                    self._in_memory_sessions.discard(session)
                    removed += 1
        self._persist_snapshot()
        return removed

    # ------------------- Config storage -------------------
    async def save_session_group_id(self, chat_id: int) -> None:
        self._in_memory_config["session_group"] = chat_id
        await self._set_config_value("session_group", chat_id)

    async def get_session_group_id(self) -> int | None:
        return await self._get_config_value("session_group")

    async def save_logs_group_id(self, chat_id: int) -> None:
        self._in_memory_config["logs_group"] = chat_id
        await self._set_config_value("logs_group", chat_id)

    async def get_logs_group_id(self) -> int | None:
        return await self._get_config_value("logs_group")

    async def _set_config_value(self, key: str, value: int | None) -> None:
        if self.db:
            await self.db.config.update_one(
                {"key": key}, 
                {"$set": {"value": value}}, 
                upsert=True
            )
        else:
            self._in_memory_config[key] = value
        self._persist_snapshot()

    async def _get_config_value(self, key: str) -> int | None:
        if self.db:
            doc = await self.db.config.find_one({"key": key})
            if doc: return doc.get("value")
        return self._in_memory_config.get(key) or self._config_defaults.get(key)

    # ------------------- Sudo users -------------------
    async def add_sudo_user(self, user_id: int) -> None:
        self._sudo_users.add(user_id)
        config.SUDO_USERS.add(user_id)
        if self.db:
            await self.db.sudo.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)
        self._persist_snapshot()

    async def get_sudo_users(self) -> set[int]:
        if self.db:
            users = await self.db.sudo.find().to_list(None)
            return {u["user_id"] for u in users if "user_id" in u}
        return set(self._sudo_users)

    async def add_known_chat(self, chat_id: int) -> None:
        if self.db:
            await self.db.chats.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True)
        else:
            self._in_memory_chats.add(chat_id)
        self._persist_snapshot()

    async def close(self) -> None:
        if self.client:
            self.client.close()
