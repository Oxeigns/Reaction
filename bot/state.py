from __future__ import annotations

from telegram.ext import ContextTypes


def profile_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("profile", {})


def flow_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("flow", {})


def reset_flow_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    context.user_data["flow"] = {}
    return context.user_data["flow"]


def saved_session_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return len(profile_state(context).get("saved_sessions", []))


def active_session_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    return len(flow_state(context).get("sessions", []))

__all__ = [
    "profile_state",
    "flow_state",
    "reset_flow_state",
    "saved_session_count",
    "active_session_count",
]
