import unittest
from datetime import datetime, timezone
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
        save_state.assert_called_once()
        saved = save_state.call_args.args[0]
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["epoch"], "355")
        self.assertEqual(saved["excluded"][0]["id"], "gonka1excluded")
        self.assertEqual(saved["excluded"][0]["reason"], "failed_confirmation_poc")
        self.assertEqual(saved["excluded"][0]["exclusion_block_height"], 5481065)
        self.assertEqual(saved["excluded"][0]["weight"], 7719)
        self.assertEqual(saved["excluded"][0]["rank"], 2)
        self.assertEqual(saved["excluded"][0]["models"], ["MiniMax-M2.7", "Kimi-K2.6"])

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


class ExcludedStateTests(unittest.TestCase):
    def test_legacy_list_and_snapshot_share_id_set(self):
        self.assertEqual(
            watcher.excluded_ids_from_state(["gonka1a", "gonka1b"]),
            {"gonka1a", "gonka1b"},
        )
        self.assertEqual(
            watcher.excluded_ids_from_state(
                {
                    "schema_version": 1,
                    "excluded": [{"id": "gonka1a"}, {"address": "gonka1b"}],
                }
            ),
            {"gonka1a", "gonka1b"},
        )
        self.assertIsNone(watcher.excluded_ids_from_state(None))

    def test_command_message_uses_snapshot_details(self):
        text = watcher.format_command_excluded_message(
            {
                "checked_at": "2026-08-24T07:00:00+00:00",
                "epoch": 370,
                "excluded": [
                    {
                        "id": "gonka1excluded",
                        "reason": "failed_confirmation_poc",
                        "exclusion_block_height": 5481065,
                        "weight": 7719,
                        "network_share_percent": 43.6,
                        "rank": 2,
                        "participant_count": 29,
                        "models": ["MiniMax-M2.7"],
                        "ml_node_count": 2,
                    }
                ],
            },
            now=datetime(2026, 8, 24, 7, 14, tzinfo=timezone.utc),
        )
        self.assertIn("📊 Исключены после CPoC, эпоха 370", text)
        self.assertIn("gonka1excluded", text)
        self.assertIn("не пройден Confirmation PoC", text)
        self.assertIn("Блок: 5 481 065", text)
        self.assertIn("Вес: 7 719 (43.6%)", text)
        self.assertIn("место 2 из 29", text)
        self.assertIn("MiniMax-M2.7 · 2 ML-ноды", text)

    def test_command_message_legacy_list_warns_about_details(self):
        text = watcher.format_command_excluded_message(["gonka1excluded"])
        self.assertIn("gonka1excluded", text)
        self.assertIn("появятся после следующего прогона", text)

    def test_command_message_empty_snapshot(self):
        text = watcher.format_command_excluded_message(
            {"epoch": 370, "checked_at": "2026-08-24T07:00:00Z", "excluded": []}
        )
        self.assertIn("никто не в excluded_participants", text)


if __name__ == "__main__":
    unittest.main()
