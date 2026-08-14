import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import chain_load_watcher as watcher


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def config(**overrides):
    value = {
        "rpc_urls": ["https://rpc1.example/chain-rpc", "https://rpc2.example/chain-rpc"],
        "expected_chain_id": "gonka-mainnet",
        "window_blocks": 10,
        "warning_bytes": 50_000_000,
        "critical_after_consecutive_windows": 2,
        "recovery_after_clean_windows": 2,
        "unavailable_alert_after_runs": 3,
        "critical_reminder_minutes": 30,
        "request_timeout_seconds": 15,
        "poll_interval_seconds": 60,
        "top_message_types": 3,
        "top_hot_blocks": 3,
    }
    value.update(overrides)
    return watcher.validate_config(value)


def status_payload(height, chain_id="gonka-mainnet"):
    return {
        "result": {
            "node_info": {"network": chain_id},
            "sync_info": {"latest_block_height": str(height)},
        }
    }


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def block_payload(height, transactions=()):
    txs = [encoded(item) for item in transactions] if transactions is not None else None
    return {
        "result": {
            "block": {
                "header": {"height": str(height)},
                "data": {"txs": txs},
            }
        }
    }


def snapshot(height, total, *, start=None, tx_count=1):
    start = height - 9 if start is None else start
    return {
        "rpc": "https://rpc1.example/chain-rpc",
        "chain_id": "gonka-mainnet",
        "latest_height": height,
        "window_start": start,
        "window_end": height,
        "window_block_count": height - start + 1,
        "tx_count": tx_count,
        "sum_tx_bytes": total,
        "max_tx_bytes": total if tx_count else 0,
        "message_types": [
            {"type": "MsgSubmitHardwareDiff", "bytes": total, "count": tx_count}
        ]
        if tx_count
        else [],
        "blocks": [],
        "hot_blocks": [{"height": height, "bytes": total, "tx_count": tx_count}],
    }


def available_result(value, rpc_index=0):
    return {
        "available": True,
        "snapshot": value,
        "sources": [
            watcher.source_observation(
                rpc_index,
                value["rpc"],
                "available",
                latest_height=value["latest_height"],
                error_category=None,
                error=None,
            )
        ],
    }


def unavailable_sources():
    return [
        watcher.source_observation(
            0,
            "https://rpc1.example/chain-rpc",
            "unavailable",
            error_category="timeout",
            error="ConnectTimeout: full internal details",
        ),
        watcher.source_observation(
            1,
            "https://rpc2.example/chain-rpc",
            "unavailable",
            error_category="HTTP 503",
            error="HTTPError: 503 Server Error with URL",
        ),
    ]


class Response:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class MappingSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        value = self.mapping[url]
        if isinstance(value, list):
            value = value.pop(0)
        return value if isinstance(value, Response) else Response(value)


class ConfigTests(unittest.TestCase):
    def test_valid_config_and_rpc_env_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            loaded = watcher.load_config(
                path,
                environ={
                    "CHAIN_LOAD_RPC_URLS": "https://a.example/rpc, https://b.example/rpc"
                },
            )
        self.assertEqual(
            loaded["rpc_urls"],
            ["https://a.example/rpc", "https://b.example/rpc"],
        )

    def test_all_config_values_are_validated(self):
        with self.assertRaisesRegex(ValueError, "expected_chain_id"):
            config(expected_chain_id="")
        with self.assertRaisesRegex(ValueError, "rpc_urls"):
            config(rpc_urls=[])
        with self.assertRaisesRegex(ValueError, "duplicates"):
            config(rpc_urls=["https://same.example", "https://same.example/"])
        for field in (
            "window_blocks",
            "warning_bytes",
            "recovery_after_clean_windows",
            "unavailable_alert_after_runs",
            "critical_reminder_minutes",
            "request_timeout_seconds",
            "poll_interval_seconds",
            "top_message_types",
            "top_hot_blocks",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                config(**{field: 0})
        with self.assertRaises(ValueError):
            config(critical_after_consecutive_windows=1)

    def test_runtime_settings(self):
        self.assertEqual(watcher.runtime_settings(config(), environ={}), ("once", 60))
        self.assertEqual(
            watcher.runtime_settings(
                config(),
                environ={
                    "CHAIN_LOAD_MODE": "daemon",
                    "CHAIN_LOAD_POLL_INTERVAL_SECONDS": "30",
                },
            ),
            ("daemon", 30),
        )


class BlockAnalysisTests(unittest.TestCase):
    def test_multiple_transactions_are_summed_as_decoded_raw_bytes(self):
        result = watcher.analyze_block(
            block_payload(10, [b"abc", b"12345"]),
            10,
        )
        self.assertEqual(result["sum_tx_bytes"], 8)
        self.assertEqual(result["tx_count"], 2)
        self.assertEqual(result["max_tx_bytes"], 5)

    def test_empty_array_and_null_are_empty_blocks(self):
        for transactions in ([], None):
            with self.subTest(transactions=transactions):
                result = watcher.analyze_block(block_payload(10, transactions), 10)
                self.assertEqual(result["sum_tx_bytes"], 0)
                self.assertEqual(result["tx_count"], 0)

    def test_malformed_base64_rejects_entire_block(self):
        payload = block_payload(10)
        payload["result"]["block"]["data"]["txs"] = ["not valid base64***"]
        with self.assertRaisesRegex(watcher.MalformedTransactionError, "malformed"):
            watcher.analyze_block(payload, 10)

    def test_wrong_block_height_is_rejected(self):
        with self.assertRaisesRegex(watcher.BlockUnavailableError, "requested block 10"):
            watcher.analyze_block(block_payload(11), 10)

    def test_multi_message_tx_is_counted_once_with_combined_signature(self):
        raw = (
            b"prefix/cosmos.authz.v1beta1.MsgExec"
            b"/inference.inference.MsgSubmitHardwareDiff"
            b"/cosmos.bank.v1beta1.MsgSend"
        )
        result = watcher.analyze_block(block_payload(7, [raw]), 7)
        self.assertEqual(result["sum_tx_bytes"], len(raw))
        self.assertEqual(result["tx_count"], 1)
        self.assertEqual(len(result["message_types"]), 1)
        signature = "MsgSubmitHardwareDiff + MsgSend"
        self.assertEqual(result["message_types"][signature]["count"], 1)
        self.assertEqual(result["message_types"][signature]["bytes"], len(raw))

    def test_unknown_message_type_is_explicit(self):
        self.assertEqual(watcher.classify_message_types(b"opaque protobuf"), "(unknown)")


class SnapshotCollectionTests(unittest.TestCase):
    def mapping_for_window(self, rpc, latest, first, blocks):
        mapping = {f"{rpc}/status": status_payload(latest)}
        for height in range(first, latest + 1):
            mapping[f"{rpc}/block?height={height}"] = block_payload(
                height, blocks.get(height, [])
            )
        return mapping

    def test_window_is_exactly_ten_blocks_and_sums_each_block_once(self):
        rpc = "https://rpc1.example/chain-rpc"
        mapping = self.mapping_for_window(
            rpc,
            latest=20,
            first=11,
            blocks={11: [b"a"], 15: [b"bb"], 20: [b"ccc"]},
        )
        session = MappingSession(mapping)
        result = watcher.fetch_rpc_snapshot(rpc, config(), session=session)
        self.assertEqual(result["window_start"], 11)
        self.assertEqual(result["window_end"], 20)
        self.assertEqual(result["window_block_count"], 10)
        self.assertEqual(result["sum_tx_bytes"], 6)
        self.assertEqual(result["tx_count"], 3)
        self.assertEqual(result["max_tx_bytes"], 3)
        self.assertEqual(len(session.calls), 11)

    def test_chain_shorter_than_window_starts_at_one(self):
        rpc = "https://rpc1.example/chain-rpc"
        session = MappingSession(
            self.mapping_for_window(rpc, latest=3, first=1, blocks={})
        )
        result = watcher.fetch_rpc_snapshot(rpc, config(), session=session)
        self.assertEqual(result["window_start"], 1)
        self.assertEqual(result["window_block_count"], 3)

    def test_hot_blocks_are_sorted_by_bytes(self):
        rpc = "https://rpc1.example/chain-rpc"
        session = MappingSession(
            self.mapping_for_window(
                rpc,
                latest=3,
                first=1,
                blocks={1: [b"a"], 2: [b"12345"], 3: [b"abc"]},
            )
        )
        result = watcher.fetch_rpc_snapshot(
            rpc, config(window_blocks=3), session=session
        )
        self.assertEqual(
            [item["height"] for item in result["hot_blocks"]],
            [2, 3, 1],
        )

    def test_failed_partial_window_is_discarded_and_rebuilt_on_next_rpc(self):
        rpc1, rpc2 = config(window_blocks=2)["rpc_urls"]
        mapping = {
            f"{rpc1}/status": status_payload(2),
            f"{rpc1}/block?height=1": block_payload(1, [b"do-not-count"]),
            f"{rpc1}/block?height=2": Response(error=RuntimeError("503 Server Error")),
            f"{rpc2}/status": status_payload(2),
            f"{rpc2}/block?height=1": block_payload(1, [b"a"]),
            f"{rpc2}/block?height=2": block_payload(2, [b"bb"]),
        }
        session = MappingSession(mapping)
        result = watcher.collect_snapshot(
            config(window_blocks=2),
            session=session,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["snapshot"]["rpc"], rpc2)
        self.assertEqual(result["snapshot"]["sum_tx_bytes"], 3)
        self.assertIn(f"{rpc2}/block?height=1", session.calls)
        self.assertIn(f"{rpc2}/block?height=2", session.calls)

    def test_wrong_chain_falls_back_to_next_rpc(self):
        rpc1, rpc2 = config(window_blocks=1)["rpc_urls"]
        mapping = {
            f"{rpc1}/status": status_payload(5, chain_id="other-chain"),
            f"{rpc2}/status": status_payload(5),
            f"{rpc2}/block?height=5": block_payload(5),
        }
        result = watcher.collect_snapshot(
            config(window_blocks=1),
            session=MappingSession(mapping),
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["sources"][0]["error_category"], "wrong chain")
        self.assertEqual(result["snapshot"]["rpc"], rpc2)


class TransitionTests(unittest.TestCase):
    def evaluate(self, state, value, when=NOW):
        return watcher.evaluate_available(
            state,
            value,
            available_result(value)["sources"],
            config(),
            when,
        )

    def test_threshold_is_strictly_greater(self):
        state, messages = self.evaluate(
            watcher.default_state(), snapshot(100, 50_000_000)
        )
        self.assertEqual(messages, [])
        self.assertEqual(state["alert_level"], "none")

        state, messages = self.evaluate(
            watcher.default_state(), snapshot(100, 50_000_001)
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Аномальный объём", messages[0])
        self.assertEqual(state["alert_level"], "warning")

    def test_first_breach_warns_and_second_new_window_is_critical(self):
        state, messages = self.evaluate(
            watcher.default_state(), snapshot(100, 60_000_000)
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(state["consecutive_breaches"], 1)
        state, messages = self.evaluate(
            state, snapshot(101, 55_000_000), NOW + timedelta(minutes=1)
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Продолжается", messages[0])
        self.assertEqual(state["alert_level"], "critical")
        self.assertEqual(state["consecutive_breaches"], 2)

    def test_same_or_older_height_does_not_change_counters_or_repeat_alert(self):
        state, _ = self.evaluate(
            watcher.default_state(), snapshot(100, 60_000_000)
        )
        for height in (100, 99):
            with self.subTest(height=height):
                repeated, messages = self.evaluate(
                    state,
                    snapshot(height, 70_000_000),
                    NOW + timedelta(minutes=40),
                )
                self.assertEqual(messages, [])
                self.assertEqual(repeated["consecutive_breaches"], 1)
                self.assertEqual(repeated["last_evaluated_height"], 100)

    def test_recovery_requires_two_new_clean_windows(self):
        state, _ = self.evaluate(
            watcher.default_state(), snapshot(100, 60_000_000)
        )
        state, _ = self.evaluate(
            state, snapshot(101, 55_000_000), NOW + timedelta(minutes=1)
        )
        state, messages = self.evaluate(
            state, snapshot(102, 50_000_000), NOW + timedelta(minutes=2)
        )
        self.assertEqual(messages, [])
        self.assertEqual(state["alert_level"], "critical")
        state, messages = self.evaluate(
            state, snapshot(103, 10_000_000), NOW + timedelta(minutes=3)
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("вернулся к нормальному", messages[0])
        self.assertEqual(state["alert_level"], "none")

    def test_critical_reminder_requires_new_window_and_interval(self):
        state, _ = self.evaluate(
            watcher.default_state(), snapshot(100, 60_000_000)
        )
        state, _ = self.evaluate(
            state, snapshot(101, 60_000_000), NOW + timedelta(minutes=1)
        )
        state, messages = self.evaluate(
            state, snapshot(102, 60_000_000), NOW + timedelta(minutes=29)
        )
        self.assertEqual(messages, [])
        state, messages = self.evaluate(
            state, snapshot(103, 60_000_000), NOW + timedelta(minutes=31)
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Продолжается", messages[0])

    def test_monitoring_unavailable_alerts_once_and_preserves_business_state(self):
        state, _ = self.evaluate(
            watcher.default_state(), snapshot(100, 60_000_000)
        )
        sources = unavailable_sources()
        for run in range(1, 5):
            state, messages = watcher.evaluate_unavailable(
                state,
                sources,
                config(),
                NOW + timedelta(minutes=run),
            )
            self.assertEqual(len(messages), 1 if run == 3 else 0)
            if messages:
                self.assertIn("0/2", messages[0])
                self.assertNotIn("full internal details", messages[0])
        self.assertEqual(state["alert_level"], "warning")
        self.assertEqual(state["consecutive_breaches"], 1)
        self.assertEqual(state["sources"][0]["error"], "ConnectTimeout: full internal details")

    def test_monitoring_recovery_is_separate_from_load_result(self):
        state = watcher.default_state()
        state["monitoring"]["alerted"] = True
        state["monitoring"]["unavailable_runs"] = 3
        state, messages = self.evaluate(state, snapshot(100, 1_000_000))
        self.assertEqual(len(messages), 1)
        self.assertIn("Наблюдаемость", messages[0])
        self.assertEqual(state["status"], "healthy")


class PersistenceAndRuntimeTests(unittest.TestCase):
    def test_state_round_trip_and_restart_suppresses_duplicate_window(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            sent = []
            value = snapshot(100, 60_000_000)
            watcher.run_once(
                config(),
                now=NOW,
                collector=lambda _config: available_result(value),
                sender=sent.append,
                state_file=state_file,
            )
            watcher.run_once(
                config(),
                now=NOW + timedelta(minutes=1),
                collector=lambda _config: available_result(value),
                sender=sent.append,
                state_file=state_file,
            )
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            loaded = watcher.load_state(state_file)
        self.assertEqual(len(sent), 1)
        self.assertEqual(saved["last_evaluated_height"], 100)
        self.assertEqual(loaded["consecutive_breaches"], 1)

    def test_no_notify_does_not_persist_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            original = watcher.default_state()
            state_file.write_text(json.dumps(original), encoding="utf-8")
            sent = []
            watcher.run_once(
                config(),
                now=NOW,
                collector=lambda _config: available_result(
                    snapshot(100, 60_000_000)
                ),
                sender=sent.append,
                state_file=state_file,
                persist=False,
                notify=False,
            )
            after = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(sent, [])
        self.assertEqual(after, original)

    def test_full_rpc_error_is_logged_and_saved_but_not_sent(self):
        result = {
            "available": False,
            "snapshot": None,
            "sources": unavailable_sources(),
        }
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
                    collector=lambda _config: result,
                    sender=sent.append,
                    state_file=state_file,
                )
            saved = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn("full internal details", output.getvalue())
        self.assertIn("full internal details", saved["sources"][0]["error"])
        self.assertNotIn("full internal details", sent[0])

    def test_daemon_survives_one_iteration_error(self):
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
            [sys.executable, "-c", "import chain_load_watcher"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_request_errors_have_short_categories(self):
        cases = (
            (TimeoutError("timed out"), "timeout"),
            (RuntimeError("503 Server Error"), "HTTP 503"),
            (RuntimeError("DNS connection could not resolve host"), "connection error"),
            (ValueError("bad response"), "invalid JSON"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(watcher.exception_category(error), expected)


class WorkflowTests(unittest.TestCase):
    def test_workflow_has_schedule_mode_state_and_all_telegram_secrets(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/check-chain-load.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "3-58/5 * * * *"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn("CHAIN_LOAD_MODE: once", workflow)
        self.assertIn("run: python chain_load_watcher.py", workflow)
        self.assertIn("state/chain_load.json", workflow)
        self.assertIn("scripts/commit_state.sh", workflow)
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_MESSAGE_THREAD_ID",
            "TELEGRAM_SECONDARY_CHAT_ID",
        ):
            self.assertIn(name, workflow)


if __name__ == "__main__":
    unittest.main()
