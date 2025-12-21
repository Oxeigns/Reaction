"""Lightweight monitoring/reporting loop for validated targets."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Iterable

from pyrogram import Client

from .error_map import ErrorInfo, map_exc
from .target_resolver import TargetPreview

ReportProgress = Callable[[Dict[str, str]], Awaitable[None]]


async def _check_once(client: Client, preview: TargetPreview) -> str:
    try:
        msg = await client.get_messages(preview.chat_id, preview.msg_id)
        if not msg:
            return "MESSAGE_NOT_FOUND"
        return "OK"
    except Exception as exc:  # noqa: BLE001
        info: ErrorInfo = map_exc(exc)
        if info.code == "FLOOD_WAIT" and info.retry_after:
            await asyncio.sleep(info.retry_after)
            return "FLOOD_WAIT"
        return info.code


async def start_reporting(
    clients: Iterable[tuple[str, Client]],
    target_preview: TargetPreview,
    progress_cb: ReportProgress,
    *,
    interval: float = 5.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    status: Dict[str, str] = {}
    stop_event = stop_event or asyncio.Event()

    while not stop_event.is_set():
        for name, client in clients:
            status[name] = await _check_once(client, target_preview)
        await progress_cb(dict(status))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


__all__ = ["start_reporting"]
