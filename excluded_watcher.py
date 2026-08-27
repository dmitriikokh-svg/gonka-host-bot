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
    format_snapshot_age,
    load_json,
    participant_id,
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


def parse_exclusion_block(entry):
    block = entry.get("exclusion_block_height") if isinstance(entry, dict) else None
    if isinstance(block, bool):
        return None
    try:
        block = int(block)
    except (TypeError, ValueError):
        return None
    return block if block >= 0 else None


def reason_label(reason) -> str:
    if isinstance(reason, str) and reason.strip():
        return REASON_LABELS.get(reason, reason)
    return "нет данных"


def exclusion_detail_lines(entry):
    payload = entry if isinstance(entry, dict) else {}
    block = parse_exclusion_block(payload)
    block_text = format_integer(block) if block is not None else "нет данных"
    return (
        f"Причина: <b>{escape_html(reason_label(payload.get('reason')))}</b>",
        f"Блок исключения: <code>{escape_html(block_text)}</code>",
    )


def excluded_ids_from_state(value):
    if value is None:
        return None
    if isinstance(value, list):
        return {item.strip() for item in value if isinstance(item, str) and item.strip()}
    if not isinstance(value, dict):
        raise ValueError("excluded state must be a JSON list or snapshot object")
    records = value.get("excluded")
    if not isinstance(records, list):
        raise ValueError("excluded snapshot must contain an excluded list")
    ids = set()
    for item in records:
        if isinstance(item, str) and item.strip():
            ids.add(item.strip())
            continue
        if isinstance(item, dict):
            node_id = item.get("id") or item.get("address")
            if isinstance(node_id, str) and node_id.strip():
                ids.add(node_id.strip())
    return ids


def load_previous_ids():
    return excluded_ids_from_state(load_json(STATE_FILE))


def build_excluded_snapshot(snapshot, *, checked_at=None) -> dict:
    excluded_entries = snapshot.get("excluded") or []
    active_entries = snapshot.get("active") or []
    if not isinstance(excluded_entries, list):
        excluded_entries = []
    if not isinstance(active_entries, list):
        active_entries = []
    excluded_by_id = {participant_id(entry): entry for entry in excluded_entries}
    active_by_id = {participant_id(entry): entry for entry in active_entries}
    ranks = participant_weight_ranks(active_entries)
    total_weight, total_weight_complete = participant_total_weight(active_entries)
    participant_count = len(active_by_id)
    records = []
    for node_id in sorted(excluded_by_id):
        excluded_entry = excluded_by_id[node_id]
        host_entry = active_by_id.get(node_id, excluded_entry)
        weight = participant_weight(host_entry)
        share = None
        if (
            weight is not None
            and total_weight_complete
            and total_weight > 0
        ):
            share = round(weight / total_weight * 100, 4)
        records.append(
            {
                "id": node_id,
                "reason": (
                    excluded_entry.get("reason")
                    if isinstance(excluded_entry, dict)
                    else None
                ),
                "exclusion_block_height": parse_exclusion_block(excluded_entry),
                "weight": weight,
                "network_share_percent": share,
                "rank": ranks.get(node_id),
                "participant_count": participant_count or None,
                "models": participant_models(host_entry),
                "ml_node_count": participant_ml_node_count(host_entry),
                "inference_url": participant_url(host_entry) or None,
            }
        )
    return {
        "schema_version": 1,
        "checked_at": checked_at or utc_now(),
        "epoch": snapshot.get("epoch"),
        "excluded": records,
    }


def save_state(state):
    if isinstance(state, (set, list, tuple)):
        raise TypeError("excluded state must be a snapshot object")
    save_json_atomic(STATE_FILE, state, sort_keys=True)


def format_command_excluded_message(state, *, now=None) -> str:
    if state is None:
        return "Снимка исключений ещё нет: watcher не запускался."
    if isinstance(state, list):
        epoch_note = ""
        records = [{"id": item} for item in state if isinstance(item, str) and item.strip()]
        checked_at = None
        legacy = True
    elif isinstance(state, dict):
        epoch = state.get("epoch")
        epoch_note = f", эпоха {epoch}" if epoch is not None else ""
        raw = state.get("excluded")
        records = raw if isinstance(raw, list) else []
        checked_at = state.get("checked_at")
        legacy = False
    else:
        return "Снимок исключений повреждён."

    lines = [f"📊 Исключены после CPoC{epoch_note}", ""]
    if not records:
        lines.append("Сейчас никто не в excluded_participants.")
    else:
        cards = []
        for item in records:
            if isinstance(item, str):
                item = {"id": item}
            if not isinstance(item, dict):
                continue
            node_id = item.get("id") or item.get("address") or "нет id"
            card = [f"• {node_id}"]
            reason = item.get("reason")
            block = parse_exclusion_block(item)
            if reason or block is not None:
                card.append(f"  Причина: {reason_label(reason)}")
                card.append(
                    f"  Блок: {format_integer(block) if block is not None else 'нет данных'}"
                )
            weight = item.get("weight")
            share = item.get("network_share_percent")
            rank = item.get("rank")
            count = item.get("participant_count")
            weight_bits = []
            if isinstance(weight, int) and not isinstance(weight, bool):
                bit = f"Вес: {format_integer(weight)}"
                if isinstance(share, (int, float)) and not isinstance(share, bool):
                    bit += f" ({share:.1f}%)"
                weight_bits.append(bit)
            if rank is not None and count:
                weight_bits.append(f"место {rank} из {count}")
            if weight_bits:
                card.append("  " + ", ".join(weight_bits))
            models = item.get("models") or []
            ml_nodes = item.get("ml_node_count")
            extras = []
            if models:
                extras.append(", ".join(str(model) for model in models))
            if ml_nodes is not None:
                extras.append(f"{ml_nodes} ML-ноды")
            if extras:
                card.append("  " + " · ".join(extras))
            cards.append("\n".join(card))
        if not cards:
            lines.append("Сейчас никто не в excluded_participants.")
        else:
            lines.append("\n\n".join(cards))
        if legacy:
            lines.extend(
                [
                    "",
                    "Причина и блок появятся после следующего прогона watcher.",
                ]
            )
    lines.extend(["", format_snapshot_age(checked_at, now=now)])
    return "\n".join(lines)


def main():
    snapshot = fetch_snapshot()
    excluded_entries = snapshot["excluded"]
    active_entries = snapshot["active"]
    excluded_by_id = {participant_id(entry): entry for entry in excluded_entries}
    active_by_id = {participant_id(entry): entry for entry in active_entries}
    current_ids = set(excluded_by_id)
    previous_ids = load_previous_ids()
    state = build_excluded_snapshot(snapshot)

    if previous_ids is None:
        print(
            f"First run. Saving baseline of {len(current_ids)} excluded "
            "participant(s), no notification sent."
        )
        save_state(state)
        return

    new_ids = current_ids - previous_ids

    if new_ids:
        ranks = participant_weight_ranks(active_entries)
        total_weight, total_weight_complete = participant_total_weight(active_entries)
        participant_count = len(active_by_id)
        lines = "\n\n".join(
            build_host_details(
                active_by_id.get(node_id, excluded_by_id[node_id]),
                ranks.get(node_id),
                participant_count,
                detail_lines=exclusion_detail_lines(excluded_by_id[node_id]),
                total_weight=total_weight if total_weight_complete else None,
            )
            for node_id in sorted(new_ids)
        )
        count = len(new_ids)
        if count == 1:
            title = "Исключён после Confirmation PoC"
        else:
            title = f"Исключены после Confirmation PoC ({count})"
        epoch = snapshot.get("epoch")
        epoch_note = (
            f" — эпоха {escape_html(epoch)}" if epoch is not None else ""
        )
        message = f"⚠️ <b>{title}{epoch_note}</b>\n\n{lines}"
        send_telegram_message(message)
        print(f"Sent alert for {len(new_ids)} newly excluded participant(s).")
    else:
        print(f"No new exclusions. Current total excluded: {len(current_ids)}.")

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
