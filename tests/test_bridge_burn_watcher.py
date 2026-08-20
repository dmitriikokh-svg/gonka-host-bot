import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import bridge_burn_watcher as watcher


CONTRACT = "0x972a7A92D92796a98801A8818bcF91f1648f2F68"


def config(**overrides):
    value = {
        "owner": "Dmitrii Kokh",
        "origin_chain": "ethereum",
        "wgnk_contract": CONTRACT,
        "initial_check_after_minutes": 5,
        "warning_after_minutes": 10,
        "critical_overdue_transactions": 2,
        "bootstrap_lookback_blocks": 128,
        "max_blocks_per_log_request": 500,
        "source_unavailable_alert_after_runs": 3,
        "request_timeout_seconds": 15,
        "attempts_per_source": 1,
        "completed_history_limit": 20,
        "ethereum_rpc_urls": ["https://rpc.example"],
        "gonka_transaction_url_templates": [
            "https://gonka.example/{origin_chain}/{block_number}/{receipt_index}"
        ],
    }
    value.update(overrides)
    return value


def burn_log(block=100, position=3, log_index=7, amount=25, suffix="1"):
    return {
        "address": CONTRACT.lower(),
        "topics": [
            watcher.TRANSFER_TOPIC,
            "0x" + "1" * 64,
            watcher.ZERO_ADDRESS_TOPIC,
        ],
        "blockNumber": hex(block),
        "transactionIndex": hex(position),
        "logIndex": hex(log_index),
        "transactionHash": "0x" + suffix * 64,
        "data": hex(amount),
    }


def queued_item(block, detected="2026-07-29T00:00:00+00:00"):
    state = watcher.default_state()
    watcher.add_burn_logs(
        state,
        config(),
        [burn_log(block=block, position=block % 10, suffix=str(block % 10))],
        detected,
    )
    return next(iter(state["queue"].values()))


class BurnParsingTests(unittest.TestCase):
    def test_parses_transfer_to_zero_and_uses_transaction_index(self):
        parsed = watcher.parse_burn_log(burn_log(), CONTRACT)
        self.assertEqual(parsed["block_number"], 100)
        self.assertEqual(parsed["receipt_index"], 3)
        self.assertEqual(parsed["log_index"], 7)
        self.assertEqual(parsed["amount_raw"], "25")
        self.assertEqual(parsed["key"], "ethereum/100/3")

    def test_rejects_non_burn_transfer(self):
        value = burn_log()
        value["topics"][2] = "0x" + "2" * 64
        with self.assertRaises(ValueError):
            watcher.parse_burn_log(value, CONTRACT)

    def test_multiple_burn_logs_in_one_transaction_are_deduplicated(self):
        state = watcher.default_state()
        logs = [
            burn_log(log_index=7, amount=25),
            burn_log(log_index=8, amount=10),
        ]
        added = watcher.add_burn_logs(
            state,
            config(),
            logs,
            "2026-07-29T00:00:00+00:00",
        )
        self.assertEqual(added, 1)
        self.assertEqual(len(state["queue"]), 1)
        item = state["queue"]["ethereum/100/3"]
        self.assertEqual(item["amount_raw"], "35")
        self.assertEqual(item["log_indices"], [7, 8])


class BridgeResponseTests(unittest.TestCase):
    def test_completed_wins_and_validators_are_parsed(self):
        observation = watcher.parse_bridge_observation(
            {
                "bridgeTransactions": [
                    {"status": "BRIDGE_PENDING", "validators": ["gonka1a"]},
                    {"status": "BRIDGE_COMPLETED", "validators": ["gonka1b"]},
                ]
            }
        )
        self.assertEqual(observation["status"], "BRIDGE_COMPLETED")
        self.assertEqual(observation["validators"], ["gonka1a", "gonka1b"])
        self.assertEqual(observation["completed_validators"], ["gonka1b"])
        self.assertIsNone(observation["epoch_index"])

    def test_completed_epoch_and_signers_are_parsed(self):
        observation = watcher.parse_bridge_observation(
            {
                "bridge_transactions": [
                    {
                        "status": "BRIDGE_COMPLETED",
                        "epoch_index": "342",
                        "validators": ["gonka1b", "gonka1a"],
                    }
                ]
            }
        )
        self.assertEqual(observation["epoch_index"], 342)
        self.assertEqual(observation["completed_validators"], ["gonka1a", "gonka1b"])

    def test_valid_empty_response_means_missing(self):
        self.assertEqual(
            watcher.parse_bridge_observation({"bridgeTransactions": []}),
            {
                "status": "MISSING",
                "validators": [],
                "completed_validators": [],
                "epoch_index": None,
            },
        )


class RpcConfigurationTests(unittest.TestCase):
    def test_secret_urls_are_first_support_both_separators_and_are_deduplicated(self):
        cfg = config(
            ethereum_rpc_urls=[
                "https://public.example/rpc",
                "https://duplicate.example/rpc/",
            ]
        )
        with patch.dict(
            os.environ,
            {
                "ETHEREUM_RPC_URLS": (
                    " https://secret.example/token?key=value,\n"
                    "https://duplicate.example/rpc,,\n"
                    "https://secret.example/token?key=value "
                )
            },
            clear=True,
        ):
            urls = watcher.configured_rpc_urls(cfg)

        self.assertEqual(
            urls,
            [
                "https://secret.example/token?key=value",
                "https://duplicate.example/rpc",
                "https://public.example/rpc",
            ],
        )

    def test_invalid_secret_url_does_not_echo_provider_token(self):
        token = "TOP_SECRET_PROVIDER_TOKEN"
        with (
            patch.dict(
                os.environ,
                {"ETHEREUM_RPC_URLS": f"ftp://provider.example/{token}"},
                clear=True,
            ),
            self.assertRaises(ValueError) as caught,
        ):
            watcher.configured_rpc_urls(config())
        self.assertNotIn(token, str(caught.exception))

    def test_provider_token_is_absent_from_exception_state_and_message(self):
        token = "TOP_SECRET_PROVIDER_TOKEN"
        cfg = config(
            source_unavailable_alert_after_runs=1,
            ethereum_rpc_urls=[],
        )

        class Session:
            @staticmethod
            def post(url, **_kwargs):
                raise RuntimeError(f"403 denied for {url}")

        with (
            patch.dict(
                os.environ,
                {"ETHEREUM_RPC_URLS": f"https://provider.example/v2/{token}?key={token}"},
                clear=True,
            ),
            self.assertRaises(watcher.SourcesUnavailable) as caught,
        ):
            watcher.rpc_call(cfg, "eth_getBlockByNumber", ["finalized", False], session=Session)

        self.assertNotIn(token, str(caught.exception))
        self.assertIn("https://provider.example", str(caught.exception))
        state = watcher.default_state()
        messages = watcher.source_failure(
            state,
            cfg,
            "ethereum",
            caught.exception,
            "2026-07-29T00:00:00+00:00",
        )
        self.assertNotIn(token, repr(state))
        self.assertNotIn(token, "\n".join(messages))

    def test_workflow_reads_rpc_only_from_github_secret(self):
        workflow = (
            watcher.ROOT / ".github" / "workflows" / "check-bridge-burn.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ETHEREUM_RPC_URLS: ${{ secrets.ETHEREUM_RPC_URLS }}",
            workflow,
        )


class QueueAlertTests(unittest.TestCase):
    def state_with_items(self, *blocks):
        state = watcher.default_state()
        for block in blocks:
            item = queued_item(block)
            state["queue"][f"ethereum/{item['block_number']}/{item['receipt_index']}"] = item
        return state

    def test_pending_warns_at_ten_minutes_once_and_recovers(self):
        state = self.state_with_items(101)
        cfg = config()
        pending = ({"status": "BRIDGE_PENDING", "validators": ["gonka1a"]}, "archive")

        with patch.object(watcher, "fetch_bridge_observation", return_value=pending):
            messages = watcher.process_queue(state, cfg, "2026-07-29T00:05:00+00:00")
        self.assertEqual(messages, [])
        self.assertFalse(next(iter(state["queue"].values()))["overdue"])
        self.assertEqual(
            watcher.evaluate_alerts(state, cfg, "2026-07-29T00:05:00+00:00"),
            [],
        )

        with patch.object(watcher, "fetch_bridge_observation", return_value=pending):
            watcher.process_queue(state, cfg, "2026-07-29T00:10:00+00:00")
        messages = watcher.evaluate_alerts(state, cfg, "2026-07-29T00:10:00+00:00")
        self.assertEqual(len(messages), 1)
        self.assertIn("не завершена вовремя", messages[0])
        self.assertEqual(
            watcher.evaluate_alerts(state, cfg, "2026-07-29T00:11:00+00:00"),
            [],
        )

        completed = (
            {
                "status": "BRIDGE_COMPLETED",
                "validators": ["gonka1a"],
                "completed_validators": ["gonka1a"],
                "epoch_index": 342,
            },
            "archive",
        )
        with patch.object(watcher, "fetch_bridge_observation", return_value=completed):
            messages = watcher.process_queue(state, cfg, "2026-07-29T00:15:00+00:00")
        self.assertEqual(len(messages), 1)
        self.assertIn("завершена", messages[0])
        self.assertEqual(state["queue"], {})
        self.assertEqual(len(state["completed_history"]), 1)
        evidence = state["completed_history"][0]
        self.assertEqual(evidence["epoch_index"], 342)
        self.assertEqual(evidence["validators"], ["gonka1a"])

    def test_completed_signer_history_is_deduplicated_and_bounded(self):
        state = watcher.default_state()
        cfg = config(completed_history_limit=2)
        observation = {
            "status": "BRIDGE_COMPLETED",
            "validators": ["gonka1fallback"],
            "completed_validators": ["gonka1signer"],
            "epoch_index": 342,
        }
        for block in (101, 102, 103):
            item = queued_item(block)
            key = f"ethereum/{item['block_number']}/{item['receipt_index']}"
            watcher.remember_completed_transaction(
                state, cfg, key, item, observation, "2026-07-29T00:15:00+00:00"
            )
        watcher.remember_completed_transaction(
            state,
            cfg,
            state["completed_history"][-1]["key"],
            queued_item(103),
            observation,
            "2026-07-29T00:16:00+00:00",
        )
        self.assertEqual(len(state["completed_history"]), 2)
        self.assertEqual(
            [item["block_number"] for item in state["completed_history"]],
            [102, 103],
        )
        self.assertEqual(state["completed_history"][-1]["validators"], ["gonka1signer"])

    def test_missing_is_treated_as_overdue_after_successful_query(self):
        state = self.state_with_items(101)
        missing = ({"status": "MISSING", "validators": []}, "archive")
        with patch.object(watcher, "fetch_bridge_observation", return_value=missing):
            watcher.process_queue(state, config(), "2026-07-29T00:10:00+00:00")
        messages = watcher.evaluate_alerts(
            state,
            config(),
            "2026-07-29T00:10:00+00:00",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("MISSING", messages[0])

    def test_api_failure_does_not_create_false_stuck_alert(self):
        state = self.state_with_items(101)
        with patch.object(
            watcher,
            "fetch_bridge_observation",
            side_effect=watcher.SourcesUnavailable("down"),
        ):
            watcher.process_queue(state, config(), "2026-07-29T00:10:00+00:00")
        item = next(iter(state["queue"].values()))
        self.assertFalse(item["overdue"])
        self.assertEqual(
            watcher.evaluate_alerts(state, config(), "2026-07-29T00:10:00+00:00"),
            [],
        )

    def test_two_overdue_transactions_trigger_one_critical_alert(self):
        state = self.state_with_items(101, 102)
        pending = ({"status": "BRIDGE_PENDING", "validators": []}, "archive")
        with patch.object(watcher, "fetch_bridge_observation", return_value=pending):
            watcher.process_queue(state, config(), "2026-07-29T00:10:00+00:00")

        messages = watcher.evaluate_alerts(
            state,
            config(),
            "2026-07-29T00:10:00+00:00",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Несколько", messages[0])
        self.assertTrue(state["critical_alerted"])
        self.assertTrue(
            all(item["alert_level"] == "critical" for item in state["queue"].values())
        )
        self.assertEqual(
            watcher.evaluate_alerts(state, config(), "2026-07-29T00:11:00+00:00"),
            [],
        )

    def test_critical_downgrades_when_only_one_stuck_transaction_remains(self):
        state = self.state_with_items(101, 102)
        for item in state["queue"].values():
            item["overdue"] = True
            item["alert_level"] = "critical"
        state["critical_alerted"] = True
        state["critical_since"] = "2026-07-29T00:10:00+00:00"

        first_key = next(iter(state["queue"]))
        state["queue"].pop(first_key)
        messages = watcher.evaluate_alerts(
            state,
            config(),
            "2026-07-29T00:15:00+00:00",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Критическое состояние", messages[0])
        self.assertFalse(state["critical_alerted"])
        self.assertEqual(next(iter(state["queue"].values()))["alert_level"], "warning")


class SourceAvailabilityTests(unittest.TestCase):
    def test_source_alerts_on_third_failure_and_recovers(self):
        state = watcher.default_state()
        cfg = config()
        for run in range(1, 4):
            messages = watcher.source_failure(
                state,
                cfg,
                "ethereum",
                RuntimeError("down"),
                f"2026-07-29T00:0{run}:00+00:00",
            )
            self.assertEqual(len(messages), 1 if run == 3 else 0)
        messages = watcher.source_success(
            state,
            "ethereum",
            "2026-07-29T00:04:00+00:00",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("снова доступен", messages[0])


class RunIntegrationTests(unittest.TestCase):
    def test_run_discovers_finalized_burn_persists_it_and_sends_manual_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "bridge.json"
            with (
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "load_config", return_value=config()),
                patch.object(watcher, "fetch_finalized_block", return_value=(200, "rpc")),
                patch.object(
                    watcher,
                    "fetch_burn_logs",
                    return_value=[burn_log(block=150, position=2)],
                ) as fetch_logs,
                patch.object(watcher, "send_telegram_message") as send,
                patch.dict(os.environ, {"SEND_BRIDGE_SUMMARY": "true"}, clear=True),
            ):
                state = watcher.run()

        fetch_logs.assert_called_once_with(config(), 73, 200)
        self.assertEqual(state["last_scanned_finalized_block"], 200)
        self.assertIn("ethereum/150/2", state["queue"])
        self.assertEqual(send.call_count, 1)
        self.assertIn("Проверка WGNK burn bridge", send.call_args.args[0])

    def test_full_ethereum_failure_does_not_advance_cursor(self):
        previous = watcher.default_state()
        previous["ethereum_finalized_block"] = 150
        previous["last_scanned_finalized_block"] = 150
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "bridge.json"
            watcher.save_json_atomic(state_file, previous, sort_keys=True)
            with (
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "load_config", return_value=config()),
                patch.object(
                    watcher,
                    "fetch_finalized_block",
                    side_effect=watcher.SourcesUnavailable("safe failure"),
                ),
                patch.object(watcher, "fetch_burn_logs") as fetch_logs,
                patch.object(watcher, "send_telegram_message"),
                patch.dict(os.environ, {}, clear=True),
            ):
                state = watcher.run()

        self.assertEqual(state["ethereum_finalized_block"], 150)
        self.assertEqual(state["last_scanned_finalized_block"], 150)
        fetch_logs.assert_not_called()
        self.assertEqual(state["sources"]["ethereum"]["status"], "unavailable")

    def test_empty_logs_are_success_and_next_run_starts_after_cursor(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "bridge.json"
            with (
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "load_config", return_value=cfg),
                patch.object(
                    watcher,
                    "fetch_finalized_block",
                    side_effect=[
                        (200, "https://provider.example"),
                        (205, "https://provider.example"),
                    ],
                ),
                patch.object(watcher, "fetch_burn_logs", return_value=[]) as fetch_logs,
                patch.object(watcher, "send_telegram_message"),
                patch.dict(os.environ, {}, clear=True),
            ):
                first = watcher.run()
                second = watcher.run()

        self.assertEqual(first["ethereum_finalized_block"], 200)
        self.assertEqual(first["last_scanned_finalized_block"], 200)
        self.assertEqual(second["ethereum_finalized_block"], 205)
        self.assertEqual(second["last_scanned_finalized_block"], 205)
        self.assertEqual(second["queue"], {})
        self.assertEqual(second["sources"]["ethereum"]["status"], "available")
        self.assertEqual(
            second["sources"]["ethereum"]["endpoint"],
            "https://provider.example",
        )
        self.assertEqual(
            fetch_logs.call_args_list,
            [call(cfg, 73, 200), call(cfg, 201, 205)],
        )


if __name__ == "__main__":
    unittest.main()
