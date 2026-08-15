import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import new_host_bot as watcher


CONFIG = {"network_weight_change_warning_percent": 20}


def entry(node_id, weight=100, *, url=None, models=None, ml_nodes=1):
    value = {
        "index": node_id,
        "weight": weight,
        "inference_url": url or f"https://{node_id}.example",
        "models": models if models is not None else ["org/model"],
        "ml_nodes": [{"ml_nodes": [{"node_id": f"{node_id}-ml-{index}"} for index in range(ml_nodes)]}],
    }
    return value


def snapshot(epoch, entries):
    return watcher.parse_snapshot_payload(
        {
            "active_participants": {
                "epoch_group_id": str(epoch),
                "participants": entries,
            }
        }
    )


def baseline(epoch=350, entries=None, legacy=None):
    return watcher.baseline_state(
        snapshot(epoch, entries or [entry("gonka1a")]),
        legacy or {},
    )


class SnapshotTests(unittest.TestCase):
    def test_epoch_and_participants_come_from_same_payload(self):
        payload = {
            "active_participants": {
                "epoch_group_id": "357",
                "participants": [entry("gonka1a")],
            }
        }
        with patch.object(
            watcher,
            "fetch_json_with_fallback",
            return_value=(payload, "https://source.example"),
        ) as fetch:
            result = watcher.fetch_snapshot()

        self.assertEqual(result["epoch"], 357)
        self.assertEqual(set(result["by_id"]), {"gonka1a"})
        validator = fetch.call_args.kwargs["validator"]
        validator(payload)

    def test_invalid_epoch_empty_participants_and_duplicates_are_rejected(self):
        cases = (
            {"active_participants": {"epoch_group_id": None, "participants": [entry("a")]}},
            {"active_participants": {"epoch_group_id": 1, "participants": []}},
            {
                "active_participants": {
                    "epoch_group_id": 1,
                    "participants": [entry("a"), entry("a")],
                }
            },
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                watcher.parse_snapshot_payload(payload)

    def test_incomplete_weight_keeps_presence_snapshot_valid(self):
        result = snapshot(357, [entry("a", 100), entry("b", None)])
        self.assertEqual(result["total_weight"], 100)
        self.assertFalse(result["total_weight_complete"])


class MigrationTests(unittest.TestCase):
    def test_legacy_json_and_csv_are_loaded_without_inventing_periods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_state = root / "hosts.json"
            legacy_log = root / "host_log.csv"
            legacy_state.write_text('["legacy-json", "legacy-csv"]', encoding="utf-8")
            with legacy_log.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(["node_id", "first_seen_epoch", "first_seen_utc", "inference_url"])
                writer.writerow(["legacy-csv", 325, "2026-01-01T00:00:00Z", "https://legacy.example"])
                writer.writerow(["legacy-csv", 330, "2026-01-02T00:00:00Z", "https://legacy.example"])
            known = watcher.load_legacy_hosts(legacy_state, legacy_log)

        state = watcher.baseline_state(snapshot(357, [entry("current")]), known)
        self.assertEqual(state["history_complete_from_epoch"], 357)
        self.assertEqual(state["hosts"]["legacy-csv"]["first_seen_epoch"], 325)
        self.assertEqual(state["hosts"]["legacy-csv"]["periods"], [])
        self.assertFalse(state["hosts"]["legacy-json"]["active"])

    def test_first_baseline_saves_state_without_messages_or_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            presence = root / "host_presence.json"
            events = root / "host_events.csv"
            with (
                patch.object(watcher, "PRESENCE_STATE_FILE", presence),
                patch.object(watcher, "EVENT_LOG_FILE", events),
                patch.object(watcher, "LEGACY_STATE_FILE", root / "hosts.json"),
                patch.object(watcher, "LEGACY_LOG_FILE", root / "host_log.csv"),
                patch.object(watcher, "load_config", return_value=CONFIG),
                patch.object(watcher, "fetch_snapshot", return_value=snapshot(357, [entry("a")])),
                patch.object(watcher, "send_telegram_message") as send,
            ):
                watcher.main()

            saved = json.loads(presence.read_text(encoding="utf-8"))
            self.assertFalse(events.exists())
        self.assertEqual(saved["last_processed_epoch"], 357)
        send.assert_not_called()

    def test_legacy_inactive_host_returns_but_is_not_new(self):
        legacy = {"legacy": {"first_seen_epoch": 325, "inference_url": ""}}
        state = watcher.baseline_state(snapshot(357, [entry("current")]), legacy)
        result = watcher.process_snapshot(
            state,
            snapshot(358, [entry("current"), entry("legacy")]),
            CONFIG,
        )
        events = {event["node_id"]: event["event"] for event in result["events"]}
        self.assertEqual(events["legacy"], "returned")
        message = "\n".join(watcher.event_messages(result, snapshot(358, [entry("current"), entry("legacy")])))
        self.assertIn("Ранее фиксировался в эпохе 325", message)
        self.assertNotIn("Точная история ведётся", message)


class TransitionTests(unittest.TestCase):
    def test_new_returned_and_left_are_classified_once(self):
        state = baseline(entries=[entry("a")])
        first = watcher.process_snapshot(
            state,
            snapshot(351, [entry("a"), entry("b")]),
            CONFIG,
        )
        self.assertEqual([(item["event"], item["node_id"]) for item in first["events"]], [("new", "b")])

        second = watcher.process_snapshot(
            first["state"],
            snapshot(352, [entry("b")]),
            CONFIG,
        )
        self.assertEqual([(item["event"], item["node_id"]) for item in second["events"]], [("left", "a")])

        third = watcher.process_snapshot(
            second["state"],
            snapshot(353, [entry("a"), entry("b")]),
            CONFIG,
        )
        self.assertEqual([(item["event"], item["node_id"]) for item in third["events"]], [("returned", "a")])

        repeat = watcher.process_snapshot(third["state"], snapshot(353, [entry("a"), entry("b")]), CONFIG)
        self.assertTrue(repeat["ignored"])
        self.assertEqual(repeat["events"], [])

    def test_periods_open_close_and_multiple_periods_are_exact(self):
        state = baseline(entries=[entry("a")])
        state = watcher.process_snapshot(state, snapshot(351, [entry("a")]), CONFIG)["state"]
        state = watcher.process_snapshot(state, snapshot(352, [entry("b")]), CONFIG)["state"]
        state = watcher.process_snapshot(state, snapshot(353, [entry("a"), entry("b")]), CONFIG)["state"]
        periods = state["hosts"]["a"]["periods"]
        self.assertEqual(periods[0]["from"], 350)
        self.assertEqual(periods[0]["to"], 351)
        self.assertTrue(periods[0]["end_exact"])
        self.assertEqual(periods[1]["from"], 353)
        self.assertIsNone(periods[1]["to"])

    def test_gap_splits_observed_period_without_return_event(self):
        state = baseline(entries=[entry("a")])
        result = watcher.process_snapshot(state, snapshot(353, [entry("a")]), CONFIG)
        periods = result["state"]["hosts"]["a"]["periods"]
        self.assertEqual(result["events"], [])
        self.assertEqual([(item["from"], item["to"]) for item in periods], [(350, 350), (353, None)])
        self.assertFalse(periods[0]["end_exact"])
        self.assertTrue(result["state"]["hosts"]["a"]["history_has_gaps"])
        self.assertEqual(
            result["state"]["observation_gaps"],
            [{"from_epoch": 351, "to_epoch": 352, "detected_at_epoch": 353}],
        )

    def test_gap_left_message_reports_observation_not_invented_range(self):
        state = baseline(entries=[entry("a")])
        current = snapshot(353, [entry("b")])
        result = watcher.process_snapshot(state, current, CONFIG)
        message = "\n".join(watcher.event_messages(result, current))
        self.assertIn("Последний раз был активен в эпохе 350", message)
        self.assertIn("Отсутствие обнаружено в эпохе 353", message)
        self.assertNotIn("эпохи 350–352", message)

    def test_stale_snapshot_does_not_change_state(self):
        state = baseline(epoch=357)
        before = copy_json(state)
        result = watcher.process_snapshot(state, snapshot(356, [entry("a")]), CONFIG)
        self.assertTrue(result["ignored"])
        self.assertEqual(state, before)
        self.assertEqual(result["state"], before)


class WeightTests(unittest.TestCase):
    def test_network_share_and_equal_weight_ranks(self):
        current = snapshot(357, [entry("a", 75), entry("b", 25), entry("c", 25)])
        profile = watcher.profile_from_entry(current["by_id"]["a"], current["total_weight"])
        self.assertEqual(profile["network_share_percent"], 60.0)
        self.assertEqual(current["ranks"], {"a": 1, "b": 2, "c": 2})

    def test_plus_and_minus_twenty_percent_trigger_warning(self):
        for previous, current in ((100, 120), (100, 80)):
            with self.subTest(previous=previous, current=current):
                state = baseline(entries=[entry("a", previous)])
                result = watcher.process_snapshot(
                    state,
                    snapshot(351, [entry("a", current)]),
                    CONFIG,
                )
                self.assertIsNotNone(result["weight_alert"])
                self.assertAlmostEqual(
                    result["weight_alert"]["change_percent"],
                    20 if current > previous else -20,
                )

    def test_below_threshold_does_not_warn(self):
        state = baseline(entries=[entry("a", 100)])
        result = watcher.process_snapshot(state, snapshot(351, [entry("a", 119)]), CONFIG)
        self.assertIsNone(result["weight_alert"])

    def test_incomplete_zero_previous_and_epoch_gap_disable_warning(self):
        cases = (
            (baseline(entries=[entry("a", 100)]), snapshot(351, [entry("a", None)])),
            (baseline(entries=[entry("a", 0)]), snapshot(351, [entry("a", 100)])),
            (baseline(entries=[entry("a", 100)]), snapshot(352, [entry("a", 200)])),
        )
        for state, current in cases:
            with self.subTest(current=current):
                result = watcher.process_snapshot(state, current, CONFIG)
                self.assertIsNone(result["weight_alert"])

    def test_weight_warning_formats_positive_and_negative_signs(self):
        positive = watcher.weight_warning_message(
            {
                "from_epoch": 356,
                "to_epoch": 357,
                "previous_total": 100,
                "current_total": 120,
                "change": 20,
                "change_percent": 20,
                "previous_host_count": 2,
                "current_host_count": 3,
            }
        )
        negative = watcher.weight_warning_message(
            {
                "from_epoch": 356,
                "to_epoch": 357,
                "previous_total": 100,
                "current_total": 80,
                "change": -20,
                "change_percent": -20,
                "previous_host_count": 2,
                "current_host_count": 1,
            }
        )
        self.assertIn("+20 (+20.0%)", positive)
        self.assertIn("−20 (−20.0%)", negative)


class MessageAndPersistenceTests(unittest.TestCase):
    def test_new_returned_and_left_message_templates_include_profiles(self):
        state = baseline(entries=[entry("a", 100)])
        left_snapshot = snapshot(351, [entry("b", 300)])
        first = watcher.process_snapshot(state, left_snapshot, CONFIG)
        messages = "\n".join(watcher.event_messages(first, left_snapshot))
        self.assertIn("Новый хост в сети Gonka — эпоха 351", messages)
        self.assertIn("Хост покинул активный набор — эпоха 351", messages)
        self.assertIn("Доля общего веса сети: <b>100.0%</b>", messages)
        returned_snapshot = snapshot(352, [entry("a", 100), entry("b", 300)])
        second = watcher.process_snapshot(first["state"], returned_snapshot, CONFIG)
        returned = "\n".join(watcher.event_messages(second, returned_snapshot))
        self.assertIn("Хост вернулся в сеть — эпоха 352", returned)
        self.assertIn("Отсутствовал: эпоха 351", returned)

    def test_large_event_lists_are_split_below_telegram_limit(self):
        blocks = [f"• host-{index}\n" + ("x" * 180) for index in range(100)]
        messages = watcher.chunk_messages("🆕 <b>Новые хосты</b>", blocks)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= watcher.TELEGRAM_SAFE_LIMIT for message in messages))
        self.assertIn("часть 1/", messages[0])

    def test_state_and_event_log_round_trip(self):
        state = baseline(entries=[entry("a")])
        current = snapshot(351, [entry("a"), entry("b")])
        result = watcher.process_snapshot(state, current, CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "host_presence.json"
            event_file = root / "host_events.csv"
            watcher.save_json_atomic(state_file, result["state"], sort_keys=True)
            loaded = watcher.load_presence_state(state_file)
            watcher.append_event_log(result["events"], current, "2026-08-13T00:00:00Z", event_file)
            with event_file.open(encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(loaded["last_processed_epoch"], 351)
        self.assertEqual(rows[0]["event"], "new")
        self.assertEqual(rows[0]["node_id"], "b")

    def test_module_import_does_not_require_telegram_secrets(self):
        environment = os.environ.copy()
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_MESSAGE_THREAD_ID",
            "TELEGRAM_SECONDARY_CHAT_ID",
        ):
            environment.pop(name, None)
        result = subprocess.run(
            [sys.executable, "-c", "import new_host_bot"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_commits_new_state_files_without_changing_schedule(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/check-new-hosts.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "7,37 * * * *"', workflow)
        self.assertIn("state/host_presence.json", workflow)
        self.assertIn("state/host_events.csv", workflow)


def copy_json(value):
    return json.loads(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
