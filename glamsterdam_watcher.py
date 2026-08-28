"""
Glamsterdam (Ethereum hard fork) release-date watcher.

Forkcast (https://forkcast.org/upgrade/glamsterdam/) is a React SPA, so it
can't be scraped directly. Its underlying data, however, is open source and
lives in a plain TypeScript file in the ethereum/forkcast GitHub repo:

  https://github.com/ethereum/forkcast/blob/main/src/data/upgrades.ts

This script fetches that file's raw content, extracts the `status`,
`activationDate` and optional `projectedActivation` fields for the
'glamsterdam' entry (via regex -- it's a .ts object literal, not JSON, so it
can't be json.loads'd directly), and sends a Telegram alert whenever a tracked
value changes. `projectedActivation` is only an estimate, not a confirmed
mainnet activation date.
"""

import re
import sys
from pathlib import Path

from bot_common import (
    escape_html,
    fetch_text_with_fallback,
    load_json,
    save_json_atomic,
    send_telegram_message,
)

UPGRADES_TS_URL = "https://raw.githubusercontent.com/ethereum/forkcast/main/src/data/upgrades.ts"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "glamsterdam_upgrade.json"


def optional_quoted_field(block, field):
    match = re.search(rf"\b{re.escape(field)}\s*:\s*'([^']*)'", block)
    if match:
        return match.group(1)
    if re.search(rf"\b{re.escape(field)}\s*:", block):
        raise ValueError(f"Invalid {field} field in the glamsterdam block")
    return None


def fetch_glamsterdam_fields():
    text, source = fetch_text_with_fallback([UPGRADES_TS_URL], timeout=30)
    print(f"Forkcast source: {source}")

    start = text.find("id: 'glamsterdam'")
    if start == -1:
        raise ValueError("Could not find 'glamsterdam' entry in upgrades.ts -- "
                          "file structure may have changed, needs manual check.")

    next_id = text.find("id: '", start + len("id: 'glamsterdam'"))
    block = text[start: next_id if next_id != -1 else start + 3000]

    status_match = re.search(r"status:\s*'([^']*)'", block)
    date_match = re.search(r"activationDate:\s*'([^']*)'", block)
    projected_activation = optional_quoted_field(block, "projectedActivation")
    tagline_match = re.search(r"tagline:\s*'([^']*)'", block)

    if not status_match or not date_match:
        raise ValueError(
            "Could not find status/activationDate fields in the glamsterdam "
            "block -- field names may have changed. Block preview:\n"
            + block[:800]
        )

    return {
        "status": status_match.group(1),
        "activationDate": date_match.group(1),
        "projectedActivation": projected_activation,
        "tagline": tagline_match.group(1) if tagline_match else None,
    }


def load_previous_state():
    value = load_json(STATE_FILE)
    if value is not None and not isinstance(value, dict):
        raise ValueError("Glamsterdam state must be a JSON object")
    return value


def save_state(fields):
    save_json_atomic(STATE_FILE, fields)


def display_value(value):
    return escape_html(value) if value is not None else "не указана"


def main():
    current = fetch_glamsterdam_fields()
    previous = load_previous_state()

    if previous is None:
        print(f"First run. Baseline saved: {current}. No notification sent.")
        save_state(current)
        return

    status_changed = current["status"] != previous.get("status")
    date_changed = current["activationDate"] != previous.get("activationDate")
    projected_changed = (
        "projectedActivation" in previous
        and current.get("projectedActivation") != previous.get("projectedActivation")
    )

    if status_changed or date_changed or projected_changed:
        lines = [f"\U0001F517 <b>Glamsterdam upgrade \u2014 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0435!</b>"]
        if date_changed:
            lines.append(
                "Дата активации в источнике: "
                f"{display_value(previous.get('activationDate'))} \u2192 "
                f"<b>{display_value(current['activationDate'])}</b>"
            )
        if projected_changed:
            lines.append(
                "Ориентировочная дата (не подтверждена): "
                f"{display_value(previous.get('projectedActivation'))} \u2192 "
                f"<b>{display_value(current.get('projectedActivation'))}</b>"
            )
        if status_changed:
            lines.append(
                f"\u0421\u0442\u0430\u0442\u0443\u0441: {escape_html(previous.get('status'))} \u2192 "
                f"<b>{escape_html(current['status'])}</b>"
            )
        if current.get("tagline"):
            lines.append(f"<i>{escape_html(current['tagline'])}</i>")
        lines.append("https://forkcast.org/upgrade/glamsterdam/")

        send_telegram_message("\n".join(lines))
        print("Change detected, sent Telegram alert:", current)
    else:
        print(f"No change. Current: {current}")

    save_state(current)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
