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

PARTICIPANTS_URL = "http://node2.gonka.ai:8000/v1/epochs/current/participants"
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

    entries = data.get("active_participants")
    if not isinstance(entries, list):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        raise ValueError(
            "'active_participants' is not a plain list as expected.\n"
            f"Preview:\n{preview}"
        )
    return entries


def participant_identity(entry):
    """Best-effort extraction of (id, weight, url) from one participant entry.
    Field names are guessed and NOT yet verified against a live response --
    see the calibration printout below on first run.
    """
    pid = None
    for key in ("participant_id", "address", "id"):
        if key in entry:
            pid = str(entry[key])
            break

    weight = None
    for key in ("weight", "power", "voting_power", "stake"):
        if key in entry:
            try:
                weight = int(entry[key])
            except (TypeError, ValueError):
                pass
            break

    url = None
    for key in ("inference_url", "url", "api_url"):
        if key in entry:
            url = str(entry[key]).rstrip("/")
            break

    return pid, weight, url


def fetch_version(url):
    """Query one participant's own /v1/versions. Returns a version string
    or None if unreachable / unparsable."""
    try:
        resp = requests.get(f"{url}/v1/versions", timeout=VERSION_CHECK_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    for key in ("api_version", "version", "decentralized_api_version"):
        if isinstance(data, dict) and key in data:
            return str(data[key])
    # Unknown shape -- return the raw dump so it shows up in logs once,
    # instead of silently guessing wrong.
    return f"UNRECOGNIZED_SHAPE:{json.dumps(data)[:200]}"


def main():
    entries = fetch_active_participants()

    # --- Calibration printout: shows real structure of the first entry.
    # Remove/ignore once field names above are confirmed correct.
    if entries:
        print("Sample participant entry (for field-name calibration):")
        print(json.dumps(entries[0], indent=2, ensure_ascii=False)[:1500])

    parsed = []
    for e in entries:
        pid, weight, url = participant_identity(e)
        if url:
            parsed.append({"id": pid, "weight": weight, "url": url})
        else:
            print(f"WARNING: no usable URL field found for participant {pid}")

    total_weight = sum(p["weight"] or 0 for p in parsed)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_version, p["url"]): p for p in parsed}
        first_printed = False
        for future in concurrent.futures.as_completed(futures):
            p = futures[future]
            version = future.result()
            results[p["id"]] = version
            if version and not first_printed:
                print(f"Sample /v1/versions result (for calibration): {version}")
                first_printed = True

    adopted_weight = 0
    unreachable = 0
    for p in parsed:
        v = results.get(p["id"])
        if v is None:
            unreachable += 1
        elif v == TARGET_VERSION:
            adopted_weight += p["weight"] or 0

    pct = (adopted_weight / total_weight * 100) if total_weight else 0
    print(
        f"Adoption: {adopted_weight}/{total_weight} ({pct:.1f}%), "
        f"threshold {THRESHOLD}, unreachable hosts: {unreachable}"
    )

    previous = None
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            previous = json.load(f)

    crossed_threshold_now = (
        adopted_weight >= THRESHOLD
        and (previous is None or previous.get("adopted_weight", 0) < THRESHOLD)
    )
    changed = previous is None or previous.get("adopted_weight") != adopted_weight

    if changed:
        status_line = "\u2705 \u041f\u043e\u0440\u043e\u0433 \u0434\u043e\u0441\u0442\u0438\u0433\u043d\u0443\u0442!" if crossed_threshold_now else ""
        message = (
            f"\U0001F4CA \u041f\u0440\u043e\u0433\u0440\u0435\u0441\u0441 \u0430\u043f\u0433\u0440\u0435\u0439\u0434\u0430 \u0434\u043e {TARGET_VERSION}:\n"
            f"{adopted_weight} / {total_weight} \u0432\u0435\u0441\u0430 ({pct:.1f}%)\n"
            f"\u041f\u043e\u0440\u043e\u0433: {THRESHOLD}\n"
            f"\u041d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0445 \u0445\u043e\u0441\u0442\u043e\u0432: {unreachable}\n"
            f"{status_line}"
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=15).raise_for_status()
        print("Sent Telegram update.")
    else:
        print("No change since last run, no message sent.")

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"adopted_weight": adopted_weight, "total_weight": total_weight}, f, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
