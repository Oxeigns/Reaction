#!/usr/bin/env python3
"""OxyReport: multi-target Telegram reporting utility."""

from __future__ import annotations

import asyncio
import datetime
import re
import sys
from typing import Dict, List

from pyrogram import Client
from pyrogram.errors import BadRequest, FloodWait, RPCError, SessionPasswordNeeded, UserNotParticipant

from report import report_profile_photo, send_report

DEVELOPER_SIGNATURE = "@oxeign"
DEFAULT_REPORT_COUNT = 5000

REPORT_REASONS: Dict[str, str] = {
    "1": "Spamming and Unwanted Content",
    "2": "Child Abuse and Nudity",
    "3": "Pornography and Explicit Material",
    "4": "Promoting Violence and Gore",
    "5": "Illegal Drug Sales and Activity",
    "6": "Hate Speech and Discrimination",
    "7": "Copyright and Intellectual Property Infringement",
    "8": "Impersonation and Scams",
    "9": "Other (Custom Text Required)",
}

REASON_CODE_MAP: Dict[str, int] = {
    "1": 0,
    "2": 3,
    "3": 2,
    "4": 1,
    "5": 5,
    "6": 5,
    "7": 4,
    "8": 5,
    "9": 5,
}

TOTAL_SENT = 0
STOP_EVENT = asyncio.Event()


def parse_url(url: str) -> Dict[str, object]:
    """Parse a Telegram URL and classify the target type."""
    url = url.strip()
    result: Dict[str, object] = {"type": "unknown", "entity_id": None, "message_id": None}

    msg_match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/(c/)?([^/]+)/([0-9]+)", url)
    if msg_match:
        is_c_group = msg_match.group(1)
        part = msg_match.group(2)
        message_id = int(msg_match.group(3))
        result.update({"type": "message", "message_id": message_id})

        if is_c_group == "c/":
            result["entity_id"] = int(f"-100{part}") if part.isdigit() else f"-100{part}"
        elif part.isdigit():
            result["entity_id"] = int(part)
        else:
            result["entity_id"] = part
        return result

    invite_match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/(joinchat/[^/?#]+|\+[^/?#]+)", url)
    if invite_match:
        entity = invite_match.group(1)
        result.update({"type": "invite", "entity_id": entity})
        return result

    chat_match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/([^/]+)/?$", url)
    if chat_match:
        entity = chat_match.group(1)
        if entity.startswith("+") or entity.startswith("joinchat/"):
            result.update({"type": "invite", "entity_id": entity})
        elif entity.isdigit() or entity.startswith("-100"):
            result.update({"type": "chat", "entity_id": int(entity) if entity.isdigit() else entity})
        else:
            result.update({"type": "profile", "entity_id": entity})
        return result

    raise ValueError("Invalid Telegram URL format.")


async def join_private_group(client: Client, invite_link: str):
    """Attempt to join a private group/channel using the provided invite link."""
    try:
        joined = await client.join_chat(invite_link)
        chat_id = getattr(joined, "id", None) or getattr(joined, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(joined, "username", None) or getattr(joined, "title", None) or invite_link
        print(f"[{client.name}] Successfully joined the private chat via invite link: {chat_id}")
        return chat_id
    except UserNotParticipant:
        print(f"[{client.name}] Failed to join. Invalid invite link or access denied.")
        return None
    except Exception as exc:
        print(f"[{client.name}] Error during group join attempt: {exc}")
        return None


async def verify_target(client: Client, target: Dict[str, object]) -> bool:
    """Verify the target exists (used for message targets)."""
    if target["type"] != "message":
        return True

    try:
        message = await client.get_messages(target["entity_id"], message_ids=target["message_id"])
        if not message:
            print(f"Verification: message not found {target['entity_id']}/{target['message_id']}")
            return False
        return True
    except Exception as exc:
        print(f"Verification failed for {target['entity_id']}/{target['message_id']}: {exc}")
        return False


async def multi_target_report_worker(
    client: Client,
    target_urls: List[Dict[str, object]],
    reason_int: int,
    reason_text: str,
    total_target: int,
    session_name: str,
) -> None:
    """Worker loop for each session string."""
    global TOTAL_SENT

    target_index = 0
    num_targets = len(target_urls)

    while not STOP_EVENT.is_set():
        if TOTAL_SENT >= total_target:
            break

        target = target_urls[target_index % num_targets]
        target_index += 1

        entity_id = target["entity_id"]
        msg_id = target.get("message_id")
        target_type = target["type"]

        try:
            success = False
            if target_type == "message":
                success = await send_report(client, entity_id, msg_id, reason_int, reason_text)
            elif target_type in ("profile", "chat", "invite"):
                success = await report_profile_photo(client, entity_id, reason_int, reason_text)

            if success:
                TOTAL_SENT += 1
                print(f"[{TOTAL_SENT}/{total_target}] Sent {target_type.upper()} Report for {entity_id} via {session_name}")
            else:
                await asyncio.sleep(1)

        except FloodWait as exc:
            wait_seconds = getattr(exc, "value", None) or getattr(exc, "x", None) or 30
            print(f"[{session_name}] ⚠️ FloodWait hit! Sleeping for {wait_seconds} seconds.")
            await asyncio.sleep(wait_seconds)
        except BadRequest as exc:
            print(f"[{session_name}] ❌ Stop Signal: Fatal Request Error. ({exc})")
            STOP_EVENT.set()
            break
        except RPCError as exc:
            print(f"[{session_name}] ⚠️ RPC Error: {exc}. Retrying in 3s.")
            await asyncio.sleep(3)
        except Exception as exc:
            print(f"[{session_name}] ❌ Generic Error: {exc}. Retrying in 1s.")
            await asyncio.sleep(1)

        await asyncio.sleep(0.05)


def prompt_api_credentials():
    while True:
        api_id_raw = input("Enter API ID (Mandatory): ").strip()
        if api_id_raw and api_id_raw.isdigit():
            api_id = int(api_id_raw)
            break
        print("API ID must be a number and cannot be empty.")

    while True:
        api_hash = input("Enter API HASH (Mandatory): ").strip()
        required_len = 5 + len(DEVELOPER_SIGNATURE)
        if api_hash and len(api_hash) >= required_len:
            break
        print(f"Invalid API Hash. Hash length must be at least {required_len} characters.")

    return api_id, api_hash


def prompt_sessions() -> List[str]:
    sessions: List[str] = []
    print("\n--- Session Entry ---")
    print("Enter at least one session string.")
    while True:
        sess_str = input(f"Enter Session String #{len(sessions) + 1}: ").strip()
        if sess_str:
            sessions.append(sess_str)
            print(f"Session #{len(sessions)} added.")

        if sessions:
            choice = input("Add another session? (y/n): ").strip().lower()
            if choice == "n":
                break
        elif not sess_str:
            print("No session entered.")
            break
    return sessions


async def prompt_targets(clients: List[Client]) -> List[Dict[str, object]]:
    target_urls: List[Dict[str, object]] = []

    print("\n--- Target URL Entry ---")
    print("Enter up to 5 Telegram URLs (Messages, Profiles, Public/Private Chats).")
    for i in range(1, 6):
        url_input = input(f"Enter Target URL #{i} (or press Enter to finish): ").strip()
        if not url_input:
            if i == 1:
                print("At least one URL is required.")
                continue
            break

        try:
            parsed_target = parse_url(url_input)
            if parsed_target["type"] == "invite":
                print(f"Detected potential private group/invite link: {url_input}")
                invite_link = input("-> Please enter the FULL INVITE LINK (+ABCxyz or joinchat/...): ").strip()

                if invite_link:
                    print("Attempting to join the private chat...")
                    joined_chat_id = await join_private_group(clients[0], invite_link)
                    if joined_chat_id:
                        parsed_target["entity_id"] = joined_chat_id
                        parsed_target["type"] = "chat"
                    else:
                        print("Failed to join the chat. Skipping this URL.")
                        continue
                else:
                    print("Invite link not provided. Skipping this URL.")
                    continue

            target_urls.append(parsed_target)
        except Exception as exc:
            print(f"Invalid URL entered: {exc}. Please try again.")

    return target_urls


def prompt_reason_and_description():
    while True:
        print("\nSelect Report Reason (Mandatory):")
        for code, reason in REPORT_REASONS.items():
            print(f"  [{code}] {reason}")
        reason_code = input("Select Option: ").strip()
        if reason_code in REPORT_REASONS:
            break
        print("Invalid option selected.")

    while True:
        reason_text = input("Enter Report Description (Mandatory): ").strip()
        if reason_text:
            break
        print("Description cannot be empty.")

    mapped_reason_int = REASON_CODE_MAP.get(reason_code, 5)
    return mapped_reason_int, reason_text


def prompt_total_reports():
    while True:
        report_input = input(f"Total Reports to send (Default {DEFAULT_REPORT_COUNT}): ").strip()
        if not report_input:
            return DEFAULT_REPORT_COUNT
        try:
            total_reports = int(report_input)
            if total_reports > 0:
                return total_reports
            print("Number must be greater than 0.")
        except ValueError:
            print("Invalid number entered.")


def print_summary(start_time: datetime.datetime):
    end_time = datetime.datetime.now()
    print("\n" + "=" * 40)
    print("✅ PROCESS FINISHED")
    print("=" * 40)
    print(f"Total Sent  : {TOTAL_SENT}")
    print(f"Duration    : {end_time - start_time}")
    print("=" * 40)


async def run():
    print("\n--- OxyReport (Ultimate Multi-Target Mode) ---")
    print(f"--- Developer: {DEVELOPER_SIGNATURE} ---\n")

    print("--- Login Configuration ---")
    api_id, api_hash = prompt_api_credentials()

    sessions = prompt_sessions()
    if not sessions:
        print("No active sessions. Exiting.")
        return

    active_clients: List[Client] = []
    print(f"\nLogging in {len(sessions)} sessions...")

    for idx, sess_str in enumerate(sessions):
        session_name = f"{DEVELOPER_SIGNATURE}_sess_{idx}"
        try:
            client = Client(
                name=session_name,
                api_id=api_id,
                api_hash=api_hash,
                session_string=sess_str,
                in_memory=True,
                no_updates=True,
            )
            await client.start()
            active_clients.append(client)
            print(f"✅ Session {session_name} connected.")
        except SessionPasswordNeeded:
            print(f"❌ Session {session_name} Failed: Requires 2FA password.")
        except Exception as exc:
            print(f"❌ Session {session_name} Failed: {exc}")

    if not active_clients:
        print("No active sessions could be started. Exiting.")
        return

    target_urls = await prompt_targets(active_clients)
    if not target_urls:
        print("No valid targets provided. Exiting.")
        stop_tasks = [c.stop() for c in active_clients]
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        return

    print("\nVerifying all targets...")
    valid_targets: List[Dict[str, object]] = []
    for target in target_urls:
        if await verify_target(active_clients[0], target):
            valid_targets.append(target)
            print(f"✅ Valid: {target['entity_id']}")
        else:
            print(f"❌ Invalid: {target['entity_id']} (Skipping)")

    if not valid_targets:
        print("All targets failed verification. Exiting.")
        stop_tasks = [c.stop() for c in active_clients]
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        return

    reason_int, reason_text = prompt_reason_and_description()
    total_reports = prompt_total_reports()

    print("Starting concurrent tasks...")
    start_time = datetime.datetime.now()

    tasks = [
        asyncio.create_task(
            multi_target_report_worker(
                client,
                valid_targets,
                reason_int,
                reason_text,
                total_reports,
                f"{DEVELOPER_SIGNATURE}_Worker-{i + 1}",
            )
        )
        for i, client in enumerate(active_clients)
    ]

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        print("\nProcess manually stopped by user.")
        STOP_EVENT.set()
    except Exception as exc:
        print(f"An unexpected error occurred during task gathering: {exc}")
        STOP_EVENT.set()
    finally:
        print("Initiating final cleanup and task cancellation...")
        pending_tasks = [t for t in tasks if not t.done()]
        if pending_tasks:
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        print_summary(start_time)

        print("Stopping sessions concurrently...")
        stop_tasks = [c.stop() for c in active_clients]
        await asyncio.gather(*stop_tasks, return_exceptions=True)
        print("Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nProgram exit initiated.")
        sys.exit(0)
