import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import excluded_watcher as watcher


class ExclusionDetailsTests(unittest.TestCase):
    def test_reason_and_block_are_formatted(self):
        lines = watcher.exclusion_detail_lines(
            {
                "reason": "failed_confirmation_poc",
                "exclusion_block_height": 5481065,
            }
        )

        self.assertEqual(
            lines,
            (
                "Причина: <b>не пройден Confirmation PoC</b>",
                "Блок исключения: <code>5 481 065</code>",
            ),
        )

    def test_unknown_reason_is_escaped_and_missing_block_is_safe(self):
        lines = watcher.exclusion_detail_lines({"reason": "other<&"})

        self.assertEqual(lines[0], "Причина: <b>other&lt;&amp;</b>")
        self.assertEqual(lines[1], "Блок исключения: <code>нет данных</code>")


class NotificationTests(unittest.TestCase):
    def test_main_sends_enriched_exclusion_notification(self):
        snapshot = {
            "epoch": "355",
            "excluded": [
                {
                    "address": "gonka1excluded",
                    "reason": "failed_confirmation_poc",
                    "exclusion_block_height": 5481065,
                }
            ],
            "active": [
                {
                    "index": "gonka1larger",
                    "weight": "10000",
                    "models": ["MiniMaxAI/MiniMax-M2.7"],
                    "ml_nodes": [{"ml_nodes": [{"node_id": "node1"}]}],
                    "inference_url": "https://larger.example",
                },
                {
                    "index": "gonka1excluded",
                    "weight": "7719",
                    "models": [
                        "MiniMaxAI/MiniMax-M2.7",
                        "moonshotai/Kimi-K2.6",
                    ],
                    "ml_nodes": [
                        {"ml_nodes": [{"node_id": "node811"}]},
                        {"ml_nodes": [{"node_id": "node801"}]},
                    ],
                    "inference_url": "https://excluded.example/?a=1&b=2",
                },
            ],
        }

        with (
            patch.object(watcher, "fetch_snapshot", return_value=snapshot),
            patch.object(watcher, "load_previous_ids", return_value=set()),
            patch.object(watcher, "send_telegram_message") as send_message,
            patch.object(watcher, "save_state") as save_state,
        ):
            watcher.main()

        message = send_message.call_args.args[0]
        self.assertIn("после Confirmation PoC — эпоха 355", message)
        self.assertIn("Исключён после Confirmation PoC", message)
        self.assertIn("<code>gonka1excluded</code>", message)
        self.assertIn("Причина: <b>не пройден Confirmation PoC</b>", message)
        self.assertIn("Блок исключения: <code>5 481 065</code>", message)
        self.assertIn("Вес: <b>7 719</b>", message)
        self.assertIn("Доля общего веса сети: <b>43.6%</b>", message)
        self.assertIn("Место по весу: <b>2 из 2</b>", message)
        self.assertIn("<code>MiniMax-M2.7</code>", message)
        self.assertIn("<code>Kimi-K2.6</code>", message)
        self.assertIn("ML-ноды: <b>2</b>", message)
        self.assertIn(
            "API: <code>https://excluded.example/?a=1&amp;b=2</code>",
            message,
        )
        save_state.assert_called_once_with({"gonka1excluded"})

    def test_missing_active_details_do_not_hide_exclusion(self):
        snapshot = {
            "epoch": 356,
            "excluded": [
                {
                    "address": "gonka1excluded",
                    "reason": "failed_confirmation_poc",
                    "exclusion_block_height": 100,
                }
            ],
            "active": [],
        }

        with (
            patch.object(watcher, "fetch_snapshot", return_value=snapshot),
            patch.object(watcher, "load_previous_ids", return_value=set()),
            patch.object(watcher, "send_telegram_message") as send_message,
            patch.object(watcher, "save_state"),
        ):
            watcher.main()

        message = send_message.call_args.args[0]
        self.assertIn("<code>gonka1excluded</code>", message)
        self.assertIn("Вес: нет данных", message)
        self.assertIn("Доля общего веса сети: нет данных", message)
        self.assertIn("Место по весу: нет данных", message)
        self.assertIn("Модели: нет данных", message)
        self.assertIn("ML-ноды: нет данных", message)
        self.assertIn("API: нет данных", message)


if __name__ == "__main__":
    unittest.main()
