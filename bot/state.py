from __future__ import annotations

import asyncio

import asyncio

from telegram.ext import ContextTypes


def profile_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("profile", {})


def flow_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("flow", {})


def reset_flow_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    context.user_data["flow"] = {}
    return context.user_data["flow"]


def clear_report_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove conversation-specific report data so fresh runs start cleanly."""
    context.user_data.pop("flow", None)
    context.user_data.pop("report", None)


def reset_user_context(context: ContextTypes.DEFAULT_TYPE, user_id: int | None = None) -> None:
    """Clear any per-user report context and cancel running tasks."""

    task = context.user_data.pop("active_report_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()

    clear_report_state(context)


def saved_session_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return len(profile_state(context).get("saved_sessions", []))


def active_session_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return len(flow_state(context).get("sessions", []))

__all__ = [
    "profile_state",
    "flow_state",
    "reset_flow_state",
    "clear_report_state",
    "reset_user_context",
    "saved_session_count",
    "active_session_count",
]
