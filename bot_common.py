"""Shared helpers for Gonka monitoring scripts.

The module deliberately keeps runtime configuration lazy: importing a watcher
for tests must not require Telegram secrets to be present.
"""

from __future__ import annotations

import copy
import html
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests


class SourcesUnavailable(RuntimeError):
    """Raised when every configured HTTP source failed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def participant_id(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("index", "participant_id", "address", "id", "inference_url"):
            if key in entry:
                return str(entry[key])
    if isinstance(entry, str):
        return entry
    return json.dumps(entry, sort_keys=True)


def participant_url(entry: Any) -> Any:
    if isinstance(entry, dict):
        return entry.get("inference_url", "")
    return ""


def participant_weight(entry: Any) -> int | None:
    if not isinstance(entry, dict):
        return None
    value = entry.get("weight")
    if isinstance(value, bool):
        return None
    try:
        weight = int(value)
    except (TypeError, ValueError):
        return None
    return weight if weight >= 0 else None


def participant_models(entry: Any) -> list[str]:
    if not isinstance(entry, dict) or not isinstance(entry.get("models"), list):
        return []

    models = []
    for value in entry["models"]:
        if not isinstance(value, str) or not value.strip():
            continue
        model = value.strip().rstrip("/").rsplit("/", 1)[-1]
        if model and model not in models:
            models.append(model)
    return models


def participant_ml_node_count(entry: Any) -> int | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("ml_nodes"), list):
        return None

    count = 0
    for group in entry["ml_nodes"]:
        nodes = group.get("ml_nodes") if isinstance(group, dict) else None
        if isinstance(nodes, list):
            count += sum(1 for node in nodes if isinstance(node, dict))
    return count


def participant_weight_ranks(entries: Iterable[Any]) -> dict[str, int]:
    weighted = []
    for entry in entries:
        weight = participant_weight(entry)
        if weight is not None:
            weighted.append((participant_id(entry), weight))

    return {
        node_id: 1 + sum(other_weight > weight for _, other_weight in weighted)
        for node_id, weight in weighted
    }


def format_integer(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def build_host_details(
    entry: Any,
    rank: int | None,
    participant_count: int,
    *,
    detail_lines: Iterable[str] = (),
) -> str:
    node_id = participant_id(entry)
    weight = participant_weight(entry)
    models = participant_models(entry)
    ml_node_count = participant_ml_node_count(entry)
    url = participant_url(entry)

    weight_text = f"<b>{format_integer(weight)}</b>" if weight is not None else "нет данных"
    rank_text = (
        f"<b>{rank} из {participant_count}</b>"
        if rank is not None
        else "нет данных"
    )
    models_text = (
        ", ".join(f"<code>{escape_html(model)}</code>" for model in models)
        if models
        else "нет данных"
    )
    ml_nodes_text = (
        f"<b>{ml_node_count}</b>" if ml_node_count is not None else "нет данных"
    )
    url_text = f"<code>{escape_html(url)}</code>" if url else "нет данных"

    lines = [f"• <code>{escape_html(node_id)}</code>"]
    lines.extend(f"  {line}" for line in detail_lines)
    lines.extend(
        (
            f"  Вес: {weight_text}",
            f"  Место по весу: {rank_text}",
            f"  Модели: {models_text}",
            f"  ML-ноды: {ml_nodes_text}",
            f"  API: {url_text}",
        )
    )
    return "\n".join(lines)


def load_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def save_json_atomic(
    path: str | Path,
    value: Any,
    *,
    sort_keys: bool = False,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            value,
            file,
            indent=2,
            ensure_ascii=False,
            sort_keys=sort_keys,
        )
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def _request_with_fallback(
    urls: Iterable[str],
    *,
    timeout: int,
    attempts: int,
    retry_delay: float,
    response_reader: Callable[[requests.Response], Any],
    validator: Callable[[Any], None] | None = None,
    session=requests,
) -> tuple[Any, str]:
    configured_urls = [url for url in urls if isinstance(url, str) and url]
    if not configured_urls:
        raise ValueError("at least one source URL is required")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    errors: list[str] = []
    for url in configured_urls:
        for attempt in range(1, attempts + 1):
            try:
                response = session.get(
                    url,
                    timeout=timeout,
                    headers={"User-Agent": "gonka-host-bot/1.0"},
                )
                response.raise_for_status()
                value = response_reader(response)
                if validator:
                    validator(value)
                return value, url
            except Exception as exc:  # noqa: BLE001 - aggregate source errors
                errors.append(
                    f"{url} attempt {attempt}/{attempts}: "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt < attempts and retry_delay > 0:
                    time.sleep(retry_delay)

    raise SourcesUnavailable("all sources failed: " + " | ".join(errors))


def fetch_json_with_fallback(
    urls: Iterable[str],
    *,
    timeout: int = 15,
    attempts: int = 2,
    retry_delay: float = 1,
    validator: Callable[[Any], None] | None = None,
    session=requests,
) -> tuple[Any, str]:
    return _request_with_fallback(
        urls,
        timeout=timeout,
        attempts=attempts,
        retry_delay=retry_delay,
        response_reader=lambda response: response.json(),
        validator=validator,
        session=session,
    )


def fetch_text_with_fallback(
    urls: Iterable[str],
    *,
    timeout: int = 15,
    attempts: int = 2,
    retry_delay: float = 1,
    validator: Callable[[str], None] | None = None,
    session=requests,
) -> tuple[str, str]:
    return _request_with_fallback(
        urls,
        timeout=timeout,
        attempts=attempts,
        retry_delay=retry_delay,
        response_reader=lambda response: response.text,
        validator=validator,
        session=session,
    )


def send_telegram_message(
    text: str,
    *,
    parse_mode: str | None = "HTML",
    session=requests,
) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
    if not thread_id:
        raise RuntimeError(
            "TELEGRAM_MESSAGE_THREAD_ID is required; refusing to send outside "
            "the configured Telegram topic"
        )
    try:
        parsed_thread_id = int(thread_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_MESSAGE_THREAD_ID must be an integer") from exc
    if parsed_thread_id <= 0:
        raise ValueError("TELEGRAM_MESSAGE_THREAD_ID must be positive")

    destinations: list[tuple[str, int | None]] = [(chat_id, parsed_thread_id)]
    secondary_chat_id = os.environ.get("TELEGRAM_SECONDARY_CHAT_ID", "").strip()
    if secondary_chat_id and secondary_chat_id != chat_id:
        destinations.append((secondary_chat_id, None))

    errors: list[str] = []
    for destination_chat_id, destination_thread_id in destinations:
        payload: dict[str, Any] = {
            "chat_id": destination_chat_id,
            "text": text,
        }
        if destination_thread_id is not None:
            payload["message_thread_id"] = destination_thread_id
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - attempt every destination
            errors.append(
                f"chat {destination_chat_id}: {type(exc).__name__}: {exc}"
            )

    if errors:
        raise RuntimeError(
            "Telegram delivery failed for one or more destinations: "
            + " | ".join(errors)
        )
