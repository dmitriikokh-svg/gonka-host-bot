"""Monitor finalized WGNK burns until the Gonka bridge completes them.

The watcher scans only Ethereum's finalized block, discovers ERC-20 Transfer
events to the zero address, and keeps each transaction in a persistent queue.
An unavailable data source is never interpreted as a missing or pending bridge
transaction.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from bot_common import (
    SourcesUnavailable,
    escape_html,
    load_json,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "bridge_burn.json"
STATE_FILE = ROOT / "state" / "bridge_burn.json"

TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)
ZERO_ADDRESS_TOPIC = "0x" + "0" * 64
ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def positive_int(config: dict, field: str, *, minimum: int = 1) -> int:
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def validate_url(value: str, field: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid URL in {field}")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid port in {field}") from exc


def load_config() -> dict:
    config = load_json(CONFIG_FILE)
    if not isinstance(config, dict):
        raise ValueError("bridge burn config must be a JSON object")

    owner = config.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("owner is required")

    origin_chain = config.get("origin_chain")
    if not isinstance(origin_chain, str) or not origin_chain.strip():
        raise ValueError("origin_chain is required")

    contract = config.get("wgnk_contract")
    if not isinstance(contract, str) or not ETH_ADDRESS_RE.fullmatch(contract):
        raise ValueError("wgnk_contract must be a 20-byte Ethereum address")

    rpc_urls = config.get("ethereum_rpc_urls")
    if not isinstance(rpc_urls, list) or not rpc_urls:
        raise ValueError("ethereum_rpc_urls must be a non-empty list")
    for url in rpc_urls:
        if not isinstance(url, str):
            raise ValueError("each Ethereum RPC URL must be a string")
        validate_url(url, "ethereum_rpc_urls")

    templates = config.get("gonka_transaction_url_templates")
    if not isinstance(templates, list) or not templates:
        raise ValueError("gonka_transaction_url_templates must be a non-empty list")
    placeholders = ("{origin_chain}", "{block_number}", "{receipt_index}")
    for template in templates:
        if not isinstance(template, str):
            raise ValueError("each Gonka URL template must be a string")
        if any(template.count(placeholder) != 1 for placeholder in placeholders):
            raise ValueError(
                "each Gonka URL template must contain origin_chain, "
                "block_number and receipt_index exactly once"
            )
        validate_url(
            template.format(
                origin_chain="ethereum",
                block_number=1,
                receipt_index=0,
            ),
            "gonka_transaction_url_templates",
        )

    initial = positive_int(config, "initial_check_after_minutes")
    warning = positive_int(config, "warning_after_minutes")
    if warning <= initial:
        raise ValueError("warning_after_minutes must exceed initial_check_after_minutes")

    positive_int(config, "critical_overdue_transactions", minimum=2)
    positive_int(config, "bootstrap_lookback_blocks")
    positive_int(config, "max_blocks_per_log_request")
    positive_int(config, "source_unavailable_alert_after_runs")
    positive_int(config, "request_timeout_seconds")
    positive_int(config, "attempts_per_source")
    config.setdefault("completed_history_limit", 20)
    positive_int(config, "completed_history_limit", minimum=2)
    return config


def default_state() -> dict:
    return {
        "checked_at": None,
        "ethereum_finalized_block": None,
        "last_scanned_finalized_block": None,
        "queue": {},
        "completed_history": [],
        "sources": {},
        "critical_alerted": False,
        "critical_since": None,
    }


def load_state() -> dict:
    state = load_json(STATE_FILE, default_state())
    if not isinstance(state, dict):
        raise ValueError("bridge burn state must be a JSON object")
    for field, default in (("queue", {}), ("sources", {})):
        if not isinstance(state.get(field), dict):
            state[field] = copy.deepcopy(default)
    if not isinstance(state.get("completed_history"), list):
        state["completed_history"] = []
    state.setdefault("critical_alerted", False)
    state.setdefault("critical_since", None)
    return state


def save_state(state: dict) -> None:
    save_json_atomic(STATE_FILE, state, sort_keys=True)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def source_label(url: str) -> str:
    """Return a URL label that cannot expose provider tokens or paths."""
    parsed = urlparse(url)
    if not parsed.hostname:
        return "unknown"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def safe_rpc_error(exc: Exception) -> str:
    """Return diagnostics that cannot echo a credentialed provider URL."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return f"HTTP {status_code}"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection error"
    if isinstance(exc, requests.RequestException):
        return "request error"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "invalid response"
    return type(exc).__name__


def configured_rpc_urls(config: dict) -> list[str]:
    secret_urls: list[str] = []
    raw = os.environ.get("ETHEREUM_RPC_URLS", "")
    for part in raw.replace("\n", ",").split(","):
        value = part.strip()
        if value:
            validate_url(value, "ETHEREUM_RPC_URLS")
            secret_urls.append(value)

    result: list[str] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for url in secret_urls + config["ethereum_rpc_urls"]:
        parsed = urlparse(url)
        key = (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def rpc_call(
    config: dict,
    method: str,
    params: list[Any],
    *,
    validator: Callable[[Any], None] | None = None,
    session=requests,
) -> tuple[Any, str]:
    errors: list[str] = []
    for url in configured_rpc_urls(config):
        label = source_label(url)
        for attempt in range(1, config["attempts_per_source"] + 1):
            try:
                response = session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    headers={"User-Agent": "gonka-host-bot/bridge-burn-v1"},
                    timeout=config["request_timeout_seconds"],
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("JSON-RPC response must be an object")
                if payload.get("error") is not None:
                    raise ValueError(f"JSON-RPC error: {payload['error']}")
                if "result" not in payload:
                    raise ValueError("JSON-RPC response is missing result")
                result = payload["result"]
                if validator:
                    validator(result)
                return result, label
            except Exception as exc:  # noqa: BLE001 - aggregate safe labels
                errors.append(
                    f"{label} attempt {attempt}/{config['attempts_per_source']}: "
                    f"{safe_rpc_error(exc)}"
                )
                if attempt < config["attempts_per_source"]:
                    time.sleep(1)
    raise SourcesUnavailable("all Ethereum RPC sources failed: " + " | ".join(errors))


def parse_hex_int(value: Any, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"{field} must be a hexadecimal string")
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid hexadecimal") from exc
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result


def fetch_finalized_block(
    config: dict,
    *,
    minimum_block: int | None = None,
) -> tuple[int, str]:
    def validate(result: Any) -> None:
        if not isinstance(result, dict):
            raise ValueError("Ethereum finalized block is unavailable")
        block_number = parse_hex_int(result.get("number"), "finalized block number")
        if minimum_block is not None and block_number < minimum_block:
            raise ValueError(
                f"finalized block regressed from {minimum_block} to {block_number}"
            )

    result, source = rpc_call(
        config,
        "eth_getBlockByNumber",
        ["finalized", False],
        validator=validate,
    )
    return parse_hex_int(result.get("number"), "finalized block number"), source


def fetch_burn_logs(config: dict, from_block: int, to_block: int) -> list[dict]:
    if from_block > to_block:
        return []
    logs: list[dict] = []
    step = config["max_blocks_per_log_request"]
    current = from_block
    while current <= to_block:
        chunk_end = min(current + step - 1, to_block)

        def validate(result: Any) -> None:
            if not isinstance(result, list):
                raise ValueError("eth_getLogs result must be a list")
            if any(not isinstance(item, dict) for item in result):
                raise ValueError("eth_getLogs returned a malformed log")

        result, _source = rpc_call(
            config,
            "eth_getLogs",
            [
                {
                    "address": config["wgnk_contract"],
                    "fromBlock": hex(current),
                    "toBlock": hex(chunk_end),
                    "topics": [TRANSFER_TOPIC, None, ZERO_ADDRESS_TOPIC],
                }
            ],
            validator=validate,
        )
        logs.extend(result)
        current = chunk_end + 1
    return logs


def parse_burn_log(log: dict, contract: str) -> dict:
    address = log.get("address")
    topics = log.get("topics")
    if not isinstance(address, str) or address.lower() != contract.lower():
        raise ValueError("burn log has an unexpected contract address")
    if not isinstance(topics, list) or len(topics) < 3:
        raise ValueError("burn log is missing indexed Transfer fields")
    if str(topics[0]).lower() != TRANSFER_TOPIC or str(topics[2]).lower() != ZERO_ADDRESS_TOPIC:
        raise ValueError("log is not a Transfer to the zero address")

    tx_hash = log.get("transactionHash")
    if not isinstance(tx_hash, str) or not TX_HASH_RE.fullmatch(tx_hash):
        raise ValueError("burn log has an invalid transaction hash")
    block_number = parse_hex_int(log.get("blockNumber"), "burn block number")
    receipt_index = parse_hex_int(log.get("transactionIndex"), "transaction index")
    log_index = parse_hex_int(log.get("logIndex"), "log index")
    amount = parse_hex_int(log.get("data"), "burn amount")
    return {
        "key": f"ethereum/{block_number}/{receipt_index}",
        "origin_chain": "ethereum",
        "block_number": block_number,
        "receipt_index": receipt_index,
        "transaction_hash": tx_hash.lower(),
        "log_index": log_index,
        "amount_raw": str(amount),
    }


def add_burn_logs(state: dict, config: dict, logs: list[dict], now: str) -> int:
    added = 0
    for raw_log in logs:
        burn = parse_burn_log(raw_log, config["wgnk_contract"])
        key = burn.pop("key")
        existing = state["queue"].get(key)
        if isinstance(existing, dict):
            indices = existing.setdefault("log_indices", [])
            if burn["log_index"] not in indices:
                indices.append(burn["log_index"])
                existing["amount_raw"] = str(
                    int(existing.get("amount_raw", "0")) + int(burn["amount_raw"])
                )
            continue

        burn.update(
            {
                "detected_finalized_at": now,
                "gonka_status": "UNVERIFIED",
                "validators": [],
                "last_attempted_at": None,
                "last_checked_at": None,
                "last_error": None,
                "source": None,
                "overdue": False,
                "alert_level": None,
                "alerted_at": None,
                "log_indices": [burn.pop("log_index")],
            }
        )
        state["queue"][key] = burn
        added += 1
    return added


def bridge_urls(config: dict, item: dict) -> list[str]:
    return [
        template.format(
            origin_chain=config["origin_chain"],
            block_number=item["block_number"],
            receipt_index=item["receipt_index"],
        )
        for template in config["gonka_transaction_url_templates"]
    ]


def parse_bridge_observation(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("bridge transaction response must be an object")
    transactions = payload.get("bridgeTransactions")
    if transactions is None:
        transactions = payload.get("bridge_transactions")
    if not isinstance(transactions, list):
        raise ValueError("bridge transaction response is missing transactions list")
    if any(not isinstance(tx, dict) for tx in transactions):
        raise ValueError("bridge transaction list contains a malformed item")
    if not transactions:
        return {
            "status": "MISSING",
            "validators": [],
            "completed_validators": [],
            "epoch_index": None,
        }

    statuses: list[str] = []
    validators: set[str] = set()
    normalized: list[dict] = []
    for transaction in transactions:
        status = transaction.get("status")
        normalized_status = status.upper() if isinstance(status, str) and status else "UNKNOWN"
        if isinstance(status, str) and status:
            statuses.append(normalized_status)
        raw_validators = transaction.get("validators") or []
        if not isinstance(raw_validators, list):
            raise ValueError("bridge validators must be a list")
        parsed_validators = {
            value for value in raw_validators if isinstance(value, str) and value
        }
        validators.update(parsed_validators)
        raw_epoch = transaction.get("epochIndex")
        if raw_epoch is None:
            raw_epoch = transaction.get("epoch_index")
        try:
            epoch_index = int(raw_epoch) if raw_epoch is not None else None
        except (TypeError, ValueError):
            epoch_index = None
        if epoch_index is not None and epoch_index < 0:
            epoch_index = None
        normalized.append(
            {
                "status": normalized_status,
                "validators": parsed_validators,
                "epoch_index": epoch_index,
            }
        )

    if "BRIDGE_COMPLETED" in statuses:
        status = "BRIDGE_COMPLETED"
    elif "BRIDGE_PENDING" in statuses:
        status = "BRIDGE_PENDING"
    elif statuses:
        status = statuses[0]
    else:
        status = "UNKNOWN"

    matching = [item for item in normalized if item["status"] == status]
    epoch_values = [
        item["epoch_index"] for item in matching if item["epoch_index"] is not None
    ]
    epoch_index = max(epoch_values) if epoch_values else None
    selected = [
        item
        for item in matching
        if epoch_index is None or item["epoch_index"] == epoch_index
    ]
    completed_validators = {
        validator
        for item in selected
        if item["status"] == "BRIDGE_COMPLETED"
        for validator in item["validators"]
    }
    return {
        "status": status,
        "validators": sorted(validators),
        "completed_validators": sorted(completed_validators),
        "epoch_index": epoch_index,
    }


def fetch_bridge_observation(
    config: dict,
    item: dict,
    *,
    session=requests,
) -> tuple[dict, str]:
    """Query every configured Gonka source and use the most advanced status."""
    observations: list[tuple[dict, str]] = []
    errors: list[str] = []
    for url in bridge_urls(config, item):
        label = source_label(url)
        for attempt in range(1, config["attempts_per_source"] + 1):
            try:
                response = session.get(
                    url,
                    timeout=config["request_timeout_seconds"],
                    headers={"User-Agent": "gonka-host-bot/bridge-burn-v1"},
                )
                response.raise_for_status()
                observation = parse_bridge_observation(response.json())
                observations.append((observation, label))
                if observation["status"] == "BRIDGE_COMPLETED":
                    return observation, label
                break
            except Exception as exc:  # noqa: BLE001 - aggregate public sources
                errors.append(
                    f"{label} attempt {attempt}/{config['attempts_per_source']}: "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
                if attempt < config["attempts_per_source"]:
                    time.sleep(1)

    if not observations:
        raise SourcesUnavailable("all Gonka sources failed: " + " | ".join(errors))

    rank = {"MISSING": 0, "UNKNOWN": 1, "BRIDGE_PENDING": 2, "BRIDGE_COMPLETED": 3}
    best, best_source = max(
        observations,
        key=lambda value: rank.get(value[0]["status"], 1),
    )
    all_validators = {
        validator
        for observation, _label in observations
        for validator in observation["validators"]
    }
    best = dict(best)
    best["validators"] = sorted(all_validators)
    successful_sources = ", ".join(dict.fromkeys(label for _value, label in observations))
    return best, successful_sources or best_source


def remember_completed_transaction(
    state: dict,
    config: dict,
    key: str,
    item: dict,
    observation: dict,
    now: str,
) -> None:
    """Persist signer evidence before a completed queue item is removed."""

    history = state.setdefault("completed_history", [])
    if not isinstance(history, list):
        history = []
        state["completed_history"] = history
    validators = observation.get("completed_validators")
    if not isinstance(validators, list):
        validators = observation.get("validators", [])
    record = {
        "key": key,
        "origin_chain": item.get("origin_chain", config["origin_chain"]),
        "block_number": item["block_number"],
        "receipt_index": item["receipt_index"],
        "transaction_hash": item["transaction_hash"],
        "status": "BRIDGE_COMPLETED",
        "epoch_index": observation.get("epoch_index"),
        "validators": sorted(
            {value for value in validators if isinstance(value, str) and value}
        ),
        "completed_at": now,
    }
    history[:] = [
        value
        for value in history
        if not isinstance(value, dict) or value.get("key") != key
    ]
    history.append(record)

    def history_position(value: Any) -> tuple[int, int]:
        if not isinstance(value, dict):
            return (-1, -1)
        try:
            return (
                int(value.get("block_number", -1)),
                int(value.get("receipt_index", -1)),
            )
        except (TypeError, ValueError):
            return (-1, -1)

    history.sort(
        key=history_position
    )
    del history[:-positive_int(config, "completed_history_limit", minimum=2)]


def age_minutes(item: dict, now: datetime) -> int:
    detected = parse_timestamp(item.get("detected_finalized_at"))
    if detected is None:
        return 0
    return max(0, int((now - detected).total_seconds() // 60))


def source_success(
    state: dict,
    source: str,
    now: str,
    *,
    endpoint: str | None = None,
) -> list[str]:
    previous = state["sources"].get(source, {})
    messages: list[str] = []
    if previous.get("alerted"):
        messages.append(
            "🟢 <b>Источник bridge-мониторинга снова доступен</b>\n\n"
            f"Источник: <code>{escape_html(source)}</code>"
        )
    state["sources"][source] = {
        "status": "available",
        "failed_runs": 0,
        "failed_since": None,
        "alerted": False,
        "last_success_at": now,
        "last_error": None,
        "endpoint": endpoint,
    }
    return messages


def source_failure(
    state: dict,
    config: dict,
    source: str,
    error: Exception,
    now: str,
) -> list[str]:
    previous = state["sources"].get(source, {})
    runs = int(previous.get("failed_runs", 0)) + 1
    alerted = bool(previous.get("alerted"))
    messages: list[str] = []
    if runs >= config["source_unavailable_alert_after_runs"] and not alerted:
        messages.append(
            "🟡 <b>Источник bridge-мониторинга недоступен</b>\n\n"
            f"Источник: <code>{escape_html(source)}</code>\n"
            f"Последовательных неудачных проверок: {runs}\n"
            "Недоступность API не считается застрявшей bridge-транзакцией."
        )
        alerted = True
    state["sources"][source] = {
        "status": "unavailable",
        "failed_runs": runs,
        "failed_since": previous.get("failed_since") or now,
        "alerted": alerted,
        "last_success_at": previous.get("last_success_at"),
        "last_error": str(error)[:2000],
        "endpoint": previous.get("endpoint"),
    }
    return messages


def tx_link(item: dict) -> str:
    tx_hash = item["transaction_hash"]
    label = tx_hash[:10] + "…" + tx_hash[-6:]
    return f'<a href="https://etherscan.io/tx/{escape_html(tx_hash)}">{escape_html(label)}</a>'


def tx_details(item: dict, now: datetime) -> str:
    return (
        f"Ethereum tx: {tx_link(item)}\n"
        f"Block / position: <code>{item['block_number']} / {item['receipt_index']}</code>\n"
        f"Gonka status: <code>{escape_html(item.get('gonka_status', 'UNKNOWN'))}</code>\n"
        f"В очереди: <b>{age_minutes(item, now)} мин.</b>"
    )


def process_queue(state: dict, config: dict, now_text: str) -> list[str]:
    messages: list[str] = []
    now = parse_timestamp(now_text)
    if now is None:
        raise ValueError("now must be an ISO-8601 timestamp")

    attempted = 0
    successful = 0
    errors: list[str] = []
    completed_keys: list[str] = []

    for key, item in list(state["queue"].items()):
        if age_minutes(item, now) < config["initial_check_after_minutes"]:
            continue
        attempted += 1
        item["last_attempted_at"] = now_text
        try:
            observation, source = fetch_bridge_observation(config, item)
            successful += 1
            item.update(
                {
                    "gonka_status": observation["status"],
                    "validators": observation["validators"],
                    "last_checked_at": now_text,
                    "last_error": None,
                    "source": source,
                }
            )
            if observation["status"] == "BRIDGE_COMPLETED":
                remember_completed_transaction(
                    state,
                    config,
                    key,
                    item,
                    observation,
                    now_text,
                )
                if item.get("alert_level") == "warning":
                    messages.append(
                        "🟢 <b>Bridge-транзакция завершена</b>\n\n"
                        + tx_details(item, now)
                    )
                completed_keys.append(key)
            else:
                item["overdue"] = (
                    age_minutes(item, now) >= config["warning_after_minutes"]
                )
        except (SourcesUnavailable, ValueError) as exc:
            item["last_error"] = str(exc)[:2000]
            errors.append(str(exc))

    for key in completed_keys:
        state["queue"].pop(key, None)

    if successful:
        messages.extend(source_success(state, "gonka", now_text))
    elif attempted and errors:
        messages.extend(
            source_failure(
                state,
                config,
                "gonka",
                SourcesUnavailable(" | ".join(errors)),
                now_text,
            )
        )
    return messages


def compact_tx_list(items: list[dict], now: datetime, *, limit: int = 5) -> str:
    lines: list[str] = []
    for item in items[:limit]:
        lines.append(
            f"• {tx_link(item)} — {age_minutes(item, now)} мин., "
            f"<code>{escape_html(item.get('gonka_status', 'UNKNOWN'))}</code>"
        )
    if len(items) > limit:
        lines.append(f"• …и ещё {len(items) - limit}")
    return "\n".join(lines)


def evaluate_alerts(state: dict, config: dict, now_text: str) -> list[str]:
    now = parse_timestamp(now_text)
    if now is None:
        raise ValueError("now must be an ISO-8601 timestamp")
    overdue = sorted(
        [item for item in state["queue"].values() if item.get("overdue")],
        key=lambda item: item.get("detected_finalized_at", ""),
    )
    threshold = config["critical_overdue_transactions"]
    critical_active = bool(state.get("critical_alerted"))
    messages: list[str] = []

    if len(overdue) >= threshold:
        if not critical_active:
            messages.append(
                "🔴 <b>Несколько bridge-транзакций застряли</b>\n\n"
                f"Просрочено транзакций: <b>{len(overdue)}</b>\n"
                f"Порог: <code>{threshold}</code>\n\n"
                + compact_tx_list(overdue, now)
            )
            state["critical_since"] = now_text
        state["critical_alerted"] = True
        for item in overdue:
            item["alert_level"] = "critical"
            item["alerted_at"] = item.get("alerted_at") or now_text
        return messages

    if critical_active:
        state["critical_alerted"] = False
        state["critical_since"] = None
        if overdue:
            messages.append(
                "🟡 <b>Критическое состояние bridge-очереди снято</b>\n\n"
                "Осталась одна просроченная транзакция:\n"
                + compact_tx_list(overdue, now)
            )
            for item in overdue:
                item["alert_level"] = "warning"
        else:
            messages.append(
                "🟢 <b>Bridge-очередь восстановилась</b>\n\n"
                "Просроченных burn-транзакций больше нет."
            )

    for item in overdue:
        if item.get("alert_level") is None:
            messages.append(
                "🟡 <b>Bridge-транзакция не завершена вовремя</b>\n\n"
                + tx_details(item, now)
            )
            item["alert_level"] = "warning"
            item["alerted_at"] = now_text
    return messages


def build_summary(config: dict, state: dict) -> str:
    queued = list(state["queue"].values())
    overdue = [item for item in queued if item.get("overdue")]
    return (
        "🟢 <b>Проверка WGNK burn bridge</b>\n\n"
        f"Ethereum finalized block: <code>{escape_html(state.get('ethereum_finalized_block'))}</code>\n"
        f"Последний просканированный блок: <code>{escape_html(state.get('last_scanned_finalized_block'))}</code>\n"
        f"В очереди: <b>{len(queued)}</b>\n"
        f"Просрочено: <b>{len(overdue)}</b>\n"
        f"Контракт: <code>{escape_html(config['wgnk_contract'])}</code>"
    )


def run() -> dict:
    config = load_config()
    state = load_state()
    state["owner"] = config["owner"]
    now = utc_now()
    state["checked_at"] = now
    messages: list[str] = []
    added = 0

    try:
        previous_cursor = state.get("last_scanned_finalized_block")
        minimum_block = (
            previous_cursor
            if isinstance(previous_cursor, int) and not isinstance(previous_cursor, bool)
            else None
        )
        finalized, ethereum_source = fetch_finalized_block(
            config,
            minimum_block=minimum_block,
        )
        if isinstance(previous_cursor, int) and not isinstance(previous_cursor, bool):
            from_block = previous_cursor + 1
        else:
            from_block = max(0, finalized - config["bootstrap_lookback_blocks"] + 1)
        logs = fetch_burn_logs(config, from_block, finalized)
        added = add_burn_logs(state, config, logs, now)
        state["ethereum_finalized_block"] = finalized
        state["last_scanned_finalized_block"] = finalized
        messages.extend(
            source_success(
                state,
                "ethereum",
                now,
                endpoint=ethereum_source,
            )
        )
    except (SourcesUnavailable, ValueError) as exc:
        messages.extend(source_failure(state, config, "ethereum", exc, now))

    messages.extend(process_queue(state, config, now))
    messages.extend(evaluate_alerts(state, config, now))

    if os.environ.get("SEND_BRIDGE_SUMMARY", "").lower() in {"1", "true", "yes"}:
        messages.append(build_summary(config, state))

    for message in messages:
        send_telegram_message(message)
    save_state(state)

    print(
        json.dumps(
            {
                "finalized_block": state.get("ethereum_finalized_block"),
                "new_burns": added,
                "queued": len(state["queue"]),
                "overdue": sum(
                    1 for item in state["queue"].values() if item.get("overdue")
                ),
                "critical": bool(state.get("critical_alerted")),
                "ethereum_source": state["sources"].get("ethereum", {}).get("status"),
                "ethereum_endpoint": state["sources"].get("ethereum", {}).get("endpoint"),
                "gonka_source": state["sources"].get("gonka", {}).get("status"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return state


if __name__ == "__main__":
    run()
