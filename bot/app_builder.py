from __future__ import annotations

import logging

from telegram.error import NetworkError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.constants import (
    ADD_SESSIONS,
    API_HASH_STATE,
    API_ID_STATE,
    PRIVATE_INVITE,
    PRIVATE_MESSAGE,
    PUBLIC_MESSAGE,
    REPORT_COUNT,
    REPORT_MESSAGE,
    REPORT_REASON_TYPE,
    REPORT_SESSIONS,
    REPORT_URLS,
    SESSION_MODE,
    STORY_URL,
    TARGET_KIND,
)
from bot.dependencies import ensure_token
from bot.handlers import (
    cancel,
    error_handler,
    handle_action_buttons,
    handle_add_sessions,
    handle_api_hash,
    handle_api_id,
    handle_confirmation,
    handle_private_invite,
    handle_private_message_link,
    handle_public_message_link,
    handle_reason_message,
    handle_reason_type,
    handle_report_count,
    handle_report_urls,
    handle_session_mode,
    handle_sessions,
    handle_status_chip,
    handle_story_url,
    handle_target_kind,
    help_command,
    receive_added_sessions,
    show_sessions,
    start,
    start_report,
)


def build_app() -> Application:
    application = (
        ApplicationBuilder()
        .token(ensure_token())
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )

    report_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("report", start_report),
            CallbackQueryHandler(handle_action_buttons, pattern=r"^action:"),
        ],
        states={
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_id)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_hash)],
            REPORT_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sessions)],
            SESSION_MODE: [CallbackQueryHandler(handle_session_mode, pattern=r"^session_mode:")],
            TARGET_KIND: [CallbackQueryHandler(handle_target_kind, pattern=r"^kind:")],
            REPORT_URLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_urls)],
            PRIVATE_INVITE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_invite)],
            PRIVATE_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_message_link)],
            PUBLIC_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_public_message_link)],
            STORY_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_story_url)],
            REPORT_REASON_TYPE: [CallbackQueryHandler(handle_reason_type, pattern=r"^reason:")],
            REPORT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reason_message)],
            REPORT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_count)],
            ADD_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_added_sessions)],
            ConversationHandler.WAITING: [CallbackQueryHandler(handle_confirmation, pattern=r"^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=True,
        per_chat=True,
        per_user=True,
    )

    add_sessions_conv = ConversationHandler(
        entry_points=[CommandHandler("addsessions", handle_add_sessions)],
        states={ADD_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_added_sessions)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True,
        per_chat=True,
        per_user=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("sessions", show_sessions))
    application.add_handler(add_sessions_conv)
    application.add_handler(report_conversation)
    application.add_handler(CallbackQueryHandler(handle_status_chip, pattern=r"^status:"))
    application.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^confirm:"))

    application.add_error_handler(error_handler)
    return application


async def run_polling(application: Application) -> None:
    try:
        logging.info("Bot started and polling.")
        await application.run_polling()
    except NetworkError as exc:
        logging.error("Failed to connect to Telegram: %s", exc)
        raise SystemExit(1) from exc


__all__ = ["build_app", "run_polling"]
