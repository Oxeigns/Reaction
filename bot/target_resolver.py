from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse


@dataclass(frozen=True)
class TargetSpec:
    raw: str
    normalized: str
    kind: str
    username: str | None = None
    numeric_id: int | None = None
    invite_hash: str | None = None
    invite_link: str | None = None
    message_id: int | None = None
    internal_id: int | None = None

    def cache_key(self) -> str:
        return self.normalized.lower()

    @property
    def requires_join(self) -> bool:
        return self.kind == "invite" or bool(self.invite_hash)


@dataclass
class JoinResult:
    ok: bool
    joined: bool
    reason: str | None = None
    error: str | None = None


@dataclass
class ResolvedTarget:
    ok: bool
    peer: Any | None
    chat_id: int | None
    method: str | None
    error: str | None = None


@dataclass
class TargetDetails:
    type: str | None
    title: str | None
    id: int | None
    username: str | None
    members: int | None
    private: bool
    description: str | None = None
    is_bot: bool | None = None
    is_verified: bool | None = None
    is_scam: bool | None = None
    is_fake: bool | None = None


_CACHE: dict[str, tuple[ResolvedTarget, datetime]] = {}
_CACHE_TTL = timedelta(minutes=10)
_FAILURE_CACHE: dict[str, tuple[ResolvedTarget, datetime]] = {}
_FAILURE_TTL = timedelta(minutes=5)
_FATAL_ERRORS = {
    "PeerIdInvalid",
    "ChannelInvalid",
    "ChannelPrivate",
    "ChatIdInvalid",
    "UsernameInvalid",
    "UsernameNotOccupied",
    "invalid_invite",
}


def _strip_query(url: str) -> str:
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    path = parsed.path.rstrip("/")
    prefix = f"{parsed.scheme + '://' if parsed.scheme else ''}{parsed.netloc}"
    return f"{prefix}{path}" if prefix else path


def parse_target(raw_input: str) -> TargetSpec:
    """Normalize user-provided Telegram targets.

    Handles usernames, numeric IDs, public/private invite links, and message links
    including the internal ``t.me/c/<id>/<msg>`` form without ever treating ``c``
    as a username. Query parameters and trailing slashes are stripped.
    """

    raw = (raw_input or "").strip()
    if not raw:
        raise ValueError("Target is empty; provide a username, invite link, or numeric ID.")

    cleaned = _strip_query(raw)

    # Numeric IDs (-100..., user id, etc.)
    numeric_candidate = raw.replace(" ", "")
    if numeric_candidate.startswith("http://") or numeric_candidate.startswith("https://"):
        numeric_candidate = numeric_candidate.split("://", 1)[1]
    if numeric_candidate.startswith("t.me/"):
        numeric_candidate = numeric_candidate.split("/", 1)[1]
    if numeric_candidate.lstrip("+").lstrip("-").isdigit():
        numeric_id = int(numeric_candidate)
        return TargetSpec(
            raw=raw,
            normalized=str(numeric_id),
            kind="numeric",
            username=None,
            numeric_id=numeric_id,
        )

    parsed = urlparse(cleaned if cleaned.startswith("http") else f"https://{cleaned}")
    path_parts: list[str] = [part for part in parsed.path.split("/") if part]
    netloc = parsed.netloc.lower()

    if netloc.endswith("t.me") and not path_parts:
        raise ValueError("The t.me link is missing a username.")

    # Internal message links (t.me/c/<id>/<msg>)
    if netloc.endswith("t.me") and path_parts and path_parts[0].lower() == "c":
        internal_id = int(path_parts[1]) if len(path_parts) >= 2 and path_parts[1].isdigit() else None
        message_id = int(path_parts[2]) if len(path_parts) >= 3 and path_parts[2].isdigit() else None
        normalized = f"c/{internal_id}" if internal_id is not None else "c"
        if internal_id is None:
            raise ValueError("Internal message link missing chat id.")
        return TargetSpec(
            raw=raw,
            normalized=normalized,
            kind="internal_message",
            internal_id=internal_id,
            message_id=message_id,
        )

    # Invite links
    if netloc.endswith("t.me") and path_parts:
        first = path_parts[0]
        if first.startswith("+"):
            invite_hash = first.lstrip("+")
            return TargetSpec(
                raw=raw,
                normalized=f"invite:{invite_hash}",
                kind="invite",
                invite_hash=invite_hash,
                invite_link=f"https://t.me/+{invite_hash}",
            )
        if first.lower() == "joinchat" and len(path_parts) >= 2:
            invite_hash = path_parts[1]
            return TargetSpec(
                raw=raw,
                normalized=f"invite:{invite_hash}",
                kind="invite",
                invite_hash=invite_hash,
                invite_link=f"https://t.me/joinchat/{invite_hash}",
            )

    # Message links with username
    if netloc.endswith("t.me") and len(path_parts) >= 2 and path_parts[1].isdigit():
        username = path_parts[0].lstrip("@")
        message_id = int(path_parts[1])
        return TargetSpec(
            raw=raw,
            normalized=username,
            kind="message",
            username=username,
            message_id=message_id,
        )

    # Plain username/link
    if netloc.endswith("t.me") and path_parts:
        username = path_parts[0].lstrip("@")
        if not username:
            raise ValueError("The t.me link is missing a username.")
        return TargetSpec(raw=raw, normalized=username, kind="username", username=username)

    # Bare usernames like @foo or foo
    username = raw.lstrip("@")
    normalized = username
    if not normalized:
        raise ValueError("Unable to parse the target. Please provide a valid username or link.")

    return TargetSpec(raw=raw, normalized=normalized, kind="username", username=username)


async def ensure_join_if_needed(client: Any, target_spec: TargetSpec) -> JoinResult:
    from pyrogram.errors import (
        ChatAdminRequired,
        FloodWait,
        InviteHashExpired,
        InviteHashInvalid,
        RPCError,
        UserAlreadyParticipant,
    )

    if not target_spec.invite_link and not target_spec.invite_hash:
        return JoinResult(ok=True, joined=False)

    invite_link = target_spec.invite_link or f"https://t.me/+{target_spec.invite_hash}"
    attempts = 0
    while attempts < 3:
        attempts += 1
        try:
            await client.join_chat(invite_link)
            logging.info(
                "TargetResolver: joined invite",
                extra={
                    "invite_link": invite_link,
                    "session_name": getattr(client, "name", "client"),
                    "step": "ensure_join",
                    "attempt": attempts,
                },
            )
            return JoinResult(ok=True, joined=True)
        except UserAlreadyParticipant:
            logging.info(
                "TargetResolver: already participant",
                extra={
                    "invite_link": invite_link,
                    "session_name": getattr(client, "name", "client"),
                    "step": "ensure_join",
                },
            )
            return JoinResult(ok=True, joined=False, reason="already_participant")
        except FloodWait as fw:
            wait_seconds = min(getattr(fw, "value", 1), 60)
            logging.warning(
                "Flood wait %ss while joining %s (attempt %s)", wait_seconds, invite_link, attempts
            )
            await asyncio.sleep(wait_seconds)
            continue
        except (InviteHashInvalid, InviteHashExpired) as exc:
            logging.error(
                "TargetResolver: invalid invite",
                extra={
                    "invite_link": invite_link,
                    "session_name": getattr(client, "name", "client"),
                    "step": "ensure_join",
                    "error": exc.__class__.__name__,
                },
            )
            return JoinResult(ok=False, joined=False, reason="invalid_invite", error=str(exc))
        except ChatAdminRequired as exc:
            return JoinResult(ok=False, joined=False, reason="admin_required", error=str(exc))
        except RPCError as exc:
            # Retry transient RPC errors once with a short delay.
            logging.warning(
                "TargetResolver: transient join failure",
                extra={
                    "invite_link": invite_link,
                    "session_name": getattr(client, "name", "client"),
                    "step": "ensure_join",
                    "error": exc.__class__.__name__,
                    "attempt": attempts,
                },
            )
            await asyncio.sleep(min(5, attempts))
            continue
        except Exception as exc:  # noqa: BLE001
            logging.exception(
                "TargetResolver: unexpected join failure",
                extra={
                    "invite_link": invite_link,
                    "session_name": getattr(client, "name", "client"),
                    "step": "ensure_join",
                    "error": exc.__class__.__name__,
                },
            )
            return JoinResult(ok=False, joined=False, reason="join_failed", error=str(exc))

    return JoinResult(ok=False, joined=False, reason="join_exhausted", error="join attempts exceeded")


async def ensure_joined(client: Any, target_spec: TargetSpec) -> JoinResult:
    """Wrapper to keep a stable public helper name."""

    return await ensure_join_if_needed(client, target_spec)


async def resolve_peer(client: Any, target_spec: TargetSpec, *, max_attempts: int = 3) -> ResolvedTarget:
    from pyrogram.errors import (  # type: ignore
        BadRequest,
        ChannelInvalid,
        ChannelPrivate,
        ChatIdInvalid,
        FloodWait,
        PeerIdInvalid,
        RPCError,
        UsernameInvalid,
        UsernameNotOccupied,
    )

    cached = _get_cached_resolution(target_spec)
    if cached:
        return cached

    attempts = 0
    last_error: str | None = None

    while attempts < max_attempts:
        attempts += 1
        method = "get_chat"
        try:
            if target_spec.kind == "numeric" and target_spec.numeric_id is not None:
                chat = await client.get_chat(target_spec.numeric_id)
            elif target_spec.kind in {"username", "message"} and target_spec.username:
                chat = await client.get_chat(target_spec.username)
            elif target_spec.kind == "internal_message" and target_spec.internal_id is not None:
                chat_id = int(f"-100{target_spec.internal_id}")
                chat = await client.get_chat(chat_id)
            elif target_spec.kind == "invite" and target_spec.invite_link:
                chat = await client.get_chat(target_spec.invite_link)
            else:
                return ResolvedTarget(ok=False, peer=None, chat_id=None, method=None, error="unsupported_target")

            chat_id = _chat_id_from_chat(chat)
            resolved = ResolvedTarget(ok=True, peer=chat, chat_id=chat_id, method=method)
            _cache_resolution(target_spec, resolved)
            logging.info(
                "TargetResolver: resolved target",
                extra={
                    "target": target_spec.raw,
                    "normalized": target_spec.normalized,
                    "kind": target_spec.kind,
                    "method": method,
                    "chat_id": chat_id,
                    "session_name": getattr(client, "name", "client"),
                },
            )
            return resolved
        except FloodWait as fw:
            wait_seconds = min(getattr(fw, "value", 1), 60)
            logging.warning(
                "Flood wait %ss while resolving %s (attempt %s)",
                wait_seconds,
                target_spec.raw,
                attempts,
            )
            await asyncio.sleep(wait_seconds)
            last_error = fw.__class__.__name__
            continue
        except (UsernameNotOccupied, UsernameInvalid, PeerIdInvalid, ChannelInvalid, ChannelPrivate, ChatIdInvalid) as exc:
            last_error = exc.__class__.__name__
            resolved = ResolvedTarget(ok=False, peer=None, chat_id=None, method=method, error=last_error)
            _cache_resolution(target_spec, resolved, failure=True)
            return resolved
        except BadRequest as exc:
            last_error = exc.__class__.__name__
            resolved = ResolvedTarget(ok=False, peer=None, chat_id=None, method=method, error=last_error)
            _cache_resolution(target_spec, resolved, failure=True)
            return resolved
        except RPCError as exc:
            last_error = exc.__class__.__name__
            logging.warning(
                "TargetResolver: RPC error while resolving",
                extra={
                    "target": target_spec.raw,
                    "kind": target_spec.kind,
                    "method": method,
                    "error": last_error,
                    "attempt": attempts,
                    "session_name": getattr(client, "name", "client"),
                },
            )
            await asyncio.sleep(min(3, attempts))
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc.__class__.__name__
            resolved = ResolvedTarget(ok=False, peer=None, chat_id=None, method=method, error=last_error)
            _cache_resolution(target_spec, resolved, failure=True)
            return resolved

    resolved = ResolvedTarget(ok=False, peer=None, chat_id=None, method=None, error=last_error)
    if last_error in _FATAL_ERRORS:
        _cache_resolution(target_spec, resolved, failure=True)
    return resolved


async def resolve_entity(client: Any, target_spec: TargetSpec, *, max_attempts: int = 3) -> ResolvedTarget:
    """Resolve a target into a peer, retrying after a join when needed."""

    # Join first for private targets to avoid PeerIdInvalid on lookups.
    join_result = None
    if target_spec.requires_join:
        join_result = await ensure_join_if_needed(client, target_spec)
        if not join_result.ok:
            return ResolvedTarget(ok=False, peer=None, chat_id=None, method="ensure_join", error=join_result.reason)

    resolved = _get_cached_resolution(target_spec)
    if resolved:
        return resolved

    resolved = await resolve_peer(client, target_spec, max_attempts=max_attempts)
    if resolved.ok:
        return resolved

    if resolved.error in {"PeerIdInvalid", "ChannelPrivate", "ChatIdInvalid", "ChannelInvalid"} and target_spec.requires_join:
        retry_join = await ensure_join_if_needed(client, target_spec)
        if retry_join.ok:
            resolved = await resolve_peer(client, target_spec, max_attempts=max_attempts)
            if resolved.ok:
                return resolved

    logging.warning(
        "TargetResolver: failed resolution",
        extra={
            "target": target_spec.raw,
            "kind": target_spec.kind,
            "error": resolved.error,
            "joined": join_result.reason if join_result else None,
            "session_name": getattr(client, "name", "client"),
        },
    )
    if resolved.error in _FATAL_ERRORS:
        _cache_resolution(target_spec, resolved, failure=True)
    return resolved


def _chat_id_from_chat(chat: Any) -> int:
    if hasattr(chat, "id"):
        return int(getattr(chat, "id"))
    if hasattr(chat, "chat_id"):
        return int(getattr(chat, "chat_id"))
    if hasattr(chat, "channel_id"):
        return int(f"-100{getattr(chat, 'channel_id')}")
    raise ValueError("Chat has no identifiable id field")


async def fetch_target_details(client: Any, resolved: ResolvedTarget) -> TargetDetails:
    if not resolved.ok or not resolved.peer:
        return TargetDetails(type=None, title=None, id=None, username=None, members=None, private=False)

    chat = resolved.peer
    chat_id = _chat_id_from_chat(chat)

    # Refresh chat info to capture member counts when possible
    try:
        chat = await client.get_chat(chat_id)
    except Exception:
        pass

    peer_type = getattr(chat, "type", None)
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
    username = getattr(chat, "username", None)
    members = getattr(chat, "members_count", None) or getattr(chat, "participants_count", None)
    private = bool(getattr(chat, "is_private", False) or (username is None))
    description = getattr(chat, "description", None) or getattr(chat, "bio", None)
    is_bot = getattr(chat, "is_bot", None)
    is_verified = getattr(chat, "is_verified", None)
    is_scam = getattr(chat, "is_scam", None)
    is_fake = getattr(chat, "is_fake", None)

    return TargetDetails(
        type=str(peer_type) if peer_type else None,
        title=title,
        id=chat_id,
        username=username,
        members=members,
        private=private,
        description=description,
        is_bot=is_bot,
        is_verified=is_verified,
        is_scam=is_scam,
        is_fake=is_fake,
    )


def _purge_cache() -> None:
    now = datetime.now(timezone.utc)
    stale: list[str] = []
    for key, (_, expires_at) in _CACHE.items():
        if expires_at <= now:
            stale.append(key)
    for key in stale:
        _CACHE.pop(key, None)

    stale_failures: list[str] = []
    for key, (_, expires_at) in _FAILURE_CACHE.items():
        if expires_at <= now:
            stale_failures.append(key)
    for key in stale_failures:
        _FAILURE_CACHE.pop(key, None)


def _cache_resolution(target_spec: TargetSpec, resolved: ResolvedTarget, *, failure: bool = False) -> None:
    ttl = _FAILURE_TTL if failure else _CACHE_TTL
    cache = _FAILURE_CACHE if failure else _CACHE
    cache[target_spec.cache_key()] = (resolved, datetime.now(timezone.utc) + ttl)


def _get_cached_resolution(target_spec: TargetSpec) -> ResolvedTarget | None:
    _purge_cache()
    key = target_spec.cache_key()
    now = datetime.now(timezone.utc)
    cached = _CACHE.get(key)
    if cached:
        resolved, expires_at = cached
        if expires_at > now:
            return resolved

    failure_entry = _FAILURE_CACHE.get(key)
    if failure_entry:
        resolved, expires_at = failure_entry
        if expires_at > now:
            return resolved
        _FAILURE_CACHE.pop(key, None)

    return None


def debug_resolve_targets(client: Any, targets: Iterable[str]) -> list[dict[str, Any]]:
    """Helper for manual debugging; resolves a list and returns structured data."""

    results: list[dict[str, Any]] = []

    async def _resolve_one(target: str) -> None:
        spec = parse_target(target)
        join_result = await ensure_join_if_needed(client, spec)
        resolved = await resolve_peer(client, spec)
        details = await fetch_target_details(client, resolved)
        results.append(
            {
                "raw": target,
                "parsed": spec,
                "joined": join_result,
                "resolved": resolved,
                "details": details,
            }
        )
        logging.info(
            "TargetResolver: parsed=%s joined=%s resolved=%s title=%s members=%s",
            spec,
            join_result.reason or join_result.joined,
            resolved.method,
            details.title,
            details.members,
        )

    async def _runner() -> None:
        for target in targets:
            await _resolve_one(target)

    asyncio.get_event_loop().run_until_complete(_runner())
    return results


__all__ = [
    "TargetSpec",
    "JoinResult",
    "ResolvedTarget",
    "TargetDetails",
    "parse_target",
    "ensure_join_if_needed",
    "ensure_joined",
    "resolve_peer",
    "resolve_entity",
    "fetch_target_details",
    "debug_resolve_targets",
]
