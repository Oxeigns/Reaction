from __future__ import annotations

import json
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable, Iterable

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

SUDO_STORE = Path(__file__).with_name("sudo_users.json")
OWNERS: set[int] = {1888832817, 8191161834}
UNAUTHORIZED_MESSAGE = "You are unauthorised. Take sudo from owner to use this bot."


@dataclass
class SudoUser:
    user_id: int
    username: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict) -> "SudoUser":
        return cls(int(payload.get("user_id")), payload.get("username"))

    def to_mapping(self) -> dict:
        return {"user_id": self.user_id, "username": self.username}


def _load_store() -> list[SudoUser]:
    if not SUDO_STORE.exists():
        return []
    try:
        data = json.loads(SUDO_STORE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    users: list[SudoUser] = []
    for item in data:
        if isinstance(item, dict) and "user_id" in item:
            try:
                users.append(SudoUser.from_mapping(item))
            except Exception:
                continue
    return users


def _save_store(users: Iterable[SudoUser]) -> None:
    payload = [user.to_mapping() for user in users]
    SUDO_STORE.write_text(json.dumps(payload, indent=2))


def list_sudo_users() -> list[SudoUser]:
    return _load_store()


def is_owner(user_id: int | None) -> bool:
    return bool(user_id) and user_id in OWNERS


def is_sudo(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if is_owner(user_id):
        return True
    return any(user.user_id == user_id for user in list_sudo_users())


def add_sudo_user(user_id: int, username: str | None = None) -> bool:
    if is_owner(user_id):
        return False
    users = list_sudo_users()
    if any(user.user_id == user_id for user in users):
        return False
    users.append(SudoUser(user_id=user_id, username=username))
    _save_store(users)
    return True


def remove_sudo_user(user_id: int) -> bool:
    users = list_sudo_users()
    remaining = [user for user in users if user.user_id != user_id]
    if len(remaining) == len(users):
        return False
    _save_store(remaining)
    return True


async def _unauthorized_reply(update: Update) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer(UNAUTHORIZED_MESSAGE, show_alert=True)
        except Exception:
            pass
    if update.effective_message:
        try:
            await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
        except Exception:
            pass


def require_owner(func: Callable) -> Callable:
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not is_owner(user_id):
            await _unauthorized_reply(update)
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


async def auth_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if is_sudo(user_id):
        return

    await _unauthorized_reply(update)
    raise ApplicationHandlerStop


@require_owner
async def addsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /addsudo <numeric_id> <username>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("First argument must be a numeric user ID.")
        return

    username = context.args[1] if len(context.args) > 1 else None
    added = add_sudo_user(target_id, username)
    if added:
        display = f"{target_id} ({username})" if username else str(target_id)
        await update.effective_message.reply_text(f"Added sudo user: {display}")
    else:
        await update.effective_message.reply_text("User already sudo or is an owner; nothing changed.")


@require_owner
async def rmsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /rmsudo <numeric_id> <username>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("First argument must be a numeric user ID.")
        return

    removed = remove_sudo_user(target_id)
    username = context.args[1] if len(context.args) > 1 else None
    if removed:
        display = f"{target_id} ({username})" if username else str(target_id)
        await update.effective_message.reply_text(f"Removed sudo user: {display}")
    else:
        await update.effective_message.reply_text("User not found in sudo list.")


@require_owner
async def sudolist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = list_sudo_users()
    if not users:
        await update.effective_message.reply_text("No sudo users configured.")
        return

    lines = []
    for user in users:
        username = f" (@{user.username})" if user.username else ""
        lines.append(f"{user.user_id}{username}")

    await update.effective_message.reply_text("\n".join(lines))


__all__ = [
    "OWNERS",
    "UNAUTHORIZED_MESSAGE",
    "auth_guard",
    "addsudo_command",
    "rmsudo_command",
    "sudolist_command",
    "is_owner",
    "is_sudo",
]
