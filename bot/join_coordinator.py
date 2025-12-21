"""Coordinate joining multiple Pyrogram clients to a chat with retries."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable

from pyrogram import Client, errors

from .error_map import ErrorInfo, map_exc
from .link_parser import JoinLink


@dataclass
class JoinResult:
    status: str  # joined | already | floodwait | failed
    code: str
    detail: str
    retry_after: int | None = None
    attempts: int = 1


ProgressCallback = Callable[[Dict[str, JoinResult]], Awaitable[None]]


async def _join_client(client: Client, join_link: JoinLink) -> None:
    if join_link.kind == "public_username":
        username = join_link.value
        if not username.startswith("@"):
            username = f"@{username}"
        await client.join_chat(username)
    else:
        # Preserve the raw link to satisfy Pyrogram expectations for invite joins.
        link = join_link.raw
        await client.join_chat(link)


async def join_all_clients(
    join_link: JoinLink,
    clients: Iterable[tuple[str, Client]],
    progress_cb: ProgressCallback,
    semaphore: asyncio.Semaphore,
    *,
    max_attempts: int = 5,
) -> Dict[str, JoinResult]:
    """Join all clients to the given link with FloodWait-aware retries."""

    results: Dict[str, JoinResult] = {}

    async def worker(name: str, client: Client) -> None:
        attempts = 0
        while attempts < max_attempts:
            attempts += 1
            try:
                async with semaphore:
                    await _join_client(client, join_link)
                results[name] = JoinResult("joined", "JOINED", "joined", attempts=attempts)
                await progress_cb(results)
                return
            except errors.UserAlreadyParticipant:
                results[name] = JoinResult("already", "ALREADY_MEMBER", "already", attempts=attempts)
                await progress_cb(results)
                return
            except Exception as exc:  # noqa: BLE001
                info: ErrorInfo = map_exc(exc)
                if info.code == "FLOOD_WAIT" and info.retry_after:
                    results[name] = JoinResult(
                        "floodwait",
                        info.code,
                        info.detail,
                        retry_after=info.retry_after,
                        attempts=attempts,
                    )
                    await progress_cb(results)
                    await asyncio.sleep(info.retry_after)
                    continue
                results[name] = JoinResult("failed", info.code, info.detail, retry_after=info.retry_after, attempts=attempts)
                await progress_cb(results)
                return

        if name not in results:
            results[name] = JoinResult("failed", "ATTEMPTS_EXHAUSTED", "Max attempts exceeded", attempts=max_attempts)
            await progress_cb(results)

    tasks = [asyncio.create_task(worker(name, client)) for name, client in clients]
    if tasks:
        await asyncio.wait(tasks)
    return results


__all__ = ["JoinResult", "join_all_clients"]
