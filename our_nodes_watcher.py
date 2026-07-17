"""Monitor the team's Gonka host nodes.

The watcher combines three independent signals:
  * the configured participant address is present in the current participants;
  * the configured public endpoint answers the configured health endpoint.
  * the participant's authoritative current_epoch_stats.confirmationPoCRatio.

One scheduled run performs several HTTP attempts. A node is alerted only when
all attempts fail in the same run. State is persisted between GitHub Actions
runs so recovery alerts are emitted once and repeated failures stay quiet.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config" / "our_nodes.json"
STATE_FILE = ROOT / "state" / "our_nodes_state.json"
RATIO_METRIC_VERSION = "confirmation_poc_ratio_v1"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")


def load_config() -> dict:
    with CONFIG_FILE.open(encoding="utf-8") as f:
        config = json.load(f)

    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("config must contain a non-empty nodes list")

    for node in nodes:
        for field in ("name", "participant_address", "endpoint"):
            if not isinstance(node.get(field), str) or not node[field].strip():
                raise ValueError(f"node is missing required field: {field}")
        validate_public_http_url(node["endpoint"])

    return config


def validate_public_http_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid endpoint URL: {raw_url!r}")

    # These are operator-owned, reviewed config entries. Rejecting literal
    # private/loopback targets still prevents an accidental unsafe config from
    # turning the public GitHub runner into an SSRF proxy.
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError(f"private or loopback endpoint is not allowed: {raw_url!r}")

    return parsed.geturl().rstrip("/")


def fetch_participants(urls: list[str], timeout: int) -> list[dict]:
    errors = []
    for url in urls:
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            participants = data.get("active_participants", {}).get("participants")
            if not isinstance(participants, list):
                raise ValueError("participants list is missing")
            print(f"Participants source: {url}")
            return [entry for entry in participants if isinstance(entry, dict)]
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")

    raise RuntimeError("all participants sources failed: " + " | ".join(errors))


def fetch_epoch(urls: list[str], timeout: int) -> int | None:
    for url in urls:
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            value = data.get("latest_epoch", {}).get("index")
            if value is not None:
                return int(value)
        except Exception as exc:
            print(f"WARNING: epoch source failed {url}: {type(exc).__name__}: {exc}")
    return None


def participant_map(entries: list[dict]) -> dict[str, dict]:
    result = {}
    for entry in entries:
        address = entry.get("index") or entry.get("participant_id") or entry.get("address")
        if address:
            result[str(address)] = entry
    return result


def participant_weight(entry: dict | None):
    if not entry:
        return None
    value = entry.get("weight")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def participant_confirmation_ratio(entry: dict | None):
    """Return authoritative cPoC ratio as a percentage, or None.

    The chain exposes current_epoch_stats.confirmationPoCRatio as a ratio in
    the 0..1 range. Accept a percentage-shaped value as a compatibility
    fallback because some dashboard/API versions serialize it differently.
    """
    if not entry:
        return None
    stats = entry.get("current_epoch_stats")
    if not isinstance(stats, dict):
        return None
    value = stats.get("confirmationPoCRatio")
    if value is None:
        value = stats.get("confirmation_poc_ratio")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value * 100 if value <= 1 else value


def check_endpoint(node: dict, health_path: str, timeout: int, retries: int, delay: int) -> dict:
    endpoint = validate_public_http_url(node["endpoint"])
    url = endpoint + "/" + health_path.lstrip("/")
    last_error = None

    for attempt in range(1, retries + 1):
        started = time.monotonic()
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "gonka-our-nodes-monitor/1.0"},
            )
            response.raise_for_status()
            # /v1/versions is expected to be JSON. This also avoids treating a
            # proxy's HTML 200 page as a healthy node.
            response.json()
            return {
                "ok": True,
                "http_status": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "attempt": attempt,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(delay)

    return {
        "ok": False,
        "error": last_error or "unknown endpoint error",
        "attempts": retries,
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"nodes": {}}
    with STATE_FILE.open(encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state.get("nodes"), dict):
        return {"nodes": {}}
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    temp_file.replace(STATE_FILE)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def escape_text(value) -> str:
    return html.escape(str(value), quote=False)


def send_telegram_message(text: str) -> None:
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if THREAD_ID:
        payload["message_thread_id"] = int(THREAD_ID)
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=15,
    )
    response.raise_for_status()


def build_alert(node: dict, result: dict, now: str) -> str:
    return (
        "🔴 <b>Нода недоступна</b>\n\n"
        f"Имя: <code>{escape_text(node['name'])}</code>\n"
        f"Адрес: <code>{escape_text(node['participant_address'])}</code>\n"
        f"Роль: {escape_text(node.get('role', 'unknown'))}\n"
        f"Причина: <code>{escape_text(result['reason'])}</code>\n"
        f"Проверка: {escape_text(now)}\n"
        f"Детали: <code>{escape_text(result.get('details', ''))}</code>"
    )


def build_recovery(node: dict, previous: dict, result: dict, now: str) -> str:
    started = previous.get("first_failed_at", "unknown")
    return (
        "🟢 <b>Нода восстановлена</b>\n\n"
        f"Имя: <code>{escape_text(node['name'])}</code>\n"
        f"Адрес: <code>{escape_text(node['participant_address'])}</code>\n"
        f"Недоступность с: {escape_text(started)}\n"
        f"Восстановлена: {escape_text(now)}\n"
        f"Проверка endpoint: {escape_text(result.get('details', 'ok'))}"
    )


def build_weight_alert(node: dict, result: dict, epoch: int | None) -> str:
    epoch_text = str(epoch) if epoch is not None else "unknown"
    return (
        "⚠️ <b>Confirmation PoC ratio упал</b>\n\n"
        f"Нода: <code>{escape_text(node['name'])}</code> "
        f"(<code>{escape_text(node['participant_address'])}</code>)\n"
        f"Confirmation PoC ratio: <b>{result['weight_ratio']:.2f}%</b>\n"
        f"Вес ноды: {result['weight']}\n"
        f"Эпоха: {escape_text(epoch_text)}\n"
        f"Порог: {result['ratio_alert_threshold']:.2f}%"
    )


def build_weight_recovery(node: dict, result: dict, epoch: int | None) -> str:
    epoch_text = str(epoch) if epoch is not None else "unknown"
    return (
        "✅ <b>Confirmation PoC ratio восстановился</b>\n\n"
        f"Нода: <code>{escape_text(node['name'])}</code>\n"
        f"Confirmation PoC ratio: <b>{result['weight_ratio']:.2f}%</b>\n"
        f"Вес ноды: {result['weight']}\n"
        f"Эпоха: {escape_text(epoch_text)}"
    )


def inspect_node(
    node: dict,
    participants: dict[str, dict],
    config: dict,
) -> dict:
    entry = participants.get(node["participant_address"])
    weight = participant_weight(entry)
    confirmation_ratio = participant_confirmation_ratio(entry)
    endpoint = check_endpoint(
        node,
        config.get("health_path", "/v1/versions"),
        int(config.get("request_timeout_seconds", 10)),
        int(config.get("health_retries", 3)),
        int(config.get("retry_delay_seconds", 2)),
    )

    if not entry:
        return {
            "ok": False,
            "reason": "participant_absent",
            "details": "address not found in current participants",
            "endpoint": endpoint,
            "weight": None,
            "weight_ratio": None,
        }
    if not endpoint["ok"]:
        return {
            "ok": False,
            "reason": "endpoint_unhealthy",
            "details": endpoint.get("error", "endpoint check failed"),
            "weight": weight,
            "weight_ratio": confirmation_ratio,
            "endpoint": endpoint,
        }

    return {
        "ok": True,
        "reason": "ok",
        "details": f"HTTP {endpoint['http_status']} in {endpoint['latency_ms']} ms",
        "weight": weight,
        "weight_ratio": confirmation_ratio,
        "endpoint": endpoint,
    }


def main() -> None:
    config = load_config()
    participant_urls = config.get("participants_urls")
    if not isinstance(participant_urls, list) or not participant_urls:
        # Backward-compatible fallback for an older local config.
        participant_urls = [config["participants_url"]]
    entries = fetch_participants(participant_urls, 30)
    participants = participant_map(entries)
    epoch_urls = config.get("epoch_urls", [])
    epoch = fetch_epoch(epoch_urls, 15) if isinstance(epoch_urls, list) else None
    ratio_alert_threshold = float(config.get("weight_ratio_alert_below_percent", 25.0))
    ratio_recovery_threshold = float(
        config.get("weight_ratio_recovery_above_percent", ratio_alert_threshold)
    )
    if ratio_recovery_threshold < ratio_alert_threshold:
        raise ValueError("weight ratio recovery threshold must be >= alert threshold")
    state = load_state()
    now = utc_now()
    alerts = []

    for node in config["nodes"]:
        node_id = node["name"]
        previous = state["nodes"].get(node_id, {"status": "unknown"})
        result = inspect_node(node, participants, config)
        result["ratio_alert_threshold"] = ratio_alert_threshold

        current_status = "up" if result["ok"] else "down"
        previous_status = previous.get("status", "unknown")

        if current_status == "down" and previous_status != "down":
            alerts.append(build_alert(node, {**result, "reason": result["reason"]}, now))
        elif current_status == "up" and previous_status == "down":
            alerts.append(build_recovery(node, previous, result, now))

        # Reset the old state once after migrating from the incorrect
        # network-share calculation to the authoritative cPoC ratio.
        previous_ratio_alerted = (
            previous.get("ratio_metric_version") == RATIO_METRIC_VERSION
            and bool(previous.get("weight_ratio_alerted", False))
        )
        ratio = result.get("weight_ratio")
        ratio_alerted = previous_ratio_alerted
        if ratio is not None:
            if not previous_ratio_alerted and ratio < ratio_alert_threshold:
                alerts.append(build_weight_alert(node, result, epoch))
                ratio_alerted = True
            elif previous_ratio_alerted and ratio >= ratio_recovery_threshold:
                alerts.append(build_weight_recovery(node, result, epoch))
                ratio_alerted = False

        state["nodes"][node_id] = {
            "status": current_status,
            "last_checked_at": now,
            "first_failed_at": (
                previous.get("first_failed_at", now)
                if current_status == "down"
                else None
            ),
            "participant_present": node["participant_address"] in participants,
            "weight": result.get("weight"),
            "weight_ratio": result.get("weight_ratio"),
            "weight_ratio_alerted": ratio_alerted,
            "ratio_metric_version": RATIO_METRIC_VERSION,
            "epoch": epoch,
            "reason": result.get("reason"),
            "details": result.get("details"),
        }

        print(
            f"{node_id}: {current_status}; reason={result.get('reason')}; "
            f"weight={result.get('weight')}; ratio={result.get('weight_ratio')}%"
        )

    for message in alerts:
        send_telegram_message(message)
    if alerts:
        print(f"Sent {len(alerts)} Telegram alert(s).")
    else:
        print("No state changes; no Telegram messages sent.")

    state["checked_at"] = now
    state["participant_count"] = len(participants)
    state["epoch"] = epoch
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
