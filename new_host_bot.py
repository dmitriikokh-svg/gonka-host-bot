"""
Gonka new-host notifier.

Polls the participants endpoint, compares against previously saved state,
and sends a Telegram alert for newly appeared hosts. Also appends every
newly seen host to a running CSV table (state/host_log.csv) with the
epoch it was first spotted in, so there's a persistent history to refer
back to -- not just a live "current snapshot" diff.
"""

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bot_common import (
    escape_html,
    fetch_json_with_fallback,
    load_json,
    save_json_atomic,
    send_telegram_message,
)

ROOT = Path(__file__).resolve().parent
PARTICIPANTS_URLS = (
    "https://node3.gonka.ai/v1/epochs/current/participants",
    "http://node2.gonka.ai:8000/v1/epochs/current/participants",
    "http://node1.gonka.ai:8000/v1/epochs/current/participants",
)
EPOCH_URLS = (
    "https://node3.gonka.ai/v1/epochs/latest",
    "http://node2.gonka.ai:8000/v1/epochs/latest",
    "http://node1.gonka.ai:8000/v1/epochs/latest",
)
STATE_FILE = ROOT / "state" / "hosts.json"
LOG_FILE = ROOT / "state" / "host_log.csv"


def validate_participants_response(data):
    entries = (
        data.get("active_participants", {}).get("participants")
        if isinstance(data, dict)
        else None
    )
    if not isinstance(entries, list):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        raise ValueError(
            "Could not find participants list under active_participants.participants.\n"
            f"Preview:\n{preview}"
        )


def fetch_participants():
    data, source = fetch_json_with_fallback(
        PARTICIPANTS_URLS,
        timeout=30,
        validator=validate_participants_response,
    )
    print(f"Participants source: {source}")
    return data["active_participants"]["participants"]


def fetch_current_epoch():
    try:
        data, source = fetch_json_with_fallback(EPOCH_URLS, timeout=15)
        print(f"Epoch source: {source}")
        return data.get("latest_epoch", {}).get("index")
    except Exception as exc:  # noqa: BLE001 - epoch is optional for this alert
        print(f"WARNING: could not fetch epoch: {type(exc).__name__}: {exc}")
        return None


def participant_id(entry):
    # "index" is the confirmed real field name (gonka1... address).
    for key in ("index", "participant_id", "address", "id", "inference_url"):
        if isinstance(entry, dict) and key in entry:
            return str(entry[key])
    return json.dumps(entry, sort_keys=True)


def participant_url(entry):
    if isinstance(entry, dict):
        return entry.get("inference_url", "")
    return ""


def participant_weight(entry):
    if not isinstance(entry, dict):
        return None
    value = entry.get("weight")
    if isinstance(value, bool):
        return None
    try:
        weight = int(value)
    except (TypeError, ValueError):
        return None
    return weight if weight >= 0 else None


def participant_models(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("models"), list):
        return []

    models = []
    for value in entry["models"]:
        if not isinstance(value, str) or not value.strip():
            continue
        model = value.strip().rstrip("/").rsplit("/", 1)[-1]
        if model and model not in models:
            models.append(model)
    return models


def participant_ml_node_count(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("ml_nodes"), list):
        return None

    count = 0
    for group in entry["ml_nodes"]:
        nodes = group.get("ml_nodes") if isinstance(group, dict) else None
        if isinstance(nodes, list):
            count += sum(1 for node in nodes if isinstance(node, dict))
    return count


def participant_weight_ranks(entries):
    weighted = []
    for entry in entries:
        weight = participant_weight(entry)
        if weight is not None:
            weighted.append((participant_id(entry), weight))

    return {
        node_id: 1 + sum(other_weight > weight for _, other_weight in weighted)
        for node_id, weight in weighted
    }


def format_integer(value):
    return f"{value:,}".replace(",", " ")


def build_host_details(entry, rank, participant_count):
    node_id = participant_id(entry)
    weight = participant_weight(entry)
    models = participant_models(entry)
    ml_node_count = participant_ml_node_count(entry)
    url = participant_url(entry)

    weight_text = f"<b>{format_integer(weight)}</b>" if weight is not None else "нет данных"
    rank_text = (
        f"<b>{rank} из {participant_count}</b>"
        if rank is not None
        else "нет данных"
    )
    models_text = (
        ", ".join(f"<code>{escape_html(model)}</code>" for model in models)
        if models
        else "нет данных"
    )
    ml_nodes_text = (
        f"<b>{ml_node_count}</b>" if ml_node_count is not None else "нет данных"
    )
    url_text = f"<code>{escape_html(url)}</code>" if url else "нет данных"

    return (
        f"• <code>{escape_html(node_id)}</code>\n"
        f"  Вес: {weight_text}\n"
        f"  Место по весу: {rank_text}\n"
        f"  Модели: {models_text}\n"
        f"  ML-ноды: {ml_nodes_text}\n"
        f"  API: {url_text}"
    )


def load_previous_ids():
    value = load_json(STATE_FILE)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("hosts state must be a JSON list")
    return set(value)


def save_state(ids):
    save_json_atomic(STATE_FILE, sorted(ids))


def append_to_log(rows):
    """rows: list of (node_id, epoch_index, inference_url) tuples."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["node_id", "first_seen_epoch", "first_seen_utc", "inference_url"])
        now = datetime.now(timezone.utc).isoformat()
        for node_id, epoch, url in rows:
            writer.writerow([node_id, epoch, now, url])


def main():
    entries = fetch_participants()
    by_id = {participant_id(e): e for e in entries}
    current_ids = set(by_id.keys())
    previous_ids = load_previous_ids()

    if previous_ids is None:
        print(f"First run. Saving baseline of {len(current_ids)} host(s), no notification sent.")
        save_state(current_ids)
        return

    new_ids = current_ids - previous_ids

    if new_ids:
        epoch = fetch_current_epoch()
        log_rows = [(nid, epoch, participant_url(by_id[nid])) for nid in new_ids]
        append_to_log(log_rows)

        ranks = participant_weight_ranks(entries)
        lines = "\n\n".join(
            build_host_details(by_id[node_id], ranks.get(node_id), len(current_ids))
            for node_id in sorted(new_ids)
        )
        epoch_note = f" (\u044d\u043f\u043e\u0445\u0430 {epoch})" if epoch is not None else ""
        message = f"\U0001F195 \u041d\u043e\u0432\u044b\u0439 \u0445\u043e\u0441\u0442(\u044b) \u0432 \u0441\u0435\u0442\u0438 Gonka ({len(new_ids)}){epoch_note}:\n{lines}"
        send_telegram_message(message)
        print(f"Sent notification for {len(new_ids)} new host(s). Logged to {LOG_FILE}.")
    else:
        print(f"No new hosts. Current total: {len(current_ids)}.")

    save_state(current_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
