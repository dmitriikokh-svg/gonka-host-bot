"""Telegram commands from the latest watcher snapshots.

Long-polls getUpdates. Outgoing replies go through send_telegram_message so
alerts stay in the configured topic. Do not run two listeners on one token.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

from bot_common import load_json, save_json_atomic, send_telegram_message
import chain_halt_watcher
import escrow_balance_watcher
import excluded_watcher
import model_coefficients_watcher
import our_nodes_watcher
import upgrade_adoption_watcher as adoption_watcher
import gateway_watcher
import devshard_watcher

ROOT = Path(__file__).resolve().parent
OFFSET_FILE = ROOT / "state" / "command_listener.json"
POLL_TIMEOUT_SECONDS = 30
API_COMMANDS = frozenset({"api", "апи"})
MLNODE_COMMANDS = frozenset({"mlnode", "mlnodes", "млнода"})
HALT_COMMANDS = frozenset({"halt", "халт", "chain"})
NODES_COMMANDS = frozenset({"nodes", "ноды", "our"})
ESCROW_COMMANDS = frozenset({"escrow", "эскроу"})
MODELS_COMMANDS = frozenset({"models", "модели"})
EXCLUDED_COMMANDS = frozenset({"excluded", "exclude", "исключения"})
DEVSHARD_COMMANDS = frozenset({"devshard", "девшард", "ds"})
GATEWAY_COMMANDS = frozenset({"gateways", "gateway", "гейтвеи", "брокеры"})
BOT_COMMANDS = [
    {"command": "api", "description": "Текущая раскатка API"},
    {"command": "mlnode", "description": "Текущая раскатка MLNode"},
    {"command": "halt", "description": "Жива ли цепь"},
    {"command": "nodes", "description": "Наши ноды и CPoC"},
    {"command": "escrow", "description": "Балансы эскроу-ключей"},
    {"command": "models", "description": "Коэффициенты PoC моделей"},
    {"command": "excluded", "description": "Исключённые после CPoC"},
    {"command": "devshard", "description": "Слоты devshard у хостов"},
    {"command": "gateways", "description": "Протокол гейтвеев брокеров"},
]


def parse_command(text: str) -> str | None:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    first = stripped.split()[0]
    name = first[1:].split("@", 1)[0]
    return name.casefold() or None


def configured_destination() -> tuple[str, int]:
    chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    thread_raw = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
    if not chat_id or not thread_raw:
        raise RuntimeError("TELEGRAM_CHAT_ID and TELEGRAM_MESSAGE_THREAD_ID are required")
    return chat_id, int(thread_raw)


def message_matches_topic(message: dict, chat_id: str, thread_id: int) -> bool:
    chat = message.get("chat") or {}
    incoming_chat = str(chat.get("id") or "")
    incoming_thread = message.get("message_thread_id")
    try:
        incoming_thread_id = int(incoming_thread)
    except (TypeError, ValueError):
        return False
    return incoming_chat == chat_id and incoming_thread_id == thread_id


def load_state(path: Path):
    return load_json(path)


def reply_for_command(command: str) -> str | None:
    if command in API_COMMANDS:
        payload = load_state(adoption_watcher.STATE_FILE)
        return adoption_watcher.format_command_api_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in MLNODE_COMMANDS:
        payload = load_state(adoption_watcher.STATE_FILE)
        return adoption_watcher.format_command_mlnode_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in HALT_COMMANDS:
        payload = load_state(chain_halt_watcher.STATE_FILE)
        return chain_halt_watcher.format_command_halt_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in NODES_COMMANDS:
        payload = load_state(our_nodes_watcher.STATE_FILE)
        return our_nodes_watcher.format_command_nodes_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in ESCROW_COMMANDS:
        payload = load_state(escrow_balance_watcher.STATE_FILE)
        return escrow_balance_watcher.format_command_escrow_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in MODELS_COMMANDS:
        payload = load_state(model_coefficients_watcher.STATE_FILE)
        return model_coefficients_watcher.format_command_models_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in EXCLUDED_COMMANDS:
        return excluded_watcher.format_command_excluded_message(
            load_state(excluded_watcher.STATE_FILE)
        )
    if command in DEVSHARD_COMMANDS:
        payload = load_state(devshard_watcher.STATE_FILE)
        return devshard_watcher.format_command_devshard_message(
            payload if isinstance(payload, dict) else {}
        )
    if command in GATEWAY_COMMANDS:
        payload = load_state(gateway_watcher.STATE_FILE)
        return gateway_watcher.format_command_gateway_message(
            payload if isinstance(payload, dict) else {}
        )
    return None


def telegram_call(method: str, payload: dict, *, session=requests, timeout: int = 45):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    response = session.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"{method} failed")
    return data.get("result")


def register_bot_commands(session=requests) -> None:
    telegram_call("setMyCommands", {"commands": BOT_COMMANDS}, session=session)


def load_offset() -> int:
    stored = load_json(OFFSET_FILE)
    if isinstance(stored, dict):
        try:
            return int(stored.get("offset") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def save_offset(offset: int) -> None:
    save_json_atomic(OFFSET_FILE, {"offset": offset})


def handle_update(update: dict, chat_id: str, thread_id: int, session=requests) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return
    if not message_matches_topic(message, chat_id, thread_id):
        return
    command = parse_command(str(message.get("text") or ""))
    if not command:
        return
    reply = reply_for_command(command)
    if not reply:
        return
    send_telegram_message(
        reply,
        parse_mode=None,
        include_secondary=False,
        session=session,
    )


def run_forever(session=requests, sleeper=time.sleep) -> None:
    chat_id, thread_id = configured_destination()
    register_bot_commands(session=session)
    offset = load_offset()
    print(f"Command listener started for chat {chat_id} topic {thread_id}")
    while True:
        try:
            updates = telegram_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message"],
                },
                session=session,
                timeout=POLL_TIMEOUT_SECONDS + 15,
            ) or []
        except Exception as exc:  # noqa: BLE001 - keep polling
            print(f"getUpdates failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            sleeper(5)
            continue
        for update in updates:
            update_id = int(update["update_id"])
            try:
                handle_update(update, chat_id, thread_id, session=session)
            except Exception:
                print(f"Failed to handle update {update_id}", file=sys.stderr)
            offset = max(offset, update_id + 1)
            save_offset(offset)


if __name__ == "__main__":
    try:
        run_forever()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
