"""
Gonka new-host notifier.

Polls the participants endpoint, compares against previously saved state,
and sends a Telegram alert for newly appeared hosts. Also appends every
newly seen host to a running CSV table (state/host_log.csv) with the
epoch it was first spotted in, so there's a persistent history to refer
back to -- not just a live "current snapshot" diff.
"""

import csv
import json
import os
import sys
import html
from datetime import datetime, timezone

import requests

PARTICIPANTS_URL = "http://node2.gonka.ai:8000/v1/epochs/current/participants"
EPOCH_URL = "https://node3.gonka.ai/v1/epochs/latest"
STATE_FILE = "state/hosts.json"
LOG_FILE = "state/host_log.csv"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def fetch_participants():
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


def participant_id(entry):
    # "index" is the confirmed real field name (gonka1... address).
    for key in ("index", "participant_id", "address", "id", "inference_url"):
        if isinstance(entry, dict) and key in entry:
            return str(entry[key])
    return json.dumps(entry, sort_keys=True)


def participant_url(entry):
    if isinstance(entry, dict):
        return entry.get("inference_url", "")
    return ""


def load_previous_ids():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return set(json.load(f))


def save_state(ids):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2, ensure_ascii=False)


def append_to_log(rows):
    """rows: list of (node_id, epoch_index, inference_url) tuples."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    is_new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["node_id", "first_seen_epoch", "first_seen_utc", "inference_url"])
        now = datetime.now(timezone.utc).isoformat()
        for node_id, epoch, url in rows:
            writer.writerow([node_id, epoch, now, url])


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
    by_id = {participant_id(e): e for e in entries}
    current_ids = set(by_id.keys())
    previous_ids = load_previous_ids()

    if previous_ids is None:
        print(f"First run. Saving baseline of {len(current_ids)} host(s), no notification sent.")
        save_state(current_ids)
        return

    new_ids = current_ids - previous_ids

    if new_ids:
        epoch = fetch_current_epoch()
        log_rows = [(nid, epoch, participant_url(by_id[nid])) for nid in new_ids]
        append_to_log(log_rows)

        lines = "\n".join(f"• <code>{html.escape(i)}</code>" for i in sorted(new_ids))
        epoch_note = f" (\u044d\u043f\u043e\u0445\u0430 {epoch})" if epoch is not None else ""
        message = f"\U0001F195 \u041d\u043e\u0432\u044b\u0439 \u0445\u043e\u0441\u0442(\u044b) \u0432 \u0441\u0435\u0442\u0438 Gonka ({len(new_ids)}){epoch_note}:\n{lines}"
        send_telegram_message(message)
        print(f"Sent notification for {len(new_ids)} new host(s). Logged to {LOG_FILE}.")
    else:
        print(f"No new hosts. Current total: {len(current_ids)}.")

    save_state(current_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
