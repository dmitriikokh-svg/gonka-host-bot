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
import os
import sys

import requests

PARTICIPANTS_URL = "http://node2.gonka.ai:8000/v1/epochs/current/participants"
STATE_FILE = "state/excluded.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def fetch_excluded():
    resp = requests.get(PARTICIPANTS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict) or "excluded_participants" not in data:
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:3000]
        raise ValueError(
            "Could not find 'excluded_participants' in the response.\n"
            f"Preview (truncated):\n{preview}"
        )

    return data["excluded_participants"] or []


def participant_id(entry):
    """Extract a stable, readable identifier for one excluded-participant entry."""
    if isinstance(entry, dict):
        for key in ("participant_id", "address", "id", "inference_url"):
            if key in entry:
                return str(entry[key])
    if isinstance(entry, str):
        return entry
    # Fallback: keep the whole thing, less pretty but never silently drops data
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
    entries = fetch_excluded()
    current_ids = {participant_id(e) for e in entries}
    previous_ids = load_previous_ids()

    if previous_ids is None:
        print(f"First run. Saving baseline of {len(current_ids)} excluded participant(s), no notification sent.")
        save_state(current_ids)
        return

    new_ids = current_ids - previous_ids

    if new_ids:
        lines = "\n".join(f"\u2022 <code>{i}</code>" for i in sorted(new_ids))
        message = (
            f"\u26A0\uFE0F \u041d\u043e\u0432\u043e\u0435 \u0438\u0441\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 "
            f"\u043f\u043e\u0441\u043b\u0435 cPoC ({len(new_ids)}):\n{lines}"
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
