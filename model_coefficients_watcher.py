"""Monitor changes to Gonka PoC model coefficients."""

from __future__ import annotations

import ipaddress
import re
import sys
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bot_common import (
    escape_html,
    fetch_json_with_fallback,
    load_json,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "model_coefficients.json"
STATE_FILE = ROOT / "state" / "model_coefficients.json"


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
        raise ValueError("model-coefficients config must be a JSON object")
    for field in ("params_urls", "epoch_group_data_urls"):
        urls = config.get(field)
        if not isinstance(urls, list) or not urls:
            raise ValueError(f"{field} must be a non-empty list")
        for url in urls:
            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"{field} contains an invalid URL")
            validate_public_http_url(url)
    for field in ("request_timeout_seconds", "attempts_per_source"):
        if int(config.get(field, 1)) < 1:
            raise ValueError(f"{field} must be positive")
    if int(config.get("unavailable_alert_after_runs", 3)) < 1:
        raise ValueError("unavailable_alert_after_runs must be positive")
    return config


def decimal_value(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def canonical_decimal(value: Any) -> str:
    parsed = decimal_value(value, "coefficient")
    if parsed == 0:
        return "0"
    result = format(parsed.normalize(), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def parse_scale_factor(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("weight_scale_factor must be an object")
    coefficient = decimal_value(value.get("value"), "weight_scale_factor.value")
    exponent_raw = value.get("exponent")
    if isinstance(exponent_raw, bool):
        raise ValueError("weight_scale_factor.exponent is invalid")
    try:
        exponent = int(exponent_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("weight_scale_factor.exponent is invalid") from exc
    if isinstance(exponent_raw, float) and not exponent_raw.is_integer():
        raise ValueError("weight_scale_factor.exponent must be an integer")
    if isinstance(exponent_raw, str) and exponent_raw.strip() != str(exponent):
        raise ValueError("weight_scale_factor.exponent must be an integer")
    return canonical_decimal(coefficient * (Decimal(10) ** exponent))


def parse_params_payload(payload: Any) -> dict[str, str]:
    params = payload.get("params") if isinstance(payload, dict) else None
    poc_params = params.get("poc_params") if isinstance(params, dict) else None
    models = poc_params.get("models") if isinstance(poc_params, dict) else None
    if not isinstance(models, list) or not models:
        raise ValueError("params.poc_params.models must be a non-empty list")

    parsed: dict[str, str] = {}
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("model entry must be an object")
        model_id = model.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id is missing")
        model_id = model_id.strip()
        if model_id in parsed:
            raise ValueError(f"duplicate model_id: {model_id}")
        parsed[model_id] = parse_scale_factor(model.get("weight_scale_factor"))
    return parsed


def parse_epoch_group_payload(payload: Any) -> int:
    group = payload.get("epoch_group_data") if isinstance(payload, dict) else None
    raw_epoch = group.get("epoch_index") if isinstance(group, dict) else None
    if isinstance(raw_epoch, bool):
        raise ValueError("epoch_group_data.epoch_index is invalid")
    try:
        epoch = int(raw_epoch)
    except (TypeError, ValueError) as exc:
        raise ValueError("epoch_group_data.epoch_index is invalid") from exc
    if epoch < 1:
        raise ValueError("epoch_group_data.epoch_index must be positive")
    return epoch


def fetch_models(config: dict) -> tuple[dict[str, str], str]:
    payload, source = fetch_json_with_fallback(
        config["params_urls"],
        timeout=int(config.get("request_timeout_seconds", 10)),
        attempts=int(config.get("attempts_per_source", 1)),
        validator=lambda value: parse_params_payload(value),
    )
    models = parse_params_payload(payload)
    print(f"PoC params source: {source}")
    return models, source


def fetch_epoch(config: dict) -> tuple[int, str]:
    payload, source = fetch_json_with_fallback(
        config["epoch_group_data_urls"],
        timeout=int(config.get("request_timeout_seconds", 10)),
        attempts=int(config.get("attempts_per_source", 1)),
        validator=lambda value: parse_epoch_group_payload(value),
    )
    epoch = parse_epoch_group_payload(payload)
    print(f"Epoch source: {source}")
    return epoch, source


def error_category(exc: Exception) -> str:
    message = str(exc).lower()
    type_name = type(exc).__name__.lower()
    if "timeout" in type_name or "timeout" in message or "timed out" in message:
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


def load_state(path: str | Path | None = None) -> dict:
    state = load_json(path or STATE_FILE, {})
    if not isinstance(state, dict):
        raise ValueError("model-coefficients state must be a JSON object")
    return state


def save_state(state: dict, path: str | Path | None = None) -> None:
    save_json_atomic(path or STATE_FILE, state, sort_keys=True)


def model_changes(previous: dict[str, Any], current: dict[str, Any]) -> list[dict]:
    old = {model: canonical_decimal(value) for model, value in previous.items()}
    new = {model: canonical_decimal(value) for model, value in current.items()}
    changes = []
    for model_id in sorted(old.keys() | new.keys()):
        if model_id not in old:
            changes.append({"type": "added", "model_id": model_id, "new": new[model_id]})
        elif model_id not in new:
            changes.append({"type": "removed", "model_id": model_id, "old": old[model_id]})
        elif old[model_id] != new[model_id]:
            changes.append(
                {
                    "type": "changed",
                    "model_id": model_id,
                    "old": old[model_id],
                    "new": new[model_id],
                }
            )
    return changes


def build_changes_message(epoch: int | None, changes: list[dict]) -> str:
    lines = [
        "ℹ️ <b>Изменились коэффициенты PoC моделей</b>",
        "",
        f"Эпоха: {epoch if epoch is not None else 'нет данных'}",
    ]
    for change in changes:
        model_id = escape_html(change["model_id"])
        if change["type"] == "added":
            lines.append(
                f"• <code>{model_id}</code>: добавлена, коэффициент "
                f"<b>{escape_html(change['new'])}</b>"
            )
        elif change["type"] == "removed":
            lines.append(f"• <code>{model_id}</code>: удалена")
        else:
            lines.append(
                f"• <code>{model_id}</code>: "
                f"<b>{escape_html(change['old'])}</b> → "
                f"<b>{escape_html(change['new'])}</b>"
            )
    return "\n".join(lines)


def build_unavailable_message(reason: str, runs: int) -> str:
    return (
        "🟡 <b>Коэффициенты PoC моделей недоступны</b>\n\n"
        f"Последовательных проверок: <b>{runs}</b>\n"
        f"Причина: <code>{escape_html(reason)}</code>"
    )


def build_recovery_message(epoch: int | None) -> str:
    return (
        "🟢 <b>Коэффициенты PoC моделей снова доступны</b>\n\n"
        f"Эпоха: {epoch if epoch is not None else 'нет данных'}"
    )


def apply_success(
    previous: dict,
    models: dict[str, str],
    *,
    epoch: int | None,
    source: str,
    epoch_source: str | None,
    now: str,
) -> tuple[dict, list[str]]:
    messages: list[str] = []
    previous_models = previous.get("models")
    baseline = not isinstance(previous_models, dict)
    if previous.get("unavailable_alerted"):
        messages.append(build_recovery_message(epoch))
    if not baseline:
        changes = model_changes(previous_models, models)
        if changes:
            messages.append(build_changes_message(epoch, changes))
    return {
        "schema_version": 1,
        "checked_at": now,
        "epoch": epoch,
        "models": dict(sorted(models.items())),
        "source": source,
        "epoch_source": epoch_source,
        "unavailable_runs": 0,
        "unavailable_alerted": False,
        "last_error_reason": None,
        "last_error": None,
    }, messages


def apply_unavailable(
    previous: dict,
    exc: Exception,
    *,
    alert_after_runs: int,
    now: str,
) -> tuple[dict, list[str]]:
    state = deepcopy(previous)
    runs = int(previous.get("unavailable_runs", 0) or 0) + 1
    alerted = bool(previous.get("unavailable_alerted", False))
    reason = error_category(exc)
    messages = []
    if runs >= alert_after_runs and not alerted:
        alerted = True
        messages.append(build_unavailable_message(reason, runs))
    state.update(
        {
            "schema_version": 1,
            "checked_at": now,
            "unavailable_runs": runs,
            "unavailable_alerted": alerted,
            "last_error_reason": reason,
            "last_error": f"{type(exc).__name__}: {exc}",
        }
    )
    return state, messages


def main() -> None:
    config = load_config()
    previous = load_state()
    now = utc_now()
    try:
        models, source = fetch_models(config)
    except Exception as exc:  # noqa: BLE001 - monitored observability transition
        print(f"WARNING: PoC params unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        state, messages = apply_unavailable(
            previous,
            exc,
            alert_after_runs=int(config.get("unavailable_alert_after_runs", 3)),
            now=now,
        )
    else:
        try:
            epoch, epoch_source = fetch_epoch(config)
        except Exception as exc:  # noqa: BLE001 - epoch is informational
            print(f"WARNING: epoch unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
            epoch = None
            epoch_source = None
        state, messages = apply_success(
            previous,
            models,
            epoch=epoch,
            source=source,
            epoch_source=epoch_source,
            now=now,
        )

    for message in messages:
        send_telegram_message(message)
    save_state(state)
    print(
        f"Sent {len(messages)} Telegram message(s)."
        if messages
        else "No coefficient changes; no Telegram messages sent."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - command-line boundary
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
