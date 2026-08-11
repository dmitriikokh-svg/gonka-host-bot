import json
import os
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import chain_halt_watcher as watcher


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def config(**overrides):
    value = {
        "sources": [
            {"name": "node1", "url": "https://node1.example/status"},
            {"name": "node2", "url": "https://node2.example/status"},
            {"name": "node3", "url": "https://node3.example/status"},
        ],
        "expected_chain_id": "gonka-mainnet",
        "minimum_confirming_sources": 2,
        "halt_after_seconds": 60,
        "maximum_height_spread": 2,
        "recovery_confirmations": 2,
        "unavailable_alert_after_runs": 3,
        "reminder_interval_minutes": 30,
        "request_timeout_seconds": 8,
        "attempts_per_source": 1,
        "poll_interval_seconds": 30,
        "maximum_future_skew_seconds": 5,
    }
    value.update(overrides)
    return value


def payload(
    height=12345,
    block_time=None,
    *,
    chain_id="gonka-mainnet",
    catching_up=False,
):
    return {
        "result": {
            "node_info": {"network": chain_id},
            "sync_info": {
                "latest_block_height": str(height),
                "latest_block_time": block_time or watcher.iso_utc(NOW),
                "catching_up": catching_up,
            },
        }
    }


def observation(
    name,
    *,
    height=12345,
    age=10,
    status="available",
    error=None,
    error_category=None,
):
    item = {
        "name": name,
        "url": f"https://{name}.example/status",
        "status": status,
        "chain_id": "gonka-mainnet" if status == "available" else None,
        "height": height if status == "available" else None,
        "block_time": (
            watcher.iso_utc(NOW - timedelta(seconds=age))
            if status == "available"
            else None
        ),
        "block_age_seconds": age if status == "available" else None,
        "catching_up": False if status == "available" else None,
        "error_category": error_category,
        "error": error,
    }
    return item


class ConfigTests(unittest.TestCase):
    def test_valid_config_is_accepted(self):
        value = config()
        self.assertIs(watcher.validate_config(value), value)

    def test_minimum_quorum_cannot_be_one_or_exceed_source_count(self):
        with self.assertRaisesRegex(ValueError, "minimum_confirming_sources"):
            watcher.validate_config(config(minimum_confirming_sources=1))
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            watcher.validate_config(config(minimum_confirming_sources=4))

    def test_duplicate_source_and_invalid_threshold_are_rejected(self):
        duplicate = config()
        duplicate["sources"][1]["url"] = duplicate["sources"][0]["url"]
        with self.assertRaisesRegex(ValueError, "duplicate source URL"):
            watcher.validate_config(duplicate)
        with self.assertRaisesRegex(ValueError, "maximum_height_spread"):
            watcher.validate_config(config(maximum_height_spread=-1))


class PayloadTests(unittest.TestCase):
    def test_valid_payload_preserves_required_fields(self):
        source = config()["sources"][0]
        result = watcher.parse_status_payload(
            payload(block_time="2026-08-11T11:59:50.123456789Z"),
            source,
            config(),
            NOW,
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["chain_id"], "gonka-mainnet")
        self.assertEqual(result["height"], 12345)
        self.assertAlmostEqual(result["block_age_seconds"], 9.877, places=3)
        self.assertFalse(result["catching_up"])

    def test_wrong_chain_future_time_and_catching_up_are_unavailable(self):
        source = config()["sources"][0]
        cases = (
            (payload(chain_id="other-chain"), "unexpected chain ID", "wrong chain ID"),
            (
                payload(block_time=watcher.iso_utc(NOW + timedelta(seconds=6))),
                "in the future",
                "invalid response",
            ),
            (payload(catching_up=True), "catching_up=true", "node syncing"),
        )
        for value, error, category in cases:
            with self.subTest(error=error):
                result = watcher.parse_status_payload(value, source, config(), NOW)
                self.assertEqual(result["status"], "unavailable")
                self.assertIn(error, result["error"])
                self.assertEqual(result["error_category"], category)

    def test_missing_or_invalid_height_is_rejected(self):
        source = config()["sources"][0]
        for value in (None, "not-a-number", "0"):
            data = payload()
            data["result"]["sync_info"]["latest_block_height"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "latest_block_height"
            ):
                watcher.parse_status_payload(data, source, config(), NOW)


class SourcePollingTests(unittest.TestCase):
    def test_all_sources_are_polled_independently_and_order_is_stable(self):
        visited = []
        lock = threading.Lock()

        def fetcher(source, _config, _now):
            with lock:
                visited.append(source["name"])
            return observation(source["name"])

        results = watcher.check_sources(config(), NOW, fetcher=fetcher)
        self.assertCountEqual(visited, ["node1", "node2", "node3"])
        self.assertEqual([item["name"] for item in results], ["node1", "node2", "node3"])

    def test_http_error_is_unavailable_not_halt_evidence(self):
        class Response:
            def raise_for_status(self):
                raise RuntimeError("503 Service Unavailable")

        class Session:
            @staticmethod
            def get(*_args, **_kwargs):
                return Response()

        result = watcher.fetch_source(
            config()["sources"][0], config(), NOW, session=Session
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("503", result["error"])
        self.assertEqual(result["error_category"], "HTTP 503")

    def test_malformed_json_is_unavailable(self):
        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                raise ValueError("malformed JSON")

        class Session:
            @staticmethod
            def get(*_args, **_kwargs):
                return Response()

        result = watcher.fetch_source(
            config()["sources"][0], config(), NOW, session=Session
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("malformed JSON", result["error"])
        self.assertEqual(result["error_category"], "invalid response")

    def test_request_errors_are_reduced_to_short_categories(self):
        cases = (
            (TimeoutError("connection timed out"), "timeout"),
            (RuntimeError("HTTP 503 Service Unavailable"), "HTTP 503"),
            (RuntimeError("Connection refused (errno 111)"), "connection refused"),
            (RuntimeError("Connection reset by peer"), "connection error"),
            (ValueError("bad payload"), "invalid response"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(watcher.exception_category(error), expected)


class AssessmentTests(unittest.TestCase):
    def test_two_old_close_sources_confirm_halt(self):
        result = watcher.assess_network(
            [observation("node1", height=100, age=61), observation("node2", height=102, age=90)],
            config(),
        )
        self.assertEqual(result["result"], "halted")
        self.assertEqual(result["height_spread"], 2)

    def test_any_fresh_source_keeps_network_healthy(self):
        result = watcher.assess_network(
            [observation("node1", height=100, age=300), observation("node2", height=101, age=5)],
            config(),
        )
        self.assertEqual(result["result"], "healthy")
        self.assertEqual(result["reason"], "fresh_block_observed")

    def test_one_source_is_insufficient_even_when_old(self):
        result = watcher.assess_network(
            [observation("node1", age=300), observation("node2", status="unavailable", error="timeout")],
            config(),
        )
        self.assertEqual(result["result"], "insufficient")

    def test_divergent_old_heights_are_not_a_confirmed_halt(self):
        result = watcher.assess_network(
            [observation("node1", height=100, age=300), observation("node2", height=110, age=300)],
            config(),
        )
        self.assertEqual(result["result"], "insufficient")
        self.assertEqual(result["reason"], "height_spread_too_large")

    def test_partial_outage_can_still_confirm_with_two_sources(self):
        result = watcher.assess_network(
            [
                observation("node1", height=100, age=300),
                observation("node2", height=101, age=300),
                observation("node3", status="unavailable", error="timeout"),
            ],
            config(),
        )
        self.assertEqual(result["result"], "halted")
        self.assertEqual(result["confirming_sources"], 2)


class StateTransitionTests(unittest.TestCase):
    def setUp(self):
        self.old = [observation("node1", age=120), observation("node2", age=121)]
        self.fresh = [observation("node1", age=3), observation("node2", age=4)]
        self.insufficient = [
            observation("node1", age=3),
            observation("node2", status="unavailable", error="timeout"),
        ]

    def test_halt_alerts_once_then_reminds_on_interval(self):
        state, messages = watcher.evaluate_check(watcher.default_state(), config(), self.old, NOW)
        self.assertEqual(state["status"], "halted")
        self.assertEqual(len(messages), 1)
        self.assertIn("Gonka chain halt", messages[0])

        state, messages = watcher.evaluate_check(state, config(), self.old, NOW + timedelta(minutes=29))
        self.assertEqual(messages, [])
        state, messages = watcher.evaluate_check(state, config(), self.old, NOW + timedelta(minutes=30))
        self.assertEqual(len(messages), 1)
        self.assertIn("продолжается", messages[0])

    def test_recovery_requires_two_consecutive_healthy_checks(self):
        state, _ = watcher.evaluate_check(watcher.default_state(), config(), self.old, NOW)
        state, messages = watcher.evaluate_check(state, config(), self.fresh, NOW + timedelta(seconds=30))
        self.assertEqual(state["status"], "halted")
        self.assertEqual(messages, [])
        state, messages = watcher.evaluate_check(state, config(), self.fresh, NOW + timedelta(seconds=60))
        self.assertEqual(state["status"], "healthy")
        self.assertFalse(state["halt"]["active"])
        self.assertEqual(len(messages), 1)
        self.assertIn("восстановлена", messages[0])

    def test_monitoring_alerts_on_third_failure_then_recovers(self):
        state = watcher.default_state()
        for run in range(1, 4):
            state, messages = watcher.evaluate_check(
                state, config(), self.insufficient, NOW + timedelta(minutes=run)
            )
            self.assertEqual(len(messages), 1 if run == 3 else 0)
        self.assertTrue(state["monitoring"]["alerted"])

        state, messages = watcher.evaluate_check(state, config(), self.fresh, NOW + timedelta(minutes=4))
        self.assertFalse(state["monitoring"]["alerted"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Наблюдаемость", messages[0])

    def test_monitoring_message_is_concise_and_hides_full_exception(self):
        full_error = "ConnectTimeout: HTTPSConnectionPool(host=node1): internal details"
        values = [
            observation(
                "node1",
                status="unavailable",
                error=full_error,
                error_category="timeout",
            ),
            observation(
                "node2",
                status="unavailable",
                error="HTTPError: 503 Service Unavailable",
                error_category="HTTP 503",
            ),
            observation("node3", height=5508777, age=7),
        ]
        assessment = watcher.assess_network(values, config())
        message = watcher.build_monitoring_unavailable_message(
            assessment, values, config()
        )
        self.assertIn("Chain monitor: недостаточно источников", message)
        self.assertIn("Доступно: <b>1 из 3</b>, требуется: <b>2</b>", message)
        self.assertIn("<code>node1</code> — timeout", message)
        self.assertIn("<code>node2</code> — HTTP 503", message)
        self.assertIn(
            "<code>node3</code> — OK, блок <code>5 508 777</code>, "
            "возраст <b>7 сек.</b>",
            message,
        )
        self.assertIn("Chain halt не подтверждён.", message)
        self.assertNotIn(full_error, message)
        self.assertNotIn("not_enough_sources", message)

    def test_three_unavailable_sources_only_report_monitoring_unavailable(self):
        values = [
            observation(name, status="unavailable", error="timeout")
            for name in ("node1", "node2", "node3")
        ]
        state = watcher.default_state()
        for run in range(3):
            state, messages = watcher.evaluate_check(
                state, config(), values, NOW + timedelta(minutes=run)
            )
        self.assertEqual(state["status"], "monitoring_unavailable")
        self.assertFalse(state["halt"]["active"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Chain halt не подтверждён.", messages[0])

    def test_partial_failure_with_healthy_quorum_does_not_raise_yellow_alert(self):
        values = self.fresh + [observation("node3", status="unavailable", error="503")]
        state, messages = watcher.evaluate_check(watcher.default_state(), config(), values, NOW)
        self.assertEqual(state["status"], "healthy")
        self.assertEqual(messages, [])


class PersistenceAndRuntimeTests(unittest.TestCase):
    def test_run_once_saves_atomic_state_and_sends_transition(self):
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            result = watcher.run_once(
                config(),
                now=NOW,
                checker=lambda _config, _now: [
                    observation("node1", age=120),
                    observation("node2", age=121),
                ],
                sender=sent.append,
                state_file=state_file,
            )
            watcher.run_once(
                config(),
                now=NOW + timedelta(seconds=10),
                checker=lambda _config, _now: [
                    observation("node1", age=130),
                    observation("node2", age=131),
                ],
                sender=sent.append,
                state_file=state_file,
            )
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            loaded = watcher.load_state(state_file)
            self.assertFalse(state_file.with_name("state.json.tmp").exists())
        self.assertEqual(saved["status"], "halted")
        self.assertEqual(loaded["halt"]["first_detected_at"], saved["halt"]["first_detected_at"])
        self.assertEqual(result["status"], "halted")
        self.assertEqual(len(sent), 1)

    def test_runtime_defaults_to_once_and_accepts_daemon_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(watcher.runtime_settings(config()), ("once", 30))
        with patch.dict(
            os.environ,
            {"CHAIN_MONITOR_MODE": "daemon", "CHAIN_POLL_INTERVAL_SECONDS": "15"},
            clear=True,
        ):
            self.assertEqual(watcher.runtime_settings(config()), ("daemon", 15))

    def test_full_source_error_is_saved_and_printed_but_not_sent(self):
        full_error = "ConnectTimeout: full internal diagnostic"
        values = [
            observation(
                "node1",
                status="unavailable",
                error=full_error,
                error_category="timeout",
            ),
            observation("node2", age=3),
        ]
        previous = watcher.default_state()
        previous["monitoring"]["unavailable_runs"] = 2
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps(previous), encoding="utf-8")
            sent = []
            output = io.StringIO()
            with redirect_stdout(output):
                watcher.run_once(
                    config(),
                    now=NOW,
                    checker=lambda _config, _now: values,
                    sender=sent.append,
                    state_file=state_file,
                )
            saved = json.loads(state_file.read_text(encoding="utf-8"))

        self.assertEqual(saved["sources"]["node1"]["error"], full_error)
        self.assertIn(full_error, output.getvalue())
        self.assertEqual(len(sent), 1)
        self.assertNotIn(full_error, sent[0])

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
            [sys.executable, "-c", "import chain_halt_watcher"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_daemon_survives_iteration_error(self):
        stop = threading.Event()
        calls = []

        def iteration(_config):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise RuntimeError("temporary failure")
            stop.set()

        watcher.daemon_loop(
            config(),
            1,
            stop,
            iteration=iteration,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(calls, [1, 2])


class WorkflowTests(unittest.TestCase):
    def test_workflow_has_entrypoint_mode_state_and_all_telegram_secrets(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/check-chain-halt.yml").read_text(
            encoding="utf-8"
        )
        self.assertTrue((root / "chain_halt_watcher.py").is_file())
        self.assertIn("python-version: \"3.12\"", workflow)
        self.assertIn("run: python chain_halt_watcher.py", workflow)
        self.assertIn("CHAIN_MONITOR_MODE: once", workflow)
        self.assertIn("scripts/commit_state.sh", workflow)
        self.assertIn("state/chain_halt.json", workflow)
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_MESSAGE_THREAD_ID",
            "TELEGRAM_SECONDARY_CHAT_ID",
        ):
            self.assertIn(name, workflow)


if __name__ == "__main__":
    unittest.main()
