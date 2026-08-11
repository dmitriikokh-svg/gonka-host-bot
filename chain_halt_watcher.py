"""Detect a Gonka chain halt using a quorum of independent Tendermint RPCs.

The same single-check business logic is used by GitHub Actions (``once``) and
by the long-running server process (``daemon``). An unavailable, catching-up,
misconfigured, or malformed source never proves a chain halt by itself.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from bot_common import (
    escape_html,
    load_json,
    save_json_atomic,
    send_telegram_message,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "chain_halt.json"
STATE_FILE = ROOT / "state" / "chain_halt.json"
VALID_STATUSES = {"healthy", "halted", "monitoring_unavailable"}


def positive_int(config: dict, field: str, *, minimum: int = 1) -> int:
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def validate_http_url(value: str, field: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid URL in {field}: {value!r}")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid port in {field}: {value!r}") from exc


def validate_config(config: Any) -> dict:
    if not isinstance(config, dict):
        raise ValueError("chain halt config must be a JSON object")

    expected_chain_id = config.get("expected_chain_id")
    if not isinstance(expected_chain_id, str) or not expected_chain_id.strip():
        raise ValueError("expected_chain_id is required")

    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    names: set[str] = set()
    urls: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ValueError(f"sources[{index}] must be a JSON object")
        name = source.get("name")
        url = source.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"sources[{index}].name is required")
        if not isinstance(url, str):
            raise ValueError(f"sources[{index}].url is required")
        validate_http_url(url, f"sources[{index}].url")
        if name in names:
            raise ValueError(f"duplicate source name: {name}")
        if url in urls:
            raise ValueError(f"duplicate source URL: {url}")
        names.add(name)
        urls.add(url)

    for field, minimum in (
        ("minimum_confirming_sources", 2),
        ("halt_after_seconds", 1),
        ("maximum_height_spread", 0),
        ("recovery_confirmations", 1),
        ("unavailable_alert_after_runs", 1),
        ("reminder_interval_minutes", 1),
        ("request_timeout_seconds", 1),
        ("attempts_per_source", 1),
        ("poll_interval_seconds", 1),
        ("maximum_future_skew_seconds", 0),
    ):
        positive_int(config, field, minimum=minimum)

    if config["minimum_confirming_sources"] > len(sources):
        raise ValueError("minimum_confirming_sources cannot exceed source count")
    return config


def load_config(path: str | Path | None = None) -> dict:
    return validate_config(load_json(path or CONFIG_FILE))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def source_base(source: dict) -> dict:
    return {
        "name": source["name"],
        "url": source["url"],
    }


def unavailable_observation(source: dict, error: str, **details: Any) -> dict:
    result = {
        **source_base(source),
        "status": "unavailable",
        "chain_id": None,
        "height": None,
        "block_time": None,
        "block_age_seconds": None,
        "catching_up": None,
        "error": str(error)[:500],
    }
    result.update(details)
    return result


def parse_status_payload(
    payload: Any,
    source: dict,
    config: dict,
    now: datetime,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("status response must be a JSON object")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("result is missing")
    node_info = result.get("node_info")
    sync_info = result.get("sync_info")
    if not isinstance(node_info, dict) or not isinstance(sync_info, dict):
        raise ValueError("node_info or sync_info is missing")

    chain_id = node_info.get("network")
    if not isinstance(chain_id, str) or not chain_id:
        raise ValueError("node_info.network is missing")

    raw_height = sync_info.get("latest_block_height")
    if isinstance(raw_height, bool) or raw_height is None:
        raise ValueError("latest_block_height is missing")
    try:
        height = int(raw_height)
    except (TypeError, ValueError) as exc:
        raise ValueError("latest_block_height is invalid") from exc
    if height < 1:
        raise ValueError("latest_block_height must be positive")

    block_time = parse_utc_timestamp(
        sync_info.get("latest_block_time"),
        "latest_block_time",
    )
    catching_up = sync_info.get("catching_up")
    if not isinstance(catching_up, bool):
        raise ValueError("catching_up must be a boolean")

    age = (now - block_time).total_seconds()
    maximum_future_skew = positive_int(
        config,
        "maximum_future_skew_seconds",
        minimum=0,
    )
    details = {
        "chain_id": chain_id,
        "height": height,
        "block_time": iso_utc(block_time),
        "block_age_seconds": round(max(0.0, age), 3),
        "catching_up": catching_up,
    }
    if chain_id != config["expected_chain_id"]:
        return unavailable_observation(
            source,
            f"unexpected chain ID: {chain_id}",
            **details,
        )
    if age < -maximum_future_skew:
        return unavailable_observation(
            source,
            f"latest block time is {-age:.3f}s in the future",
            **details,
        )
    if catching_up:
        return unavailable_observation(
            source,
            "catching_up=true",
            **details,
        )
    return {
        **source_base(source),
        "status": "available",
        **details,
        "error": None,
    }


def fetch_source(
    source: dict,
    config: dict,
    now: datetime,
    *,
    session=requests,
) -> dict:
    errors: list[str] = []
    attempts = config["attempts_per_source"]
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(
                source["url"],
                timeout=config["request_timeout_seconds"],
                headers={"User-Agent": "gonka-host-bot/chain-halt-v1"},
            )
            response.raise_for_status()
            observation = parse_status_payload(response.json(), source, config, now)
            observation["attempts"] = attempt
            return observation
        except Exception as exc:  # noqa: BLE001 - source failures are isolated
            errors.append(f"attempt {attempt}/{attempts}: {type(exc).__name__}: {exc}")
    result = unavailable_observation(source, " | ".join(errors))
    result["attempts"] = attempts
    return result


def check_sources(
    config: dict,
    now: datetime,
    *,
    fetcher: Callable[[dict, dict, datetime], dict] = fetch_source,
) -> list[dict]:
    sources = config["sources"]
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = {
            executor.submit(fetcher, source, config, now): source for source in sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                observation = future.result()
                if not isinstance(observation, dict):
                    raise TypeError("source checker returned a non-object")
            except Exception as exc:  # pragma: no cover - defensive worker guard
                observation = unavailable_observation(
                    source,
                    f"worker failure: {type(exc).__name__}: {exc}",
                )
            results[source["name"]] = observation
    return [results[source["name"]] for source in sources]


def assess_network(observations: list[dict], config: dict) -> dict:
    available = [item for item in observations if item.get("status") == "available"]
    minimum = config["minimum_confirming_sources"]
    assessment = {
        "result": "insufficient",
        "reason": "not_enough_sources",
        "confirming_sources": len(available),
        "total_sources": len(observations),
        "fresh_sources": 0,
        "height_spread": None,
        "latest_height": None,
        "latest_block_time": None,
        "latest_block_age_seconds": None,
    }
    if available:
        heights = [int(item["height"]) for item in available]
        latest = max(available, key=lambda item: item["block_time"])
        assessment.update(
            {
                "height_spread": max(heights) - min(heights),
                "latest_height": max(heights),
                "latest_block_time": latest["block_time"],
                "latest_block_age_seconds": min(
                    float(item["block_age_seconds"]) for item in available
                ),
            }
        )
    if len(available) < minimum:
        return assessment

    fresh = [
        item
        for item in available
        if float(item["block_age_seconds"]) <= config["halt_after_seconds"]
    ]
    assessment["fresh_sources"] = len(fresh)
    if fresh:
        assessment["result"] = "healthy"
        assessment["reason"] = "fresh_block_observed"
        return assessment
    if assessment["height_spread"] > config["maximum_height_spread"]:
        assessment["reason"] = "height_spread_too_large"
        return assessment

    assessment["result"] = "halted"
    assessment["reason"] = "old_blocks_with_consistent_heights"
    return assessment


def default_state() -> dict:
    return {
        "version": 1,
        "checked_at": None,
        "status": "healthy",
        "sources": {},
        "assessment": {},
        "halt": {
            "active": False,
            "first_detected_at": None,
            "last_alert_at": None,
            "last_reminder_at": None,
            "last_recovered_at": None,
            "recovery_runs": 0,
        },
        "monitoring": {
            "alerted": False,
            "unavailable_runs": 0,
            "first_unavailable_at": None,
            "last_alert_at": None,
            "last_recovered_at": None,
        },
    }


def load_state(path: str | Path | None = None) -> dict:
    value = load_json(path or STATE_FILE, default_state())
    if not isinstance(value, dict):
        raise ValueError("chain halt state must be a JSON object")
    state = default_state()
    state.update(value)
    for field in ("sources", "assessment"):
        if not isinstance(state.get(field), dict):
            state[field] = {}
    for field in ("halt", "monitoring"):
        current = value.get(field, {})
        if not isinstance(current, dict):
            current = {}
        state[field] = {**state[field], **current}
    if state.get("status") not in VALID_STATUSES:
        state["status"] = "monitoring_unavailable"
    return state


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def parse_saved_time(value: Any) -> datetime | None:
    try:
        return parse_utc_timestamp(value, "state timestamp")
    except ValueError:
        return None


def source_lines(observations: list[dict]) -> str:
    lines = []
    for item in observations:
        name = escape_html(item.get("name", "unknown"))
        if item.get("status") == "available":
            lines.append(
                f"• <code>{name}</code>: height <code>{escape_html(item['height'])}</code>, "
                f"block age <b>{escape_html(format_duration(item['block_age_seconds']))}</b>"
            )
        else:
            lines.append(
                f"• <code>{name}</code>: <b>unavailable</b> "
                f"(<code>{escape_html(item.get('error', 'unknown error'))}</code>)"
            )
    return "\n".join(lines)


def build_halt_message(
    assessment: dict,
    observations: list[dict],
    config: dict,
    *,
    reminder: bool = False,
) -> str:
    title = "Gonka chain halt продолжается" if reminder else "Gonka chain halt"
    return (
        f"🔴 <b>{title}</b>\n\n"
        f"Новые блоки не создаются более {config['halt_after_seconds']} секунд.\n"
        f"Последняя подтверждённая высота: "
        f"<code>{escape_html(assessment['latest_height'])}</code>\n"
        f"Время последнего блока: "
        f"<code>{escape_html(assessment['latest_block_time'])}</code> UTC\n"
        f"Возраст последнего блока: "
        f"<b>{escape_html(format_duration(assessment['latest_block_age_seconds']))}</b>\n"
        f"Подтвердившие источники: "
        f"<b>{assessment['confirming_sources']}/{assessment['total_sources']}</b>\n\n"
        f"Источники:\n{source_lines(observations)}"
    )


def build_halt_recovery_message(
    state: dict,
    assessment: dict,
    now: datetime,
) -> str:
    first_detected = parse_saved_time(state["halt"].get("first_detected_at"))
    duration = (now - first_detected).total_seconds() if first_detected else None
    return (
        "🟢 <b>Gonka chain восстановлена</b>\n\n"
        "Создание блоков возобновилось.\n"
        f"Текущая высота: <code>{escape_html(assessment['latest_height'])}</code>\n"
        f"Время простоя (с момента обнаружения): "
        f"<b>{escape_html(format_duration(duration))}</b>\n"
        f"Подтвердившие источники: "
        f"<b>{assessment['confirming_sources']}/{assessment['total_sources']}</b>"
    )


def build_monitoring_unavailable_message(
    assessment: dict,
    observations: list[dict],
    config: dict,
) -> str:
    return (
        "🟡 <b>Chain halt monitor не может подтвердить состояние сети</b>\n\n"
        f"Корректных источников: "
        f"<b>{assessment['confirming_sources']}/{assessment['total_sources']}</b>; "
        f"для halt-кворума требуется минимум "
        f"<b>{config['minimum_confirming_sources']}</b> согласованных.\n"
        f"Причина оценки: <code>{escape_html(assessment['reason'])}</code>.\n\n"
        "Это потеря наблюдаемости, а не подтверждённый chain halt.\n\n"
        f"Источники:\n{source_lines(observations)}"
    )


def build_monitoring_recovery_message(assessment: dict) -> str:
    return (
        "🟢 <b>Наблюдаемость chain halt monitor восстановлена</b>\n\n"
        f"Снова доступно достаточно источников: "
        f"<b>{assessment['confirming_sources']}/{assessment['total_sources']}</b>."
    )


def reminder_due(halt_state: dict, config: dict, now: datetime) -> bool:
    previous = parse_saved_time(
        halt_state.get("last_reminder_at") or halt_state.get("last_alert_at")
    )
    if previous is None:
        return True
    interval = config["reminder_interval_minutes"] * 60
    return (now - previous).total_seconds() >= interval


def evaluate_check(
    previous_state: dict,
    config: dict,
    observations: list[dict],
    now: datetime,
) -> tuple[dict, list[str]]:
    state = copy.deepcopy(previous_state)
    assessment = assess_network(observations, config)
    now_text = iso_utc(now)
    state["checked_at"] = now_text
    state["sources"] = {item["name"]: copy.deepcopy(item) for item in observations}
    state["assessment"] = assessment
    halt_state = state["halt"]
    monitoring = state["monitoring"]
    messages: list[str] = []

    sufficient = assessment["result"] != "insufficient"
    if sufficient:
        if monitoring.get("alerted"):
            messages.append(build_monitoring_recovery_message(assessment))
            monitoring["last_recovered_at"] = now_text
        monitoring["alerted"] = False
        monitoring["unavailable_runs"] = 0
        monitoring["first_unavailable_at"] = None
    else:
        runs = int(monitoring.get("unavailable_runs", 0) or 0) + 1
        monitoring["unavailable_runs"] = runs
        monitoring["first_unavailable_at"] = (
            monitoring.get("first_unavailable_at") or now_text
        )
        halt_state["recovery_runs"] = 0
        if runs >= config["unavailable_alert_after_runs"] and not monitoring.get(
            "alerted"
        ):
            messages.append(
                build_monitoring_unavailable_message(assessment, observations, config)
            )
            monitoring["alerted"] = True
            monitoring["last_alert_at"] = now_text

    if assessment["result"] == "halted":
        halt_state["recovery_runs"] = 0
        state["status"] = "halted"
        if not halt_state.get("active"):
            halt_state.update(
                {
                    "active": True,
                    "first_detected_at": now_text,
                    "last_alert_at": now_text,
                    "last_reminder_at": now_text,
                    "last_recovered_at": None,
                }
            )
            messages.append(build_halt_message(assessment, observations, config))
        elif reminder_due(halt_state, config, now):
            messages.append(
                build_halt_message(
                    assessment,
                    observations,
                    config,
                    reminder=True,
                )
            )
            halt_state["last_reminder_at"] = now_text
    elif assessment["result"] == "healthy":
        if halt_state.get("active"):
            recovery_runs = int(halt_state.get("recovery_runs", 0) or 0) + 1
            halt_state["recovery_runs"] = recovery_runs
            if recovery_runs >= config["recovery_confirmations"]:
                messages.append(build_halt_recovery_message(state, assessment, now))
                halt_state["active"] = False
                halt_state["last_recovered_at"] = now_text
                halt_state["recovery_runs"] = 0
                state["status"] = "healthy"
            else:
                state["status"] = "halted"
        else:
            halt_state["recovery_runs"] = 0
            state["status"] = "healthy"
    else:
        state["status"] = "monitoring_unavailable"

    return state, messages


def run_once(
    config: dict,
    *,
    now: datetime | None = None,
    checker: Callable[[dict, datetime], list[dict]] = check_sources,
    sender: Callable[[str], None] = send_telegram_message,
    state_file: str | Path | None = None,
) -> dict:
    check_time = now or utc_now()
    state_path = Path(state_file or STATE_FILE)
    state = load_state(state_path)
    observations = checker(config, check_time)
    next_state, messages = evaluate_check(state, config, observations, check_time)
    for message in messages:
        sender(message)
    save_json_atomic(state_path, next_state, sort_keys=True)
    print(
        json.dumps(
            {
                "checked_at": next_state["checked_at"],
                "status": next_state["status"],
                "assessment": next_state["assessment"].get("result"),
                "confirming_sources": next_state["assessment"].get(
                    "confirming_sources"
                ),
                "messages": len(messages),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return next_state


def runtime_settings(config: dict) -> tuple[str, int]:
    mode = os.environ.get("CHAIN_MONITOR_MODE", "once").strip().lower()
    if mode not in {"once", "daemon"}:
        raise ValueError("CHAIN_MONITOR_MODE must be once or daemon")
    raw_interval = os.environ.get("CHAIN_POLL_INTERVAL_SECONDS")
    if raw_interval is None or not raw_interval.strip():
        interval = config["poll_interval_seconds"]
    else:
        try:
            interval = int(raw_interval)
        except ValueError as exc:
            raise ValueError("CHAIN_POLL_INTERVAL_SECONDS must be an integer") from exc
        if interval < 1:
            raise ValueError("CHAIN_POLL_INTERVAL_SECONDS must be positive")
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
        except Exception as exc:  # noqa: BLE001 - daemon must survive one iteration
            print(
                f"ERROR: chain halt iteration failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if stop_event.is_set():
            break
        elapsed = max(0.0, monotonic() - started)
        stop_event.wait(max(0.0, interval - elapsed))


def install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum, _frame):
        print(f"Received signal {signum}; stopping chain halt monitor.", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def main() -> None:
    config = load_config()
    mode, interval = runtime_settings(config)
    if mode == "once":
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
