"""
Gonka cPoC exclusion watcher.

Polls the participants endpoint frequently and watches the
`excluded_participants` field. Sends a Telegram alert as soon as a new
participant shows up there (i.e. got excluded after a Confirmation PoC
round), so a human doesn't have to keep refreshing a dashboard manually.

Meant to run more often than the new-host bot (every few minutes),
since cPoC rounds happen much more frequently than new hosts joining.
"""

import json
import sys
from pathlib import Path

from bot_common import (
    build_host_details,
    escape_html,
    fetch_json_with_fallback,
    format_integer,
    load_json,
    participant_id,
    participant_weight_ranks,
    save_json_atomic,
    send_telegram_message,
)

ROOT = Path(__file__).resolve().parent
PARTICIPANTS_URLS = (
    "https://node3.gonka.ai/v1/epochs/current/participants",
    "http://node2.gonka.ai:8000/v1/epochs/current/participants",
    "http://node1.gonka.ai:8000/v1/epochs/current/participants",
)
STATE_FILE = ROOT / "state" / "excluded.json"
REASON_LABELS = {
    "failed_confirmation_poc": "не пройден Confirmation PoC",
}


def validate_excluded_response(data):
    entries = data.get("excluded_participants") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        raise ValueError(
            "Could not find an excluded_participants list in the response.\n"
            f"Preview (truncated):\n{preview}"
        )


def fetch_snapshot():
    data, source = fetch_json_with_fallback(
        PARTICIPANTS_URLS,
        timeout=30,
        validator=validate_excluded_response,
    )
    print(f"Participants source: {source}")
    active = data.get("active_participants", {})
    active_entries = active.get("participants", []) if isinstance(active, dict) else []
    if not isinstance(active_entries, list):
        active_entries = []
    epoch = None
    if isinstance(active, dict):
        epoch = active.get("epoch_id") or active.get("epoch_group_id")
    return {
        "excluded": data["excluded_participants"] or [],
        "active": active_entries,
        "epoch": epoch,
    }


def exclusion_detail_lines(entry):
    reason = entry.get("reason") if isinstance(entry, dict) else None
    if isinstance(reason, str) and reason:
        reason_text = REASON_LABELS.get(reason, reason)
    else:
        reason_text = "нет данных"

    block = entry.get("exclusion_block_height") if isinstance(entry, dict) else None
    if isinstance(block, bool):
        block = None
    try:
        block = int(block)
    except (TypeError, ValueError):
        block = None
    block_text = format_integer(block) if block is not None and block >= 0 else "нет данных"

    return (
        f"Причина: <b>{escape_html(reason_text)}</b>",
        f"Блок исключения: <code>{escape_html(block_text)}</code>",
    )


def load_previous_ids():
    value = load_json(STATE_FILE)
    if value is None:
        return None  # signals "first run"
    if not isinstance(value, list):
        raise ValueError("excluded state must be a JSON list")
    return set(value)


def save_state(ids):
    save_json_atomic(STATE_FILE, sorted(ids))


def main():
    snapshot = fetch_snapshot()
    excluded_entries = snapshot["excluded"]
    active_entries = snapshot["active"]
    excluded_by_id = {participant_id(entry): entry for entry in excluded_entries}
    active_by_id = {participant_id(entry): entry for entry in active_entries}
    current_ids = set(excluded_by_id)
    previous_ids = load_previous_ids()

    if previous_ids is None:
        print(f"First run. Saving baseline of {len(current_ids)} excluded participant(s), no notification sent.")
        save_state(current_ids)
        return

    new_ids = current_ids - previous_ids

    if new_ids:
        ranks = participant_weight_ranks(active_entries)
        participant_count = len(active_by_id)
        lines = "\n\n".join(
            build_host_details(
                active_by_id.get(node_id, excluded_by_id[node_id]),
                ranks.get(node_id),
                participant_count,
                detail_lines=exclusion_detail_lines(excluded_by_id[node_id]),
            )
            for node_id in sorted(new_ids)
        )
        epoch = snapshot.get("epoch")
        epoch_note = f" (эпоха {escape_html(epoch)})" if epoch is not None else ""
        message = (
            f"\u26A0\uFE0F \u041d\u043e\u0432\u043e\u0435 \u0438\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 "
            f"\u043f\u043e\u0441\u043b\u0435 cPoC ({len(new_ids)}){epoch_note}:\n{lines}"
        )
        send_telegram_message(message)
        print(f"Sent alert for {len(new_ids)} newly excluded participant(s).")
    else:
        print(f"No new exclusions. Current total excluded: {len(current_ids)}.")

    save_state(current_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
