import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import our_nodes_watcher
from our_nodes_watcher import (
    evaluate_metric_availability,
    participant_confirmation_ratio,
)


class ConfirmationRatioTests(unittest.TestCase):
    def test_ratio_shape_is_converted_to_percent(self):
        entry = {"current_epoch_stats": {"confirmationPoCRatio": 0.3}}
        self.assertEqual(participant_confirmation_ratio(entry), 30.0)

    def test_percentage_shape_is_preserved(self):
        entry = {"current_epoch_stats": {"confirmation_poc_ratio": "30"}}
        self.assertEqual(participant_confirmation_ratio(entry), 30.0)

    def test_missing_metric_returns_none(self):
        self.assertIsNone(participant_confirmation_ratio({"weight": 100}))

    def test_negative_metric_is_rejected(self):
        entry = {"current_epoch_stats": {"confirmationPoCRatio": -1}}
        self.assertIsNone(participant_confirmation_ratio(entry))


class MetricAvailabilityTests(unittest.TestCase):
    def test_alerts_once_after_configured_number_of_missing_runs(self):
        first = evaluate_metric_availability(
            {}, None, enabled=True, alert_after_runs=2
        )
        self.assertEqual(first, (1, False, None))

        second = evaluate_metric_availability(
            {"ratio_missing_runs": 1},
            None,
            enabled=True,
            alert_after_runs=2,
        )
        self.assertEqual(second, (2, True, "unavailable"))

        third = evaluate_metric_availability(
            {"ratio_missing_runs": 2, "ratio_unavailable_alerted": True},
            None,
            enabled=True,
            alert_after_runs=2,
        )
        self.assertEqual(third, (3, True, None))

    def test_emits_recovery_when_metric_returns(self):
        result = evaluate_metric_availability(
            {"ratio_missing_runs": 3, "ratio_unavailable_alerted": True},
            31.5,
            enabled=True,
            alert_after_runs=2,
        )
        self.assertEqual(result, (0, False, "available"))

    def test_disabled_monitor_does_not_accumulate_missing_runs(self):
        result = evaluate_metric_availability(
            {"ratio_missing_runs": 10, "ratio_unavailable_alerted": True},
            None,
            enabled=False,
            alert_after_runs=2,
        )
        self.assertEqual(result, (0, False, None))

    def test_main_persists_missing_runs_and_sends_transition_once(self):
        config = {
            "participants_urls": ["https://source"],
            "epoch_urls": [],
            "health_path": "/v1/versions",
            "weight_ratio_alert_below_percent": 25,
            "weight_ratio_recovery_above_percent": 27,
            "metric_unavailable_alert_after_runs": 2,
            "nodes": [
                {
                    "name": "node1",
                    "participant_address": "gonka1test",
                    "endpoint": "https://node.example",
                    "ratio_monitoring_enabled": True,
                }
            ],
        }
        participants = [{"index": "gonka1test", "weight": "100"}]
        healthy = {"ok": True, "http_status": 200, "latency_ms": 1}

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with (
                patch.object(our_nodes_watcher, "STATE_FILE", state_file),
                patch.object(our_nodes_watcher, "load_config", return_value=config),
                patch.object(
                    our_nodes_watcher,
                    "fetch_participants",
                    return_value=participants,
                ),
                patch.object(our_nodes_watcher, "check_endpoint", return_value=healthy),
                patch.object(our_nodes_watcher, "send_telegram_message") as send,
            ):
                our_nodes_watcher.main()
                self.assertEqual(send.call_count, 0)

                our_nodes_watcher.main()
                self.assertEqual(send.call_count, 1)
                self.assertIn("недоступна", send.call_args.args[0])

                our_nodes_watcher.main()
                self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
