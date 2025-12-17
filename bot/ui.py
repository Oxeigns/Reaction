from __future__ import annotations

import textwrap
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.constants import MENU_LIVE_STATUS, MAX_SESSIONS, MIN_SESSIONS, REASON_LABELS

# Mobile-friendly width (Telegram monospace). 70–78 looks best.
CARD_WIDTH = 74


def render_card(
    title: str,
    body_lines: list[str] | tuple[str, ...],
    footer_lines: list[str] | tuple[str, ...] | None = None,
) -> str:
    body_lines = list(body_lines)
    footer_lines = list(footer_lines or [])

    hint = "Help: 🔄 Restart or /restart"
    if hint not in footer_lines:
        footer_lines.append(hint)

    inner = CARD_WIDTH - 4  # │ <content> │

    def _wrap(lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            line = "" if line is None else str(line)
            if not line.strip():
                out.append("")
                continue
            out.extend(
                textwrap.wrap(
                    line,
                    width=inner,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        return out

    def _pad_line(content: str) -> str:
        content = (content or "")[:inner]
        return f"│ {content}{' ' * (inner - len(content))} │"

    title = (title or "").strip()
    title_block = f" {title} " if title else " "
    dash_space = (CARD_WIDTH - 2) - len(title_block)
    left = max(1, dash_space // 2)
    right = max(1, dash_space - left)

    top = f"┌{'─' * left}{title_block}{'─' * right}┐"
    divider = f"├{'─' * (CARD_WIDTH - 2)}┤"
    bottom = f"└{'─' * (CARD_WIDTH - 2)}┘"

    body = _wrap(body_lines)
    footer = _wrap(footer_lines)

    lines: list[str] = [top]
    lines.extend(_pad_line(line) for line in body)
    lines.append(divider)
    lines.extend(_pad_line(line) for line in footer)
    lines.append(bottom)
    return "\n".join(lines)


def _with_restart_row(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows = [list(r) for r in rows]
    rows.append([InlineKeyboardButton("🔄 Restart", callback_data="restart")])
    return InlineKeyboardMarkup(rows)


def add_restart_button(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    if markup is None:
        return _with_restart_row([])
    return _with_restart_row(markup.inline_keyboard)


def report_again_keyboard() -> InlineKeyboardMarkup:
    return add_restart_button(
        InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Report Again", callback_data="report_again")]])
    )


def main_menu_keyboard(
    saved_sessions: int = 0,
    active_sessions: int = 0,
    live_status: str = MENU_LIVE_STATUS,
) -> InlineKeyboardMarkup:
    # Clamp counts to avoid ugly negative / out-of-range display
    saved_sessions = max(0, min(int(saved_sessions), int(MAX_SESSIONS)))
    active_sessions = max(0, int(active_sessions))

    return _with_restart_row(
        [
            [InlineKeyboardButton("▶ Start Report", callback_data="action:start")],
            [
                InlineKeyboardButton("➕ Add Sessions", callback_data="action:add"),
                InlineKeyboardButton("💾 Saved Sessions", callback_data="action:sessions"),
            ],
            [
                InlineKeyboardButton(f"🟢 Status: {live_status}", callback_data="status:live"),
                InlineKeyboardButton(f"🎯 Loaded: {active_sessions}", callback_data="status:active"),
                InlineKeyboardButton(f"📦 Saved: {saved_sessions}", callback_data="status:saved"),
            ],
        ]
    )


def target_kind_keyboard() -> InlineKeyboardMarkup:
    return _with_restart_row(
        [
            [InlineKeyboardButton("🔒 Private Channel / Group", callback_data="kind:private")],
            [InlineKeyboardButton("🌐 Public Channel / Group", callback_data="kind:public")],
            [InlineKeyboardButton("📎 Story URL (Profile)", callback_data="kind:story")],
        ]
    )


def reason_keyboard() -> InlineKeyboardMarkup:
    """Buttons covering the available Pyrogram/Telegram report reasons."""
    # Keep your original callback mapping/order (0,3,2,1,6,4,5)
    order = [0, 3, 2, 1, 6, 4, 5]
    buttons = [
        InlineKeyboardButton(REASON_LABELS[i], callback_data=f"reason:{i}")
        for i in order
    ]

    rows = [
        buttons[0:2],
        buttons[2:4],
        buttons[4:6],
        buttons[6:7],
    ]
    return _with_restart_row(rows)


def session_mode_keyboard() -> InlineKeyboardMarkup:
    return _with_restart_row(
        [
            [InlineKeyboardButton("Use Saved Sessions", callback_data="session_mode:reuse")],
            [InlineKeyboardButton("Add New Sessions", callback_data="session_mode:new")],
        ]
    )


def render_greeting() -> str:
    return render_card(
        "Nightfall Reporter",
        [
            "Welcome to Nightfall Reporter.",
            "Start with saved sessions, or add new sessions anytime.",
            "Use the status chips to track readiness and loaded/saved sessions.",
            "Choose an action below to continue.",
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
