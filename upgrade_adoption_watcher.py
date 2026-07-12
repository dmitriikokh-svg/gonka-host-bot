"""
Gonka upgrade-adoption watcher.

For every active participant, checks its own /v1/versions endpoint to see
which API version it's running, sums up the "weight" of participants that
are already on the target version, and reports progress toward a weight
threshold to Telegram.

Target version and weight threshold are read from GitHub Actions
repository variables (not secrets, not code) so they can be updated for
future upgrades without touching this file:
  - TARGET_API_VERSION   e.g. "v0.2.13-post8"
  - ADOPTION_THRESHOLD   e.g. "267800"
"""

import concurrent.futures
import json
import os
import sys

import requests
import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

PARTICIPANTS_URL = "http://node2.gonka.ai:8000/v1/epochs/current/participants"
EPOCH_URL = "https://node3.gonka.ai/v1/epochs/latest"
STATE_FILE = "state/upgrade_adoption.json"
VERSION_CHECK_TIMEOUT = 5
MAX_WORKERS = 20

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TARGET_VERSION = os.environ["TARGET_API_VERSION"]
THRESHOLD = int(os.environ["ADOPTION_THRESHOLD"])


def fetch_active_participants():
    resp = requests.get(PARTICIPANTS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    entries = data.get("active_participants", {}).get("participants")
    if not isinstance(entries, list):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        raise ValueError(
            "Could not find participants list under active_participants.participants.\n"
            f"Preview:\n{preview}"
        )
    return entries

def fetch_current_epoch():
    try:
        resp = requests.get(EPOCH_URL, timeout=15)
        resp.raise_for_status()
        return resp.json().get("latest_epoch", {}).get("index")
    except Exception:
        return None

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

def fetch_version(url, retries=2, timeout=VERSION_CHECK_TIMEOUT):
    """Query one participant's own /v1/versions, with a couple of retries
    before giving up -- a single slow response shouldn't count a host as
    unreachable and silently drop its weight from the numerator."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{url}/v1/versions", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("api_version"), dict):
                v = data["api_version"].get("version")
                if v:
                    return str(v)
            for key in ("version", "decentralized_api_version"):
                if isinstance(data, dict) and key in data:
                    return str(data[key])
            return f"UNRECOGNIZED_SHAPE:{json.dumps(data)[:200]}"
        except Exception as exc:
            last_error = exc
            continue
    print(f"WARNING: {url} unreachable after {retries + 1} attempts ({last_error})")
    return None


def normalize_version(v):
    return v.lstrip("vV") if isinstance(v, str) else v


def main():
    entries = fetch_active_participants()

    if entries:
        print("Sample participant entry (for field-name calibration):")
        print(json.dumps(entries[0], indent=2, ensure_ascii=False)[:1500])

    parsed = []
    seen_ids = set()
    network_total_weight = 0
    unknown_weight = 0

    for entry in entries:
        pid, weight, url = participant_identity(entry)

        if not pid:
            raise ValueError("participant has no stable id")

        if pid in seen_ids:
            raise ValueError(f"duplicate participant id: {pid}")

        seen_ids.add(pid)

        if weight is None or weight < 0:
            raise ValueError(
                f"participant {pid} has invalid weight: {weight!r}"
            )

        network_total_weight += weight

        if not url:
            print(
                f"WARNING: no usable URL field found for participant {pid}"
            )
            unknown_weight += weight
            continue

        try:
            safe_url = validate_public_url(url)
        except ValueError as exc:
            print(
                f"WARNING: skipping unsafe URL for {pid}: {exc}"
            )
            unknown_weight += weight
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
            pool.submit(fetch_version, participant["url"]): participant
            for participant in parsed
        }

        first_printed = False

        for future in concurrent.futures.as_completed(futures):
            participant = futures[future]
            version = future.result()

            results[participant["id"]] = version

            if version and not first_printed:
                print(
                    "Sample /v1/versions result "
                    f"(for calibration): {version}"
                )
                first_printed = True

    adopted_weight = 0
    unreachable = 0

    for participant in parsed:
        version = results.get(participant["id"])

        if version is None:
            unreachable += 1
            unknown_weight += participant["weight"]
            continue

        if normalize_version(version) == normalize_version(
            TARGET_VERSION
        ):
            adopted_weight += participant["weight"]

    pct = (
        adopted_weight / network_total_weight * 100
        if network_total_weight
        else 0
    )

    threshold_reached = (
        adopted_weight >= THRESHOLD
        and unknown_weight == 0
    )

    print(
        f"Adoption: {adopted_weight}/{network_total_weight} "
        f"({pct:.1f}%), "
        f"threshold {THRESHOLD}, "
        f"unreachable hosts: {unreachable}, "
        f"unknown weight: {unknown_weight}"
    )

    previous = None

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            previous = json.load(f)

    threshold_reached = adopted_weight >= THRESHOLD

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
    
    changed = (
        previous is None
        or previous.get("target_version") != TARGET_VERSION
        or previous.get("threshold") != THRESHOLD
        or previous.get("threshold_reached") != threshold_reached
        or previous.get("unknown_band") != unknown_band
    )

    epoch = fetch_current_epoch()
    epoch_note = (
        f" (эпоха {epoch})"
        if epoch is not None
        else ""
    )

    if changed:
        status_line = (
            "✅ Порог достигнут!"
            if crossed_threshold_now
            else ""
        )

        message = (
            f"📊 Прогресс апгрейда до "
            f"{TARGET_VERSION}{epoch_note}:\n"
            f"{adopted_weight} / {network_total_weight} "
            f"веса ({pct:.1f}%)\n"
            f"Порог: {THRESHOLD}\n"
            f"Недоступных хостов: {unreachable}\n"
            f"Неизвестный вес: {unknown_weight}\n"
            f"{status_line}"
        )

        telegram_url = (
            f"https://api.telegram.org/"
            f"bot{BOT_TOKEN}/sendMessage"
        )

        requests.post(
            telegram_url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
            },
            timeout=15,
        ).raise_for_status()

        print("Sent Telegram update.")
    else:
        print("No change since last run, no message sent.")

    os.makedirs(
        os.path.dirname(STATE_FILE),
        exist_ok=True,
    )

    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "target_version": TARGET_VERSION,
                "threshold": THRESHOLD,
                "adopted_weight": adopted_weight,
                "network_total_weight": network_total_weight,
                "unknown_weight": unknown_weight,
                "unreachable_count": unreachable,
                "threshold_reached": threshold_reached,
                "unknown_band": unknown_band,
            },
            f,
            indent=2,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
