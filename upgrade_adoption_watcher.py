"""
Gonka upgrade-adoption watcher.

For every active participant, checks its own /v1/versions endpoint once and
reports two independent rollout signals: decentralized API adoption by host
weight and the MLNode version census exposed in ``mlnodes[].version``.

Target version and weight threshold are read from GitHub Actions
repository variables (not secrets, not code) so they can be updated for
future upgrades without touching this file:
  - TARGET_API_VERSION   e.g. "v0.2.13-post8"
  - ADOPTION_THRESHOLD   e.g. "267800"
  - TARGET_MLNODE_VERSION e.g. "3.0.16"
"""

import concurrent.futures
import json
import os
import sys
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
STATE_FILE = ROOT / "state" / "upgrade_adoption.json"
VERSION_CHECK_TIMEOUT = 5
MAX_WORKERS = 20


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


def fetch_version_info(url, retries=2, timeout=VERSION_CHECK_TIMEOUT):
    """Query one participant's own /v1/versions, with a couple of retries
    before giving up -- a single slow response shouldn't count a host as
    unreachable and silently drop its weight from the numerator."""
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
            continue
    print(f"WARNING: {url} unreachable after {retries + 1} attempts ({last_error})")
    return None


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
        else:
            status = "other"

        if has_missing:
            unknown_hosts += 1
            unknown_weight += weight
            status = "incomplete" if status == "other" else status

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
        "unknown_host_count": unknown_hosts,
        "unknown_weight": unknown_weight,
        "unavailable_host_count": unavailable_hosts,
        "version_distribution": dict(sorted(distribution.items())),
        "hosts": hosts,
    }


def mlnode_signature(summary):
    return ":".join(
        str(summary[key])
        for key in (
            "target_node_count",
            "fully_updated_host_count",
            "fully_updated_weight",
        )
    )


def evaluate_mlnode_notification(previous, target_version, signature, *, force=False):
    if force or previous.get("target_mlnode_version") != target_version:
        return True, signature, None, 0

    reported = previous.get("mlnode_reported_signature")
    if signature == reported:
        return False, reported, None, 0

    candidate = previous.get("mlnode_candidate_signature")
    runs = int(previous.get("mlnode_candidate_runs", 0) or 0)
    runs = runs + 1 if candidate == signature else 1
    if runs >= 2:
        return True, signature, None, 0
    return False, reported, signature, runs


def main():
    target_version = os.environ["TARGET_API_VERSION"]
    target_mlnode_version = os.environ["TARGET_MLNODE_VERSION"]
    threshold = int(os.environ["ADOPTION_THRESHOLD"])
    entries, epoch = fetch_active_snapshot()

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

    results = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as pool:
        futures = {
            pool.submit(fetch_version_info, participant["url"]): participant
            for participant in parsed
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

    adopted_weight = 0
    unreachable = 0

    for participant in parsed:
        info = results.get(participant["id"])

        if info is None:
            unreachable += 1
            unknown_weight += participant["weight"]
            continue

        version = info.get("api_version")
        if version is None:
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

    pct = (
        adopted_weight / network_total_weight * 100
        if network_total_weight
        else 0
    )

    print(
        f"Adoption: {adopted_weight}/{network_total_weight} "
        f"({pct:.1f}%), "
        f"threshold {threshold}, "
        f"unreachable hosts: {unreachable}, "
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
    for participant_id, details in sorted(mlnode["hosts"].items()):
        print(
            f"MLNode host {participant_id}: status={details['status']}; "
            f"weight={details['weight']}; versions={details['versions']}"
        )

    previous = load_json(STATE_FILE)
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("upgrade-adoption state must be a JSON object")

    threshold_reached = adopted_weight >= threshold

    unknown_pct = (
        unknown_weight / network_total_weight * 100
        if network_total_weight
        else 0
    )
    
    if unknown_pct < 10:
        unknown_band = "normal"
    elif unknown_pct < 25:
        unknown_band = "degraded"
    else:
        unknown_band = "unreliable"
    
    crossed_threshold_now = (
        threshold_reached
        and (
            previous is None
            or not previous.get("threshold_reached", False)
        )
    )
    
    api_changed = (
        previous is None
        or previous.get("target_version") != target_version
        or previous.get("threshold") != threshold
        or previous.get("threshold_reached") != threshold_reached
        or previous.get("unknown_band") != unknown_band
    )

    previous_state = previous or {}
    current_mlnode_signature = mlnode_signature(mlnode)
    mlnode_changed, reported_signature, candidate_signature, candidate_runs = (
        evaluate_mlnode_notification(
            previous_state,
            target_mlnode_version,
            current_mlnode_signature,
            force=api_changed,
        )
    )
    changed = api_changed or mlnode_changed

    epoch_note = (
        f" (эпоха {epoch})"
        if epoch is not None
        else ""
    )

    if changed:
        status_line = (
            "✅ Порог достигнут!\n"
            if crossed_threshold_now
            else ""
        )

        message = (
            f"📊 Прогресс обновлений{epoch_note}\n\n"
            f"API {target_version}\n"
            f"{adopted_weight} / {network_total_weight} "
            f"веса ({pct:.1f}%)\n"
            f"Порог: {threshold}\n"
            f"Недоступных хостов: {unreachable}\n"
            f"Неизвестный вес: {unknown_weight}\n"
            f"{status_line}"
            f"Участников без веса: {unknown_participants}\n\n"
            f"MLNode {target_mlnode_version}\n"
            f"Подтверждено MLNode: {mlnode['target_node_count']} из "
            f"{mlnode['visible_node_count']} видимых\n"
            f"Полностью обновлённых хостов: "
            f"{mlnode['fully_updated_host_count']} из "
            f"{network_host_count} с известным весом\n"
            f"Их вес: {mlnode['fully_updated_weight']} / "
            f"{network_total_weight} "
            f"({mlnode['fully_updated_weight_percent']:.1f}%)\n"
            f"Смешанных хостов: {mlnode['mixed_host_count']}\n"
            f"Неполные/недоступные MLNode-данные: "
            f"{mlnode['unknown_host_count']} хостов, "
            f"{mlnode['unknown_weight']} веса\n"
        )

        send_telegram_message(message, parse_mode=None)

        print("Sent Telegram update.")
    else:
        print("No change since last run, no message sent.")

    save_json_atomic(
        STATE_FILE,
        {
            "target_version": target_version,
            "threshold": threshold,
            "adopted_weight": adopted_weight,
            "network_total_weight": network_total_weight,
            "unknown_weight": unknown_weight,
            "unreachable_count": unreachable,
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
            "mlnode_unknown_host_count": mlnode["unknown_host_count"],
            "mlnode_unknown_weight": mlnode["unknown_weight"],
            "mlnode_unavailable_host_count": mlnode[
                "unavailable_host_count"
            ],
            "mlnode_version_distribution": mlnode["version_distribution"],
            "mlnode_hosts": mlnode["hosts"],
            "mlnode_reported_signature": reported_signature,
            "mlnode_candidate_signature": candidate_signature,
            "mlnode_candidate_runs": candidate_runs,
        },
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
