from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

import config
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.constants import DEFAULT_REPORTS
from bot.dependencies import API_HASH, API_ID, data_store, ensure_pyrogram_creds
from bot.state import reset_user_context
from bot.ui import render_card, report_again_keyboard
from bot.target_resolver import (
    ensure_join_if_needed,
    fetch_target_details,
    parse_target,
    resolve_peer,
)
from bot.utils import validate_sessions
from report import report_profile_photo

if TYPE_CHECKING:
    # Keep type information for editors without importing Pyrogram's sync wrapper
    # during module import. Pyrogram's top-level import path currently touches
    # ``asyncio.get_event_loop`` at import time, which raises under Python 3.14
    # when no loop exists yet. Delaying the import until we are already inside a
    # running event loop keeps startup stable on Heroku.
    from pyrogram.client import Client
    from pyrogram.errors import (
        BadRequest,
        FloodWait,
        PeerIdInvalid,
        RPCError,
        UsernameInvalid,
        UsernameNotOccupied,
        UserDeactivated,
    )


def _session_label(session: str) -> str:
    return f"session_{abs(hash(session)) % 10_000}" if session else "session_unknown"


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

    await context.bot.send_message(chat_id=chat_id, text="Preparing clients…")

    if sessions:
        try:
            await data_store.add_sessions(sessions, added_by=user.id if user else None)
        except Exception:
            logging.exception("Failed to persist sessions before reporting")

    messages = []
    total_success = 0
    total_failed = 0
    total_sessions_started = 0
    total_sessions_failed = 0
    halted = False
    last_error: str | None = None

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
                logging.exception("Failed to complete reporting job for target '%s'", target)
                summary = {"success": 0, "failed": 0, "halted": True, "error": str(exc)}

            ended = datetime.now(timezone.utc)
            sessions_used = summary.get("sessions_started", len(sessions))
            messages.append(
                "\n".join(
                    [
                        f"Target: {target}",
                        f"Reasons: {', '.join(reasons)}",
                        f"Requested: {count}",
                        f"Sessions used: {sessions_used}",
                        f"Success: {summary['success']} | Failed: {summary['failed']}",
                        f"Stopped early: {'Yes' if summary.get('halted') else 'No'}",
                        f"Error: {summary.get('error', 'None')}",
                        f"Started: {started.isoformat()}",
                        f"Ended: {ended.isoformat()}",
                    ]
                )
            )

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

            total_success += summary.get("success", 0)
            total_failed += summary.get("failed", 0)
            total_sessions_started += summary.get("sessions_started", 0)
            total_sessions_failed += summary.get("sessions_failed", 0)
            last_error = summary.get("error") or last_error
            halted = halted or summary.get("halted", False)

            if summary.get("halted"):
                break
    except asyncio.CancelledError:  # pragma: no cover - application shutdown
        logging.info("Report job cancelled during shutdown")
        card = render_card("Report cancelled", ["The reporting task was cancelled."], [])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"<pre>{card}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=report_again_keyboard(),
        )
        reset_user_context(context, user.id if user else None)
        return

    body_lines: list[str] = []
    for msg in messages:
        body_lines.extend(msg.splitlines())
        body_lines.append("")

    if body_lines:
        body_lines = body_lines[:-1]
    else:
        body_lines = ["No report output generated."]

    footer = [
        f"Total success: {total_success} | failed: {total_failed}",
        f"Sessions started: {total_sessions_started} | failed/removed: {total_sessions_failed}",
    ]
    if last_error:
        footer.append(f"Last error: {last_error}")

    title = "Report halted" if halted else "Report completed"
    card = render_card(title.title(), body_lines, footer)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<pre>{card}</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=report_again_keyboard(),
    )

    reset_user_context(context, user.id if user else None)


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
    # Import Pyrogram lazily so we avoid its sync wrapper touching the default
    # event loop during module import. Python 3.14 tightened ``get_event_loop``
    # semantics, so we only import once we know an event loop is already
    # running (inside an async function owned by our single asyncio.run entry).
    from pyrogram.client import Client
    from pyrogram.errors import (
        BadRequest,
        FloodWait,
        PeerIdInvalid,
        RPCError,
        UsernameInvalid,
        UsernameNotOccupied,
        UserDeactivated,
    )

    if not (api_id and api_hash):
        ensure_pyrogram_creds()
        api_id = API_ID
        api_hash = API_HASH

    sessions_to_use = list(sessions)
    invalid_sessions: set[str] = set()

    try:
        valid_sessions, invalid_sessions = await validate_sessions(api_id, api_hash, sessions_to_use)
    except Exception:
        valid_sessions, invalid_sessions = sessions_to_use, set()

    if invalid_sessions:
        removed = await data_store.remove_sessions(invalid_sessions)
        logging.warning(
            "Pruned %s invalid session(s) before run (removed %s persisted entries).",
            len(invalid_sessions),
            removed,
        )

    clients: list[Client] = []
    client_session_map: dict[Client, str] = {}
    failed_sessions = 0

    for idx, session in enumerate(valid_sessions):
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
            client_session_map[client] = session
        except UserDeactivated:
            failed_sessions += 1
            invalid_sessions.add(session)
            logging.warning("Session %s is deactivated; skipping and removing.", _session_label(session))
        except Exception:
            failed_sessions += 1
            logging.exception("Failed to start client %s during reporting", client.name)

    if invalid_sessions:
        await data_store.remove_sessions(invalid_sessions)

    if not clients:
        return {"success": 0, "failed": 0, "halted": True, "error": "No sessions could be started"}

    reason_text = "; ".join(reasons)[:512] or "No reason provided"

    try:
        target_spec = parse_target(target)
        if invite_link and not target_spec.invite_link:
            # Preserve the original normalized form but attach the invite link/hash for joining.
            invite_hash_match = None
            if "+" in invite_link:
                invite_hash_match = invite_link.split("+")[-1]
            else:
                invite_hash_match = invite_link.rsplit("/", 1)[-1]
            target_spec = target_spec.__class__(
                raw=target_spec.raw,
                normalized=target_spec.normalized,
                kind="invite" if target_spec.kind == "username" else target_spec.kind,
                username=target_spec.username,
                numeric_id=target_spec.numeric_id,
                invite_hash=invite_hash_match,
                invite_link=invite_link,
                message_id=target_spec.message_id,
                internal_id=target_spec.internal_id,
            )

        normalized_label = target_spec.username or target_spec.normalized or target_spec.raw

        resolved_chat_id: int | None = None
        target_details: dict | None = None
        for client in clients:
            join_result = await ensure_join_if_needed(client, target_spec)
            if not join_result.ok:
                logging.warning(
                    "Join attempt failed for %s via %s: %s", target, client.name, join_result.reason or join_result.error
                )

            resolution = await resolve_peer(client, target_spec)
            details = await fetch_target_details(client, resolution)
            logging.info(
                "TargetResolver: parsed=%s joined=%s resolved=%s title=%s members=%s",
                target_spec,
                join_result.reason or join_result.joined,
                resolution.method,
                details.title,
                details.members,
            )

            if resolution.ok and resolution.chat_id is not None:
                resolved_chat_id = resolution.chat_id
                target_details = {
                    "type": details.type,
                    "title": details.title,
                    "id": details.id,
                    "username": details.username,
                    "members": details.members,
                    "private": details.private,
                }
                break

        if resolved_chat_id is None:
            return {
                "success": 0,
                "failed": 0,
                "halted": False,
                "error": "Unable to resolve the target with the available sessions (likely invalid/private).",
            }

        if target_spec.requires_join:
            for client in clients:
                join_result = await ensure_join_if_needed(client, target_spec)
                if not join_result.ok:
                    logging.warning(
                        "Join attempt failed for %s via %s: %s",
                        target,
                        client.name,
                        join_result.reason or join_result.error,
                    )

        success = 0
        failed = 0
        
        halted = False
        invalidated_mid_run: set[str] = set()

        async def report_once(client: Client) -> bool:
            nonlocal halted
            try:
                return await report_profile_photo(client, resolved_chat_id, reason=reason_code, reason_text=reason_text)
            except FloodWait as fw:
                wait_for = getattr(fw, "value", 1)
                logging.warning("Flood wait %ss while reporting %s via %s", wait_for, target, client.name)
                await asyncio.sleep(wait_for)
                try:
                    return await report_profile_photo(client, resolved_chat_id, reason=reason_code, reason_text=reason_text)
                except Exception:
                    logging.exception("Retry after flood wait failed for %s via %s", target, client.name)
                    return False
            except UserDeactivated:
                session_string = client_session_map.get(client)
                if session_string:
                    invalidated_mid_run.add(session_string)
                    await data_store.remove_sessions([session_string])
                    logging.warning(
                        "Session %s deactivated mid-run; marking as invalid.",
                        _session_label(session_string),
                    )
                return False
            except (PeerIdInvalid, UsernameInvalid, UsernameNotOccupied) as exc:
                logging.error(
                    "Peer not resolvable while reporting %s (normalized %s) via %s: %s",
                    target,
                    normalized_label,
                    client.name,
                    exc.__class__.__name__,
                )
                return False
            except (BadRequest, RPCError) as exc:
                halted = True
                logging.error(
                    "Halting report run due to RPC/BadRequest error for %s via %s: %s",
                    target,
                    client.name,
                    exc,
                )
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

                try:
                    result = await report_once(client)
                    if result:
                        success += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                    logging.exception("Unexpected error while reporting via %s", getattr(client, "name", "unknown"))
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await queue.join()
        await asyncio.gather(*workers)

        if invalidated_mid_run:
            await data_store.remove_sessions(invalidated_mid_run)

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
