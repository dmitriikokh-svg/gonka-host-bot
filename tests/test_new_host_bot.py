import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import new_host_bot as watcher


class ParticipantDetailsTests(unittest.TestCase):
    def test_extracts_models_and_nested_ml_node_count(self):
        entry = {
            "models": [
                "MiniMaxAI/MiniMax-M2.7",
                "moonshotai/Kimi-K2.6",
                "moonshotai/Kimi-K2.6",
            ],
            "ml_nodes": [
                {"ml_nodes": [{"node_id": "node1"}]},
                {"ml_nodes": [{"node_id": "node2"}]},
            ],
        }

        self.assertEqual(
            watcher.participant_models(entry),
            ["MiniMax-M2.7", "Kimi-K2.6"],
        )
        self.assertEqual(watcher.participant_ml_node_count(entry), 2)

    def test_weight_ranks_share_a_position_for_equal_weights(self):
        entries = [
            {"index": "first", "weight": "10000"},
            {"index": "second", "weight": "7719"},
            {"index": "also-second", "weight": 7719},
            {"index": "unknown", "weight": None},
        ]

        self.assertEqual(
            watcher.participant_weight_ranks(entries),
            {"first": 1, "second": 2, "also-second": 2},
        )

    def test_missing_optional_fields_are_reported_as_unavailable(self):
        details = watcher.build_host_details({"index": "gonka1new"}, None, 3)

        self.assertIn("Вес: нет данных", details)
        self.assertIn("Место по весу: нет данных", details)
        self.assertIn("Модели: нет данных", details)
        self.assertIn("ML-ноды: нет данных", details)
        self.assertIn("API: нет данных", details)


class NotificationTests(unittest.TestCase):
    def test_main_sends_enriched_new_host_notification(self):
        entries = [
            {
                "index": "gonka1old",
                "inference_url": "https://old.example",
                "weight": "10000",
                "models": ["MiniMaxAI/MiniMax-M2.7"],
                "ml_nodes": [{"ml_nodes": [{"node_id": "old-node"}]}],
            },
            {
                "index": "gonka1new",
                "inference_url": "https://new.example/?a=1&b=2",
                "weight": "7719",
                "models": [
                    "MiniMaxAI/MiniMax-M2.7",
                    "moonshotai/Kimi-K2.6",
                ],
                "ml_nodes": [
                    {"ml_nodes": [{"node_id": "node811"}]},
                    {"ml_nodes": [{"node_id": "node801"}]},
                ],
            },
        ]

        with (
            patch.object(watcher, "fetch_participants", return_value=entries),
            patch.object(watcher, "fetch_current_epoch", return_value=355),
            patch.object(watcher, "load_previous_ids", return_value={"gonka1old"}),
            patch.object(watcher, "append_to_log") as append_to_log,
            patch.object(watcher, "send_telegram_message") as send_message,
            patch.object(watcher, "save_state") as save_state,
        ):
            watcher.main()

        message = send_message.call_args.args[0]
        self.assertIn("(1) (\u044d\u043f\u043e\u0445\u0430 355)", message)
        self.assertIn("<code>gonka1new</code>", message)
        self.assertIn("Вес: <b>7 719</b>", message)
        self.assertIn("Место по весу: <b>2 из 2</b>", message)
        self.assertIn("<code>MiniMax-M2.7</code>", message)
        self.assertIn("<code>Kimi-K2.6</code>", message)
        self.assertIn("ML-ноды: <b>2</b>", message)
        self.assertIn(
            "API: <code>https://new.example/?a=1&amp;b=2</code>",
            message,
        )
        append_to_log.assert_called_once_with(
            [("gonka1new", 355, "https://new.example/?a=1&b=2")]
        )
        save_state.assert_called_once_with({"gonka1old", "gonka1new"})


if __name__ == "__main__":
    unittest.main()
