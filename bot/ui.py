from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.constants import MENU_LIVE_STATUS, MAX_SESSIONS, MIN_SESSIONS, REASON_LABELS


def main_menu_keyboard(saved_sessions: int = 0, active_sessions: int = 0, live_status: str = MENU_LIVE_STATUS) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Start report", callback_data="action:start")],
            [InlineKeyboardButton("🧩 Add sessions", callback_data="action:add")],
            [InlineKeyboardButton("💾 Saved sessions", callback_data="action:sessions")],
            [
                InlineKeyboardButton(f"🟢 {live_status} · Dark UI", callback_data="status:live"),
                InlineKeyboardButton(f"🎯 Loaded: {active_sessions}", callback_data="status:active"),
                InlineKeyboardButton(f"📦 Saved: {saved_sessions}", callback_data="status:saved"),
            ],
        ]
    )


def target_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Private Channel / Private Group", callback_data="kind:private")],
            [InlineKeyboardButton("Public Channel / Public Group", callback_data="kind:public")],
            [InlineKeyboardButton("Story URL (Profile Story)", callback_data="kind:story")],
        ]
    )


def reason_keyboard() -> InlineKeyboardMarkup:
    """Buttons covering the available Pyrogram/Telegram report reasons."""

    buttons = [
        InlineKeyboardButton(REASON_LABELS[0], callback_data="reason:0"),
        InlineKeyboardButton(REASON_LABELS[3], callback_data="reason:3"),
        InlineKeyboardButton(REASON_LABELS[2], callback_data="reason:2"),
        InlineKeyboardButton(REASON_LABELS[1], callback_data="reason:1"),
        InlineKeyboardButton(REASON_LABELS[6], callback_data="reason:6"),
        InlineKeyboardButton(REASON_LABELS[4], callback_data="reason:4"),
        InlineKeyboardButton(REASON_LABELS[5], callback_data="reason:5"),
    ]

    rows = [
        buttons[0:2],
        buttons[2:4],
        buttons[4:6],
        [buttons[6]],
    ]

    return InlineKeyboardMarkup(
        rows
    )


def session_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Report with saved sessions", callback_data="session_mode:reuse")],
            [InlineKeyboardButton("Add new sessions", callback_data="session_mode:new")],
        ]
    )


def render_greeting() -> str:
    return (
        "━━━━━━━✦ DARK MODE ONLINE ✦━━━━━━━╮\n"
        "🤖 *Nightfall Reporter* — premium chat cockpit engaged.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n"
        "🖤 Polished bubbles, elevated reply cards, and tactile pill buttons are live.\n"
        "🌙 Start reporting instantly with saved creds or add new sessions on the fly.\n"
        "✨ Dynamic status chips below keep you oriented as you move through each step.\n"
        "\nTap a control to begin."
    )

__all__ = [
    "main_menu_keyboard",
    "target_kind_keyboard",
    "reason_keyboard",
    "session_mode_keyboard",
    "render_greeting",
]
