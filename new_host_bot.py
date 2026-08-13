"""Track Gonka host presence transitions once per participant epoch."""

from __future__ import annotations

import copy
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from bot_common import (
    build_host_details,
    escape_html,
    fetch_json_with_fallback,
    format_integer,
    load_json,
    participant_ml_node_count,
    participant_models,
    participant_total_weight,
    participant_url,
    participant_weight,
    participant_weight_ranks,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)


ROOT = Path(__file__).resolve().parent
PARTICIPANTS_URLS = (
    "https://node3.gonka.ai/v1/epochs/current/participants",
    "http://node2.gonka.ai:8000/v1/epochs/current/participants",
    "http://node1.gonka.ai:8000/v1/epochs/current/participants",
)
CONFIG_FILE = ROOT / "config" / "host_monitor.json"
PRESENCE_STATE_FILE = ROOT / "state" / "host_presence.json"
EVENT_LOG_FILE = ROOT / "state" / "host_events.csv"
LEGACY_STATE_FILE = ROOT / "state" / "hosts.json"
LEGACY_LOG_FILE = ROOT / "state" / "host_log.csv"
EVENT_LOG_FIELDS = (
    "epoch",
    "event",
    "node_id",
    "observed_at_utc",
    "weight",
    "total_weight",
    "network_share_percent",
    "inference_url",
)
TELEGRAM_SAFE_LIMIT = 3800


def positive_epoch(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("active_participants.epoch_group_id is invalid")
    try:
        epoch = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("active_participants.epoch_group_id is invalid") from exc
    if epoch < 1:
        raise ValueError("active_participants.epoch_group_id must be positive")
    return epoch


def snapshot_node_id(entry: Any) -> str:
    if not isinstance(entry, dict):
        raise ValueError("every participant must be an object")
    for key in ("index", "participant_id", "address", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("participant address is missing")


def parse_snapshot_payload(data: Any) -> dict:
    active = data.get("active_participants") if isinstance(data, dict) else None
    if not isinstance(active, dict):
        raise ValueError("active_participants is missing")
    epoch = positive_epoch(active.get("epoch_group_id"))
    entries = active.get("participants")
    if not isinstance(entries, list) or not entries:
        raise ValueError("active_participants.participants must be a non-empty list")

    by_id: dict[str, dict] = {}
    for entry in entries:
        node_id = snapshot_node_id(entry)
        if node_id in by_id:
            raise ValueError(f"duplicate participant address: {node_id}")
        by_id[node_id] = entry

    total_weight, total_weight_complete = participant_total_weight(entries)
    return {
        "epoch": epoch,
        "entries": entries,
        "by_id": by_id,
        "total_weight": total_weight,
        "total_weight_complete": total_weight_complete,
        "ranks": participant_weight_ranks(entries),
    }


def validate_participants_response(data: Any) -> None:
    parse_snapshot_payload(data)


def fetch_snapshot() -> dict:
    data, source = fetch_json_with_fallback(
        PARTICIPANTS_URLS,
        timeout=30,
        validator=validate_participants_response,
    )
    print(f"Participants source: {source}")
    return parse_snapshot_payload(data)


def load_config(path: str | Path | None = None) -> dict:
    config = load_json(path or CONFIG_FILE)
    if not isinstance(config, dict):
        raise ValueError("host monitor config must be a JSON object")
    threshold = config.get("network_weight_change_warning_percent")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("network_weight_change_warning_percent must be a number")
    if threshold < 0:
        raise ValueError("network_weight_change_warning_percent cannot be negative")
    return config


def load_presence_state(path: str | Path | None = None) -> dict | None:
    state = load_json(path or PRESENCE_STATE_FILE)
    if state is None:
        return None
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("unsupported host presence state")
    if not isinstance(state.get("hosts"), dict):
        raise ValueError("host presence state hosts must be an object")
    positive_epoch(state.get("last_processed_epoch"))
    return state


def load_legacy_hosts(
    state_path: str | Path | None = None,
    log_path: str | Path | None = None,
) -> dict[str, dict]:
    known: dict[str, dict] = {}
    legacy_state = load_json(state_path or LEGACY_STATE_FILE, [])
    if not isinstance(legacy_state, list):
        raise ValueError("legacy hosts state must be a JSON list")
    for value in legacy_state:
        if isinstance(value, str) and value.strip():
            known[value.strip()] = {
                "first_seen_epoch": None,
                "inference_url": "",
            }

    path = Path(log_path or LEGACY_LOG_FILE)
    if not path.exists():
        return known
    with path.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            node_id = (row.get("node_id") or "").strip()
            if not node_id:
                continue
            try:
                epoch = positive_epoch(row.get("first_seen_epoch"))
            except ValueError:
                epoch = None
            item = known.setdefault(
                node_id,
                {"first_seen_epoch": None, "inference_url": ""},
            )
            previous = item.get("first_seen_epoch")
            if epoch is not None and (previous is None or epoch < previous):
                item["first_seen_epoch"] = epoch
            url = (row.get("inference_url") or "").strip()
            if url:
                item["inference_url"] = url
    return known


def profile_from_entry(entry: dict, total_weight: int | None) -> dict:
    weight = participant_weight(entry)
    share = (
        weight / total_weight * 100
        if weight is not None and total_weight is not None and total_weight > 0
        else None
    )
    return {
        "weight": weight,
        "network_share_percent": round(share, 6) if share is not None else None,
        "inference_url": participant_url(entry) or "",
        "models": participant_models(entry),
        "ml_node_count": participant_ml_node_count(entry),
    }


def new_period(epoch: int, *, started_after_gap: bool = False) -> dict:
    return {
        "from": epoch,
        "to": None,
        "end_exact": None,
        "absence_detected_epoch": None,
        "started_after_gap": started_after_gap,
    }


def baseline_state(snapshot: dict, legacy_hosts: dict[str, dict]) -> dict:
    epoch = snapshot["epoch"]
    total_for_share = (
        snapshot["total_weight"] if snapshot["total_weight_complete"] else None
    )
    hosts: dict[str, dict] = {}
    all_ids = set(legacy_hosts) | set(snapshot["by_id"])
    for node_id in sorted(all_ids):
        legacy = legacy_hosts.get(node_id, {})
        legacy_epoch = legacy.get("first_seen_epoch")
        active = node_id in snapshot["by_id"]
        entry = snapshot["by_id"].get(node_id)
        if entry is not None:
            profile = profile_from_entry(entry, total_for_share)
        else:
            profile = {
                "weight": None,
                "network_share_percent": None,
                "inference_url": legacy.get("inference_url", ""),
                "models": [],
                "ml_node_count": None,
            }
        hosts[node_id] = {
            "first_seen_epoch": (
                legacy_epoch
                if legacy_epoch is not None
                else (epoch if active else None)
            ),
            "legacy_first_seen_epoch": legacy_epoch,
            "legacy_known": node_id in legacy_hosts,
            "active": active,
            "last_seen_epoch": epoch if active else legacy_epoch,
            "periods": [new_period(epoch)] if active else [],
            "absence_from_epoch": None,
            "absence_detected_epoch": epoch if not active else None,
            "absence_history_complete": False if not active else None,
            "history_has_gaps": False,
            "last_profile": profile,
        }
    return {
        "schema_version": 1,
        "history_complete_from_epoch": epoch,
        "last_processed_epoch": epoch,
        "last_total_weight": snapshot["total_weight"],
        "last_total_weight_complete": snapshot["total_weight_complete"],
        "last_host_count": len(snapshot["entries"]),
        "observation_gaps": [],
        "hosts": hosts,
    }


def close_open_period(
    host: dict,
    *,
    last_seen_epoch: int,
    end_exact: bool,
    absence_detected_epoch: int | None,
) -> None:
    periods = host.setdefault("periods", [])
    if periods and periods[-1].get("to") is None:
        periods[-1]["to"] = last_seen_epoch
        periods[-1]["end_exact"] = end_exact
        periods[-1]["absence_detected_epoch"] = absence_detected_epoch


def weight_change_alert(
    previous_state: dict,
    snapshot: dict,
    threshold: float,
) -> dict | None:
    previous_epoch = previous_state["last_processed_epoch"]
    if snapshot["epoch"] != previous_epoch + 1:
        return None
    if not previous_state.get("last_total_weight_complete"):
        return None
    if not snapshot["total_weight_complete"]:
        return None
    previous_total = previous_state.get("last_total_weight")
    if not isinstance(previous_total, int) or previous_total <= 0:
        return None
    current_total = snapshot["total_weight"]
    change = current_total - previous_total
    change_percent = change / previous_total * 100
    if abs(change_percent) < threshold:
        return None
    return {
        "from_epoch": previous_epoch,
        "to_epoch": snapshot["epoch"],
        "previous_total": previous_total,
        "current_total": current_total,
        "change": change,
        "change_percent": change_percent,
        "previous_host_count": previous_state.get("last_host_count"),
        "current_host_count": len(snapshot["entries"]),
    }


def process_snapshot(state: dict, snapshot: dict, config: dict) -> dict:
    epoch = snapshot["epoch"]
    previous_epoch = state["last_processed_epoch"]
    if epoch <= previous_epoch:
        return {
            "ignored": True,
            "reason": "same_epoch" if epoch == previous_epoch else "stale_epoch",
            "state": state,
            "events": [],
            "weight_alert": None,
        }

    next_state = copy.deepcopy(state)
    hosts = next_state["hosts"]
    current_ids = set(snapshot["by_id"])
    sequential = epoch == previous_epoch + 1
    events: list[dict] = []
    warning = weight_change_alert(
        state,
        snapshot,
        float(config["network_weight_change_warning_percent"]),
    )
    if not sequential:
        next_state.setdefault("observation_gaps", []).append(
            {
                "from_epoch": previous_epoch + 1,
                "to_epoch": epoch - 1,
                "detected_at_epoch": epoch,
            }
        )

    for node_id, host in hosts.items():
        if host.get("active") and node_id not in current_ids:
            previous = copy.deepcopy(host)
            last_seen = host.get("last_seen_epoch") or previous_epoch
            close_open_period(
                host,
                last_seen_epoch=last_seen,
                end_exact=sequential,
                absence_detected_epoch=epoch,
            )
            host["active"] = False
            host["absence_from_epoch"] = epoch if sequential else None
            host["absence_detected_epoch"] = epoch
            host["absence_history_complete"] = sequential
            if not sequential:
                host["history_has_gaps"] = True
            events.append(
                {
                    "event": "left",
                    "node_id": node_id,
                    "previous": previous,
                    "host": copy.deepcopy(host),
                    "entry": None,
                }
            )
        elif not sequential and not host.get("active"):
            host["absence_history_complete"] = False
            host["history_has_gaps"] = True

    total_for_share = (
        snapshot["total_weight"] if snapshot["total_weight_complete"] else None
    )
    for node_id, entry in snapshot["by_id"].items():
        profile = profile_from_entry(entry, total_for_share)
        if node_id not in hosts:
            host = {
                "first_seen_epoch": epoch,
                "legacy_first_seen_epoch": None,
                "legacy_known": False,
                "active": True,
                "last_seen_epoch": epoch,
                "periods": [new_period(epoch, started_after_gap=not sequential)],
                "absence_from_epoch": None,
                "absence_detected_epoch": None,
                "absence_history_complete": None,
                "history_has_gaps": not sequential,
                "last_profile": profile,
            }
            hosts[node_id] = host
            events.append(
                {
                    "event": "new",
                    "node_id": node_id,
                    "previous": None,
                    "host": copy.deepcopy(host),
                    "entry": entry,
                }
            )
            continue

        host = hosts[node_id]
        if host.get("active"):
            if not sequential:
                close_open_period(
                    host,
                    last_seen_epoch=host.get("last_seen_epoch") or previous_epoch,
                    end_exact=False,
                    absence_detected_epoch=None,
                )
                host.setdefault("periods", []).append(
                    new_period(epoch, started_after_gap=True)
                )
                host["history_has_gaps"] = True
        else:
            previous = copy.deepcopy(host)
            host["active"] = True
            host.setdefault("periods", []).append(
                new_period(epoch, started_after_gap=not sequential)
            )
            events.append(
                {
                    "event": "returned",
                    "node_id": node_id,
                    "previous": previous,
                    "host": None,
                    "entry": entry,
                }
            )
            host["absence_from_epoch"] = None
            host["absence_detected_epoch"] = None
            host["absence_history_complete"] = None
            if not sequential:
                host["history_has_gaps"] = True
        host["last_seen_epoch"] = epoch
        host["last_profile"] = profile
        host["active"] = True
        if events and events[-1]["node_id"] == node_id and events[-1]["event"] == "returned":
            events[-1]["host"] = copy.deepcopy(host)

    next_state.update(
        {
            "last_processed_epoch": epoch,
            "last_total_weight": snapshot["total_weight"],
            "last_total_weight_complete": snapshot["total_weight_complete"],
            "last_host_count": len(snapshot["entries"]),
        }
    )
    return {
        "ignored": False,
        "reason": None,
        "state": next_state,
        "events": events,
        "weight_alert": warning,
    }


def epoch_range(start: int, end: int) -> str:
    return f"эпоха {start}" if start == end else f"эпохи {start}–{end}"


def returned_history_lines(
    previous: dict,
    current_epoch: int,
    history_from: int,
) -> list[str]:
    periods = previous.get("periods") or []
    lines: list[str] = []
    if periods:
        period = periods[-1]
        start = period.get("from")
        end = period.get("to") or previous.get("last_seen_epoch")
        if period.get("end_exact") and isinstance(start, int) and isinstance(end, int):
            lines.append(f"Ранее участвовал: {epoch_range(start, end)}")
        elif isinstance(end, int):
            lines.append(f"Последний раз был активен в эпохе {end}")
            detected = previous.get("absence_detected_epoch")
            if isinstance(detected, int):
                lines.append(f"Отсутствие обнаружено в эпохе {detected}")
    else:
        legacy_epoch = previous.get("legacy_first_seen_epoch")
        if isinstance(legacy_epoch, int):
            lines.append(f"Ранее фиксировался в эпохе {legacy_epoch}")
        else:
            lines.append("Ранее фиксировался до начала точной истории")
        lines.append(f"Точная история ведётся с эпохи {history_from}")

    absence_from = previous.get("absence_from_epoch")
    if previous.get("absence_history_complete") and isinstance(absence_from, int):
        if absence_from <= current_epoch - 1:
            lines.append(f"Отсутствовал: {epoch_range(absence_from, current_epoch - 1)}")
    return lines


def left_detail_lines(host: dict) -> list[str]:
    periods = host.get("periods") or []
    lines: list[str] = []
    if periods:
        period = periods[-1]
        start = period.get("from")
        end = period.get("to")
        if period.get("end_exact") and isinstance(start, int) and isinstance(end, int):
            lines.append(f"Последний период участия: {epoch_range(start, end)}")
        elif isinstance(end, int):
            lines.append(f"Последний раз был активен в эпохе {end}")
            detected = host.get("absence_detected_epoch")
            if isinstance(detected, int):
                lines.append(f"Отсутствие обнаружено в эпохе {detected}")
    profile = host.get("last_profile") or {}
    weight = profile.get("weight")
    share = profile.get("network_share_percent")
    lines.append(
        f"Последний вес: <b>{format_integer(weight)}</b>"
        if isinstance(weight, int)
        else "Последний вес: нет данных"
    )
    lines.append(
        f"Доля сети в последней эпохе: <b>{share:.1f}%</b>"
        if isinstance(share, (int, float))
        else "Доля сети в последней эпохе: нет данных"
    )
    return lines


def host_block(event: dict, result: dict, snapshot: dict) -> str:
    node_id = event["node_id"]
    if event["event"] == "left":
        lines = [f"• <code>{escape_html(node_id)}</code>"]
        lines.extend(f"  {line}" for line in left_detail_lines(event["host"]))
        return "\n".join(lines)

    detail_lines: Iterable[str] = ()
    if event["event"] == "returned":
        detail_lines = returned_history_lines(
            event["previous"],
            snapshot["epoch"],
            result["state"]["history_complete_from_epoch"],
        )
    return build_host_details(
        event["entry"],
        snapshot["ranks"].get(node_id),
        len(snapshot["entries"]),
        detail_lines=detail_lines,
        total_weight=(
            snapshot["total_weight"] if snapshot["total_weight_complete"] else None
        ),
    )


def chunk_messages(
    header: str,
    blocks: list[str],
    limit: int = TELEGRAM_SAFE_LIMIT,
) -> list[str]:
    if not blocks:
        return []
    groups: list[list[str]] = []
    current: list[str] = []
    for block in blocks:
        candidate = current + [block]
        preview = header + "\n\n" + "\n\n".join(candidate)
        if current and len(preview) > limit - 40:
            groups.append(current)
            current = [block]
        else:
            current = candidate
    groups.append(current)

    messages = []
    for index, group in enumerate(groups, 1):
        part = f" (часть {index}/{len(groups)})" if len(groups) > 1 else ""
        message = header + part + "\n\n" + "\n\n".join(group)
        if len(message) > limit:
            raise ValueError("one host event is too large for Telegram")
        messages.append(message)
    return messages


def event_messages(result: dict, snapshot: dict) -> list[str]:
    definitions = (
        ("new", "🆕", "Новый хост в сети Gonka", "Новые хосты в сети Gonka"),
        ("returned", "↩️", "Хост вернулся в сеть", "Хосты вернулись в сеть"),
        ("left", "👋", "Хост покинул активный набор", "Хосты покинули активный набор"),
    )
    messages: list[str] = []
    for event_type, icon, singular, plural in definitions:
        events = sorted(
            (item for item in result["events"] if item["event"] == event_type),
            key=lambda item: item["node_id"],
        )
        if not events:
            continue
        title = singular if len(events) == 1 else f"{plural} ({len(events)})"
        header = f"{icon} <b>{title} — эпоха {snapshot['epoch']}</b>"
        blocks = [host_block(item, result, snapshot) for item in events]
        messages.extend(chunk_messages(header, blocks))
    return messages


def weight_warning_message(alert: dict) -> str:
    change = alert["change"]
    percent = alert["change_percent"]
    sign = "+" if change >= 0 else "−"
    host_counts = (
        f"{alert['previous_host_count']} → {alert['current_host_count']}"
        if isinstance(alert.get("previous_host_count"), int)
        else f"нет данных → {alert['current_host_count']}"
    )
    return (
        "⚠️ <b>Резко изменился общий вес сети</b>\n\n"
        f"Эпохи: {alert['from_epoch']} → {alert['to_epoch']}\n"
        f"Было: <b>{format_integer(alert['previous_total'])}</b>\n"
        f"Стало: <b>{format_integer(alert['current_total'])}</b>\n"
        f"Изменение: <b>{sign}{format_integer(abs(change))} "
        f"({sign}{abs(percent):.1f}%)</b>\n"
        f"Хостов: {host_counts}"
    )


def append_event_log(
    events: list[dict],
    snapshot: dict,
    observed_at: str,
    path: str | Path | None = None,
) -> None:
    if not events:
        return
    log_path = Path(path or EVENT_LOG_FILE)
    existing: list[dict] = []
    if log_path.exists():
        with log_path.open(newline="", encoding="utf-8") as file:
            existing = list(csv.DictReader(file))
    total = snapshot["total_weight"] if snapshot["total_weight_complete"] else ""
    for event in events:
        profile = event["host"].get("last_profile") or {}
        existing.append(
            {
                "epoch": snapshot["epoch"],
                "event": event["event"],
                "node_id": event["node_id"],
                "observed_at_utc": observed_at,
                "weight": profile.get("weight") if profile.get("weight") is not None else "",
                "total_weight": total,
                "network_share_percent": (
                    profile.get("network_share_percent")
                    if profile.get("network_share_percent") is not None
                    else ""
                ),
                "inference_url": profile.get("inference_url", ""),
            }
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = log_path.with_name(log_path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=EVENT_LOG_FIELDS)
        writer.writeheader()
        writer.writerows(existing)
    temporary.replace(log_path)


def main() -> None:
    config = load_config()
    snapshot = fetch_snapshot()
    state = load_presence_state()
    if state is None:
        state = baseline_state(snapshot, load_legacy_hosts())
        save_json_atomic(PRESENCE_STATE_FILE, state, sort_keys=True)
        print(
            f"Initialized host presence baseline at epoch {snapshot['epoch']} with "
            f"{len(snapshot['entries'])} active host(s); no notifications sent."
        )
        return

    result = process_snapshot(state, snapshot, config)
    if result["ignored"]:
        print(
            f"Ignored {result['reason']} snapshot epoch {snapshot['epoch']}; "
            f"last processed epoch is {state['last_processed_epoch']}."
        )
        return

    messages = event_messages(result, snapshot)
    if result["weight_alert"]:
        messages.append(weight_warning_message(result["weight_alert"]))
    for message in messages:
        send_telegram_message(message)

    observed_at = utc_now()
    append_event_log(result["events"], snapshot, observed_at)
    save_json_atomic(PRESENCE_STATE_FILE, result["state"], sort_keys=True)
    counts = {
        name: sum(item["event"] == name for item in result["events"])
        for name in ("new", "returned", "left")
    }
    print(
        json.dumps(
            {
                "epoch": snapshot["epoch"],
                "events": counts,
                "messages": len(messages),
                "total_weight": snapshot["total_weight"],
                "total_weight_complete": snapshot["total_weight_complete"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - command-line boundary
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
