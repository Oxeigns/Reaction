from __future__ import annotations

"""Concurrent MTProto reporting helpers built on Pyrogram."""

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Tuple

from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, BadRequest, FloodWait, RPCError, UserPrivacyRestricted
from pyrogram.raw.functions.account import ReportPeer
from pyrogram.raw.types import (
    InputReportReasonChildAbuse,
    InputReportReasonCopyright,
    InputReportReasonDrugs,
    InputReportReasonFake,
    InputReportReasonOther,
    InputReportReasonPersonalDetails,
    InputReportReasonPornography,
    InputReportReasonSpam,
    InputReportReasonViolence,
)

LOG_FILE = Path("logs.csv")
MAX_CONCURRENCY = 10
REPORT_REASONS: List[Tuple[str, type]] = [
    ("spam", InputReportReasonSpam),
    ("fake", InputReportReasonFake),
    ("drugs", InputReportReasonDrugs),
    ("pornography", InputReportReasonPornography),
    ("violence", InputReportReasonViolence),
    ("child_abuse", InputReportReasonChildAbuse),
    ("copyright", InputReportReasonCopyright),
    ("personal_details", InputReportReasonPersonalDetails),
    ("other", InputReportReasonOther),
]


async def _log_attempt(lock: asyncio.Lock, session_name: str, target_username: str, reason: str, status: str) -> None:
    """Append a log entry to ``logs.csv`` in a threadsafe manner."""

    async with lock:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_exists = LOG_FILE.exists()
        with LOG_FILE.open("a", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            if not file_exists:
                writer.writerow(["timestamp", "session", "target", "reason", "status"])
            writer.writerow([datetime.now(timezone.utc).isoformat(), session_name, target_username, reason, status])


async def _report_reason(
    client: Client,
    target_username: str,
    reason: Tuple[str, type],
    counter_lock: asyncio.Lock,
    log_lock: asyncio.Lock,
    stop_event: asyncio.Event,
    stats: dict,
    max_reports: int,
) -> None:
    """Report a target for a single reason with timeout and robust error handling."""

    if stop_event.is_set():
        return

    reason_key, reason_cls = reason

    try:
        peer = await asyncio.wait_for(client.resolve_peer(target_username), timeout=15)

        async def _invoke() -> None:
            await client.invoke(ReportPeer(peer=peer, reason=reason_cls(), message="Automated report"))

        await asyncio.wait_for(_invoke(), timeout=15)
        status = "ok"
        async with counter_lock:
            stats["success"] += 1
            stats["attempts"] += 1
            if stats["attempts"] >= max_reports:
                stop_event.set()
        logging.info("Report ok | session=%s reason=%s target=%s", client.name, reason_key, target_username)
    except asyncio.TimeoutError:
        status = "timeout"
        async with counter_lock:
            stats["failed"] += 1
            stats["attempts"] += 1
        logging.warning("Timeout | session=%s reason=%s target=%s", client.name, reason_key, target_username)
    except FloodWait as fw:
        status = f"floodwait_{fw.value}s"
        async with counter_lock:
            stats["failed"] += 1
            stats["attempts"] += 1
        logging.warning("FloodWait %ss | session=%s reason=%s", fw.value, client.name, reason_key)
    except (UserPrivacyRestricted, AuthKeyUnregistered, BadRequest, RPCError) as err:
        status = f"error:{err.__class__.__name__}"
        async with counter_lock:
            stats["failed"] += 1
            stats["attempts"] += 1
        logging.error("Skipping session=%s reason=%s error=%s", client.name, reason_key, err)
    await _log_attempt(log_lock, client.name, target_username, reason_key, status)


async def _handle_session(
    semaphore: asyncio.Semaphore,
    client: Client,
    target_username: str,
    counter_lock: asyncio.Lock,
    log_lock: asyncio.Lock,
    stop_event: asyncio.Event,
    stats: dict,
    max_reports: int,
) -> None:
    """Start a client and iterate through all reasons with proper throttling."""

    async with semaphore:
        try:
            await asyncio.wait_for(client.start(), timeout=15)
        except asyncio.TimeoutError:
            await _log_attempt(log_lock, client.name, target_username, "startup", "timeout")
            logging.warning("Startup timeout | session=%s", client.name)
            return
        except (AuthKeyUnregistered, BadRequest, RPCError) as err:
            await _log_attempt(log_lock, client.name, target_username, "startup", f"error:{err.__class__.__name__}")
            logging.error("Startup failed | session=%s error=%s", client.name, err)
            return

        try:
            for reason in REPORT_REASONS:
                if stop_event.is_set():
                    break
                await _report_reason(
                    client,
                    target_username,
                    reason,
                    counter_lock,
                    log_lock,
                    stop_event,
                    stats,
                    max_reports,
                )
        finally:
            try:
                await asyncio.wait_for(client.stop(), timeout=10)
            except Exception:
                logging.debug("Client %s already stopped or failed to stop.", client.name)


async def report_user(username: str, clients: Iterable[Client], max_reports: int = 5000) -> dict:
    """Report ``username`` with every available session and reason concurrently."""

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    counter_lock = asyncio.Lock()
    log_lock = asyncio.Lock()
    stop_event = asyncio.Event()

    stats = {"success": 0, "failed": 0, "attempts": 0}
    tasks: List[asyncio.Task] = []

    for client in clients:
        if stop_event.is_set():
            break
        tasks.append(
            asyncio.create_task(
                _handle_session(
                    semaphore,
                    client,
                    username,
                    counter_lock,
                    log_lock,
                    stop_event,
                    stats,
                    max_reports,
                )
            )
        )

    if tasks:
        await asyncio.gather(*tasks)

    return stats
