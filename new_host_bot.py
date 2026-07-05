"""
Gonka new-host notifier.

Polls the active-participants endpoint, compares the result against the
previously saved state, and sends a Telegram message if new hosts appeared.

State is stored as a plain JSON file (state/hosts.json) and is meant to be
committed back into the repo by the GitHub Actions workflow, so no external
database is needed.
"""

import json
import os
import sys

import requests

PARTICIPANTS_URL = "http://node2.gonka.ai:8000/v1/epochs/current/participants"
STATE_FILE = "state/hosts.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def fetch_participants():
    resp = requests.get(PARTICIPANTS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # NOTE: the exact shape of this response has not been fully verified
    # against a live call yet. This function tries the most likely shapes:
    # either a bare list, or a dict with the list under one of a few
    # common keys. Run the script once manually (see README) and check
    # the printed "Current total" / any KeyError to confirm which branch
    # actually applies, then simplify this function if needed.
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("active_participants", "participants", "result"):
            if key in data and isinstance(data[key], list):
                return data[key]
        raise ValueError(
            f"Could not find a participants list in the response. "
            f"Top-level keys were: {list(data.keys())}"
        )

    raise ValueError("Unexpected response type from participants endpoint")


def participant_id(entry):
    """Extract a stable identifier for a single participant entry."""
    if isinstance(entry, dict):
        for key in ("participant_id", "address", "id", "inference_url"):
            if key in entry:
                return str(entry[key])
    # Fallback: use the whole entry as a string. Works, but less readable
    # in notifications -- fine as a safety net until the real field is confirmed.
    return json.dumps(entry, sort_keys=True)


def load_previous_ids():
    if not os.path.exists(STATE_FILE):
        return None  # signals "first run"
    with open(STATE_FILE, "r") as f:
        return set(json.load(f))


def save_state(ids):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2, ensure_ascii=False)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def main():
    entries = fetch_participants()
    current_ids = {participant_id(e) for e in entries}
    previous_ids = load_previous_ids()

    if previous_ids is None:
        # First ever run: just save the baseline, don't spam a notification
        # for every host that already existed before we started watching.
        print(f"First run. Saving baseline of {len(current_ids)} host(s), no notification sent.")
        save_state(current_ids)
        return

    new_ids = current_ids - previous_ids

    if new_ids:
        lines = "\n".join(f"\u2022 <code>{i}</code>" for i in sorted(new_ids))
        message = f"\U0001F195 \u041d\u043e\u0432\u044b\u0439 \u0445\u043e\u0441\u0442(\u044b) \u0432 \u0441\u0435\u0442\u0438 Gonka ({len(new_ids)}):\n{lines}"
        send_telegram_message(message)
        print(f"Sent notification for {len(new_ids)} new host(s).")
    else:
        print(f"No new hosts. Current total: {len(current_ids)}.")

    save_state(current_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level guard for a CI script
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
