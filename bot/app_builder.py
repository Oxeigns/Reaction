from __future__ import annotations

import asyncio
import logging
import contextlib

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.ext import AIORateLimiter, Application, ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from bot.dependencies import ensure_token
from bot.handlers import error_handler, handle_message, start, status, stop

DEFAULT_POLL_TIMEOUT = 30


def build_app() -> Application:
    request = HTTPXRequest(
        connect_timeout=5,
        read_timeout=DEFAULT_POLL_TIMEOUT,
        write_timeout=20,
        pool_timeout=5,
    )

    application = (
        ApplicationBuilder()
        .token(ensure_token())
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .request(request)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.add_error_handler(error_handler)
    return application


async def run_polling(application: Application, shutdown_event: asyncio.Event) -> None:
    backoff_seconds = 1
    while not shutdown_event.is_set():
        try:
            logging.info("Bot starting polling cycle.")
            await application.initialize()
            await application.start()
            await application.updater.start_polling(
                timeout=DEFAULT_POLL_TIMEOUT,
                drop_pending_updates=True,
            )
            logging.info("Bot started and polling.")
            backoff_seconds = 1
            await shutdown_event.wait()
        except asyncio.CancelledError:
            raise
        except (NetworkError, TimedOut, httpx.ReadTimeout) as exc:
            logging.warning("Telegram network error: %s. Retrying in %s seconds.", exc, backoff_seconds)
        except Exception:
            logging.exception("Polling crashed unexpectedly. Retrying in %s seconds.", backoff_seconds)
        finally:
            try:
                with contextlib.suppress(TimedOut, httpx.ReadTimeout):
                    await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception:
                logging.exception("Error while shutting down application components")

        if shutdown_event.is_set():
            logging.info("Shutdown event set; exiting polling loop.")
            break
        await asyncio.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, 30)


__all__ = ["build_app", "run_polling"]
