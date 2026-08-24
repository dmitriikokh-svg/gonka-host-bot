"""
Gonka upgrade-adoption watcher.

For every active participant, checks its own /v1/versions endpoint once and
reports two independent rollout signals: decentralized API adoption by host
weight and the MLNode version census exposed in ``mlnodes[].version``.

Target versions are read from GitHub Actions repository variables:
  - TARGET_API_VERSION   e.g. "v0.2.13-post8"
  - TARGET_MLNODE_VERSION e.g. "3.0.16"
  - ADOPTION_PERCENT     optional, default 80 (share of total network weight)
"""

import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

import requests
import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

from bot_common import (
    fetch_json_with_fallback,
    load_json,
    save_json_atomic,
    send_telegram_message,
)

ROOT = Path(__file__).resolve().parent
PARTICIPANTS_URLS = (
    "https://node3.gonka.ai/v1/epochs/current/participants",
    "https://node2.gonka.ai:8443/v1/epochs/current/participants",
    "http://node2.gonka.ai:8000/v1/epochs/current/participants",
    "http://node1.gonka.ai:8000/v1/epochs/current/participants",
)
EPOCH_LATEST_URLS = (
    "https://node3.gonka.ai/v1/epochs/latest",
    "https://node2.gonka.ai:8443/v1/epochs/latest",
    "http://node2.gonka.ai:8000/v1/epochs/latest",
    "http://node1.gonka.ai:8000/v1/epochs/latest",
)
STATE_FILE = ROOT / "state" / "upgrade_adoption.json"
VERSION_CHECK_TIMEOUT = 10
VERSION_RETRY_DELAY = 5
VERSION_RETRIES = 2
VERSION_SECOND_PASS_TIMEOUT = 15
VERSION_SECOND_PASS_DELAY = 5
MAX_WORKERS = 20
ADOPTION_PERCENT_DEFAULT = 80.0
EVENT_PP_THRESHOLD = 5.0
DIGEST_AFTER_CLAIM_BLOCKS = 2000
DIGEST_BEFORE_NEXT_POC_BLOCKS = 500
BAND_RANK = {"normal": 0, "degraded": 1, "unreliable": 2}


def validate_participants_response(data):
    active = data.get("active_participants") if isinstance(data, dict) else None
    entries = active.get("participants") if isinstance(active, dict) else None
    epoch = active.get("epoch_group_id") if isinstance(active, dict) else None
    if not isinstance(entries, list) or not entries or epoch is None:
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        raise ValueError(
            "Could not find a complete active_participants snapshot.\n"
            f"Preview:\n{preview}"
        )


def fetch_active_snapshot():
    data, source = fetch_json_with_fallback(
        PARTICIPANTS_URLS,
        timeout=30,
        validator=validate_participants_response,
    )
    print(f"Participants source: {source}")
    active = data["active_participants"]
    return active["participants"], active["epoch_group_id"]


def parse_epoch_stage(data):
    if not isinstance(data, dict):
        raise ValueError("epoch latest payload must be an object")
    stages = data.get("epoch_stages")
    if not isinstance(stages, dict):
        raise ValueError("epoch_stages is missing")
    try:
        block_height = int(data.get("block_height"))
        claim_money = int(stages.get("claim_money"))
        next_poc_start = int(stages.get("next_poc_start"))
    except (TypeError, ValueError) as exc:
        raise ValueError("epoch stage heights must be integers") from exc
    epoch_index = stages.get("epoch_index")
    if epoch_index is None:
        latest = data.get("latest_epoch")
        if isinstance(latest, dict):
            epoch_index = latest.get("index")
    return {
        "phase": str(data.get("phase") or ""),
        "is_confirmation_poc_active": bool(data.get("is_confirmation_poc_active")),
        "block_height": block_height,
        "claim_money": claim_money,
        "next_poc_start": next_poc_start,
        "epoch_index": epoch_index,
    }


def fetch_epoch_stage():
    data, source = fetch_json_with_fallback(
        EPOCH_LATEST_URLS,
        timeout=30,
        validator=parse_epoch_stage,
    )
    print(f"Epoch stage source: {source}")
    return parse_epoch_stage(data)


def telegram_allowed(stage) -> bool:
    return bool(
        stage
        and stage.get("phase") == "Inference"
        and not stage.get("is_confirmation_poc_active")
    )


def digest_window(stage) -> bool:
    if not telegram_allowed(stage):
        return False
    claim_money = int(stage.get("claim_money") or 0)
    next_poc_start = int(stage.get("next_poc_start") or 0)
    height = int(stage.get("block_height") or 0)
    if claim_money <= 0 or next_poc_start <= 0 or height <= 0:
        return False
    if height - claim_money < DIGEST_AFTER_CLAIM_BLOCKS:
        return False
    if next_poc_start - height <= DIGEST_BEFORE_NEXT_POC_BLOCKS:
        return False
    return True

def participant_identity(entry):
    if not isinstance(entry, dict):
        raise ValueError(
            "Participant entry must be an object, "
            f"got {type(entry).__name__}"
        )

    pid = None

    for key in (
        "index",
        "participant_id",
        "address",
        "id",
    ):
        value = entry.get(key)

        if value:
            pid = str(value)
            break

    weight = None

    # Основной источник веса
    raw_weight = entry.get("weight")

    if raw_weight is not None:
        try:
            weight = int(raw_weight)
        except (TypeError, ValueError):
            weight = None

    # Fallback для API-ответов, где weight = null
    if weight is None:
        voting_powers = entry.get("voting_powers")

        if isinstance(voting_powers, list):
            powers = []

            for item in voting_powers:
                if not isinstance(item, dict):
                    continue

                raw_power = item.get("voting_power")

                if raw_power is None:
                    continue

                try:
                    powers.append(int(raw_power))
                except (TypeError, ValueError):
                    continue

            if powers:
                weight = sum(powers)

                print(
                    f"WARNING: using voting_powers fallback "
                    f"for participant {pid}: {weight}"
                )

    url = None

    for key in (
        "inference_url",
        "url",
        "api_url",
    ):
        value = entry.get(key)

        if value:
            url = str(value).rstrip("/")
            break

    return pid, weight, url

def validate_public_url(raw_url):
    if not raw_url:
        raise ValueError("empty participant URL")

    parsed = urlparse(raw_url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")

    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed")

    if not parsed.hostname:
        raise ValueError("URL has no hostname")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid URL port: {exc}") from exc

    try:
        addresses = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(
                parsed.hostname,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError(
            f"hostname does not resolve: {parsed.hostname!r}"
        ) from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError(
            f"URL resolves to a non-public address: {parsed.hostname!r}"
        )

    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )

def extract_api_version(data):
    if not isinstance(data, dict):
        return None
    api_version = data.get("api_version")
    if isinstance(api_version, dict):
        value = api_version.get("version")
        if value:
            return str(value)
    for key in ("version", "decentralized_api_version"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def extract_mlnodes(data):
    if not isinstance(data, dict):
        return None
    raw_nodes = data.get("mlnodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return None

    nodes = []
    seen_ids = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            return None
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            return None
        node_id = node_id.strip()
        if node_id in seen_ids:
            return None
        seen_ids.add(node_id)
        raw_version = item.get("version")
        version = str(raw_version).strip() if raw_version is not None else ""
        nodes.append(
            {
                "node_id": node_id,
                "version": version or None,
                "poc_validation_inference": bool(
                    item.get("poc_validation_inference", False)
                ),
            }
        )
    return nodes


def fetch_version_info(
    url,
    retries=VERSION_RETRIES,
    timeout=VERSION_CHECK_TIMEOUT,
    delay=VERSION_RETRY_DELAY,
):
    """Query one participant's own /v1/versions, with retries and a pause
    between attempts so a single slow response is not counted as unreachable.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{url}/v1/versions", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("/v1/versions response must be an object")
            return {
                "api_version": extract_api_version(data),
                "mlnodes": extract_mlnodes(data),
            }
        except Exception as exc:
            last_error = exc
            if attempt < retries and delay:
                time.sleep(delay)
            continue
    print(f"WARNING: {url} unreachable after {retries + 1} attempts ({last_error})")
    return None


def collect_version_results(participants):
    results = {}

    def probe(subset, *, retries, timeout, delay):
        if not subset:
            return
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as pool:
            futures = {
                pool.submit(
                    fetch_version_info,
                    participant["url"],
                    retries,
                    timeout,
                    delay,
                ): participant
                for participant in subset
            }
            first_printed = False
            for future in concurrent.futures.as_completed(futures):
                participant = futures[future]
                info = future.result()
                results[participant["id"]] = info
                if info and not first_printed:
                    print(
                        "Sample /v1/versions result "
                        f"(for calibration): api={info.get('api_version')}, "
                        f"mlnodes={len(info.get('mlnodes') or [])}"
                    )
                    first_printed = True

    probe(
        participants,
        retries=VERSION_RETRIES,
        timeout=VERSION_CHECK_TIMEOUT,
        delay=VERSION_RETRY_DELAY,
    )
    missing = [
        participant
        for participant in participants
        if results.get(participant["id"]) is None
    ]
    if missing:
        print(f"Retrying {len(missing)} unreachable hosts with a longer timeout")
        probe(
            missing,
            retries=1,
            timeout=VERSION_SECOND_PASS_TIMEOUT,
            delay=VERSION_SECOND_PASS_DELAY,
        )
    return results


def normalize_version(v):
    return v.lstrip("vV") if isinstance(v, str) else v


def summarize_mlnode_adoption(
    participants,
    results,
    target_version,
    *,
    network_total_weight,
    network_host_count,
    initial_unknown_weight=0,
    initial_unknown_hosts=0,
):
    target_nodes = 0
    visible_nodes = 0
    missing_version_nodes = 0
    fully_updated_hosts = 0
    fully_updated_weight = 0
    mixed_hosts = 0
    other_hosts = 0
    unknown_hosts = initial_unknown_hosts
    unknown_weight = initial_unknown_weight
    unavailable_hosts = 0
    distribution = {}
    hosts = {}

    normalized_target = normalize_version(target_version)
    for participant in participants:
        participant_id = participant["id"]
        weight = participant["weight"]
        info = results.get(participant_id)
        if info is None:
            unavailable_hosts += 1
            unknown_hosts += 1
            unknown_weight += weight
            hosts[participant_id] = {
                "status": "unavailable",
                "weight": weight,
                "versions": [],
            }
            continue

        nodes = info.get("mlnodes")
        if not nodes:
            unknown_hosts += 1
            unknown_weight += weight
            hosts[participant_id] = {
                "status": "no_mlnode_data",
                "weight": weight,
                "versions": [],
            }
            continue

        versions = [node.get("version") for node in nodes]
        visible_nodes += len(versions)
        normalized = [normalize_version(value) for value in versions]
        matching = [value == normalized_target for value in normalized]
        target_nodes += sum(matching)
        has_missing = any(value is None for value in versions)
        missing_version_nodes += sum(value is None for value in versions)

        for version in versions:
            label = version if version is not None else "MISSING_VERSION"
            distribution[label] = distribution.get(label, 0) + 1

        if all(matching):
            status = "target"
            fully_updated_hosts += 1
            fully_updated_weight += weight
        elif any(matching):
            status = "mixed"
            mixed_hosts += 1
        elif has_missing:
            status = "incomplete"
            unknown_hosts += 1
            unknown_weight += weight
        else:
            status = "other"
            other_hosts += 1

        hosts[participant_id] = {
            "status": status,
            "weight": weight,
            "versions": [value or "MISSING_VERSION" for value in versions],
        }

    pct = (
        fully_updated_weight / network_total_weight * 100
        if network_total_weight
        else 0
    )
    return {
        "target_version": target_version,
        "target_node_count": target_nodes,
        "visible_node_count": visible_nodes,
        "missing_version_node_count": missing_version_nodes,
        "fully_updated_host_count": fully_updated_hosts,
        "network_host_count": network_host_count,
        "fully_updated_weight": fully_updated_weight,
        "fully_updated_weight_percent": pct,
        "mixed_host_count": mixed_hosts,
        "other_host_count": other_hosts,
        "unknown_host_count": unknown_hosts,
        "unknown_weight": unknown_weight,
        "unavailable_host_count": unavailable_hosts,
        "version_distribution": dict(sorted(distribution.items())),
        "hosts": hosts,
    }


def weight_percent(part, total) -> float:
    if not total:
        return 0.0
    return part / total * 100


def unknown_band_for(unknown_pct: float) -> str:
    if unknown_pct < 10:
        return "normal"
    if unknown_pct < 25:
        return "degraded"
    return "unreliable"


def signed_int(delta: int) -> str:
    if delta > 0:
        return f"+{delta}"
    if delta < 0:
        return f"−{abs(delta)}"
    return "0"


def format_pp_suffix(current: float, previous) -> str:
    if previous is None:
        return ""
    delta = current - float(previous)
    if abs(delta) < 0.05:
        return ""
    if delta > 0:
        return f"  (+{delta:.1f} п.п.)"
    return f"  (−{abs(delta):.1f} п.п.)"


def format_count_suffix(current: int, previous) -> str:
    if previous is None:
        return ""
    previous = int(previous)
    if current == previous:
        return ""
    return f"  (было {previous}, {signed_int(current - previous)})"


def format_mlnode_distribution_lines(
    distribution: dict,
    target_version: str,
    previous: dict | None = None,
) -> list[str]:
    """Telegram list of exact MLNode versions: target first, then by count."""
    missing = int(distribution.get("MISSING_VERSION") or 0)
    counts = {
        str(raw): int(n)
        for raw, n in distribution.items()
        if raw != "MISSING_VERSION" and n
    }
    previous_counts = {}
    previous_missing = 0
    if previous:
        previous_missing = int(previous.get("MISSING_VERSION") or 0)
        previous_counts = {
            str(raw): int(n)
            for raw, n in previous.items()
            if raw != "MISSING_VERSION" and n
        }

    norm_target = normalize_version(target_version)
    ordered: list[str] = []
    for version in counts:
        if normalize_version(version) == norm_target:
            ordered.append(version)
            break
    ordered.extend(
        sorted(
            (version for version in counts if version not in ordered),
            key=lambda version: (-counts[version], version),
        )
    )
    lines = []
    for version in ordered:
        line = f"{version} — {counts[version]}"
        line += format_count_suffix(counts[version], previous_counts.get(version))
        lines.append(line)
    if missing:
        line = f"версия не указана — {missing}"
        line += format_count_suffix(missing, previous_missing if previous else None)
        lines.append(line)
    return lines


def russian_host_word(count: int) -> str:
    n = abs(count) % 100
    n1 = n % 10
    if 11 <= n <= 14:
        return "хостов"
    if n1 == 1:
        return "хост"
    if 2 <= n1 <= 4:
        return "хоста"
    return "хостов"


def format_api_unknown_lines(
    *,
    unreachable: int,
    unreachable_weight: int,
    other_unknown_weight: int,
    network_total_weight: int,
    previous: dict | None = None,
    with_deltas: bool = False,
) -> list[str]:
    unreachable_pct = weight_percent(unreachable_weight, network_total_weight)
    other_pct = weight_percent(other_unknown_weight, network_total_weight)
    if unreachable:
        line = (
            f"Не достучались до /v1/versions: {unreachable} "
            f"{russian_host_word(unreachable)}, их вес {unreachable_weight} "
            f"({unreachable_pct:.1f}%)"
        )
        if with_deltas and previous is not None:
            line += format_pp_suffix(
                unreachable_pct,
                previous.get("unreachable_pct"),
            )
        lines = [line]
    else:
        lines = [
            f"Не достучались до /v1/versions: 0 {russian_host_word(0)}"
        ]
    if other_unknown_weight:
        line = (
            "Неизвестна версия API по иным причинам: "
            f"{other_unknown_weight} ({other_pct:.1f}%)"
        )
        if with_deltas and previous is not None:
            line += format_pp_suffix(other_pct, previous.get("other_unknown_pct"))
        lines.append(line)
    return lines


def format_api_block(
    snapshot: dict,
    *,
    previous: dict | None = None,
    with_deltas: bool = False,
    crossed_threshold: bool = False,
) -> str:
    adopted_line = (
        f"Обновлено: {snapshot['adopted_weight']} / "
        f"{snapshot['network_total_weight']} веса "
        f"({snapshot['adopted_pct']:.1f}%)"
    )
    if with_deltas:
        adopted_line += format_pp_suffix(
            snapshot["adopted_pct"],
            None if previous is None else previous.get("adopted_pct"),
        )
    goal_pct = snapshot["goal_pct"]
    goal_label = (
        f"{int(goal_pct)}%" if float(goal_pct).is_integer() else f"{goal_pct:.1f}%"
    )
    lines = [
        f"API {snapshot['target_version']}",
        adopted_line,
    ]
    if crossed_threshold:
        lines.append(f"✅ Цель {goal_label} достигнута")
    lines.append(f"Цель: {goal_label} сети ({snapshot['goal_weight']})")
    lines.extend(
        format_api_unknown_lines(
            unreachable=snapshot["unreachable"],
            unreachable_weight=snapshot["unreachable_weight"],
            other_unknown_weight=snapshot["other_unknown_weight"],
            network_total_weight=snapshot["network_total_weight"],
            previous=previous,
            with_deltas=with_deltas,
        )
    )
    if snapshot.get("unknown_participants"):
        lines.append(f"Участников без веса: {snapshot['unknown_participants']}")
    return "\n".join(lines)


def format_mlnode_host_lines(
    snapshot: dict,
    previous: dict | None = None,
    *,
    only_if_changed: bool = False,
) -> list[str]:
    current = (
        snapshot["fully_updated_host_count"],
        snapshot["mixed_host_count"],
        snapshot["other_host_count"],
        snapshot["unknown_host_count"],
        snapshot["network_host_count"],
    )
    previous_tuple = None
    if previous:
        previous_tuple = (
            int(previous.get("fully_updated_host_count") or 0),
            int(previous.get("mixed_host_count") or 0),
            int(previous.get("other_host_count") or 0),
            int(previous.get("unknown_host_count") or 0),
            int(previous.get("network_host_count") or 0),
        )
    if only_if_changed and (previous_tuple is None or previous_tuple == current):
        return []
    fully_line = (
        f"Хосты полностью на {snapshot['target_version']}: "
        f"{snapshot['fully_updated_host_count']} из "
        f"{snapshot['network_host_count']}"
    )
    mixed_line = f"Частично обновлённых: {snapshot['mixed_host_count']}"
    other_line = f"На старых версиях: {snapshot['other_host_count']}"
    unknown_line = f"Нет данных: {snapshot['unknown_host_count']}"
    if previous_tuple is not None:
        fully_line += format_count_suffix(current[0], previous_tuple[0])
        mixed_line += format_count_suffix(current[1], previous_tuple[1])
        other_line += format_count_suffix(current[2], previous_tuple[2])
        unknown_line += format_count_suffix(current[3], previous_tuple[3])
    return [fully_line, mixed_line, other_line, unknown_line]


def format_mlnode_block(
    snapshot: dict,
    *,
    previous: dict | None = None,
    with_deltas: bool = False,
    hosts_only_if_changed: bool = False,
) -> str:
    node_line = (
        f"Ноды с этой версией: {snapshot['target_node_count']} из "
        f"{snapshot['visible_node_count']} ({snapshot['target_pct']:.1f}%)"
    )
    if with_deltas:
        node_line += format_pp_suffix(
            snapshot["target_pct"],
            None if previous is None else previous.get("target_pct"),
        )
    lines = [
        f"MLNode {snapshot['target_version']}",
        node_line,
    ]
    dist = format_mlnode_distribution_lines(
        snapshot["version_distribution"],
        snapshot["target_version"],
        None if not with_deltas else (previous or {}).get("version_distribution"),
    )
    if dist:
        lines.append("")
        lines.extend(dist)
    host_lines = format_mlnode_host_lines(
        snapshot,
        previous if with_deltas else None,
        only_if_changed=hosts_only_if_changed,
    )
    if host_lines:
        lines.append("")
        lines.extend(host_lines)
    return "\n".join(lines)


def format_digest_message(
    epoch, api_snapshot, mlnode_snapshot, *, crossed_threshold: bool = False
) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    return (
        f"📊 Версии API и MLNode{epoch_note}\n\n"
        f"{format_api_block(api_snapshot, crossed_threshold=crossed_threshold)}\n"
        f"---\n"
        f"{format_mlnode_block(mlnode_snapshot)}\n"
    )


def format_api_event_message(
    epoch, api_snapshot, previous, *, crossed_threshold: bool
) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    return (
        f"📊 API{epoch_note}\n\n"
        f"{format_api_block(api_snapshot, previous=previous, with_deltas=True, crossed_threshold=crossed_threshold)}\n"
    )


def format_mlnode_event_message(epoch, mlnode_snapshot, previous) -> str:
    epoch_note = f", эпоха {epoch}" if epoch is not None else ""
    return (
        f"📊 MLNode{epoch_note}\n\n"
        f"{format_mlnode_block(mlnode_snapshot, previous=previous, with_deltas=True, hosts_only_if_changed=True)}\n"
    )


def evaluate_debounce(previous, prefix, signature, *, immediate=False, needed=2):
    reported_key = f"{prefix}_reported_signature"
    candidate_key = f"{prefix}_candidate_signature"
    runs_key = f"{prefix}_candidate_runs"
    if immediate:
        return True, signature, None, 0
    reported = previous.get(reported_key)
    if signature == reported:
        return False, reported, None, 0
    candidate = previous.get(candidate_key)
    runs = int(previous.get(runs_key, 0) or 0)
    runs = runs + 1 if candidate == signature else 1
    if runs >= needed:
        return True, signature, None, 0
    return False, reported, signature, runs


def evaluate_api_event(previous, snapshot) -> tuple:
    notified = previous.get("last_notified_api") or {}
    if not notified:
        return False, previous.get("api_reported_signature"), None, 0
    immediate = False
    signature = None
    if notified.get("target_version") != snapshot["target_version"]:
        immediate = True
        signature = f"version:{snapshot['target_version']}"
    elif snapshot["threshold_reached"] and not notified.get("threshold_reached"):
        immediate = True
        signature = "threshold"
    elif BAND_RANK.get(snapshot["unknown_band"], 0) > BAND_RANK.get(
        notified.get("unknown_band"), 0
    ):
        immediate = True
        signature = f"band:{snapshot['unknown_band']}"
    elif snapshot["adopted_pct"] >= float(
        notified.get("adopted_pct") or 0
    ) + EVENT_PP_THRESHOLD:
        signature = f"pp:{int(snapshot['adopted_pct'] * 10)}"
    else:
        return False, previous.get("api_reported_signature"), None, 0
    return evaluate_debounce(
        previous, "api", signature, immediate=immediate
    )


def evaluate_mlnode_event(previous, snapshot) -> tuple:
    notified = previous.get("last_notified_mlnode") or {}
    if not notified:
        return False, previous.get("mlnode_reported_signature"), None, 0
    immediate = False
    signature = None
    if notified.get("target_version") != snapshot["target_version"]:
        immediate = True
        signature = f"version:{snapshot['target_version']}"
    elif snapshot["fully_updated_host_count"] > int(
        notified.get("fully_updated_host_count") or 0
    ):
        signature = f"hosts:{snapshot['fully_updated_host_count']}"
    elif snapshot["target_pct"] >= float(
        notified.get("target_pct") or 0
    ) + EVENT_PP_THRESHOLD:
        signature = f"pp:{int(snapshot['target_pct'] * 10)}"
    else:
        return False, previous.get("mlnode_reported_signature"), None, 0
    return evaluate_debounce(
        previous, "mlnode", signature, immediate=immediate
    )


def main():
    target_version = os.environ["TARGET_API_VERSION"]
    target_mlnode_version = os.environ["TARGET_MLNODE_VERSION"]
    adoption_percent = float(
        os.environ.get("ADOPTION_PERCENT", ADOPTION_PERCENT_DEFAULT)
    )
    entries, epoch = fetch_active_snapshot()
    try:
        stage = fetch_epoch_stage()
    except Exception as exc:
        print(f"WARNING: epoch stage unavailable: {type(exc).__name__}: {exc}")
        stage = None

    if entries:
        print("Sample participant entry (for field-name calibration):")
        print(json.dumps(entries[0], indent=2, ensure_ascii=False)[:1500])

    parsed = []
    seen_ids = set()
    network_total_weight = 0
    network_host_count = 0
    unknown_weight = 0
    unknown_participants = 0
    unqueryable_weight = 0
    unqueryable_hosts = 0

    for entry in entries:
        pid, weight, url = participant_identity(entry)

        if not pid:
            raise ValueError("participant has no stable id")

        if pid in seen_ids:
            raise ValueError(f"duplicate participant id: {pid}")

        seen_ids.add(pid)

        if weight is None or weight < 0:
            print(
                f"WARNING: skipping participant {pid} "
                f"because weight is unavailable"
            )
            print(
                "Participant payload:",
                json.dumps(entry, indent=2, ensure_ascii=False)[:3000]
            )
            unknown_participants += 1
            continue
        
        network_total_weight += weight
        network_host_count += 1

        if not url:
            print(
                f"WARNING: no usable URL field found for participant {pid}"
            )
            unknown_weight += weight
            unqueryable_weight += weight
            unqueryable_hosts += 1
            continue

        try:
            safe_url = validate_public_url(url)
        except ValueError as exc:
            print(
                f"WARNING: skipping unsafe URL for {pid}: {exc}"
            )
            unknown_weight += weight
            unqueryable_weight += weight
            unqueryable_hosts += 1
            continue

        parsed.append(
            {
                "id": pid,
                "weight": weight,
                "url": safe_url,
            }
        )

    results = collect_version_results(parsed)

    adopted_weight = 0
    unreachable = 0
    unreachable_weight = 0
    missing_api_version_weight = 0

    for participant in parsed:
        info = results.get(participant["id"])

        if info is None:
            unreachable += 1
            unreachable_weight += participant["weight"]
            unknown_weight += participant["weight"]
            continue

        version = info.get("api_version")
        if version is None:
            missing_api_version_weight += participant["weight"]
            unknown_weight += participant["weight"]
            continue

        if normalize_version(version) == normalize_version(
            target_version
        ):
            adopted_weight += participant["weight"]

    mlnode = summarize_mlnode_adoption(
        parsed,
        results,
        target_mlnode_version,
        network_total_weight=network_total_weight,
        network_host_count=network_host_count,
        initial_unknown_weight=unqueryable_weight,
        initial_unknown_hosts=unqueryable_hosts,
    )

    pct = weight_percent(adopted_weight, network_total_weight)
    goal_weight = int(round(network_total_weight * adoption_percent / 100))
    mlnode_pct = weight_percent(
        mlnode["target_node_count"], mlnode["visible_node_count"]
    )
    other_unknown_weight = missing_api_version_weight + unqueryable_weight
    unreachable_pct = weight_percent(unreachable_weight, network_total_weight)
    other_unknown_pct = weight_percent(other_unknown_weight, network_total_weight)
    unknown_pct = weight_percent(unknown_weight, network_total_weight)
    unknown_band = unknown_band_for(unknown_pct)
    threshold_reached = pct + 1e-9 >= adoption_percent

    print(
        f"Adoption: {adopted_weight}/{network_total_weight} "
        f"({pct:.1f}%), "
        f"goal {adoption_percent:g}% ({goal_weight}), "
        f"unreachable hosts: {unreachable} ({unreachable_weight}), "
        f"missing api_version weight: {missing_api_version_weight}, "
        f"unqueryable weight: {unqueryable_weight}, "
        f"unknown weight: {unknown_weight}"
    )
    print(
        f"MLNode {target_mlnode_version}: "
        f"nodes {mlnode['target_node_count']}/{mlnode['visible_node_count']}, "
        f"fully updated hosts "
        f"{mlnode['fully_updated_host_count']}/{network_host_count}, "
        f"weight {mlnode['fully_updated_weight']}/{network_total_weight}, "
        f"unknown hosts {mlnode['unknown_host_count']}"
    )
    if stage:
        print(
            f"Epoch stage: phase={stage['phase']}, "
            f"cpoc={stage['is_confirmation_poc_active']}, "
            f"height={stage['block_height']}, "
            f"claim_money={stage['claim_money']}, "
            f"next_poc_start={stage['next_poc_start']}"
        )
    for participant_id, details in sorted(mlnode["hosts"].items()):
        print(
            f"MLNode host {participant_id}: status={details['status']}; "
            f"weight={details['weight']}; versions={details['versions']}"
        )

    previous = load_json(STATE_FILE)
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("upgrade-adoption state must be a JSON object")
    previous_state = previous or {}

    api_snapshot = {
        "target_version": target_version,
        "adopted_weight": adopted_weight,
        "adopted_pct": pct,
        "network_total_weight": network_total_weight,
        "goal_pct": adoption_percent,
        "goal_weight": goal_weight,
        "unreachable": unreachable,
        "unreachable_weight": unreachable_weight,
        "unreachable_pct": unreachable_pct,
        "other_unknown_weight": other_unknown_weight,
        "other_unknown_pct": other_unknown_pct,
        "unknown_band": unknown_band,
        "threshold_reached": threshold_reached,
        "unknown_participants": unknown_participants,
    }
    mlnode_snapshot = {
        "target_version": target_mlnode_version,
        "target_node_count": mlnode["target_node_count"],
        "visible_node_count": mlnode["visible_node_count"],
        "target_pct": mlnode_pct,
        "fully_updated_host_count": mlnode["fully_updated_host_count"],
        "mixed_host_count": mlnode["mixed_host_count"],
        "other_host_count": mlnode["other_host_count"],
        "unknown_host_count": mlnode["unknown_host_count"],
        "network_host_count": network_host_count,
        "version_distribution": mlnode["version_distribution"],
    }

    last_digest_epoch = previous_state.get("last_digest_epoch")
    send_digest = False
    send_api = False
    send_mlnode = False
    api_reported = previous_state.get("api_reported_signature")
    api_candidate = previous_state.get("api_candidate_signature")
    api_runs = previous_state.get("api_candidate_runs", 0)
    mlnode_reported = previous_state.get("mlnode_reported_signature")
    mlnode_candidate = previous_state.get("mlnode_candidate_signature")
    mlnode_runs = previous_state.get("mlnode_candidate_runs", 0)
    last_notified_api = previous_state.get("last_notified_api")
    last_notified_mlnode = previous_state.get("last_notified_mlnode")

    if not telegram_allowed(stage):
        print("Quiet phase (not inference or CPoC active), no Telegram.")
    elif digest_window(stage) and last_digest_epoch != epoch:
        send_digest = True
    elif last_digest_epoch == epoch:
        send_api, api_reported, api_candidate, api_runs = evaluate_api_event(
            previous_state, api_snapshot
        )
        send_mlnode, mlnode_reported, mlnode_candidate, mlnode_runs = (
            evaluate_mlnode_event(previous_state, mlnode_snapshot)
        )
    else:
        print("Waiting for inference digest window, no Telegram.")

    crossed_threshold_now = threshold_reached and not (
        (last_notified_api or {}).get("threshold_reached")
        or previous_state.get("threshold_reached")
    )

    if send_digest:
        send_telegram_message(
            format_digest_message(
                epoch,
                api_snapshot,
                mlnode_snapshot,
                crossed_threshold=crossed_threshold_now,
            ),
            parse_mode=None,
        )
        print("Sent Telegram digest.")
        last_digest_epoch = epoch
        last_notified_api = api_snapshot
        last_notified_mlnode = mlnode_snapshot
        api_reported = None
        api_candidate = None
        api_runs = 0
        mlnode_reported = None
        mlnode_candidate = None
        mlnode_runs = 0
    elif send_api or send_mlnode:
        if send_api:
            send_telegram_message(
                format_api_event_message(
                    epoch,
                    api_snapshot,
                    last_notified_api,
                    crossed_threshold=crossed_threshold_now,
                ),
                parse_mode=None,
            )
            last_notified_api = api_snapshot
            print("Sent Telegram API event.")
        if send_mlnode:
            send_telegram_message(
                format_mlnode_event_message(
                    epoch, mlnode_snapshot, last_notified_mlnode
                ),
                parse_mode=None,
            )
            last_notified_mlnode = mlnode_snapshot
            print("Sent Telegram MLNode event.")
    elif telegram_allowed(stage) and last_digest_epoch == epoch:
        print("No Telegram message sent.")

    save_json_atomic(
        STATE_FILE,
        {
            "target_version": target_version,
            "adoption_percent": adoption_percent,
            "goal_weight": goal_weight,
            "adopted_weight": adopted_weight,
            "adopted_pct": pct,
            "network_total_weight": network_total_weight,
            "unknown_weight": unknown_weight,
            "unreachable_count": unreachable,
            "unreachable_weight": unreachable_weight,
            "missing_api_version_weight": missing_api_version_weight,
            "unqueryable_weight": unqueryable_weight,
            "unqueryable_hosts": unqueryable_hosts,
            "threshold_reached": threshold_reached,
            "unknown_band": unknown_band,
            "unknown_participants": unknown_participants,
            "target_mlnode_version": target_mlnode_version,
            "mlnode_target_node_count": mlnode["target_node_count"],
            "mlnode_visible_node_count": mlnode["visible_node_count"],
            "mlnode_missing_version_node_count": mlnode[
                "missing_version_node_count"
            ],
            "mlnode_fully_updated_host_count": mlnode[
                "fully_updated_host_count"
            ],
            "mlnode_fully_updated_weight": mlnode["fully_updated_weight"],
            "mlnode_mixed_host_count": mlnode["mixed_host_count"],
            "mlnode_other_host_count": mlnode["other_host_count"],
            "mlnode_unknown_host_count": mlnode["unknown_host_count"],
            "mlnode_unknown_weight": mlnode["unknown_weight"],
            "mlnode_unavailable_host_count": mlnode[
                "unavailable_host_count"
            ],
            "mlnode_version_distribution": mlnode["version_distribution"],
            "mlnode_hosts": mlnode["hosts"],
            "last_digest_epoch": last_digest_epoch,
            "last_notified_api": last_notified_api,
            "last_notified_mlnode": last_notified_mlnode,
            "api_reported_signature": api_reported,
            "api_candidate_signature": api_candidate,
            "api_candidate_runs": api_runs,
            "mlnode_reported_signature": mlnode_reported,
            "mlnode_candidate_signature": mlnode_candidate,
            "mlnode_candidate_runs": mlnode_runs,
            "epoch_phase": None if stage is None else stage.get("phase"),
        },
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
