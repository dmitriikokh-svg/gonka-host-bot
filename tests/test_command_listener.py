import os
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import command_listener
from test_bot_common import FakeSession


class CommandParseTests(unittest.TestCase):
    def test_latin_and_russian_aliases(self):
        self.assertEqual(command_listener.parse_command("/api"), "api")
        self.assertEqual(command_listener.parse_command("/API@SomeBot"), "api")
        self.assertEqual(command_listener.parse_command("/апи"), "апи")
        self.assertEqual(command_listener.parse_command("/mlnode extra"), "mlnode")
        self.assertEqual(command_listener.parse_command("/halt"), "halt")
        self.assertEqual(command_listener.parse_command("/ноды"), "ноды")
        self.assertEqual(command_listener.parse_command("/эскроу"), "эскроу")
        self.assertEqual(command_listener.parse_command("/модели"), "модели")
        self.assertEqual(command_listener.parse_command("/excluded"), "excluded")
        self.assertEqual(command_listener.parse_command("/исключения"), "исключения")
        self.assertEqual(command_listener.parse_command("/devshard"), "devshard")
        self.assertEqual(command_listener.parse_command("/gateways"), "gateways")
        self.assertEqual(command_listener.parse_command("/гейтвеи"), "гейтвеи")
        self.assertEqual(command_listener.parse_command("/брокеры"), "брокеры")
        self.assertEqual(command_listener.parse_command("/девшард"), "девшард")
        self.assertIsNone(command_listener.parse_command("api"))
        self.assertIsNone(command_listener.parse_command(""))

    def test_topic_filter_and_reply(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "-1001",
            "TELEGRAM_MESSAGE_THREAD_ID": "42",
            "TELEGRAM_SECONDARY_CHAT_ID": "secondary",
        }
        session = FakeSession([])
        state = {
            "epoch_index": 370,
            "last_check_at": "2026-08-24T07:26:00+00:00",
            "last_api_snapshot": {
                "target_version": "v0.2.15-post3",
                "adopted_weight": 10,
                "adopted_pct": 50.0,
                "network_total_weight": 20,
                "goal_pct": 80.0,
                "goal_weight": 16,
                "unreachable": 0,
                "unreachable_weight": 0,
                "unreachable_pct": 0.0,
                "other_unknown_weight": 0,
                "other_unknown_pct": 0.0,
                "unknown_band": "normal",
                "threshold_reached": False,
                "unknown_participants": 0,
            },
        }
        update = {
            "update_id": 8,
            "message": {
                "chat": {"id": -1001},
                "message_thread_id": 42,
                "text": "/апи",
            },
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(command_listener, "load_json", return_value=state),
        ):
            command_listener.handle_update(update, "-1001", 42, session=session)
        self.assertEqual(len(session.post_calls), 1)
        payload = session.post_calls[0][1]["json"]
        self.assertEqual(payload["chat_id"], "-1001")
        self.assertIn("📊 API, эпоха 370", payload["text"])
        self.assertNotIn("parse_mode", payload)

    def test_wrong_topic_is_ignored(self):
        session = FakeSession([])
        update = {
            "update_id": 9,
            "message": {
                "chat": {"id": -1001},
                "message_thread_id": 99,
                "text": "/api",
            },
        }
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "-1001",
                "TELEGRAM_MESSAGE_THREAD_ID": "42",
            },
            clear=True,
        ):
            command_listener.handle_update(update, "-1001", 42, session=session)
        self.assertEqual(session.post_calls, [])


if __name__ == "__main__":
    unittest.main()
