from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Iterable

import config
from pyrogram import Client
from pyrogram.errors import BadRequest, FloodWait, RPCError, UsernameNotOccupied
from telegram.ext import ContextTypes

from bot.constants import DEFAULT_REPORTS
from bot.dependencies import API_HASH, API_ID, data_store, ensure_pyrogram_creds
from bot.utils import resolve_chat_id
from report import report_profile_photo


async def run_report_job(query, context: ContextTypes.DEFAULT_TYPE, job_data: dict) -> None:
    user = query.from_user
    chat_id = query.message.chat_id

    targets = job_data.get("targets", [])
    reasons = job_data.get("reasons", [])
    count = job_data.get("count", DEFAULT_REPORTS)
    sessions = job_data.get("sessions", [])
    api_id = job_data.get("api_id")
    api_hash = job_data.get("api_hash")
    reason_code = job_data.get("reason_code", 5)

    await context.bot.send_message(chat_id=chat_id, text="Preparing clients...")

    messages = []

    try:
        for target in targets:
            started = datetime.now(timezone.utc)
            try:
                summary = await perform_reporting(
                    target,
                    reasons,
                    count,
                    sessions,
                    api_id=api_id,
                    api_hash=api_hash,
                    reason_code=reason_code,
                    invite_link=job_data.get("invite_link"),
                )
            except Exception as exc:  # pragma: no cover - runtime safety
                logging.exception("Failed to complete reporting job")
                summary = {"success": 0, "failed": 0, "halted": True, "error": str(exc)}

            ended = datetime.now(timezone.utc)
            sessions_used = summary.get("sessions_started", len(sessions))
            text = (
                f"Target: {target}\n"
                f"Reasons: {', '.join(reasons)}\n"
                f"Requested: {count}\n"
                f"Sessions used: {sessions_used}\n"
                f"Success: {summary['success']} | Failed: {summary['failed']}\n"
                f"Stopped early: {'Yes' if summary.get('halted') else 'No'}\n"
                f"Error: {summary.get('error', 'None')}\n"
                f"Started: {started.isoformat()}\n"
                f"Ended: {ended.isoformat()}"
            )
            messages.append(text)

            await data_store.record_report(
                {
                    "user_id": user.id if user else None,
                    "target": target,
                    "reasons": reasons,
                    "requested": count,
                    "sessions": sessions_used,
                    "success": summary["success"],
                    "failed": summary["failed"],
                    "started_at": started,
                    "ended_at": ended,
                    "halted": summary.get("halted", False),
                }
            )

            if summary.get("halted"):
                break
    except asyncio.CancelledError:  # pragma: no cover - application shutdown
        logging.info("Report job cancelled during shutdown")
        return

    await context.bot.send_message(chat_id=chat_id, text="\n\n".join(messages))


async def perform_reporting(
    target: str,
    reasons: Iterable[str],
    total: int,
    sessions: list[str],
    *,
    api_id: int | None,
    api_hash: str | None,
    reason_code: int = 5,
    max_concurrency: int = 25,
    invite_link: str | None = None,
) -> dict:
    """Send repeated report requests with bounded concurrency."""
    if not (api_id and api_hash):
        ensure_pyrogram_creds()
        api_id = API_ID
        api_hash = API_HASH

    clients: list[Client] = []
    failed_sessions = 0
    for idx, session in enumerate(sessions):
        client = Client(
            name=f"reporter_{idx}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            workdir=f"/tmp/report_session_{idx}",
        )
        try:
            await client.start()
            clients.append(client)
        except Exception:
            failed_sessions += 1

    if not clients:
        return {"success": 0, "failed": 0, "halted": True, "error": "No sessions could be started"}

    reason_text = "; ".join(reasons)[:512] or "No reason provided"

    try:
        chat_id: int | None = None
        last_error: str | None = None

        for client in clients:
            try:
                chat_id = await resolve_chat_id(client, target, invite_link)
                break
            except UsernameNotOccupied:
                last_error = (
                    "The username or link appears to be unoccupied or deleted. "
                    "Please verify the target and try again."
                )
                continue
            except BadRequest as exc:
                last_error = f"The link '{target}' is not valid: {exc}."
                break
            except RPCError as exc:
                last_error = f"Could not resolve '{target}' ({exc})."
                continue

        if chat_id is None:
            return {
                "success": 0,
                "failed": 0,
                "halted": True,
                "error": last_error or "Unable to resolve the target with the available sessions.",
            }

        if invite_link:
            for client in clients:
                try:
                    await client.join_chat(invite_link)
                except FloodWait as fw:
                    await asyncio.sleep(getattr(fw, "value", 1))
                    try:
                        await client.join_chat(invite_link)
                    except Exception:
                        pass
                except Exception:
                    pass

        success = 0
        failed = 0

        halted = False

        async def report_once(client: Client) -> bool:
            nonlocal halted
            try:
                return await report_profile_photo(client, chat_id, reason=reason_code, reason_text=reason_text)
            except FloodWait as fw:
                await asyncio.sleep(getattr(fw, "value", 1))
                try:
                    return await report_profile_photo(client, chat_id, reason=reason_code, reason_text=reason_text)
                except Exception:
                    return False
            except (BadRequest, RPCError):
                halted = True
                return False

        worker_count = max(1, min(max_concurrency, total, len(clients)))
        queue: asyncio.Queue[Client] = asyncio.Queue()

        for _ in range(total):
            queue.put_nowait(clients[_ % len(clients)])

        async def worker() -> None:
            nonlocal success, failed, halted
            while True:
                if halted:
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                            queue.task_done()
                        except asyncio.QueueEmpty:
                            break
                    break

                try:
                    client = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                result = await report_once(client)
                if result:
                    success += 1
                else:
                    failed += 1
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await queue.join()
        await asyncio.gather(*workers)

        return {
            "success": success,
            "failed": failed,
            "halted": halted,
            "sessions_started": len(clients),
            "sessions_failed": failed_sessions,
        }

    finally:
        for client in clients:
            try:
                await client.stop()
            except Exception:
                pass


__all__ = ["run_report_job", "perform_reporting"]
