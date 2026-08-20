import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401 - installs an optional requests stub
import requests

import our_nodes_watcher
from our_nodes_watcher import (
    RATIO_METRIC_VERSION,
    build_node_recovery,
    confirmation_metric,
    evaluate_confirmation,
    fetch_epoch_group_snapshot,
    format_alert_datetime,
    parse_epoch_group_payload,
    parse_participants_payload,
)


def participants_payload(epoch=359):
    return {
        "active_participants": {
            "epoch_group_id": str(epoch),
            "participants": [{"index": "gonka1test", "weight": "9000"}],
        }
    }


def group_payload(epoch=359, weight="5000", confirmation_weight="1200"):
    return {
        "epoch_group_data": {
            "epoch_index": str(epoch),
            "epoch_group_id": "1394",
            "validation_weights": [
                {
                    "member_address": "gonka1test",
                    "weight": weight,
                    "confirmation_weight": confirmation_weight,
                }
            ],
        }
    }


def available_metric(epoch=359, rate=24.0):
    return {
        "available": True,
        "epoch": epoch,
        "weight": 5000,
        "confirmation_weight": int(5000 * rate / 100),
        "rate": rate,
        "source": "https://source",
        "reason": None,
        "error": None,
    }


class PayloadParsingTests(unittest.TestCase):
    def test_participants_epoch_and_address_use_real_paths(self):
        parsed = parse_participants_payload(participants_payload())
        self.assertEqual(parsed["epoch"], 359)
        self.assertIn("gonka1test", parsed["by_address"])

    def test_confirmation_rate_uses_participant_weights(self):
        parsed = parse_epoch_group_payload(group_payload())
        metric = parsed["by_address"]["gonka1test"]
        self.assertEqual(metric["weight"], 5000)
        self.assertEqual(metric["confirmation_weight"], 1200)
        self.assertEqual(metric["rate"], 24.0)

    def test_zero_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "weight must be positive"):
            parse_epoch_group_payload(group_payload(weight="0"))

    def test_negative_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "weight must be positive"):
            parse_epoch_group_payload(group_payload(weight="-1"))

    def test_negative_confirmation_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "confirmation_weight must be non-negative"):
            parse_epoch_group_payload(group_payload(confirmation_weight="-1"))

    def test_confirmation_weight_above_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            parse_epoch_group_payload(group_payload(weight="10", confirmation_weight="11"))

    def test_fractional_weight_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            parse_epoch_group_payload(group_payload(weight=1.5, confirmation_weight=1))

    def test_duplicate_address_is_rejected(self):
        payload = group_payload()
        payload["epoch_group_data"]["validation_weights"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_epoch_group_payload(payload)

    def test_participant_absent_is_unavailable(self):
        group = parse_epoch_group_payload(group_payload())
        metric = confirmation_metric(359, "gonka1missing", group)
        self.assertFalse(metric["available"])
        self.assertEqual(metric["reason"], "participant not found")

    def test_epoch_mismatch_is_unavailable(self):
        group = parse_epoch_group_payload(group_payload(epoch=358))
        metric = confirmation_metric(359, "gonka1test", group)
        self.assertFalse(metric["available"])
        self.assertEqual(metric["reason"], "epoch mismatch")

    def test_group_data_falls_back_to_second_source(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = group_payload()
        with patch(
            "bot_common.requests.get",
            side_effect=[requests.Timeout("timed out"), response],
        ) as get:
            snapshot = fetch_epoch_group_snapshot(
                ["https://first.example/group", "https://second.example/group"],
                timeout=3,
            )
        self.assertEqual(get.call_count, 2)
        self.assertEqual(snapshot["source"], "https://second.example/group")


class ConfirmationTransitionTests(unittest.TestCase):
    def evaluate(self, previous, metric):
        return evaluate_confirmation(
            previous,
            metric,
            enabled=True,
            alert_threshold=30,
            recovery_threshold=30,
            alert_after_runs=2,
            unavailable_after_runs=2,
        )

    def test_two_consecutive_low_values_alert_once(self):
        first, events = self.evaluate({}, available_metric())
        self.assertEqual(first["ratio_low_runs"], 1)
        self.assertEqual(events, [])
        first["ratio_metric_version"] = RATIO_METRIC_VERSION

        second, events = self.evaluate(first, available_metric())
        self.assertEqual(second["ratio_low_runs"], 2)
        self.assertEqual(events, ["low"])
        second["ratio_metric_version"] = RATIO_METRIC_VERSION

        third, events = self.evaluate(second, available_metric())
        self.assertEqual(third["ratio_low_runs"], 3)
        self.assertEqual(events, [])

    def test_low_counter_restarts_in_new_epoch(self):
        previous = {
            "ratio_metric_version": RATIO_METRIC_VERSION,
            "ratio_epoch": 359,
            "ratio_low_runs": 1,
        }
        state, events = self.evaluate(previous, available_metric(epoch=360))
        self.assertEqual(state["ratio_low_runs"], 1)
        self.assertEqual(events, [])

    def test_recovery_at_threshold(self):
        previous = {
            "ratio_metric_version": RATIO_METRIC_VERSION,
            "ratio_epoch": 359,
            "ratio_low_runs": 3,
            "weight_ratio_alerted": True,
        }
        state, events = self.evaluate(previous, available_metric(rate=30.0))
        self.assertFalse(state["weight_ratio_alerted"])
        self.assertEqual(events, ["recovered"])

    def test_unavailable_metric_does_not_create_low_alert(self):
        unavailable = {
            "available": False,
            "epoch": 359,
            "reason": "timeout",
            "error": "full exception",
            "source": None,
        }
        first, events = self.evaluate({}, unavailable)
        self.assertEqual(first["ratio_low_runs"], 0)
        self.assertEqual(events, [])
        first["ratio_metric_version"] = RATIO_METRIC_VERSION
        second, events = self.evaluate(first, unavailable)
        self.assertEqual(events, ["unavailable"])
        self.assertFalse(second["weight_ratio_alerted"])

    def test_metric_recovery_is_separate_transition(self):
        previous = {
            "ratio_metric_version": RATIO_METRIC_VERSION,
            "ratio_epoch": 359,
            "ratio_missing_runs": 2,
            "ratio_unavailable_alerted": True,
        }
        _, events = self.evaluate(previous, available_metric(rate=40))
        self.assertEqual(events, ["available"])

    def test_metric_version_migration_closes_old_unavailable_alert(self):
        previous = {
            "ratio_metric_version": "confirmation_poc_ratio_v1",
            "ratio_missing_runs": 184,
            "ratio_unavailable_alerted": True,
        }
        state, events = self.evaluate(previous, available_metric(rate=40))
        self.assertEqual(events, ["available"])
        self.assertEqual(state["ratio_missing_runs"], 0)
        self.assertFalse(state["ratio_unavailable_alerted"])

    def test_disabled_monitor_resets_counters(self):
        state, events = evaluate_confirmation(
            {"ratio_low_runs": 4, "weight_ratio_alerted": True},
            available_metric(),
            enabled=False,
            alert_threshold=30,
            recovery_threshold=30,
            alert_after_runs=2,
            unavailable_after_runs=2,
        )
        self.assertEqual(state["ratio_low_runs"], 0)
        self.assertFalse(state["weight_ratio_alerted"])
        self.assertEqual(events, [])


class MainPersistenceTests(unittest.TestCase):
    def test_main_persists_metric_and_alerts_on_second_low_check(self):
        config = {
            "participants_urls": ["https://participants.example"],
            "epoch_group_data_urls": ["https://group.example"],
            "health_path": "/v1/versions",
            "weight_ratio_alert_below_percent": 30,
            "weight_ratio_recovery_above_percent": 30,
            "ratio_alert_after_runs": 2,
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
        participants = parse_participants_payload(participants_payload())
        participants["source"] = "https://participants.example"
        group = parse_epoch_group_payload(group_payload())
        group["source"] = "https://group.example"
        healthy = {"ok": True, "http_status": 200, "latency_ms": 1}

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with (
                patch.object(our_nodes_watcher, "STATE_FILE", state_file),
                patch.object(our_nodes_watcher, "load_config", return_value=config),
                patch.object(
                    our_nodes_watcher,
                    "fetch_participants_snapshot",
                    return_value=participants,
                ),
                patch.object(
                    our_nodes_watcher,
                    "fetch_epoch_group_snapshot",
                    return_value=group,
                ),
                patch.object(our_nodes_watcher, "check_endpoint", return_value=healthy),
                patch.object(our_nodes_watcher, "send_telegram_message") as send,
            ):
                our_nodes_watcher.main()
                self.assertEqual(send.call_count, 0)
                our_nodes_watcher.main()

            self.assertEqual(send.call_count, 1)
            state = our_nodes_watcher.load_state(state_file)
            node = state["nodes"]["node1"]
            self.assertEqual(node["epoch"], 359)
            self.assertEqual(node["weight"], 5000)
            self.assertEqual(node["confirmation_weight"], 1200)
            self.assertEqual(node["weight_ratio"], 24.0)
            self.assertEqual(node["ratio_source"], "https://group.example")
            self.assertEqual(node["ratio_low_runs"], 2)


class MessageFormatTests(unittest.TestCase):
    def test_recovery_times_use_utc_and_pacific_clock(self):
        self.assertEqual(
            format_alert_datetime("2026-08-20T20:02:03+00:00"),
            "20 авг, 20:02 UTC (13:02 PT)",
        )
        self.assertEqual(
            format_alert_datetime("2026-08-21T02:00:00+00:00"),
            "21 авг, 02:00 UTC (20 авг, 19:00 PT)",
        )
        message = build_node_recovery(
            {
                "name": "node1",
                "participant_address": "gonka1test",
            },
            {"first_failed_at": "2026-08-20T20:02:03+00:00"},
            {"details": "HTTP 200 in 45 ms"},
            "2026-08-20T20:17:03+00:00",
        )
        self.assertIn("Наша нода снова отвечает: node1", message)
        self.assertIn("Недоступность с: 20 авг, 20:02 UTC (13:02 PT)", message)
        self.assertIn(
            "Восстановлена: 20 авг, 20:17 UTC (13:17 PT) (15 мин)",
            message,
        )
        self.assertNotIn("T20:", message)


if __name__ == "__main__":
    unittest.main()
