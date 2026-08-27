"""Census of public host devshard slots versus on-chain approved_versions.

Hourly oneshot. Telegram: one digest per epoch, then events on approved-list
changes or a shift of at least HOST_CHANGE_THRESHOLD hosts. Commands read the
latest snapshot; they do not walk the network.
"""

from __future__ import annotations

import concurrent.futures
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import requests

from bot_common import (
    fetch_json_with_fallback,
    format_snapshot_age,
    load_json,
    save_json_atomic,
    send_telegram_message,
    utc_now,
)
from upgrade_adoption_watcher import (
    fetch_active_snapshot,
    participant_identity,
    validate_public_url,
)


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "devshard.json"
PARAMS_URLS = (
    "https://node3.gonka.ai/chain-api/productscience/inference/inference/params",
    "http://node2.gonka.ai:8000/chain-api/productscience/inference/inference/params",
    "http://node1.gonka.ai:8000/chain-api/productscience/inference/inference/params",
)
LIVE_STATUSES = frozenset({"running", "ok", "healthy", "alive"})
HOST_CHANGE_THRESHOLD = 3
EVENT_DEBOUNCE_RUNS = 2
PROBE_TIMEOUT_SECONDS = 8
MAX_WORKERS = 16


def canonical_epoch(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def parse_approved_versions(payload: Any) -> list[str]:
    params = payload.get("params") if isinstance(payload, dict) else None
    escrow = (
        params.get("devshard_escrow_params") if isinstance(params, dict) else None
    )
    raw = escrow.get("approved_versions") if isinstance(escrow, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("devshard_escrow_params.approved_versions must be a non-empty list")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("approved_versions entry must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("approved version name is missing")
        name = name.strip()
        if name in names:
            raise ValueError(f"duplicate approved version: {name}")
        names.append(name)
    return names


def parse_healthz(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise ValueError("/devshard/healthz must be a list")
    slots: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        status = str(item.get("status") or "").strip().lower()
        if status and status not in LIVE_STATUSES:
            continue
        slot = name.strip()
        if slot not in slots:
            slots.append(slot)
    return slots


def fetch_approved_versions() -> tuple[list[str], str]:
    payload, source = fetch_json_with_fallback(
        PARAMS_URLS,
        timeout=20,
        validator=lambda value: parse_approved_versions(value),
    )
    names = parse_approved_versions(payload)
    print(f"Devshard params source: {source}")
    return names, source


def probe_devshard(url: str, session=requests) -> dict:
    try:
        public = validate_public_url(url)
    except ValueError as exc:
        return {"status": "invalid_url", "slots": [], "error": str(exc)}
    try:
        response = session.get(
            f"{public}/devshard/healthz",
            timeout=PROBE_TIMEOUT_SECONDS,
            headers={"User-Agent": "gonka-host-bot/1.0"},
        )
        response.raise_for_status()
        return {"status": "ok", "slots": parse_healthz(response.json())}
    except Exception as exc:  # noqa: BLE001 - one host must not fail the census
        return {
            "status": "unreachable",
            "slots": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def collect_probes(hosts: list[dict], session=requests) -> list[dict]:
    results: dict[str, dict] = {}
    if not hosts:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(probe_devshard, host["url"], session): host for host in hosts
        }
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            results[host["id"]] = future.result()
    probes = []
    for host in hosts:
        probe = results[host["id"]]
        probes.append(
            {
                "id": host["id"],
                "weight": host["weight"],
                "status": probe["status"],
                "slots": probe.get("slots") or [],
            }
        )
    return probes


def build_census(approved: list[str], probes: list[dict], network_hosts: int) -> dict:
    answered = [item for item in probes if item.get("status") == "ok"]
    slot_hosts: Counter[str] = Counter()
    for item in answered:
        for slot in item.get("slots") or []:
            slot_hosts[slot] += 1
    approved_set = set(approved)
    extra = {
        slot: count
        for slot, count in sorted(slot_hosts.items())
        if slot not in approved_set
    }
    complete = 0
    incomplete = 0
    combos: Counter[str] = Counter()
    for item in answered:
        have = set(item.get("slots") or [])
        key = "+".join(slot for slot in approved if slot in have)
        combos[key] += 1
        if approved and all(slot in have for slot in approved):
            complete += 1
        else:
            incomplete += 1
    return {
        "approved": list(approved),
        "network_hosts": network_hosts,
        "answered": len(answered),
        "unreachable": max(0, network_hosts - len(answered)),
        "slot_hosts": dict(slot_hosts),
        "complete_hosts": complete,
        "incomplete_hosts": incomplete,
        "extra_slots": extra,
        "approved_combos": {key: count for key, count in combos.items() if count},
    }


def event_signature(census: dict) -> list:
    approved = list(census.get("approved") or [])
    extra = [
        [slot, int(count)]
        for slot, count in sorted((census.get("extra_slots") or {}).items())
    ]
    slot_hosts = census.get("slot_hosts") or {}
    approved_counts = [[slot, int(slot_hosts.get(slot, 0))] for slot in approved]
    return [
        approved,
        extra,
        approved_counts,
        int(census.get("complete_hosts") or 0),
    ]


def noticeable_change(previous: dict | None, current: dict) -> str | None:
    """Return 'immediate', 'debounce', or None."""
    if not isinstance(previous, dict) or not previous:
        return None
    if list(previous.get("approved") or []) != list(current.get("approved") or []):
        return "immediate"
    if dict(previous.get("extra_slots") or {}) != dict(current.get("extra_slots") or {}):
        return "debounce"
    prev_slots = previous.get("slot_hosts") or {}
    cur_slots = current.get("slot_hosts") or {}
    names = set(previous.get("approved") or []) | set(current.get("approved") or [])
    for slot in names:
        if abs(int(cur_slots.get(slot, 0)) - int(prev_slots.get(slot, 0))) >= HOST_CHANGE_THRESHOLD:
            return "debounce"
    if abs(
        int(previous.get("complete_hosts") or 0) - int(current.get("complete_hosts") or 0)
    ) >= HOST_CHANGE_THRESHOLD:
        return "debounce"
    return None


def host_word_genitive(count: int) -> str:
    """After «у N» / «из N»: 1 хоста, 2 хостов, 21 хоста, 11 хостов."""
    n = abs(int(count)) % 100
    if n % 10 == 1 and n != 11:
        return "хоста"
    return "хостов"


def combo_label(key: str, approved: list[str]) -> str:
    parts = [part for part in key.split("+") if part]
    if not parts:
        names = "/".join(approved) if approved else "approved"
        return f"без {names}"
    if approved and set(parts) == set(approved) and len(parts) > 1:
        return "и " + ", и ".join(parts)
    if len(parts) == 1 and len(approved) > 1:
        return f"только {parts[0]}"
    return "+".join(parts)


def approved_combos(census: dict) -> dict[str, int]:
    stored = census.get("approved_combos")
    if isinstance(stored, dict) and stored:
        return {
            str(key): int(value)
            for key, value in stored.items()
            if int(value) > 0
        }
    approved = list(census.get("approved") or [])
    answered = int(census.get("answered") or 0)
    complete = int(census.get("complete_hosts") or 0)
    slot_hosts = census.get("slot_hosts") or {}
    if len(approved) == 2:
        left, right = approved
        only_left = max(0, int(slot_hosts.get(left, 0)) - complete)
        only_right = max(0, int(slot_hosts.get(right, 0)) - complete)
        neither = max(0, answered - complete - only_left - only_right)
        combos: dict[str, int] = {}
        if complete:
            combos[f"{left}+{right}"] = complete
        if only_left:
            combos[left] = only_left
        if only_right:
            combos[right] = only_right
        if neither:
            combos[""] = neither
        return combos
    if len(approved) == 1:
        name = approved[0]
        has = int(slot_hosts.get(name, 0))
        combos = {}
        if has:
            combos[name] = has
        rest = max(0, answered - has)
        if rest:
            combos[""] = rest
        return combos
    if complete and approved:
        return {"+".join(approved): complete}
    return {}


def format_census_body(census: dict) -> str:
    approved = census.get("approved") or []
    answered = int(census.get("answered") or 0)
    network = int(census.get("network_hosts") or 0)
    extra = census.get("extra_slots") or {}
    answered_line = (
        f"Ответили: {answered} из {network} {host_word_genitive(network)}"
    )
    if not approved:
        lines = ["Разрешённых слотов нет", answered_line]
    elif len(approved) == 1:
        lines = [f"Разрешён слот {approved[0]}", answered_line]
    else:
        lines = [f"Разрешены слоты {', '.join(approved)}", answered_line]
    combos = approved_combos(census)
    extra_bits = [
        f"{slot} у {count} {host_word_genitive(count)}"
        for slot, count in extra.items()
    ]
    unreachable = int(census.get("unreachable") or 0)
    body: list[str] = []
    if combos:
        body.append("у ответивших:")
        ordered = sorted(
            combos.items(),
            key=lambda item: (item[0].count("+") == 0, -item[1], item[0]),
        )
        for key, count in ordered:
            body.append(f"• {combo_label(key, approved)} — {count}")
    if extra_bits:
        body.append("лишние слоты (поверх approved): " + ", ".join(extra_bits))
    if unreachable:
        body.append(f"Не ответили: {unreachable}")
    if body:
        lines.append("")
        lines.extend(body)
    return "\n".join(lines).rstrip()


def format_digest_message(epoch, census: dict) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    return f"📊 Devshard{epoch_note}\n\n{format_census_body(census)}"


def format_event_message(epoch, census: dict, previous: dict | None) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    lines = [f"ℹ️ Devshard{epoch_note}", ""]
    if isinstance(previous, dict) and previous:
        old_approved = list(previous.get("approved") or [])
        new_approved = list(census.get("approved") or [])
        if old_approved != new_approved:
            lines.append(
                "approved: "
                + (", ".join(old_approved) or "нет")
                + " → "
                + (", ".join(new_approved) or "нет")
            )
        old_extra = dict(previous.get("extra_slots") or {})
        new_extra = dict(census.get("extra_slots") or {})
        if old_extra != new_extra:
            lines.append(
                "лишние слоты: "
                + (_extra_label(old_extra) or "нет")
                + " → "
                + (_extra_label(new_extra) or "нет")
            )
        prev_slots = previous.get("slot_hosts") or {}
        cur_slots = census.get("slot_hosts") or {}
        for slot in new_approved or old_approved:
            old = int(prev_slots.get(slot, 0))
            new = int(cur_slots.get(slot, 0))
            if old != new:
                lines.append(f"{slot}: {old} → {new} хостов")
        old_complete = int(previous.get("complete_hosts") or 0)
        new_complete = int(census.get("complete_hosts") or 0)
        if old_complete != new_complete:
            lines.append(f"полный набор: {old_complete} → {new_complete}")
        if len(lines) > 2:
            lines.append("")
    lines.append(format_census_body(census))
    return "\n".join(lines)


def _extra_label(extra: dict) -> str:
    return ", ".join(f"{slot}×{count}" for slot, count in extra.items())


def format_command_devshard_message(state: dict, *, now=None) -> str:
    if not isinstance(state, dict) or not state.get("approved"):
        if isinstance(state, dict) and state.get("last_error"):
            return (
                "📊 Devshard\n\n"
                f"Не удалось снять перепись: {state.get('last_error')}\n\n"
                f"{format_snapshot_age(state.get('checked_at'), now=now)}"
            )
        return "Снимка Devshard ещё нет: watcher не запускался."
    text = format_digest_message(state.get("epoch"), state)
    return f"{text}\n\n{format_snapshot_age(state.get('checked_at'), now=now)}"


def apply_tick(
    previous: dict,
    census: dict,
    *,
    epoch,
    now: str,
) -> tuple[dict, list[str]]:
    previous = previous if isinstance(previous, dict) else {}
    epoch = canonical_epoch(epoch)
    last_digest_epoch = canonical_epoch(previous.get("last_digest_epoch"))
    last_notified = previous.get("last_notified")
    reported = previous.get("event_reported_signature")
    candidate = previous.get("event_candidate_signature")
    runs = int(previous.get("event_candidate_runs") or 0)
    messages: list[str] = []

    send_digest = last_digest_epoch != epoch
    kind = noticeable_change(last_notified, census) if last_notified else None
    signature = event_signature(census)

    if send_digest:
        messages.append(format_digest_message(epoch, census))
        last_digest_epoch = epoch
        last_notified = census
        reported = None
        candidate = None
        runs = 0
    elif kind == "immediate":
        messages.append(format_event_message(epoch, census, last_notified))
        last_notified = census
        reported = signature
        candidate = None
        runs = 0
    elif kind == "debounce":
        runs = runs + 1 if candidate == signature else 1
        candidate = signature
        if runs >= EVENT_DEBOUNCE_RUNS and reported != signature:
            messages.append(format_event_message(epoch, census, last_notified))
            last_notified = census
            reported = signature
            candidate = None
            runs = 0
    else:
        candidate = None
        runs = 0

    state = {
        "schema_version": 1,
        "checked_at": now,
        "epoch": epoch,
        **census,
        "last_digest_epoch": last_digest_epoch,
        "last_notified": last_notified,
        "event_reported_signature": reported,
        "event_candidate_signature": candidate,
        "event_candidate_runs": runs,
        "last_error": None,
    }
    return state, messages


def main() -> None:
    previous = load_json(STATE_FILE, {})
    now = utc_now()
    try:
        approved, _source = fetch_approved_versions()
        entries, epoch = fetch_active_snapshot()
        epoch = canonical_epoch(epoch)
    except Exception as exc:  # noqa: BLE001 - keep last good snapshot
        print(f"ERROR: census sources unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(previous, dict) and previous:
            previous = dict(previous)
            previous["checked_at"] = now
            previous["last_error"] = f"{type(exc).__name__}: {exc}"
            save_json_atomic(STATE_FILE, previous, sort_keys=True)
        raise

    parsed = []
    seen_ids = set()
    for entry in entries:
        pid, weight, url = participant_identity(entry)
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        if not url:
            continue
        parsed.append({"id": pid, "weight": weight if weight is not None else 0, "url": url})

    network_hosts = len(seen_ids) if seen_ids else len(entries)
    probes = collect_probes(parsed)
    census = build_census(approved, probes, network_hosts)
    state, messages = apply_tick(previous, census, epoch=epoch, now=now)
    for message in messages:
        send_telegram_message(message, parse_mode=None)
    save_json_atomic(STATE_FILE, state, sort_keys=True)
    print(
        f"epoch={epoch} answered={census['answered']}/{census['network_hosts']} "
        f"messages={len(messages)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
