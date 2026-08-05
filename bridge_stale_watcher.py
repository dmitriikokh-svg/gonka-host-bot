"""Monitor Gonka bridge signer concentration, eligibility and block freshness.

The watcher joins four independent snapshots for the same epoch:
  * actual BLS slot allocation;
  * current Cosmos group membership (the validators that can vote);
  * one frozen Ethereum finalized block number;
  * each eligible validator's reported latest bridge block.

An unreachable validator API is recorded as unknown and never as stale.
Top BLS peers have an additional individual availability rule, so one
important peer cannot be hidden below the aggregate unknown-slots threshold.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import requests

from bot_common import (
    SourcesUnavailable,
    escape_html,
    fetch_json_with_fallback,
    load_json,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)
from bridge_burn_watcher import (
    fetch_finalized_block,
    source_failure,
    source_label,
    source_success,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "bridge_stale.json"
STATE_FILE = ROOT / "state" / "bridge_stale.json"
SIGNED_PHASE = "DKG_PHASE_SIGNED"


def positive_int(config: dict, field: str, *, minimum: int = 1) -> int:
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def percent_config(config: dict, field: str) -> Decimal:
    try:
        value = Decimal(str(config.get(field)))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a percentage") from exc
    if not value.is_finite() or value <= 0 or value > 100:
        raise ValueError(f"{field} must be greater than 0 and at most 100")
    return value


def validate_http_url(value: str, field: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid URL in {field}: {value!r}")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid port in {field}: {value!r}") from exc


def load_config() -> dict:
    config = load_json(CONFIG_FILE)
    if not isinstance(config, dict):
        raise ValueError("bridge stale config must be a JSON object")
    if not isinstance(config.get("owner"), str) or not config["owner"].strip():
        raise ValueError("owner is required")
    if not isinstance(config.get("origin_chain"), str) or not config["origin_chain"].strip():
        raise ValueError("origin_chain is required")

    for field in ("chain_api_bases", "epoch_urls", "ethereum_rpc_urls"):
        values = config.get(field)
        if not isinstance(values, list) or not values:
            raise ValueError(f"{field} must be a non-empty list")
        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"each value in {field} must be a string")
            validate_http_url(value, field)

    percent_config(config, "inactive_slots_warning_percent")
    percent_config(config, "stale_slots_warning_percent")
    percent_config(config, "unknown_slots_warning_percent")
    positive_int(config, "source_unavailable_alert_after_runs")
    positive_int(config, "request_timeout_seconds")
    positive_int(config, "attempts_per_source")
    positive_int(config, "attempts_per_node")
    positive_int(config, "max_parallel_node_checks")
    positive_int(config, "top_peers_count")
    positive_int(config, "top_peer_unavailable_alert_after_runs")
    return config


def default_state() -> dict:
    return {
        "checked_at": None,
        "epoch": None,
        "dkg_phase": None,
        "ethereum_finalized_block": None,
        "signals": {},
        "sources": {},
        "participants": {},
        "top_peers": {},
    }


def load_state() -> dict:
    state = load_json(STATE_FILE, default_state())
    if not isinstance(state, dict):
        raise ValueError("bridge stale state must be a JSON object")
    for field in ("signals", "sources", "participants", "top_peers"):
        if not isinstance(state.get(field), dict):
            state[field] = {}
    return state


def save_state(state: dict) -> None:
    save_json_atomic(STATE_FILE, state, sort_keys=True)


def chain_urls(config: dict, suffix: str) -> list[str]:
    return [base.rstrip("/") + "/" + suffix.lstrip("/") for base in config["chain_api_bases"]]


def fetch_current_epoch(config: dict) -> int:
    def validate(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("epoch response must be an object")
        value = (payload.get("latest_epoch") or {}).get("index")
        if value is None or int(value) < 0:
            raise ValueError("latest epoch index is missing")

    payload, _source = fetch_json_with_fallback(
        config["epoch_urls"],
        timeout=config["request_timeout_seconds"],
        attempts=config["attempts_per_source"],
        validator=validate,
    )
    return int(payload["latest_epoch"]["index"])


def parse_bls_epoch(payload: Any, expected_epoch: int | None = None) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("epoch_data"), dict):
        raise ValueError("BLS response is missing epoch_data")
    data = payload["epoch_data"]
    epoch = int(data.get("epoch_id"))
    if expected_epoch is not None and epoch != expected_epoch:
        raise ValueError(f"BLS response epoch {epoch} does not match {expected_epoch}")
    total_slots = data.get("i_total_slots")
    if not isinstance(total_slots, int) or isinstance(total_slots, bool) or total_slots < 1:
        raise ValueError("BLS i_total_slots must be a positive integer")
    participants = data.get("participants")
    if not isinstance(participants, list) or not participants:
        raise ValueError("BLS participants must be a non-empty list")

    parsed_participants: list[dict] = []
    addresses: set[str] = set()
    occupied_slots: set[int] = set()
    for participant in participants:
        if not isinstance(participant, dict):
            raise ValueError("BLS participant must be an object")
        address = participant.get("address")
        start = participant.get("slot_start_index")
        end = participant.get("slot_end_index")
        if not isinstance(address, str) or not address.startswith("gonka1"):
            raise ValueError("BLS participant has an invalid address")
        if address in addresses:
            raise ValueError(f"duplicate BLS participant: {address}")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
            raise ValueError(f"BLS slot range for {address} must use integers")
        if start < 0 or end < start or end >= total_slots:
            raise ValueError(f"invalid BLS slot range for {address}: {start}-{end}")
        slot_range = set(range(start, end + 1))
        if occupied_slots.intersection(slot_range):
            raise ValueError(f"overlapping BLS slot range for {address}")
        occupied_slots.update(slot_range)
        addresses.add(address)
        parsed_participants.append(
            {
                "address": address,
                "slots": end - start + 1,
                "slot_start_index": start,
                "slot_end_index": end,
                "percentage_weight": participant.get("percentage_weight"),
            }
        )

    if len(occupied_slots) != total_slots:
        raise ValueError(
            f"BLS slot ranges cover {len(occupied_slots)} of {total_slots} slots"
        )
    return {
        "epoch": epoch,
        "total_slots": total_slots,
        "dkg_phase": data.get("dkg_phase"),
        "participants": parsed_participants,
    }


def fetch_bls_epoch(config: dict, epoch: int) -> dict:
    urls = chain_urls(config, f"productscience/inference/bls/epoch_data/{epoch}")

    def validate(payload: Any) -> None:
        parse_bls_epoch(payload, epoch)

    payload, _source = fetch_json_with_fallback(
        urls,
        timeout=config["request_timeout_seconds"],
        attempts=config["attempts_per_source"],
        validator=validate,
    )
    return parse_bls_epoch(payload, epoch)


def fetch_group_id(config: dict, epoch: int) -> str:
    urls = chain_urls(
        config,
        f"productscience/inference/inference/epoch_group_data/{epoch}",
    )

    def read(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ValueError("epoch group response must be an object")
        group = payload.get("epoch_group_data") or payload.get("epochGroupData")
        if not isinstance(group, dict):
            raise ValueError("epoch group data is missing")
        value = group.get("epoch_group_id") or group.get("epochGroupId")
        if value is None or not str(value):
            raise ValueError("epoch_group_id is missing")
        return str(value)

    payload, _source = fetch_json_with_fallback(
        urls,
        timeout=config["request_timeout_seconds"],
        attempts=config["attempts_per_source"],
        validator=lambda payload: read(payload),
    )
    return read(payload)


def parse_group_members(payload: Any) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("members"), list):
        raise ValueError("group members response is missing members list")
    result: set[str] = set()
    for entry in payload["members"]:
        member = entry.get("member") if isinstance(entry, dict) else None
        if not isinstance(member, dict):
            raise ValueError("group member entry is malformed")
        address = member.get("address")
        if not isinstance(address, str) or not address.startswith("gonka1"):
            raise ValueError("group member address is invalid")
        result.add(address)
    if not result:
        raise ValueError("group members list is empty")
    return result


def fetch_group_members(config: dict, group_id: str) -> set[str]:
    encoded = quote(group_id, safe="")
    urls = chain_urls(config, f"cosmos/group/v1/group_members/{encoded}")
    payload, _source = fetch_json_with_fallback(
        urls,
        timeout=config["request_timeout_seconds"],
        attempts=config["attempts_per_source"],
        validator=lambda payload: parse_group_members(payload),
    )
    return parse_group_members(payload)


def parse_participant_info(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("participant response must be an object")
    participant = payload.get("participant")
    if not isinstance(participant, dict):
        raise ValueError("participant object is missing")
    url = participant.get("inference_url") or participant.get("inferenceUrl")
    status = participant.get("status")
    if url is not None and not isinstance(url, str):
        raise ValueError("participant inference URL must be a string")
    return (url.rstrip("/") if url else None, str(status) if status is not None else None)


def fetch_participant_info(config: dict, address: str) -> tuple[str | None, str | None]:
    urls = chain_urls(
        config,
        f"productscience/inference/inference/participant/{quote(address, safe='')}",
    )
    payload, _source = fetch_json_with_fallback(
        urls,
        timeout=config["request_timeout_seconds"],
        attempts=config["attempts_per_source"],
        validator=lambda payload: parse_participant_info(payload),
    )
    return parse_participant_info(payload)


def normalized_public_node_url(raw_url: str) -> str:
    value = raw_url.strip()
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid validator endpoint: {raw_url!r}")
    if parsed.username or parsed.password:
        raise ValueError("validator endpoint must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid validator endpoint port: {raw_url!r}") from exc
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError(f"non-public validator endpoint: {raw_url!r}")
    if address is None:
        try:
            resolved = {
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(
                    parsed.hostname,
                    port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ValueError(f"validator endpoint does not resolve: {raw_url!r}") from exc
        if not resolved or any(not item.is_global for item in resolved):
            raise ValueError(f"validator endpoint resolves to a non-public IP: {raw_url!r}")
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )


def probe_bridge_latest(config: dict, node_url: str) -> tuple[int, str]:
    base = normalized_public_node_url(node_url)
    url = base + f"/v1/bridge/block/latest?chain={quote(config['origin_chain'], safe='')}"
    errors: list[str] = []
    for attempt in range(1, config["attempts_per_node"] + 1):
        try:
            response = requests.get(
                url,
                timeout=config["request_timeout_seconds"],
                headers={"User-Agent": "gonka-host-bot/bridge-stale-v1"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("bridge latest response must be an object")
            block_number = payload.get("blockNumber")
            if block_number is None:
                block_number = payload.get("block_number")
            if isinstance(block_number, bool) or block_number is None:
                raise ValueError("bridge blockNumber is missing")
            result = int(block_number)
            if result < 0:
                raise ValueError("bridge blockNumber cannot be negative")
            return result, source_label(base)
        except Exception as exc:  # noqa: BLE001 - report compact node error
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:300]}")
            if attempt < config["attempts_per_node"]:
                time.sleep(1)
    raise SourcesUnavailable(" | ".join(errors))


def inspect_participant(
    config: dict,
    participant: dict,
    finalized: int | None,
) -> dict:
    result = {
        "address": participant["address"],
        "slots": participant["slots"],
        "bridge_latest": None,
        "endpoint": None,
        "participant_status": None,
        "classification": "unknown",
        "reason": None,
    }
    try:
        node_url, participant_status = fetch_participant_info(config, participant["address"])
        result["participant_status"] = participant_status
        if not node_url:
            raise ValueError("participant has no inference_url")
        latest, endpoint = probe_bridge_latest(config, node_url)
        result["bridge_latest"] = latest
        result["endpoint"] = endpoint
        if finalized is None:
            # API availability is useful on its own for the Top-peer rule and
            # must not depend on an Ethereum RPC being reachable.
            result["classification"] = "reachable"
            result["reason"] = "bridge_api_reachable"
        else:
            # The ticket owner explicitly requested literal equality with the
            # one frozen finalized block used for the whole run.
            result["classification"] = "healthy" if latest == finalized else "stale"
            result["reason"] = (
                "equal_to_finalized" if latest == finalized else "not_equal_to_finalized"
            )
    except Exception as exc:  # noqa: BLE001 - a node failure is unknown, not fatal
        result["reason"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    return result


def inspect_eligible_participants(
    config: dict,
    participants: list[dict],
    finalized: int | None,
) -> dict[str, dict]:
    if not participants:
        return {}
    workers = min(config["max_parallel_node_checks"], len(participants))
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(inspect_participant, config, participant, finalized): participant
            for participant in participants
        }
        for future in as_completed(futures):
            participant = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - defensive executor guard
                result = {
                    "address": participant["address"],
                    "slots": participant["slots"],
                    "bridge_latest": None,
                    "endpoint": None,
                    "participant_status": None,
                    "classification": "unknown",
                    "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                }
            results[participant["address"]] = result
    return results


def slot_share(slots: int, total_slots: int) -> Decimal:
    return Decimal(slots) * Decimal(100) / Decimal(total_slots)


def format_percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'))}%"


def slot_line(item: dict) -> str:
    latest = item.get("bridge_latest")
    latest_text = "unknown" if latest is None else str(latest)
    return (
        f"• <code>{escape_html(item['address'])}</code>: "
        f"<b>{item['slots']} slots</b>, latest=<code>{escape_html(latest_text)}</code>"
    )


def compact_items(items: list[dict], *, limit: int = 6) -> str:
    lines = [slot_line(item) for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"• …и ещё {len(items) - limit}")
    return "\n".join(lines)


def apply_signal(
    state: dict,
    *,
    name: str,
    active: bool,
    value_slots: int,
    total_slots: int,
    threshold_text: str,
    now: str,
    alert_message: str,
    recovery_title: str,
) -> list[str]:
    previous = state["signals"].get(name, {})
    was_active = bool(previous.get("active"))
    messages: list[str] = []
    if active and not was_active:
        messages.append(alert_message)
    elif not active and was_active:
        messages.append(
            f"🟢 <b>{escape_html(recovery_title)}</b>\n\n"
            f"Текущее значение: <b>{value_slots}/{total_slots} slots</b> "
            f"({format_percent(slot_share(value_slots, total_slots))})"
        )
    state["signals"][name] = {
        "active": active,
        "value_slots": value_slots,
        "total_slots": total_slots,
        "threshold": threshold_text,
        "active_since": previous.get("active_since") if was_active and active else (now if active else None),
        "last_checked_at": now,
    }
    return messages


def top_peer_line(item: dict) -> str:
    reason_labels = {
        "bridge_api_unavailable": "bridge API недоступен",
        "inactive_after_cpoc": "inactive/исключён после CPoC",
    }
    reason = reason_labels.get(
        item.get("failure_reason"),
        item.get("failure_reason", "unknown"),
    )
    return (
        f"• <b>#{item['rank']}</b> <code>{escape_html(item['address'])}</code>: "
        f"<b>{item['slots']} slots</b>, {escape_html(reason)}, "
        f"проверок подряд: <b>{item['consecutive_failed_runs']}</b>"
    )


def evaluate_top_peers(
    state: dict,
    config: dict,
    bls: dict,
    eligible: set[str],
    inspected: dict[str, dict],
    now: str,
) -> list[str]:
    """Alert when any Top-N BLS peer is unavailable in consecutive runs."""

    count = positive_int(config, "top_peers_count")
    alert_after = positive_int(config, "top_peer_unavailable_alert_after_runs")
    sorted_participants = sorted(
        bls["participants"],
        key=lambda item: (-item["slots"], item["address"]),
    )
    top_peers = sorted_participants[:count]
    previous_peers = state.get("top_peers", {})
    if not isinstance(previous_peers, dict):
        previous_peers = {}

    current: dict[str, dict] = {}
    new_alerts: list[dict] = []
    recoveries: list[dict] = []
    scope_closures: list[dict] = []

    for rank, participant in enumerate(top_peers, start=1):
        address = participant["address"]
        previous = previous_peers.get(address, {})
        if not isinstance(previous, dict):
            previous = {}

        observation = inspected.get(address, {})
        if address not in eligible:
            failure_reason = "inactive_after_cpoc"
        elif observation.get("classification") == "unknown" or not observation:
            failure_reason = "bridge_api_unavailable"
        else:
            failure_reason = None

        was_failed = bool(previous.get("failure_reason"))
        was_alerted = bool(previous.get("alerted"))
        failed = failure_reason is not None
        consecutive = (
            int(previous.get("consecutive_failed_runs", 0)) + 1
            if failed and was_failed
            else (1 if failed else 0)
        )
        alerted = was_alerted if failed else False
        first_failed_at = (
            previous.get("first_failed_at")
            if failed and was_failed
            else (now if failed else None)
        )

        entry = {
            "address": address,
            "rank": rank,
            "slots": participant["slots"],
            "epoch": bls["epoch"],
            "failure_reason": failure_reason,
            "consecutive_failed_runs": consecutive,
            "alerted": alerted,
            "first_failed_at": first_failed_at,
            "last_checked_at": now,
        }

        if failed and consecutive >= alert_after and not was_alerted:
            entry["alerted"] = True
            entry["alerted_at"] = now
            new_alerts.append(entry)
        elif failed and was_alerted:
            entry["alerted_at"] = previous.get("alerted_at")
        elif not failed and was_alerted:
            recoveries.append(entry)

        current[address] = entry

    current_addresses = set(current)
    for address, previous in previous_peers.items():
        if address in current_addresses or not isinstance(previous, dict):
            continue
        if previous.get("alerted"):
            scope_closures.append(previous)

    state["top_peers"] = current
    messages: list[str] = []
    if new_alerts:
        messages.append(
            f"🟡 <b>Недоступны bridge peers из Top-{count} по BLS slots</b>\n\n"
            f"Порог последовательных сбоев: <code>{alert_after}</code>.\n\n"
            + "\n".join(top_peer_line(item) for item in new_alerts)
            + f"\n\nЭпоха: <code>{bls['epoch']}</code>"
        )
    if recoveries:
        messages.append(
            f"🟢 <b>Доступность Top-{count} bridge peers восстановилась</b>\n\n"
            + "\n".join(
                f"• <code>{escape_html(item['address'])}</code>: "
                f"<b>#{item['rank']}</b>, {item['slots']} slots"
                for item in recoveries
            )
            + f"\n\nЭпоха: <code>{bls['epoch']}</code>"
        )
    if scope_closures:
        messages.append(
            f"🟢 <b>Индивидуальный Top-{count} bridge alert закрыт</b>\n\n"
            f"Следующие участники больше не входят в текущий Top-{count} по BLS slots:\n"
            + "\n".join(
                f"• <code>{escape_html(item['address'])}</code>"
                for item in scope_closures
            )
            + f"\n\nЭпоха: <code>{bls['epoch']}</code>"
        )
    return messages


def evaluate_snapshot(
    state: dict,
    config: dict,
    bls: dict,
    eligible: set[str],
    inspected: dict[str, dict],
    finalized: int | None,
    now: str,
) -> list[str]:
    messages: list[str] = []
    total = bls["total_slots"]
    sorted_participants = sorted(bls["participants"], key=lambda item: -item["slots"])
    top_three = sorted_participants[:3]
    top_three_slots = sum(item["slots"] for item in top_three)
    majority = total // 2 + 1
    concentration_active = top_three_slots >= majority
    concentration_message = (
        "🟡 <b>Высокая концентрация BLS slots</b>\n\n"
        f"Top-3 контролируют: <b>{top_three_slots}/{total} slots</b>\n"
        f"Порог большинства: <code>{majority}</code>\n\n"
        + compact_items(top_three, limit=3)
        + f"\n\nЭпоха: <code>{bls['epoch']}</code>"
    )
    messages.extend(
        apply_signal(
            state,
            name="concentration",
            active=concentration_active,
            value_slots=top_three_slots,
            total_slots=total,
            threshold_text=f">={majority}",
            now=now,
            alert_message=concentration_message,
            recovery_title="Концентрация BLS slots вернулась в норму",
        )
    )

    inactive = [item for item in sorted_participants if item["address"] not in eligible]
    inactive_slots = sum(item["slots"] for item in inactive)
    inactive_threshold = percent_config(config, "inactive_slots_warning_percent")
    inactive_active = slot_share(inactive_slots, total) >= inactive_threshold
    inactive_message = (
        "🟡 <b>Слишком много BLS slots не могут голосовать в bridge</b>\n\n"
        f"Inactive/invalidated: <b>{inactive_slots}/{total} slots</b> "
        f"({format_percent(slot_share(inactive_slots, total))})\n"
        f"Порог: <code>&gt;= {inactive_threshold}%</code>\n\n"
        + (compact_items(inactive) if inactive else "Нет участников")
        + f"\n\nЭпоха: <code>{bls['epoch']}</code>"
    )
    messages.extend(
        apply_signal(
            state,
            name="inactive",
            active=inactive_active,
            value_slots=inactive_slots,
            total_slots=total,
            threshold_text=f">={inactive_threshold}%",
            now=now,
            alert_message=inactive_message,
            recovery_title="Доля inactive/invalidated BLS slots восстановилась",
        )
    )

    if finalized is None:
        return messages

    inspected_items = list(inspected.values())
    stale = sorted(
        [item for item in inspected_items if item["classification"] == "stale"],
        key=lambda item: -item["slots"],
    )
    unknown = sorted(
        [item for item in inspected_items if item["classification"] == "unknown"],
        key=lambda item: -item["slots"],
    )
    for name, title, items, field, recovery in (
        (
            "stale",
            "Bridge block не совпадает с Ethereum finalized",
            stale,
            "stale_slots_warning_percent",
            "Доля stale bridge slots восстановилась",
        ),
        (
            "unknown",
            "Статус bridge недоступен для значимой доли BLS slots",
            unknown,
            "unknown_slots_warning_percent",
            "Доступность bridge status восстановилась",
        ),
    ):
        slots = sum(item["slots"] for item in items)
        threshold = percent_config(config, field)
        active = slot_share(slots, total) >= threshold
        message = (
            f"🟡 <b>{escape_html(title)}</b>\n\n"
            f"Затронуто: <b>{slots}/{total} slots</b> "
            f"({format_percent(slot_share(slots, total))})\n"
            f"Порог: <code>&gt;= {threshold}%</code>\n"
            f"Ethereum finalized: <code>{finalized}</code>\n\n"
            + (compact_items(items) if items else "Нет участников")
            + f"\n\nЭпоха: <code>{bls['epoch']}</code>"
        )
        messages.extend(
            apply_signal(
                state,
                name=name,
                active=active,
                value_slots=slots,
                total_slots=total,
                threshold_text=f">={threshold}%",
                now=now,
                alert_message=message,
                recovery_title=recovery,
            )
        )
    return messages


def build_summary(state: dict, config: dict) -> str:
    signal_lines = []
    for name in ("concentration", "inactive", "stale", "unknown"):
        signal = state["signals"].get(name, {})
        status = "alert" if signal.get("active") else "ok"
        signal_lines.append(
            f"• <code>{name}</code>: <b>{status}</b>, "
            f"{signal.get('value_slots', 'unknown')}/{signal.get('total_slots', 'unknown')} slots"
        )
    top_peers = state.get("top_peers", {})
    top_peer_alerts = sum(
        1 for value in top_peers.values() if isinstance(value, dict) and value.get("alerted")
    )
    top_peer_pending = sum(
        1
        for value in top_peers.values()
        if isinstance(value, dict)
        and value.get("failure_reason")
        and not value.get("alerted")
    )
    return (
        "🟢 <b>Проверка Bridge Stale Check A</b>\n\n"
        f"Эпоха: <code>{escape_html(state.get('epoch'))}</code>\n"
        f"DKG phase: <code>{escape_html(state.get('dkg_phase'))}</code>\n"
        f"Ethereum finalized: <code>{escape_html(state.get('ethereum_finalized_block'))}</code>\n\n"
        + "\n".join(signal_lines)
        + f"\n• <code>top_peers</code>: <b>{top_peer_alerts} alert</b>, "
        f"{top_peer_pending} pending"
    )


def run() -> dict:
    config = load_config()
    state = load_state()
    state["owner"] = config["owner"]
    now = utc_now()
    state["checked_at"] = now
    messages: list[str] = []

    try:
        epoch = fetch_current_epoch(config)
        bls = fetch_bls_epoch(config, epoch)
        group_id = fetch_group_id(config, epoch)
        eligible = fetch_group_members(config, group_id)
        state["epoch"] = epoch
        state["dkg_phase"] = bls["dkg_phase"]
        messages.extend(source_success(state, "gonka_chain", now))
    except (SourcesUnavailable, ValueError, TypeError) as exc:
        messages.extend(source_failure(state, config, "gonka_chain", exc, now))
        for message in messages:
            send_telegram_message(message)
        save_state(state)
        print(json.dumps({"status": "chain_source_unavailable", "error": str(exc)[:500]}))
        return state

    if bls["dkg_phase"] == SIGNED_PHASE:
        # Concentration and eligibility do not depend on Ethereum or validator
        # APIs, so evaluate them even if those external sources are down.
        messages.extend(
            evaluate_snapshot(
                state,
                config,
                bls,
                eligible,
                {},
                None,
                now,
            )
        )
        previous_finalized = state.get("ethereum_finalized_block")
        minimum = (
            previous_finalized
            if isinstance(previous_finalized, int) and not isinstance(previous_finalized, bool)
            else None
        )
        finalized = None
        try:
            finalized, _source = fetch_finalized_block(config, minimum_block=minimum)
            state["ethereum_finalized_block"] = finalized
            messages.extend(source_success(state, "ethereum", now))
        except (SourcesUnavailable, ValueError, TypeError) as exc:
            messages.extend(source_failure(state, config, "ethereum", exc, now))

        eligible_participants = [
            item for item in bls["participants"] if item["address"] in eligible
        ]
        inspected = inspect_eligible_participants(
            config,
            eligible_participants,
            finalized,
        )
        state["participants"] = {
            item["address"]: {
                **copy.deepcopy(inspected.get(item["address"], {})),
                "address": item["address"],
                "slots": item["slots"],
                "eligible": item["address"] in eligible,
                "slot_start_index": item["slot_start_index"],
                "slot_end_index": item["slot_end_index"],
            }
            for item in bls["participants"]
        }
        messages.extend(
            evaluate_top_peers(
                state,
                config,
                bls,
                eligible,
                inspected,
                now,
            )
        )
        if finalized is not None:
            messages.extend(
                evaluate_snapshot(
                    state,
                    config,
                    bls,
                    eligible,
                    inspected,
                    finalized,
                    now,
                )
            )
    else:
        print(f"Epoch {epoch}: BLS phase {bls['dkg_phase']}; checks wait for {SIGNED_PHASE}")

    if os.environ.get("SEND_BRIDGE_STALE_SUMMARY", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        messages.append(build_summary(state, config))

    for message in messages:
        send_telegram_message(message)
    save_state(state)

    summary = {
        "status": "ok",
        "epoch": state.get("epoch"),
        "dkg_phase": state.get("dkg_phase"),
        "ethereum_finalized_block": state.get("ethereum_finalized_block"),
        "signals": {
            name: {
                "active": value.get("active"),
                "value_slots": value.get("value_slots"),
            }
            for name, value in state["signals"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return state


if __name__ == "__main__":
    run()
