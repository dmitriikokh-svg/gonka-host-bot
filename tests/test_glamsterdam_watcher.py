import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import glamsterdam_watcher as watcher


def fields(
    *,
    status="Planning",
    activation_date="2028-01-01",
    projected_activation="2027-12-15",
    tagline="Scale Ethereum",
):
    return {
        "status": status,
        "activationDate": activation_date,
        "projectedActivation": projected_activation,
        "tagline": tagline,
    }


def source_text(*, projected_line="projectedActivation: '2027-12-15',"):
    return f"""
export const upgrades = [
  {{
    id: 'glamsterdam',
    status: 'Planning',
    activationDate: '2028-01-01',
    {projected_line}
    tagline: 'Scale Ethereum',
  }},
  {{ id: 'next-upgrade', status: 'Draft' }},
]
"""


class GlamsterdamParsingTests(unittest.TestCase):
    def test_parses_all_three_tracked_fields(self):
        with patch.object(
            watcher,
            "fetch_text_with_fallback",
            return_value=(source_text(), "official"),
        ):
            self.assertEqual(watcher.fetch_glamsterdam_fields(), fields())

    def test_missing_optional_projected_activation_is_allowed(self):
        with patch.object(
            watcher,
            "fetch_text_with_fallback",
            return_value=(source_text(projected_line=""), "official"),
        ):
            result = watcher.fetch_glamsterdam_fields()

        self.assertIsNone(result["projectedActivation"])

    def test_malformed_projected_activation_is_rejected(self):
        with patch.object(
            watcher,
            "fetch_text_with_fallback",
            return_value=(
                source_text(projected_line="projectedActivation: 123,"),
                "official",
            ),
        ):
            with self.assertRaises(ValueError):
                watcher.fetch_glamsterdam_fields()

    def test_invalid_official_source_is_rejected_without_saving_state(self):
        invalid = "id: 'glamsterdam', status: 'Planning'"
        with (
            patch.object(
                watcher,
                "fetch_text_with_fallback",
                return_value=(invalid, "official"),
            ),
            patch.object(watcher, "save_state") as save_state,
        ):
            with self.assertRaises(ValueError):
                watcher.main()

        save_state.assert_not_called()


class GlamsterdamTransitionTests(unittest.TestCase):
    def test_old_state_migrates_projected_activation_without_message(self):
        current = fields()
        previous = {
            "status": current["status"],
            "activationDate": current["activationDate"],
            "tagline": current["tagline"],
        }
        with (
            patch.object(watcher, "fetch_glamsterdam_fields", return_value=current),
            patch.object(watcher, "load_previous_state", return_value=previous),
            patch.object(watcher, "save_state") as save_state,
            patch.object(watcher, "send_telegram_message") as send_message,
        ):
            watcher.main()

        send_message.assert_not_called()
        save_state.assert_called_once_with(current)

    def test_projected_date_change_sends_one_estimate_message(self):
        current = fields(projected_activation="2027-12-22")
        stored = fields(projected_activation="2027-12-15")

        def load_state():
            return dict(stored)

        def save_state(value):
            stored.clear()
            stored.update(value)

        with (
            patch.object(watcher, "fetch_glamsterdam_fields", return_value=current),
            patch.object(watcher, "load_previous_state", side_effect=load_state),
            patch.object(watcher, "save_state", side_effect=save_state),
            patch.object(watcher, "send_telegram_message") as send_message,
        ):
            watcher.main()
            watcher.main()

        send_message.assert_called_once()
        message = send_message.call_args.args[0]
        self.assertIn("Ориентировочная дата (не подтверждена)", message)
        self.assertIn("2027-12-15", message)
        self.assertIn("2027-12-22", message)

    def test_status_and_activation_date_changes_keep_alerting(self):
        current = fields(status="Scheduled", activation_date="2028-02-02")
        previous = fields(status="Planning", activation_date="2028-01-01")
        with (
            patch.object(watcher, "fetch_glamsterdam_fields", return_value=current),
            patch.object(watcher, "load_previous_state", return_value=previous),
            patch.object(watcher, "save_state"),
            patch.object(watcher, "send_telegram_message") as send_message,
        ):
            watcher.main()

        send_message.assert_called_once()
        message = send_message.call_args.args[0]
        self.assertIn("Дата активации в источнике", message)
        self.assertIn("Статус", message)

    def test_unchanged_data_does_not_send_message(self):
        current = fields()
        with (
            patch.object(watcher, "fetch_glamsterdam_fields", return_value=current),
            patch.object(watcher, "load_previous_state", return_value=dict(current)),
            patch.object(watcher, "save_state") as save_state,
            patch.object(watcher, "send_telegram_message") as send_message,
        ):
            watcher.main()

        send_message.assert_not_called()
        save_state.assert_called_once_with(current)

    def test_import_does_not_require_telegram_secrets(self):
        environment = os.environ.copy()
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_MESSAGE_THREAD_ID",
            "TELEGRAM_SECONDARY_CHAT_ID",
        ):
            environment.pop(name, None)

        result = subprocess.run(
            [sys.executable, "-c", "import glamsterdam_watcher"],
            cwd=watcher.ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
