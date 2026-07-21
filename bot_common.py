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
    thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")

    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    response = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
