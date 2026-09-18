"""Census of broker gateway protocol slots (v3/v4), not binary versions.

Twos-hourly oneshot. Reconstructs bind protocol from host
``/devshard/<slot>/stats/shards`` plus on-chain escrow creator. Telegram: one
digest per epoch, then an event when a broker's slot set changes. Commands
read the snapshot; they do not walk the network.
"""

from __future__ import annotations

import concurrent.futures
import sys
from collections import defaultdict
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
from devshard_watcher import (
    PARAMS_URLS,
    canonical_epoch,
    combo_label,
    parse_approved_versions,
)
from upgrade_adoption_watcher import (
    fetch_active_snapshot,
    participant_identity,
    validate_public_url,
)


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "gateway.json"
LABELS_FILE = ROOT / "config" / "gateway_labels.json"
ESCROW_URL_PREFIXES = (
    "https://node3.gonka.ai/chain-api/productscience/inference/inference/devshard_escrow/",
    "http://node2.gonka.ai:8000/chain-api/productscience/inference/inference/devshard_escrow/",
    "http://node1.gonka.ai:8000/chain-api/productscience/inference/inference/devshard_escrow/",
)
HEARTBEAT_OVERVIEW_URLS = (
    "https://inference.dahl.global/api/v1/heartbeat/overview",
)
LEADERBOARD_URLS = (
    "https://inference.dahl.global/api/v1/inference/leaderboard"
    "?dimension=gateway&range=24h&sort=tokens&dir=desc&limit=100",
)
EVENT_DEBOUNCE_RUNS = 2
PROBE_TIMEOUT_SECONDS = 8
ESCROW_TIMEOUT_SECONDS = 8
HOST_WORKERS = 12
ESCROW_WORKERS = 6
PRUNED_CACHE_VALUE = ""


def parse_allowlist(payload: Any) -> list[str]:
    params = payload.get("params") if isinstance(payload, dict) else None
    escrow = params.get("devshard_escrow_params") if isinstance(params, dict) else None
    raw = escrow.get("allowed_creator_addresses") if isinstance(escrow, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("devshard_escrow_params.allowed_creator_addresses must be a non-empty list")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("allowed creator address is missing")
        address = item.strip()
        if address in names:
            raise ValueError(f"duplicate allowed creator: {address}")
        names.append(address)
    return names


def parse_active_escrows(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("/stats/shards must be an object")
    raw = payload.get("active_escrows")
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        value = str(item).strip() if item is not None else ""
        if value and value not in ids:
            ids.append(value)
    return ids


def parse_escrow_creator(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("found") is False:
        return None
    escrow = payload.get("escrow")
    if not isinstance(escrow, dict):
        escrow = payload
    creator = escrow.get("creator")
    if isinstance(creator, str) and creator.strip():
        return creator.strip()
    return None


def is_gonka_addr(value: str) -> bool:
    text = value.strip()
    return text.startswith("gonka1") and len(text) >= 20


def short_gateway_label(value: str) -> str:
    text = value.strip()
    if not is_gonka_addr(text) or len(text) <= 15:
        return text
    return f"{text[:8]}…{text[-6:]}"


def parse_heartbeat_labels(payload: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not isinstance(payload, dict):
        return labels
    for provider in payload.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        name = provider.get("name")
        if not isinstance(name, str) or not name.strip() or is_gonka_addr(name):
            continue
        name = name.strip()
        for wallet in provider.get("wallets") or []:
            address = wallet.get("address") if isinstance(wallet, dict) else None
            if isinstance(address, str) and address.strip():
                labels[address.strip()] = name
    return labels


def parse_leaderboard_labels(payload: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not isinstance(payload, dict):
        return labels
    for row in payload.get("leaderboard") or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip() or is_gonka_addr(name):
            continue
        name = name.strip()
        for address in row.get("addresses") or []:
            if isinstance(address, str) and address.strip() and address.strip() != name:
                labels[address.strip()] = name
    return labels


def fetch_json_object(urls: tuple[str, ...], session=requests) -> dict | None:
    for url in urls:
        try:
            response = session.get(
                url,
                timeout=10,
                headers={"User-Agent": "gonka-host-bot/1.0"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - labels are optional
            print(f"Gateway labels source failed: {url}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(payload, dict):
            return payload
    return None


def load_labels() -> dict[str, str]:
    raw = load_json(LABELS_FILE, {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def load_display_labels(session=requests) -> dict[str, str]:
    labels = load_labels()
    board = fetch_json_object(LEADERBOARD_URLS, session=session)
    if board:
        labels.update(parse_leaderboard_labels(board))
    heartbeat = fetch_json_object(HEARTBEAT_OVERVIEW_URLS, session=session)
    if heartbeat:
        labels.update(parse_heartbeat_labels(heartbeat))
    print(f"Gateway display labels: {len(labels)}")
    return labels


def broker_label(address: str, labels: dict[str, str]) -> str:
    name = labels.get(address)
    if name and not is_gonka_addr(name):
        return name
    return short_gateway_label(address)


def fetch_gateway_params() -> tuple[list[str], list[str], str]:
    payload, source = fetch_json_with_fallback(
        PARAMS_URLS,
        timeout=20,
        validator=lambda value: (
            parse_allowlist(value),
            parse_approved_versions(value),
        ),
    )
    approved = parse_approved_versions(payload)
    allowlist = parse_allowlist(payload)
    print(f"Gateway params source: {source}")
    return approved, allowlist, source


def probe_slot(url: str, slot: str, session=requests) -> dict:
    try:
        public = validate_public_url(url)
    except ValueError as exc:
        return {"status": "invalid_url", "ids": [], "error": str(exc)}
    try:
        response = session.get(
            f"{public}/devshard/{slot}/stats/shards",
            timeout=PROBE_TIMEOUT_SECONDS,
            headers={"User-Agent": "gonka-host-bot/1.0"},
        )
        response.raise_for_status()
        return {"status": "ok", "ids": parse_active_escrows(response.json())}
    except Exception as exc:  # noqa: BLE001 - one host must not fail the census
        return {
            "status": "unreachable",
            "ids": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def collect_slot_ids(hosts: list[dict], slots: list[str], session=requests) -> dict:
    by_slot: dict[str, set[str]] = {slot: set() for slot in slots}
    answered = 0
    if not hosts or not slots:
        return {"by_slot": by_slot, "answered": 0, "network_hosts": len(hosts)}

    def probe_host(host: dict) -> tuple[str, bool, dict[str, list[str]]]:
        found: dict[str, list[str]] = {}
        ok = False
        for slot in slots:
            result = probe_slot(host["url"], slot, session=session)
            if result["status"] == "ok":
                ok = True
                found[slot] = result.get("ids") or []
        return host["id"], ok, found

    with concurrent.futures.ThreadPoolExecutor(max_workers=HOST_WORKERS) as pool:
        futures = [pool.submit(probe_host, host) for host in hosts]
        for future in concurrent.futures.as_completed(futures):
            _host_id, ok, found = future.result()
            if ok:
                answered += 1
            for slot, ids in found.items():
                by_slot[slot].update(ids)
    return {
        "by_slot": by_slot,
        "answered": answered,
        "network_hosts": len(hosts),
    }


def lookup_creator(escrow_id: str, session=requests) -> tuple[str, str | None, str]:
    """Return (id, creator_or_none, status) where status is found/pruned/failed."""
    last_error = None
    for prefix in ESCROW_URL_PREFIXES:
        try:
            response = session.get(
                f"{prefix}{escrow_id}",
                timeout=ESCROW_TIMEOUT_SECONDS,
                headers={"User-Agent": "gonka-host-bot/1.0"},
            )
            response.raise_for_status()
            creator = parse_escrow_creator(response.json())
            if creator:
                return escrow_id, creator, "found"
            return escrow_id, None, "pruned"
        except Exception as exc:  # noqa: BLE001 - try the next REST
            last_error = f"{type(exc).__name__}: {exc}"
    return escrow_id, None, "failed" if last_error else "pruned"


def resolve_creators(
    by_slot: dict[str, set[str]],
    cache: dict[str, str],
    session=requests,
) -> tuple[dict[str, str], dict[str, int]]:
    wanted: list[str] = []
    seen: set[str] = set()
    for ids in by_slot.values():
        for escrow_id in ids:
            if escrow_id in seen:
                continue
            seen.add(escrow_id)
            if escrow_id not in cache:
                wanted.append(escrow_id)
    stats = {
        "unique": len(seen),
        "cache_hits": len(seen) - len(wanted),
        "lookups": len(wanted),
        "found": 0,
        "pruned": 0,
        "failed": 0,
    }
    updated = dict(cache)
    if wanted:
        with concurrent.futures.ThreadPoolExecutor(max_workers=ESCROW_WORKERS) as pool:
            futures = [pool.submit(lookup_creator, escrow_id, session) for escrow_id in wanted]
            for future in concurrent.futures.as_completed(futures):
                escrow_id, creator, status = future.result()
                if status == "found" and creator:
                    updated[escrow_id] = creator
                    stats["found"] += 1
                elif status == "pruned":
                    updated[escrow_id] = PRUNED_CACHE_VALUE
                    stats["pruned"] += 1
                else:
                    stats["failed"] += 1
    print(
        "Gateway escrow map: "
        f"unique={stats['unique']} cache={stats['cache_hits']} "
        f"lookups={stats['lookups']} found={stats['found']} "
        f"pruned={stats['pruned']} failed={stats['failed']}"
    )
    return updated, stats


def build_census(
    *,
    approved: list[str],
    allowlist: list[str],
    by_slot: dict[str, set[str]],
    creators: dict[str, str],
    labels: dict[str, str],
    network_hosts: int,
    answered: int,
    lookup_failed: int,
) -> dict:
    slots_by_creator: dict[str, set[str]] = defaultdict(set)
    for slot, ids in by_slot.items():
        for escrow_id in ids:
            creator = creators.get(escrow_id) or ""
            if creator:
                slots_by_creator[creator].add(slot)
    brokers = []
    for address, slots in slots_by_creator.items():
        ordered = [slot for slot in approved if slot in slots]
        brokers.append(
            {
                "id": address,
                "label": broker_label(address, labels),
                "slots": ordered,
                "on_allowlist": address in allowlist,
            }
        )
    brokers.sort(key=lambda item: (not item["on_allowlist"], item["label"].casefold(), item["id"]))
    combos: dict[str, int] = {}
    for item in brokers:
        key = "+".join(item["slots"])
        combos[key] = combos.get(key, 0) + 1
    newest = approved[-1] if approved else None
    on_newest: list[str] = []
    seen_newest: set[str] = set()
    for item in brokers:
        if not newest or newest not in item["slots"]:
            continue
        label = item["label"]
        if label in seen_newest:
            continue
        seen_newest.add(label)
        on_newest.append(label)
    on_newest.sort(key=lambda label: (label.startswith("gonka"), label.casefold()))
    seen_allow = sum(1 for item in brokers if item["on_allowlist"])
    return {
        "approved": list(approved),
        "allowlist": list(allowlist),
        "allowlist_total": len(allowlist),
        "allowlist_seen": seen_allow,
        "brokers": brokers,
        "approved_combos": {key: count for key, count in combos.items() if count},
        "newest_slot": newest,
        "on_newest": on_newest,
        "network_hosts": network_hosts,
        "answered": answered,
        "unreachable": max(0, network_hosts - answered),
        "lookup_failed": int(lookup_failed),
    }


def event_signature(census: dict) -> list:
    return [
        [item["id"], list(item.get("slots") or [])]
        for item in census.get("brokers") or []
    ]


def noticeable_change(previous: dict | None, current: dict) -> str | None:
    if not isinstance(previous, dict) or not previous:
        return None
    if list(previous.get("approved") or []) != list(current.get("approved") or []):
        return "immediate"
    prev = {item["id"]: list(item.get("slots") or []) for item in previous.get("brokers") or []}
    cur = {item["id"]: list(item.get("slots") or []) for item in current.get("brokers") or []}
    if prev != cur:
        return "debounce"
    return None


def format_census_body(census: dict) -> str:
    approved = census.get("approved") or []
    seen = int(census.get("allowlist_seen") or 0)
    total = int(census.get("allowlist_total") or 0)
    brokers = census.get("brokers") or []
    if approved:
        lines = [f"Версии протокола: {', '.join(approved)}"]
    else:
        lines = ["Разрешённых слотов нет"]
    lines.append(
        f"Могут создавать девшарды: {total} ключей, живые сессии: {seen}"
    )
    combos = {
        str(key): int(value)
        for key, value in (census.get("approved_combos") or {}).items()
        if int(value) > 0
    }
    if combos:
        lines.extend(["", "у брокеров:"])
        ordered = sorted(
            combos.items(),
            key=lambda item: (item[0].count("+") == 0, -item[1], item[0]),
        )
        for key, count in ordered:
            lines.append(f"• {combo_label(key, approved)} — {count}")
    newest = census.get("newest_slot")
    on_newest = census.get("on_newest") or []
    if newest and on_newest:
        lines.extend(["", f"на {newest}:"])
        for label in on_newest:
            lines.append(f"• {label}")
    extra = [item["label"] for item in brokers if not item.get("on_allowlist")]
    if extra:
        unique_extra = list(dict.fromkeys(extra))
        lines.append("вне allowlist: " + ", ".join(unique_extra))
    return "\n".join(lines).rstrip()


def format_digest_message(epoch, census: dict) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    return f"📊 Гейтвеи{epoch_note}\n\n{format_census_body(census)}"


def format_event_message(epoch, census: dict, previous: dict | None) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    lines = [f"ℹ️ Гейтвеи{epoch_note}", ""]
    approved = list(census.get("approved") or [])
    prev_approved = list((previous or {}).get("approved") or [])
    if prev_approved != approved:
        lines.append(
            "approved: "
            + (", ".join(prev_approved) or "нет")
            + " → "
            + (", ".join(approved) or "нет")
        )
    prev_map = {
        item["id"]: item
        for item in (previous or {}).get("brokers") or []
    }
    cur_map = {item["id"]: item for item in census.get("brokers") or []}
    labels = {
        item["id"]: item.get("label") or broker_label(item["id"], {})
        for item in list(prev_map.values()) + list(cur_map.values())
    }
    for address in sorted(set(prev_map) | set(cur_map), key=lambda item: labels[item].casefold()):
        old_slots = list((prev_map.get(address) or {}).get("slots") or [])
        new_slots = list((cur_map.get(address) or {}).get("slots") or [])
        if old_slots == new_slots:
            continue
        old_text = combo_label("+".join(old_slots), approved or prev_approved) if old_slots else "нет сессий"
        new_text = combo_label("+".join(new_slots), approved) if new_slots else "нет сессий"
        lines.append(f"{labels[address]}: {old_text} → {new_text}")
    if len(lines) > 2:
        lines.append("")
    lines.append(format_census_body(census))
    return "\n".join(lines)


def format_command_gateway_message(state: dict, *, now=None) -> str:
    if not isinstance(state, dict) or not state.get("approved"):
        if isinstance(state, dict) and state.get("last_error"):
            return (
                "📊 Гейтвеи\n\n"
                f"Не удалось снять перепись: {state.get('last_error')}\n\n"
                f"{format_snapshot_age(state.get('checked_at'), now=now)}"
            )
        return "Снимка гейтвеев ещё нет: watcher не запускался."
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
        approved, allowlist, _source = fetch_gateway_params()
        entries, epoch = fetch_active_snapshot()
        epoch = canonical_epoch(epoch)
    except Exception as exc:  # noqa: BLE001 - keep last good snapshot
        print(f"ERROR: gateway sources unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
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

    probes = collect_slot_ids(parsed, approved)
    cache = {}
    if isinstance(previous, dict):
        raw_cache = previous.get("escrow_cache")
        if isinstance(raw_cache, dict):
            cache = {str(key): str(value) for key, value in raw_cache.items()}
    creators, stats = resolve_creators(probes["by_slot"], cache)
    labels = load_display_labels()
    census = build_census(
        approved=approved,
        allowlist=allowlist,
        by_slot=probes["by_slot"],
        creators=creators,
        labels=labels,
        network_hosts=probes["network_hosts"],
        answered=probes["answered"],
        lookup_failed=int(stats.get("failed") or 0),
    )
    if not census["brokers"]:
        print("WARNING: no broker sessions resolved; keeping previous notification baseline")
        state = dict(previous) if isinstance(previous, dict) else {}
        state.update(
            {
                "checked_at": now,
                "epoch": epoch,
                "escrow_cache": creators,
                "last_error": "no broker sessions resolved",
                "lookup_failed": census["lookup_failed"],
                "answered": census["answered"],
                "network_hosts": census["network_hosts"],
            }
        )
        save_json_atomic(STATE_FILE, state, sort_keys=True)
        return

    state, messages = apply_tick(previous, census, epoch=epoch, now=now)
    state["escrow_cache"] = creators
    for message in messages:
        send_telegram_message(message, parse_mode=None)
    save_json_atomic(STATE_FILE, state, sort_keys=True)
    print(
        f"epoch={epoch} brokers={len(census['brokers'])} "
        f"answered={census['answered']}/{census['network_hosts']} "
        f"messages={len(messages)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
