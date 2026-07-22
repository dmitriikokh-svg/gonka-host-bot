"""Monitor GNK balances used to create escrows.

The watcher treats a valid Cosmos bank response with no ``ngonka`` entry as
zero. HTTP failures and malformed payloads are a separate "unavailable"
state and can never become a false zero-balance alert.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bot_common import (
    SourcesUnavailable,
    escape_html,
    fetch_json_with_fallback,
    load_json,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "escrow_balances.json"
STATE_FILE = ROOT / "state" / "escrow_balances.json"


def decimal_config(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a non-negative finite number")
    return result


def load_config() -> dict:
    config = load_json(CONFIG_FILE)
    if not isinstance(config, dict):
        raise ValueError("escrow balance config must be a JSON object")

    if not isinstance(config.get("owner"), str) or not config["owner"].strip():
        raise ValueError("owner is required")
    if not isinstance(config.get("denom"), str) or not config["denom"].strip():
        raise ValueError("denom is required")

    decimals = config.get("decimals")
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 18:
        raise ValueError("decimals must be an integer between 0 and 18")

    low = decimal_config(config.get("low_balance_below_gnk"), "low_balance_below_gnk")
    recovery = decimal_config(
        config.get("recovery_at_or_above_gnk", low),
        "recovery_at_or_above_gnk",
    )
    if recovery < low:
        raise ValueError("recovery threshold cannot be below low-balance threshold")

    for field in (
        "reminder_interval_hours",
        "unavailable_alert_after_runs",
        "request_timeout_seconds",
        "attempts_per_source",
    ):
        value = config.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{field} must be a positive integer")

    templates = config.get("balance_url_templates")
    if not isinstance(templates, list) or not templates:
        raise ValueError("balance_url_templates must be a non-empty list")
    for template in templates:
        if not isinstance(template, str) or template.count("{address}") != 1:
            raise ValueError("each balance URL template must contain one {address}")
        parsed = urlparse(template.format(address="gonka1test"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"invalid balance URL template: {template!r}")

    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts must be a non-empty list")
    names: set[str] = set()
    addresses: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("each account must be a JSON object")
        name = account.get("name")
        address = account.get("address")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("account name is required")
        if not isinstance(address, str) or not address.startswith("gonka1"):
            raise ValueError(f"invalid Gonka address for {name!r}")
        if name in names or address in addresses:
            raise ValueError("account names and addresses must be unique")
        names.add(name)
        addresses.add(address)

        if "low_balance_below_gnk" in account:
            decimal_config(
                account["low_balance_below_gnk"],
                f"accounts.{name}.low_balance_below_gnk",
            )
        if "recovery_at_or_above_gnk" in account:
            decimal_config(
                account["recovery_at_or_above_gnk"],
                f"accounts.{name}.recovery_at_or_above_gnk",
            )

    return config


def load_state() -> dict:
    state = load_json(STATE_FILE, {"accounts": {}})
    if not isinstance(state, dict):
        raise ValueError("escrow balance state must be a JSON object")
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    return state


def save_state(state: dict) -> None:
    save_json_atomic(STATE_FILE, state, sort_keys=True)


def parse_balance(payload: Any, denom: str) -> int:
    """Return the base-denom balance from a validated Cosmos bank response."""
    if not isinstance(payload, dict):
        raise ValueError("bank response must be a JSON object")
    balances = payload.get("balances")
    if not isinstance(balances, list):
        raise ValueError("bank response is missing balances list")

    result = 0
    matches = 0
    for coin in balances:
        if not isinstance(coin, dict):
            raise ValueError("each balance must be a JSON object")
        coin_denom = coin.get("denom")
        amount = coin.get("amount")
        if not isinstance(coin_denom, str) or not coin_denom:
            raise ValueError("balance denom must be a non-empty string")
        if not isinstance(amount, str) or not amount.isdigit():
            raise ValueError(f"invalid amount for denom {coin_denom!r}")
        if coin_denom == denom:
            result += int(amount)
            matches += 1

    if matches > 1:
        raise ValueError(f"duplicate balance entries for denom {denom!r}")
    return result


def to_gnk(amount: int, decimals: int) -> Decimal:
    return Decimal(amount) / (Decimal(10) ** decimals)


def format_gnk(value: Decimal | str | int) -> str:
    decimal_value = Decimal(str(value))
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


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


def account_thresholds(config: dict, account: dict) -> tuple[Decimal, Decimal]:
    low = decimal_config(
        account.get("low_balance_below_gnk", config["low_balance_below_gnk"]),
        "low_balance_below_gnk",
    )
    recovery = decimal_config(
        account.get("recovery_at_or_above_gnk", config["recovery_at_or_above_gnk"]),
        "recovery_at_or_above_gnk",
    )
    if recovery < low:
        raise ValueError(f"recovery threshold for {account['name']} cannot be below low threshold")
    return low, recovery


def base_message(account: dict, owner: str, balance: Decimal, threshold: Decimal) -> str:
    return (
        f"Ключ: <code>{escape_html(account['name'])}</code>\n"
        f"Адрес: <code>{escape_html(account['address'])}</code>\n"
        f"Баланс: <b>{escape_html(format_gnk(balance))} GNK</b>\n"
        f"Порог: <code>&lt; {escape_html(format_gnk(threshold))} GNK</code>\n"
        f"Owner: {escape_html(owner)}"
    )


def apply_success(
    previous: dict,
    *,
    account: dict,
    config: dict,
    amount: int,
    source_url: str,
    now: str,
) -> tuple[dict, list[str]]:
    record = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    messages: list[str] = []
    denom = config["denom"]
    decimals = config["decimals"]
    balance = to_gnk(amount, decimals)
    low_threshold, recovery_threshold = account_thresholds(config, account)

    if record.get("unavailable_alerted"):
        messages.append(
            "🟢 <b>API баланса снова доступен</b>\n\n"
            f"Ключ: <code>{escape_html(account['name'])}</code>\n"
            f"Баланс: <b>{escape_html(format_gnk(balance))} GNK</b>\n"
            f"Источник: <code>{escape_html(source_url)}</code>"
        )

    low_alert_active = bool(record.get("low_alerted"))
    is_low = balance < low_threshold
    is_recovered = balance >= recovery_threshold

    if is_low:
        last_alert = parse_timestamp(record.get("last_low_alert_at"))
        current_time = parse_timestamp(now)
        reminder_due = (
            low_alert_active
            and current_time is not None
            and (
                last_alert is None
                or (current_time - last_alert).total_seconds()
                >= config["reminder_interval_hours"] * 3600
            )
        )
        if not low_alert_active:
            messages.append(
                "🔴 <b>Низкий баланс эскроу-ключа</b>\n\n"
                + base_message(account, config["owner"], balance, low_threshold)
            )
            record["low_since"] = now
            record["last_low_alert_at"] = now
        elif reminder_due:
            messages.append(
                "🔴 <b>Напоминание: баланс эскроу-ключа всё ещё низкий</b>\n\n"
                + base_message(account, config["owner"], balance, low_threshold)
                + f"\nНизкий баланс с: {escape_html(record.get('low_since', 'unknown'))}"
            )
            record["last_low_alert_at"] = now
        record["low_alerted"] = True
        status = "low"
    elif low_alert_active and is_recovered:
        messages.append(
            "🟢 <b>Баланс эскроу-ключа восстановлен</b>\n\n"
            + base_message(account, config["owner"], balance, low_threshold)
        )
        record["low_alerted"] = False
        record["low_since"] = None
        record["last_low_alert_at"] = None
        status = "ok"
    else:
        # A separate recovery threshold can deliberately preserve an active
        # alert in the hysteresis band between low and recovery.
        status = "low" if low_alert_active else "ok"

    record.update(
        {
            "address": account["address"],
            "balance_base": str(amount),
            "balance_gnk": format_gnk(balance),
            "denom": denom,
            "status": status,
            "last_checked_at": now,
            "last_success_at": now,
            "source_url": source_url,
            "unavailable_runs": 0,
            "unavailable_since": None,
            "unavailable_alerted": False,
            "last_error": None,
        }
    )
    return record, messages


def apply_failure(
    previous: dict,
    *,
    account: dict,
    config: dict,
    error: Exception,
    now: str,
) -> tuple[dict, list[str]]:
    record = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    runs = int(record.get("unavailable_runs", 0)) + 1
    alerted = bool(record.get("unavailable_alerted"))
    messages: list[str] = []

    if runs >= config["unavailable_alert_after_runs"] and not alerted:
        messages.append(
            "🟡 <b>Не удалось проверить баланс эскроу-ключа</b>\n\n"
            f"Ключ: <code>{escape_html(account['name'])}</code>\n"
            f"Адрес: <code>{escape_html(account['address'])}</code>\n"
            f"Последовательных неудачных проверок: {runs}\n"
            "Последний известный баланс сохранён и не считается нулевым.\n"
            f"Owner: {escape_html(config['owner'])}"
        )
        alerted = True

    record.update(
        {
            "address": account["address"],
            "status": "unavailable",
            "last_checked_at": now,
            "unavailable_runs": runs,
            "unavailable_since": record.get("unavailable_since") or now,
            "unavailable_alerted": alerted,
            "last_error": str(error)[:2000],
        }
    )
    return record, messages


def account_urls(config: dict, address: str) -> list[str]:
    return [template.format(address=address) for template in config["balance_url_templates"]]


def fetch_account_balance(config: dict, account: dict) -> tuple[int, str]:
    denom = config["denom"]

    def validate(payload: Any) -> None:
        parse_balance(payload, denom)

    payload, source_url = fetch_json_with_fallback(
        account_urls(config, account["address"]),
        timeout=config["request_timeout_seconds"],
        attempts=config["attempts_per_source"],
        retry_delay=1,
        validator=validate,
    )
    return parse_balance(payload, denom), source_url


def build_summary(config: dict, state: dict) -> str:
    lines = ["🟢 <b>Проверка балансов эскроу-ключей</b>", ""]
    for account in config["accounts"]:
        record = state["accounts"].get(account["name"], {})
        if record.get("status") == "unavailable":
            value = "метрика недоступна"
        else:
            value = f"{record.get('balance_gnk', 'unknown')} GNK"
        lines.append(
            f"• <code>{escape_html(account['name'])}</code>: "
            f"<b>{escape_html(value)}</b> ({escape_html(record.get('status', 'unknown'))})"
        )
    lines.extend(["", f"Owner: {escape_html(config['owner'])}"])
    return "\n".join(lines)


def run() -> dict:
    config = load_config()
    state = load_state()
    now = utc_now()
    state["checked_at"] = now

    enabled_accounts = [
        account for account in config["accounts"] if account.get("enabled", True)
    ]
    if not enabled_accounts:
        raise ValueError("at least one escrow balance account must be enabled")

    for account in enabled_accounts:
        name = account["name"]
        previous = state["accounts"].get(name, {})
        try:
            amount, source_url = fetch_account_balance(config, account)
            record, messages = apply_success(
                previous,
                account=account,
                config=config,
                amount=amount,
                source_url=source_url,
                now=now,
            )
        except (SourcesUnavailable, ValueError) as exc:
            record, messages = apply_failure(
                previous,
                account=account,
                config=config,
                error=exc,
                now=now,
            )

        for message in messages:
            send_telegram_message(message)

        state["accounts"][name] = record
        # Persist after every account so a later Telegram/API failure does not
        # cause already-delivered alerts for earlier accounts to repeat.
        save_state(state)

        print(
            json.dumps(
                {
                    "account": name,
                    "status": record["status"],
                    "balance_gnk": record.get("balance_gnk"),
                    "source_url": record.get("source_url"),
                    "unavailable_runs": record.get("unavailable_runs", 0),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    if os.environ.get("SEND_BALANCE_SUMMARY", "").lower() in {"1", "true", "yes"}:
        send_telegram_message(build_summary(config, state))

    return state


if __name__ == "__main__":
    run()
