from __future__ import annotations

"""Inline keyboard builders for the reporting bot."""

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

REPORT_REASONS = {
    "spam": ("Spam", 0),
    "violence": ("Violence", 1),
    "pornography": ("Pornography", 2),
    "child": ("Child Abuse", 3),
    "copyright": ("Copyright", 4),
    "fake": ("Fake", 6),
    "other": ("Other", 9),
}


def owner_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Manage Sessions", callback_data="owner:manage")],
            [InlineKeyboardButton("➕ Set Session Group", callback_data="owner:set_session_group")],
            [InlineKeyboardButton("📝 Set Logs Group", callback_data="owner:set_logs_group")],
        ]
    )


def sudo_panel(live_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Start Report", callback_data="sudo:start")]]
    )


def report_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Public", callback_data="report:type:public")],
            [InlineKeyboardButton("Private", callback_data="report:type:private")],
        ]
    )


def reason_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, (label, _code) in REPORT_REASONS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"report:reason:{key}")])
    return InlineKeyboardMarkup(rows)


def queued_message(position: int) -> str:
    return f"Report in progress. You are in queue position #{position}. Please wait..."

