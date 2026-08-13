import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

from bot_common import (
    SourcesUnavailable,
    build_host_details,
    fetch_json_with_fallback,
    load_json,
    participant_total_weight,
    save_json_atomic,
    send_telegram_message,
)


class FakeResponse:
    def __init__(self, payload=None, *, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse({"ok": True})


class JsonStateTests(unittest.TestCase):
    def test_missing_state_returns_independent_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            first = load_json(path, {"nodes": []})
            first["nodes"].append("node1")
            second = load_json(path, {"nodes": []})
            self.assertEqual(second, {"nodes": []})

    def test_atomic_save_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "data.json"
            save_json_atomic(path, {"value": "GNK"})
            self.assertEqual(json.loads(path.read_text()), {"value": "GNK"})
            self.assertFalse(path.with_name("data.json.tmp").exists())


class ParticipantWeightTests(unittest.TestCase):
    def test_total_weight_completeness_and_host_share(self):
        entries = [
            {"index": "a", "weight": 75},
            {"index": "b", "weight": 25},
        ]
        self.assertEqual(participant_total_weight(entries), (100, True))
        details = build_host_details(entries[0], 1, 2, total_weight=100)
        self.assertIn("Доля общего веса сети: <b>75.0%</b>", details)

    def test_missing_weight_marks_total_incomplete_and_old_call_stays_safe(self):
        entries = [{"index": "a", "weight": 75}, {"index": "b"}]
        self.assertEqual(participant_total_weight(entries), (75, False))
        details = build_host_details(entries[0], 1, 2)
        self.assertIn("Доля общего веса сети: нет данных", details)


class SourceFallbackTests(unittest.TestCase):
    def test_uses_second_source_after_first_fails(self):
        session = FakeSession(
            [
                FakeResponse(error=RuntimeError("down")),
                FakeResponse({"height": 123}),
            ]
        )
        value, source = fetch_json_with_fallback(
            ["https://first", "https://second"],
            attempts=1,
            retry_delay=0,
            session=session,
        )
        self.assertEqual(value, {"height": 123})
        self.assertEqual(source, "https://second")

    def test_rejects_invalid_payload_and_falls_back(self):
        def validate(payload):
            if "height" not in payload:
                raise ValueError("height missing")

        session = FakeSession(
            [FakeResponse({"wrong": 1}), FakeResponse({"height": 456})]
        )
        value, source = fetch_json_with_fallback(
            ["https://first", "https://second"],
            attempts=1,
            retry_delay=0,
            validator=validate,
            session=session,
        )
        self.assertEqual(value["height"], 456)
        self.assertEqual(source, "https://second")

    def test_raises_aggregated_error_when_all_sources_fail(self):
        session = FakeSession(
            [
                FakeResponse(error=RuntimeError("first down")),
                FakeResponse(error=RuntimeError("second down")),
            ]
        )
        with self.assertRaises(SourcesUnavailable) as context:
            fetch_json_with_fallback(
                ["https://first", "https://second"],
                attempts=1,
                retry_delay=0,
                session=session,
            )
        self.assertIn("first down", str(context.exception))
        self.assertIn("second down", str(context.exception))


class TelegramTests(unittest.TestCase):
    def test_thread_id_is_required_and_loaded_lazily(self):
        session = FakeSession([])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
            "TELEGRAM_MESSAGE_THREAD_ID": "42",
        }
        with patch.dict(os.environ, environment, clear=True):
            send_telegram_message("hello", session=session)

        _, kwargs = session.post_calls[0]
        self.assertEqual(kwargs["json"]["message_thread_id"], 42)
        self.assertEqual(kwargs["json"]["parse_mode"], "HTML")

    def test_message_is_sent_to_primary_topic_and_secondary_channel(self):
        session = FakeSession([])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "primary-chat",
            "TELEGRAM_MESSAGE_THREAD_ID": "42",
            "TELEGRAM_SECONDARY_CHAT_ID": "secondary-chat",
        }
        with patch.dict(os.environ, environment, clear=True):
            send_telegram_message("hello", session=session)

        self.assertEqual(len(session.post_calls), 2)
        primary = session.post_calls[0][1]["json"]
        secondary = session.post_calls[1][1]["json"]
        self.assertEqual(primary["chat_id"], "primary-chat")
        self.assertEqual(primary["message_thread_id"], 42)
        self.assertEqual(secondary["chat_id"], "secondary-chat")
        self.assertNotIn("message_thread_id", secondary)

    def test_duplicate_secondary_chat_is_not_sent_twice(self):
        session = FakeSession([])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "same-chat",
            "TELEGRAM_MESSAGE_THREAD_ID": "42",
            "TELEGRAM_SECONDARY_CHAT_ID": "same-chat",
        }
        with patch.dict(os.environ, environment, clear=True):
            send_telegram_message("hello", session=session)

        self.assertEqual(len(session.post_calls), 1)

    def test_missing_thread_id_refuses_to_send_to_general(self):
        session = FakeSession([])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(RuntimeError, "refusing to send outside"),
        ):
            send_telegram_message("hello", session=session)
        self.assertEqual(session.post_calls, [])

    def test_invalid_thread_id_is_rejected_before_request(self):
        session = FakeSession([])
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
            "TELEGRAM_MESSAGE_THREAD_ID": "not-a-number",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(ValueError, "must be an integer"),
        ):
            send_telegram_message("hello", session=session)
        self.assertEqual(session.post_calls, [])


if __name__ == "__main__":
    unittest.main()
