from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.constants import MENU_LIVE_STATUS, MAX_SESSIONS, MIN_SESSIONS, REASON_LABELS

CARD_WIDTH = 86


def render_card(title: str, body_lines: list[str] | tuple[str, ...], footer_lines: list[str] | tuple[str, ...] | None = None) -> str:
    body_lines = list(body_lines)
    footer_lines = list(footer_lines or [])
    hint = "If you’re facing any issues, tap 🔄 Restart Bot or type /restart"
    if hint not in footer_lines:
        footer_lines.append(hint)

    def _pad_line(content: str) -> str:
        trimmed = content[: CARD_WIDTH - 4]
        padding = " " * (CARD_WIDTH - 4 - len(trimmed))
        return f"│ {trimmed}{padding} │"

    title_space = CARD_WIDTH - len(title) - 4
    left = max(2, title_space // 2)
    right = max(2, CARD_WIDTH - len(title) - 2 - left)
    top = f"┌{'─' * left} {title} {'─' * right}┐"
    divider = f"├{'─' * (CARD_WIDTH - 2)}┤"
    bottom = f"└{'─' * (CARD_WIDTH - 2)}┘"

    lines = [top]
    lines.extend(_pad_line(line) for line in body_lines)
    lines.append(divider)
    lines.extend(_pad_line(line) for line in footer_lines)
    lines.append(bottom)
    return "\n".join(lines)


def _with_restart_row(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows = [list(r) for r in rows]
    rows.append([InlineKeyboardButton("🔄 Restart Bot", callback_data="restart")])
    return InlineKeyboardMarkup(rows)


def add_restart_button(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    if markup is None:
        return _with_restart_row([])
    return _with_restart_row(markup.inline_keyboard)


def report_again_keyboard() -> InlineKeyboardMarkup:
    return add_restart_button(
        InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Report again", callback_data="report_again")]])
    )


def main_menu_keyboard(saved_sessions: int = 0, active_sessions: int = 0, live_status: str = MENU_LIVE_STATUS) -> InlineKeyboardMarkup:
    return _with_restart_row(
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
    return _with_restart_row(
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

    return _with_restart_row(rows)


def session_mode_keyboard() -> InlineKeyboardMarkup:
    return _with_restart_row(
        [
            [InlineKeyboardButton("Report with saved sessions", callback_data="session_mode:reuse")],
            [InlineKeyboardButton("Add new sessions", callback_data="session_mode:new")],
        ]
    )


def render_greeting() -> str:
    return render_card(
        "Nightfall Reporter",
        [
            "Nightfall Reporter — premium chat cockpit engaged.",
            "Polished bubbles, elevated reply cards, and tactile pill buttons are live.",
            "Start reporting instantly with saved creds or add new sessions on the fly.",
            "Dynamic status chips below keep you oriented as you move through each step.",
            "Tap a control to begin.",
        ],
        [],
    )

__all__ = [
    "main_menu_keyboard",
    "target_kind_keyboard",
    "reason_keyboard",
    "session_mode_keyboard",
    "render_greeting",
    "render_card",
    "add_restart_button",
    "report_again_keyboard",
]






