"""Monitor operator nodes and authoritative Confirmation PoC rates."""

from __future__ import annotations

import ipaddress
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from bot_common import (
    escape_html,
    fetch_json_with_fallback,
    format_integer,
    load_json,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "our_nodes.json"
STATE_FILE = ROOT / "state" / "our_nodes_state.json"
RATIO_METRIC_VERSION = "confirmation_weight_over_weight_v2"


def validate_public_http_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid endpoint URL: {raw_url!r}")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError(f"private or loopback endpoint is not allowed: {raw_url!r}")
    return parsed.geturl().rstrip("/")


def load_config(path: str | Path | None = None) -> dict:
    config = load_json(path or CONFIG_FILE)
    if not isinstance(config, dict):
        raise ValueError("our-nodes config must be a JSON object")
    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("config must contain a non-empty nodes list")
    for node in nodes:
        for field in ("name", "endpoint"):
            if not isinstance(node.get(field), str) or not node[field].strip():
                raise ValueError(f"node is missing required field: {field}")
        if node.get("participant_required", True):
            address = node.get("participant_address")
            if not isinstance(address, str) or not address.strip():
                raise ValueError("participant-required node is missing participant_address")
        validate_public_http_url(node["endpoint"])
    for field in ("participants_urls", "epoch_group_data_urls"):
        urls = config.get(field)
        if not isinstance(urls, list) or not urls:
            raise ValueError(f"{field} must be a non-empty list")
        for url in urls:
            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"{field} contains an invalid URL")
            validate_public_http_url(url)
    return config


def positive_epoch(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if epoch < 1:
        raise ValueError(f"{field} must be positive")
    return epoch


def participant_address(entry: Any) -> str:
    if not isinstance(entry, dict):
        raise ValueError("participant entry must be an object")
    for field in ("index", "participant_id", "address"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("participant address is missing")


def parse_participants_payload(payload: Any) -> dict:
    active = payload.get("active_participants") if isinstance(payload, dict) else None
    if not isinstance(active, dict):
        raise ValueError("active_participants is missing")
    epoch = positive_epoch(
        active.get("epoch_group_id"),
        "active_participants.epoch_group_id",
    )
    entries = active.get("participants")
    if not isinstance(entries, list) or not entries:
        raise ValueError("active_participants.participants must be a non-empty list")
    by_address: dict[str, dict] = {}
    for entry in entries:
        address = participant_address(entry)
        if address in by_address:
            raise ValueError(f"duplicate participant address: {address}")
        by_address[address] = entry
    return {"epoch": epoch, "entries": entries, "by_address": by_address}


def fetch_participants_snapshot(urls: list[str], timeout: int) -> dict:
    payload, source = fetch_json_with_fallback(
        urls,
        timeout=timeout,
        attempts=1,
        validator=lambda value: parse_participants_payload(value),
    )
    snapshot = parse_participants_payload(payload)
    snapshot["source"] = source
    print(f"Participants source: {source}")
    return snapshot


def nonnegative_integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    minimum = 1 if positive else 0
    if parsed < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be {qualifier}")
    return parsed


def parse_epoch_group_payload(payload: Any) -> dict:
    group = payload.get("epoch_group_data") if isinstance(payload, dict) else None
    if not isinstance(group, dict):
        raise ValueError("epoch_group_data is missing")
    epoch = positive_epoch(group.get("epoch_index"), "epoch_group_data.epoch_index")
    values = group.get("validation_weights")
    if not isinstance(values, list) or not values:
        raise ValueError("epoch_group_data.validation_weights must be a non-empty list")
    by_address: dict[str, dict] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("validation_weights entry must be an object")
        address = item.get("member_address")
        if not isinstance(address, str) or not address.strip():
            raise ValueError("validation_weights.member_address is missing")
        address = address.strip()
        if address in by_address:
            raise ValueError(f"duplicate validation weight address: {address}")
        weight = nonnegative_integer(item.get("weight"), "weight", positive=True)
        confirmation_weight = nonnegative_integer(
            item.get("confirmation_weight"),
            "confirmation_weight",
        )
        if confirmation_weight > weight:
            raise ValueError("confirmation_weight cannot exceed weight")
        by_address[address] = {
            "weight": weight,
            "confirmation_weight": confirmation_weight,
            "rate": confirmation_weight / weight * 100,
        }
    return {"epoch": epoch, "by_address": by_address}


def fetch_epoch_group_snapshot(urls: list[str], timeout: int) -> dict:
    payload, source = fetch_json_with_fallback(
        urls,
        timeout=timeout,
        attempts=1,
        validator=lambda value: parse_epoch_group_payload(value),
    )
    snapshot = parse_epoch_group_payload(payload)
    snapshot["source"] = source
    print(f"Confirmation PoC source: {source}")
    return snapshot


def request_error_category(exc: Exception) -> str:
    message = str(exc).lower()
    type_name = type(exc).__name__.lower()
    if "timeout" in type_name or "timed out" in message or "timeout" in message:
        return "timeout"
    for pattern in (
        r"\bhttp(?: status)?[ :]+(4\d\d|5\d\d)\b",
        r"\b(4\d\d|5\d\d) server error\b",
        r"status(?:_code)?[ =:]+(4\d\d|5\d\d)\b",
    ):
        status = re.search(pattern, message)
        if status:
            return f"HTTP {status.group(1)}"
    if "connection refused" in message:
        return "connection refused"
    return "invalid response"


def confirmation_metric(
    participant_epoch: int,
    participant_address_value: str,
    group_snapshot: dict | None,
    *,
    group_error: Exception | None = None,
) -> dict:
    if group_error is not None:
        return {
            "available": False,
            "epoch": participant_epoch,
            "reason": request_error_category(group_error),
            "error": f"{type(group_error).__name__}: {group_error}",
            "source": None,
        }
    if group_snapshot is None:
        return {
            "available": False,
            "epoch": participant_epoch,
            "reason": "invalid response",
            "error": "group snapshot is missing",
            "source": None,
        }
    if group_snapshot["epoch"] != participant_epoch:
        return {
            "available": False,
            "epoch": participant_epoch,
            "reason": "epoch mismatch",
            "error": (
                f"participants epoch {participant_epoch} does not match "
                f"group-data epoch {group_snapshot['epoch']}"
            ),
            "source": group_snapshot.get("source"),
        }
    value = group_snapshot["by_address"].get(participant_address_value)
    if value is None:
        return {
            "available": False,
            "epoch": participant_epoch,
            "reason": "participant not found",
            "error": f"participant {participant_address_value} is absent in validation_weights",
            "source": group_snapshot.get("source"),
        }
    return {
        "available": True,
        "epoch": participant_epoch,
        "reason": None,
        "error": None,
        "source": group_snapshot.get("source"),
        **value,
    }


def check_endpoint(
    node: dict,
    health_path: str,
    timeout: int,
    retries: int,
    delay: int,
) -> dict:
    endpoint = validate_public_http_url(node["endpoint"])
    url = endpoint + "/" + health_path.lstrip("/")
    last_error = None
    for attempt in range(1, retries + 1):
        started = time.monotonic()
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "gonka-our-nodes-monitor/2.0"},
            )
            response.raise_for_status()
            response.json()
            return {
                "ok": True,
                "http_status": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "attempt": attempt,
            }
        except Exception as exc:  # noqa: BLE001 - retry operator endpoint
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(delay)
    return {
        "ok": False,
        "error": last_error or "unknown endpoint error",
        "reason": request_error_category(RuntimeError(last_error or "invalid response")),
        "attempts": retries,
    }


def inspect_node(node: dict, participants: dict[str, dict], config: dict) -> dict:
    entry = participants.get(node["participant_address"])
    endpoint = check_endpoint(
        node,
        config.get("health_path", "/v1/versions"),
        int(config.get("request_timeout_seconds", 10)),
        int(config.get("health_retries", 3)),
        int(config.get("retry_delay_seconds", 2)),
    )
    if entry is None and node.get("participant_required", True):
        return {
            "ok": False,
            "reason": "participant_absent",
            "details": "participant not found",
            "technical_error": None,
            "endpoint": endpoint,
        }
    if not endpoint["ok"]:
        return {
            "ok": False,
            "reason": "endpoint_unhealthy",
            "details": endpoint.get("reason", "invalid response"),
            "technical_error": endpoint.get("error"),
            "endpoint": endpoint,
        }
    return {
        "ok": True,
        "reason": "ok" if entry is not None else "participant_check_skipped",
        "details": f"HTTP {endpoint['http_status']} in {endpoint['latency_ms']} ms",
        "technical_error": None,
        "endpoint": endpoint,
    }


def load_state(path: str | Path | None = None) -> dict:
    state = load_json(path or STATE_FILE, {"nodes": {}})
    if not isinstance(state, dict):
        raise ValueError("our-nodes state must be a JSON object")
    if not isinstance(state.get("nodes"), dict):
        state["nodes"] = {}
    return state


def save_state(state: dict, path: str | Path | None = None) -> None:
    save_json_atomic(path or STATE_FILE, state, sort_keys=True)


def build_node_alert(node: dict, result: dict, now: str) -> str:
    return (
        "🔴 <b>Нода недоступна</b>\n\n"
        f"Имя: <code>{escape_html(node['name'])}</code>\n"
        f"Адрес: <code>{escape_html(node['participant_address'])}</code>\n"
        f"Роль: {escape_html(node.get('role', 'unknown'))}\n"
        f"Причина: <code>{escape_html(result['details'])}</code>\n"
        f"Проверка: {escape_html(now)}"
    )


def build_node_recovery(node: dict, previous: dict, result: dict, now: str) -> str:
    return (
        "🟢 <b>Нода восстановлена</b>\n\n"
        f"Имя: <code>{escape_html(node['name'])}</code>\n"
        f"Адрес: <code>{escape_html(node['participant_address'])}</code>\n"
        f"Недоступность с: {escape_html(previous.get('first_failed_at', 'unknown'))}\n"
        f"Восстановлена: {escape_html(now)}\n"
        f"Проверка endpoint: {escape_html(result.get('details', 'ok'))}"
    )


def build_ratio_alert(node: dict, metric: dict, threshold: float) -> str:
    return (
        "⚠️ <b>Confirmation PoC rate ниже порога</b>\n\n"
        f"Нода: <code>{escape_html(node['name'])}</code>\n"
        f"Эпоха: {metric['epoch']}\n"
        f"Confirmation weight: <b>{format_integer(metric['confirmation_weight'])}</b>\n"
        f"Weight: <b>{format_integer(metric['weight'])}</b>\n"
        f"Rate: <b>{metric['rate']:.1f}%</b>\n"
        f"Порог: <b>{threshold:.1f}%</b>"
    )


def build_ratio_recovery(node: dict, metric: dict) -> str:
    return (
        "🟢 <b>Confirmation PoC rate восстановился</b>\n\n"
        f"Нода: <code>{escape_html(node['name'])}</code>\n"
        f"Эпоха: {metric['epoch']}\n"
        f"Confirmation weight: <b>{format_integer(metric['confirmation_weight'])}</b>\n"
        f"Weight: <b>{format_integer(metric['weight'])}</b>\n"
        f"Rate: <b>{metric['rate']:.1f}%</b>"
    )


def build_metric_unavailable(node: dict, metric: dict) -> str:
    return (
        "🟡 <b>Confirmation PoC rate недоступен</b>\n\n"
        f"Нода: <code>{escape_html(node['name'])}</code>\n"
        f"Эпоха: {metric['epoch']}\n"
        f"Причина: <code>{escape_html(metric['reason'])}</code>"
    )


def build_metric_recovery(node: dict, metric: dict) -> str:
    return (
        "🟢 <b>Confirmation PoC rate снова доступен</b>\n\n"
        f"Нода: <code>{escape_html(node['name'])}</code>\n"
        f"Эпоха: {metric['epoch']}\n"
        f"Rate: <b>{metric['rate']:.1f}%</b>"
    )


def evaluate_confirmation(
    previous: dict,
    metric: dict,
    *,
    enabled: bool,
    alert_threshold: float,
    recovery_threshold: float,
    alert_after_runs: int,
    unavailable_after_runs: int,
) -> tuple[dict, list[str]]:
    if not enabled:
        return {
            "ratio_epoch": metric.get("epoch"),
            "ratio_low_runs": 0,
            "weight_ratio_alerted": False,
            "ratio_missing_runs": 0,
            "ratio_unavailable_alerted": False,
        }, []

    same_version = previous.get("ratio_metric_version") == RATIO_METRIC_VERSION
    previous_epoch = previous.get("ratio_epoch") if same_version else None
    low_runs = int(previous.get("ratio_low_runs", 0) or 0) if same_version else 0
    ratio_alerted = bool(previous.get("weight_ratio_alerted", False)) if same_version else False
    # Availability is continuous across metric-version migrations: if users
    # already saw an unavailable alert, the first valid v2 value should close
    # it. Low-rate counters are reset because the old metric was not comparable.
    missing_runs = int(previous.get("ratio_missing_runs", 0) or 0)
    unavailable_alerted = bool(previous.get("ratio_unavailable_alerted", False))
    events: list[str] = []

    if not metric["available"]:
        if metric.get("epoch") != previous_epoch:
            low_runs = 0
        missing_runs += 1
        if missing_runs >= unavailable_after_runs and not unavailable_alerted:
            unavailable_alerted = True
            events.append("unavailable")
        return {
            "ratio_epoch": metric.get("epoch"),
            "ratio_low_runs": low_runs,
            "weight_ratio_alerted": ratio_alerted,
            "ratio_missing_runs": missing_runs,
            "ratio_unavailable_alerted": unavailable_alerted,
        }, events

    if unavailable_alerted:
        events.append("available")
    missing_runs = 0
    unavailable_alerted = False
    if metric["epoch"] != previous_epoch:
        low_runs = 0

    if metric["rate"] < alert_threshold:
        low_runs += 1
        if low_runs >= alert_after_runs and not ratio_alerted:
            ratio_alerted = True
            events.append("low")
    else:
        low_runs = 0
        if ratio_alerted and metric["rate"] >= recovery_threshold:
            ratio_alerted = False
            events.append("recovered")

    return {
        "ratio_epoch": metric["epoch"],
        "ratio_low_runs": low_runs,
        "weight_ratio_alerted": ratio_alerted,
        "ratio_missing_runs": missing_runs,
        "ratio_unavailable_alerted": unavailable_alerted,
    }, events


def main() -> None:
    config = load_config()
    timeout = int(config.get("request_timeout_seconds", 10))
    participant_snapshot = fetch_participants_snapshot(
        config["participants_urls"],
        timeout,
    )
    try:
        group_snapshot = fetch_epoch_group_snapshot(
            config["epoch_group_data_urls"],
            timeout,
        )
        group_error = None
    except Exception as exc:  # noqa: BLE001 - endpoint health still runs
        group_snapshot = None
        group_error = exc
        print(
            f"WARNING: Confirmation PoC group data unavailable: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    alert_threshold = float(config.get("weight_ratio_alert_below_percent", 30.0))
    recovery_threshold = float(
        config.get("weight_ratio_recovery_above_percent", alert_threshold)
    )
    ratio_alert_after = int(config.get("ratio_alert_after_runs", 2))
    unavailable_after = int(config.get("metric_unavailable_alert_after_runs", 2))
    if not 0 <= alert_threshold <= 100:
        raise ValueError("ratio alert threshold must be between 0 and 100")
    if not alert_threshold <= recovery_threshold <= 100:
        raise ValueError("ratio recovery threshold must be between alert threshold and 100")
    if ratio_alert_after < 1 or unavailable_after < 1:
        raise ValueError("ratio confirmation counters must be positive")

    state = load_state()
    now = utc_now()
    alerts: list[str] = []
    participants = participant_snapshot["by_address"]
    for node in config["nodes"]:
        node_id = node["name"]
        previous = state["nodes"].get(node_id, {"status": "unknown"})
        result = inspect_node(node, participants, config)
        current_status = "up" if result["ok"] else "down"
        previous_status = previous.get("status", "unknown")
        if current_status == "down" and previous_status != "down":
            alerts.append(build_node_alert(node, result, now))
        elif current_status == "up" and previous_status == "down":
            alerts.append(build_node_recovery(node, previous, result, now))

        enabled = bool(
            node.get("ratio_monitoring_enabled", node.get("participant_required", True))
        )
        metric = confirmation_metric(
            participant_snapshot["epoch"],
            node.get("participant_address", ""),
            group_snapshot,
            group_error=group_error,
        )
        metric_state, metric_events = evaluate_confirmation(
            previous,
            metric,
            enabled=enabled,
            alert_threshold=alert_threshold,
            recovery_threshold=recovery_threshold,
            alert_after_runs=ratio_alert_after,
            unavailable_after_runs=unavailable_after,
        )
        for event in metric_events:
            if event == "unavailable":
                alerts.append(build_metric_unavailable(node, metric))
            elif event == "available":
                alerts.append(build_metric_recovery(node, metric))
            elif event == "low":
                alerts.append(build_ratio_alert(node, metric, alert_threshold))
            elif event == "recovered":
                alerts.append(build_ratio_recovery(node, metric))

        state["nodes"][node_id] = {
            "status": current_status,
            "last_checked_at": now,
            "first_failed_at": (
                previous.get("first_failed_at", now)
                if current_status == "down"
                else None
            ),
            "participant_present": node.get("participant_address") in participants,
            "reason": result.get("reason"),
            "details": result.get("details"),
            "technical_error": result.get("technical_error"),
            "ratio_metric_version": RATIO_METRIC_VERSION,
            **metric_state,
            "epoch": metric.get("epoch"),
            "weight": metric.get("weight"),
            "confirmation_weight": metric.get("confirmation_weight"),
            "weight_ratio": metric.get("rate"),
            "ratio_source": metric.get("source"),
            "ratio_checked_at": now,
            "ratio_unavailable_reason": metric.get("reason"),
            "ratio_error": metric.get("error"),
        }
        print(
            f"{node_id}: {current_status}; endpoint={result.get('reason')}; "
            f"confirmation_rate={metric.get('rate')}; "
            f"metric_reason={metric.get('reason')}; metric_error={metric.get('error')}"
        )

    for message in alerts:
        send_telegram_message(message)
    print(
        f"Sent {len(alerts)} Telegram alert(s)."
        if alerts
        else "No state changes; no Telegram messages sent."
    )
    state["checked_at"] = now
    state["participant_count"] = len(participants)
    state["epoch"] = participant_snapshot["epoch"]
    state["participants_source"] = participant_snapshot.get("source")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - command-line boundary
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
