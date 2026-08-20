"""Detect abnormal raw transaction byte volume in recent Gonka blocks."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from bot_common import (
    escape_html,
    format_integer,
    load_json,
    save_json_atomic,
    send_telegram_message,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "chain_load.json"
STATE_FILE = ROOT / "state" / "chain_load.json"
VALID_ALERT_LEVELS = {"none", "warning", "critical"}
VALID_STATUSES = {"healthy", "warning", "critical", "monitoring_unavailable"}
TYPE_URL_PATTERN = re.compile(rb"/(?:[A-Za-z0-9_-]+\.)+(Msg[A-Za-z0-9_]+)")
WRAPPER_MESSAGE_TYPES = {"MsgExec"}


class CategorizedError(RuntimeError):
    """A snapshot error with a concise Telegram-safe category."""

    category = "invalid JSON"


class InvalidJsonError(CategorizedError):
    category = "invalid JSON"


class WrongChainError(CategorizedError):
    category = "wrong chain"


class BlockUnavailableError(CategorizedError):
    category = "block unavailable"


class MalformedTransactionError(CategorizedError):
    category = "malformed tx"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_saved_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def positive_int(config: dict, field: str, *, minimum: int = 1) -> int:
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def validate_rpc_url(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty URL")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid URL in {field}: {value!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"credentials are not allowed in {field}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"query and fragment are not allowed in {field}")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid port in {field}: {value!r}") from exc
    return value.strip().rstrip("/")


def validate_config(config: Any) -> dict:
    if not isinstance(config, dict):
        raise ValueError("chain load config must be a JSON object")
    normalized = copy.deepcopy(config)
    expected_chain_id = normalized.get("expected_chain_id")
    if not isinstance(expected_chain_id, str) or not expected_chain_id.strip():
        raise ValueError("expected_chain_id is required")
    normalized["expected_chain_id"] = expected_chain_id.strip()

    urls = normalized.get("rpc_urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("rpc_urls must be a non-empty list")
    normalized_urls = [
        validate_rpc_url(url, f"rpc_urls[{index}]")
        for index, url in enumerate(urls)
    ]
    if len(set(normalized_urls)) != len(normalized_urls):
        raise ValueError("rpc_urls must not contain duplicates")
    normalized["rpc_urls"] = normalized_urls

    for field, minimum in (
        ("window_blocks", 1),
        ("warning_bytes", 1),
        ("critical_after_consecutive_windows", 2),
        ("recovery_after_clean_windows", 1),
        ("unavailable_alert_after_runs", 1),
        ("critical_reminder_minutes", 1),
        ("request_timeout_seconds", 1),
        ("poll_interval_seconds", 1),
        ("top_message_types", 1),
        ("top_hot_blocks", 1),
    ):
        positive_int(normalized, field, minimum=minimum)
    return normalized


def load_config(
    path: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> dict:
    config = load_json(path or CONFIG_FILE)
    if not isinstance(config, dict):
        raise ValueError("chain load config must be a JSON object")
    config = copy.deepcopy(config)
    environment = os.environ if environ is None else environ
    raw_override = environment.get("CHAIN_LOAD_RPC_URLS", "")
    override_urls = []
    for item in raw_override.replace("\n", ",").split(","):
        value = item.strip()
        if value:
            normalized = validate_rpc_url(value, "CHAIN_LOAD_RPC_URLS")
            if normalized not in override_urls:
                override_urls.append(normalized)
    if override_urls:
        config["rpc_urls"] = override_urls
    return validate_config(config)


def rpc_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def get_json(url: str, config: dict, *, session=requests) -> dict:
    response = session.get(
        url,
        timeout=config["request_timeout_seconds"],
        headers={"User-Agent": "gonka-host-bot/chain-load-v1"},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - normalize JSON decoder variants
        raise InvalidJsonError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidJsonError("RPC response must be a JSON object")
    return payload


def parse_positive_height(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise InvalidJsonError(f"{field} is missing")
    if isinstance(value, float) and not value.is_integer():
        raise InvalidJsonError(f"{field} must be an integer")
    try:
        height = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidJsonError(f"{field} is invalid") from exc
    if height < 1:
        raise InvalidJsonError(f"{field} must be positive")
    return height


def parse_status_payload(payload: Any, config: dict) -> tuple[int, str]:
    result = payload.get("result") if isinstance(payload, dict) else None
    node_info = result.get("node_info") if isinstance(result, dict) else None
    sync_info = result.get("sync_info") if isinstance(result, dict) else None
    if not isinstance(node_info, dict) or not isinstance(sync_info, dict):
        raise InvalidJsonError("result.node_info or result.sync_info is missing")
    chain_id = node_info.get("network")
    if not isinstance(chain_id, str) or not chain_id:
        raise InvalidJsonError("result.node_info.network is missing")
    if chain_id != config["expected_chain_id"]:
        raise WrongChainError(
            f"expected {config['expected_chain_id']}, received {chain_id}"
        )
    height = parse_positive_height(
        sync_info.get("latest_block_height"),
        "result.sync_info.latest_block_height",
    )
    return height, chain_id


def decode_transaction(value: Any, height: int, index: int) -> bytes:
    if not isinstance(value, str):
        raise MalformedTransactionError(
            f"block {height} transaction {index} is not a base64 string"
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedTransactionError(
            f"block {height} transaction {index} has malformed base64: {exc}"
        ) from exc


def classify_message_types(raw_tx: bytes) -> str:
    types = []
    for match in TYPE_URL_PATTERN.finditer(raw_tx):
        message_type = match.group(1).decode("ascii")
        if message_type not in types:
            types.append(message_type)
    inner_types = [item for item in types if item not in WRAPPER_MESSAGE_TYPES]
    if inner_types:
        types = inner_types
    return " + ".join(types) if types else "(unknown)"


def parse_block_payload(payload: Any, requested_height: int) -> list[Any]:
    result = payload.get("result") if isinstance(payload, dict) else None
    block = result.get("block") if isinstance(result, dict) else None
    header = block.get("header") if isinstance(block, dict) else None
    data = block.get("data") if isinstance(block, dict) else None
    if not isinstance(header, dict) or not isinstance(data, dict):
        raise BlockUnavailableError(f"block {requested_height} is missing")
    try:
        response_height = parse_positive_height(
            header.get("height"),
            "result.block.header.height",
        )
    except InvalidJsonError as exc:
        raise BlockUnavailableError(str(exc)) from exc
    if response_height != requested_height:
        raise BlockUnavailableError(
            f"requested block {requested_height}, received {response_height}"
        )
    if "txs" not in data:
        raise BlockUnavailableError(f"block {requested_height} data.txs is missing")
    txs = data["txs"]
    if txs is None:
        return []
    if not isinstance(txs, list):
        raise BlockUnavailableError(f"block {requested_height} data.txs is not a list")
    return txs


def analyze_block(payload: Any, requested_height: int) -> dict:
    txs = parse_block_payload(payload, requested_height)
    block_bytes = 0
    max_tx_bytes = 0
    message_types: dict[str, dict[str, int]] = {}
    for index, encoded in enumerate(txs):
        raw_tx = decode_transaction(encoded, requested_height, index)
        tx_bytes = len(raw_tx)
        block_bytes += tx_bytes
        max_tx_bytes = max(max_tx_bytes, tx_bytes)
        signature = classify_message_types(raw_tx)
        stats = message_types.setdefault(signature, {"bytes": 0, "count": 0})
        stats["bytes"] += tx_bytes
        stats["count"] += 1
    return {
        "height": requested_height,
        "tx_count": len(txs),
        "sum_tx_bytes": block_bytes,
        "max_tx_bytes": max_tx_bytes,
        "message_types": message_types,
    }


def merge_message_stats(target: dict, source: dict) -> None:
    for signature, values in source.items():
        stats = target.setdefault(signature, {"bytes": 0, "count": 0})
        stats["bytes"] += int(values["bytes"])
        stats["count"] += int(values["count"])


def fetch_rpc_snapshot(base_url: str, config: dict, *, session=requests) -> dict:
    status_payload = get_json(rpc_url(base_url, "/status"), config, session=session)
    latest_height, chain_id = parse_status_payload(status_payload, config)
    window_start = max(1, latest_height - config["window_blocks"] + 1)
    blocks = []
    sum_tx_bytes = 0
    tx_count = 0
    max_tx_bytes = 0
    message_types: dict[str, dict[str, int]] = {}

    for height in range(window_start, latest_height + 1):
        payload = get_json(
            rpc_url(base_url, f"/block?height={height}"),
            config,
            session=session,
        )
        block = analyze_block(payload, height)
        blocks.append(block)
        # Add each completed block exactly once, after every tx was processed.
        sum_tx_bytes += block["sum_tx_bytes"]
        tx_count += block["tx_count"]
        max_tx_bytes = max(max_tx_bytes, block["max_tx_bytes"])
        merge_message_stats(message_types, block["message_types"])

    sorted_types = [
        {"type": signature, **values}
        for signature, values in sorted(
            message_types.items(),
            key=lambda item: (-item[1]["bytes"], -item[1]["count"], item[0]),
        )
    ]
    hot_blocks = sorted(
        (
            {
                "height": block["height"],
                "bytes": block["sum_tx_bytes"],
                "tx_count": block["tx_count"],
            }
            for block in blocks
        ),
        key=lambda item: (-item["bytes"], -item["tx_count"], item["height"]),
    )
    return {
        "rpc": base_url,
        "chain_id": chain_id,
        "latest_height": latest_height,
        "window_start": window_start,
        "window_end": latest_height,
        "window_block_count": len(blocks),
        "tx_count": tx_count,
        "sum_tx_bytes": sum_tx_bytes,
        "max_tx_bytes": max_tx_bytes,
        "message_types": sorted_types,
        "blocks": blocks,
        "hot_blocks": hot_blocks,
    }


def exception_category(exc: Exception) -> str:
    if isinstance(exc, CategorizedError):
        return exc.category
    message = str(exc).lower()
    type_name = type(exc).__name__.lower()
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if "timeout" in type_name or "timeout" in message or "timed out" in message:
        return "timeout"
    if isinstance(status_code, int) and 400 <= status_code <= 599:
        return f"HTTP {status_code}"
    status_match = re.search(
        r"\b([45]\d\d)\s+(?:client error|server error|service unavailable|"
        r"bad gateway|gateway timeout|not found)\b",
        message,
    )
    if status_match:
        return f"HTTP {status_match.group(1)}"
    if "connection" in type_name or "connection" in message or "resolve" in message:
        return "connection error"
    return "invalid JSON"


def source_observation(index: int, rpc: str, status: str, **values: Any) -> dict:
    return {"name": f"rpc{index + 1}", "rpc": rpc, "status": status, **values}


def collect_snapshot(config: dict, *, session=requests) -> dict:
    observations = []
    for index, base_url in enumerate(config["rpc_urls"]):
        try:
            snapshot = fetch_rpc_snapshot(base_url, config, session=session)
        except Exception as exc:  # noqa: BLE001 - retry whole window on next RPC
            observations.append(
                source_observation(
                    index,
                    base_url,
                    "unavailable",
                    error_category=exception_category(exc),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        observations.append(
            source_observation(
                index,
                base_url,
                "available",
                latest_height=snapshot["latest_height"],
                error_category=None,
                error=None,
            )
        )
        return {"available": True, "snapshot": snapshot, "sources": observations}
    return {"available": False, "snapshot": None, "sources": observations}


def default_state() -> dict:
    return {
        "version": 1,
        "checked_at": None,
        "status": "healthy",
        "last_evaluated_height": None,
        "last_window_start": None,
        "last_sum_tx_bytes": None,
        "consecutive_breaches": 0,
        "consecutive_clean_windows": 0,
        "alert_level": "none",
        "event_started_at": None,
        "last_alert_at": None,
        "last_reminder_at": None,
        "last_recovered_at": None,
        "last_working_rpc": None,
        "snapshot": {},
        "sources": [],
        "monitoring": {
            "unavailable_runs": 0,
            "alerted": False,
            "first_unavailable_at": None,
            "last_alert_at": None,
            "last_recovered_at": None,
        },
    }


def load_state(path: str | Path | None = None) -> dict:
    value = load_json(path or STATE_FILE, default_state())
    if not isinstance(value, dict):
        raise ValueError("chain load state must be a JSON object")
    state = default_state()
    state.update(value)
    monitoring = value.get("monitoring", {})
    if not isinstance(monitoring, dict):
        monitoring = {}
    state["monitoring"] = {**default_state()["monitoring"], **monitoring}
    if not isinstance(state.get("snapshot"), dict):
        state["snapshot"] = {}
    if not isinstance(state.get("sources"), list):
        state["sources"] = []
    if state.get("alert_level") not in VALID_ALERT_LEVELS:
        state["alert_level"] = "none"
    if state.get("status") not in VALID_STATUSES:
        state["status"] = "monitoring_unavailable"
    return state


def decimal_mb(value: int) -> str:
    return f"{value / 1_000_000:.1f} MB"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "нет данных"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    if secs or not parts:
        parts.append(f"{secs} сек.")
    return " ".join(parts)


def build_warning_message(snapshot: dict, config: dict) -> str:
    lines = [
        "⚠️ <b>Аномальный объём транзакций</b>",
        "",
        f"Блоки: {format_integer(snapshot['window_start'])}–"
        f"{format_integer(snapshot['window_end'])}",
        f"Объём: <b>{decimal_mb(snapshot['sum_tx_bytes'])}</b> из допустимых "
        f"<b>{decimal_mb(config['warning_bytes'])}</b>",
        f"Транзакций: <b>{format_integer(snapshot['tx_count'])}</b>",
        f"Максимальная транзакция: "
        f"<b>{decimal_mb(snapshot['max_tx_bytes'])}</b>",
        "",
        "Основные типы:",
    ]
    types = snapshot["message_types"][: config["top_message_types"]]
    if types:
        for item in types:
            lines.append(
                f"• {escape_html(item['type'])} — <b>{decimal_mb(item['bytes'])}</b>"
            )
    else:
        lines.append("• нет транзакций")
    lines.extend(("", "Самые тяжёлые блоки:"))
    for item in snapshot["hot_blocks"][: config["top_hot_blocks"]]:
        lines.append(
            f"• {format_integer(item['height'])} — <b>{decimal_mb(item['bytes'])}</b>"
        )
    return "\n".join(lines)


def build_critical_message(snapshot: dict, config: dict, breaches: int) -> str:
    return (
        "🔴 <b>Продолжается аномальная нагрузка на chain</b>\n\n"
        f"Превышений подряд: <b>{breaches}</b>\n"
        f"Последние {snapshot['window_block_count']} блоков: "
        f"<b>{decimal_mb(snapshot['sum_tx_bytes'])}</b>\n"
        f"Порог: <b>{decimal_mb(config['warning_bytes'])}</b>"
    )


def build_recovery_message(
    snapshot: dict,
    config: dict,
    duration_seconds: float | None,
) -> str:
    return (
        "🟢 <b>Объём транзакций вернулся к нормальному "
        "уровню</b>\n\n"
        f"Последние {snapshot['window_block_count']} блоков: "
        f"<b>{decimal_mb(snapshot['sum_tx_bytes'])}</b>\n"
        f"Порог: <b>{decimal_mb(config['warning_bytes'])}</b>\n"
        f"Продолжительность события: "
        f"<b>{escape_html(format_duration(duration_seconds))}</b>"
    )


def build_monitoring_unavailable_message(sources: list[dict], total: int) -> str:
    lines = [
        "🟡 <b>Chain load monitor не может получить блоки</b>",
        "",
        f"Доступных RPC: <b>0/{total}</b>",
        "Причины:",
    ]
    for item in sources:
        lines.append(
            f"• <code>{escape_html(item['name'])}</code> — "
            f"{escape_html(item.get('error_category', 'invalid JSON'))}"
        )
    lines.extend(
        (
            "",
            "Это потеря наблюдаемости, а не подтверждённая "
            "нормальная или аномальная нагрузка.",
        )
    )
    return "\n".join(lines)


def build_monitoring_recovery_message(snapshot: dict) -> str:
    return (
        "🟢 <b>Наблюдаемость chain load monitor восстановлена</b>\n\n"
        f"Снова доступен корректный snapshot до блока "
        f"<code>{format_integer(snapshot['latest_height'])}</code>."
    )


def reminder_due(state: dict, config: dict, now: datetime) -> bool:
    previous = parse_saved_time(
        state.get("last_reminder_at") or state.get("last_alert_at")
    )
    if previous is None:
        return True
    return (now - previous).total_seconds() >= config["critical_reminder_minutes"] * 60


def active_status(state: dict) -> str:
    level = state.get("alert_level", "none")
    return level if level in {"warning", "critical"} else "healthy"


def evaluate_available(
    previous: dict,
    snapshot: dict,
    sources: list[dict],
    config: dict,
    now: datetime,
) -> tuple[dict, list[str]]:
    state = copy.deepcopy(previous)
    messages: list[str] = []
    now_text = iso_utc(now)
    state.update(
        {
            "checked_at": now_text,
            "snapshot": copy.deepcopy(snapshot),
            "sources": copy.deepcopy(sources),
            "last_working_rpc": snapshot["rpc"],
        }
    )
    monitoring = state["monitoring"]
    if monitoring.get("alerted"):
        messages.append(build_monitoring_recovery_message(snapshot))
        monitoring["last_recovered_at"] = now_text
    monitoring.update(
        {
            "unavailable_runs": 0,
            "alerted": False,
            "first_unavailable_at": None,
        }
    )

    previous_height = state.get("last_evaluated_height")
    latest_height = snapshot["latest_height"]
    is_new_window = previous_height is None or latest_height > previous_height
    if not is_new_window:
        state["status"] = active_status(state)
        return state, messages

    state.update(
        {
            "last_evaluated_height": latest_height,
            "last_window_start": snapshot["window_start"],
            "last_sum_tx_bytes": snapshot["sum_tx_bytes"],
        }
    )
    breached = snapshot["sum_tx_bytes"] > config["warning_bytes"]
    if breached:
        state["consecutive_clean_windows"] = 0
        breaches = int(state.get("consecutive_breaches", 0) or 0) + 1
        state["consecutive_breaches"] = breaches
        if not state.get("event_started_at"):
            state["event_started_at"] = now_text
        if state.get("alert_level") == "none":
            messages.append(build_warning_message(snapshot, config))
            state["alert_level"] = "warning"
            state["last_alert_at"] = now_text
            state["last_reminder_at"] = None
        if (
            breaches >= config["critical_after_consecutive_windows"]
            and state.get("alert_level") != "critical"
        ):
            messages.append(build_critical_message(snapshot, config, breaches))
            state["alert_level"] = "critical"
            state["last_alert_at"] = now_text
            state["last_reminder_at"] = now_text
        elif state.get("alert_level") == "critical" and reminder_due(
            state, config, now
        ):
            messages.append(build_critical_message(snapshot, config, breaches))
            state["last_reminder_at"] = now_text
    else:
        state["consecutive_breaches"] = 0
        clean = int(state.get("consecutive_clean_windows", 0) or 0) + 1
        state["consecutive_clean_windows"] = clean
        if (
            state.get("alert_level") in {"warning", "critical"}
            and clean >= config["recovery_after_clean_windows"]
        ):
            started = parse_saved_time(state.get("event_started_at"))
            duration = (now - started).total_seconds() if started else None
            messages.append(build_recovery_message(snapshot, config, duration))
            state["alert_level"] = "none"
            state["event_started_at"] = None
            state["last_recovered_at"] = now_text
            state["consecutive_clean_windows"] = 0
    state["status"] = active_status(state)
    return state, messages


def evaluate_unavailable(
    previous: dict,
    sources: list[dict],
    config: dict,
    now: datetime,
) -> tuple[dict, list[str]]:
    state = copy.deepcopy(previous)
    now_text = iso_utc(now)
    state["checked_at"] = now_text
    state["status"] = "monitoring_unavailable"
    state["sources"] = copy.deepcopy(sources)
    monitoring = state["monitoring"]
    runs = int(monitoring.get("unavailable_runs", 0) or 0) + 1
    monitoring["unavailable_runs"] = runs
    monitoring["first_unavailable_at"] = (
        monitoring.get("first_unavailable_at") or now_text
    )
    messages = []
    if runs >= config["unavailable_alert_after_runs"] and not monitoring.get(
        "alerted"
    ):
        messages.append(
            build_monitoring_unavailable_message(sources, len(config["rpc_urls"]))
        )
        monitoring["alerted"] = True
        monitoring["last_alert_at"] = now_text
    return state, messages


def run_once(
    config: dict,
    *,
    now: datetime | None = None,
    collector: Callable[[dict], dict] = collect_snapshot,
    sender: Callable[[str], None] = send_telegram_message,
    state_file: str | Path | None = None,
    persist: bool = True,
    notify: bool = True,
) -> dict:
    check_time = now or utc_now()
    state_path = Path(state_file or STATE_FILE)
    previous = load_state(state_path)
    result = collector(config)
    for observation in result["sources"]:
        print(
            "chain_load_source="
            + json.dumps(observation, ensure_ascii=False, sort_keys=True),
            flush=True,
        )
    if result["available"]:
        state, messages = evaluate_available(
            previous,
            result["snapshot"],
            result["sources"],
            config,
            check_time,
        )
    else:
        state, messages = evaluate_unavailable(
            previous,
            result["sources"],
            config,
            check_time,
        )
    if notify:
        for message in messages:
            sender(message)
    if persist:
        save_json_atomic(state_path, state, sort_keys=True)
    print(
        json.dumps(
            {
                "alert_level": state["alert_level"],
                "checked_at": state["checked_at"],
                "consecutive_breaches": state["consecutive_breaches"],
                "last_evaluated_height": state["last_evaluated_height"],
                "last_working_rpc": state["last_working_rpc"],
                "max_tx_bytes": state.get("snapshot", {}).get("max_tx_bytes"),
                "messages": len(messages),
                "status": state["status"],
                "sum_tx_bytes": state.get("last_sum_tx_bytes"),
                "tx_count": state.get("snapshot", {}).get("tx_count"),
                "window_start": state.get("last_window_start"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return state


def runtime_settings(
    config: dict,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[str, int]:
    environment = os.environ if environ is None else environ
    mode = environment.get("CHAIN_LOAD_MODE", "once").strip().lower()
    if mode not in {"once", "daemon"}:
        raise ValueError("CHAIN_LOAD_MODE must be once or daemon")
    raw_interval = environment.get("CHAIN_LOAD_POLL_INTERVAL_SECONDS")
    if raw_interval is None or not raw_interval.strip():
        interval = config["poll_interval_seconds"]
    else:
        try:
            interval = int(raw_interval)
        except ValueError as exc:
            raise ValueError(
                "CHAIN_LOAD_POLL_INTERVAL_SECONDS must be an integer"
            ) from exc
        if interval < 1:
            raise ValueError("CHAIN_LOAD_POLL_INTERVAL_SECONDS must be positive")
    return mode, interval


def daemon_loop(
    config: dict,
    interval: int,
    stop_event: threading.Event,
    *,
    iteration: Callable[[dict], Any] = run_once,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    while not stop_event.is_set():
        started = monotonic()
        try:
            iteration(config)
        except Exception as exc:  # noqa: BLE001 - daemon survives one iteration
            print(
                f"ERROR: chain load iteration failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if stop_event.is_set():
            break
        elapsed = max(0.0, monotonic() - started)
        stop_event.wait(max(0.0, interval - elapsed))


def install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum, _frame):
        print(f"Received signal {signum}; stopping chain load monitor.", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one check")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="do not send Telegram or persist state",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = load_config()
    mode, interval = runtime_settings(config)
    if args.once:
        mode = "once"
    if args.no_notify and mode != "once":
        raise ValueError("--no-notify is supported only in once mode")
    if mode == "once":
        if args.no_notify:
            run_once(config, persist=False, notify=False)
        else:
            run_once(config)
        return

    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    daemon_loop(config, interval, stop_event)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - command-line boundary
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
