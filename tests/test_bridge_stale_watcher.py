import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import bridge_stale_watcher as watcher


def config(**overrides):
    value = {
        "owner": "Dmitrii Kokh",
        "origin_chain": "ethereum",
        "inactive_slots_warning_percent": 35,
        "stale_slots_warning_percent": 35,
        "unknown_slots_warning_percent": 35,
        "bridge_latest_lag_tolerance_blocks": 64,
        "stale_alert_after_runs": 2,
        "top_peers_count": 10,
        "top_peer_missing_signatures_warning_count": 2,
        "top_peer_inactive_alert_after_runs": 2,
        "source_unavailable_alert_after_runs": 3,
        "request_timeout_seconds": 8,
        "attempts_per_source": 1,
        "attempts_per_node": 1,
        "max_parallel_node_checks": 10,
        "chain_api_bases": ["https://chain.example"],
        "epoch_urls": ["https://epoch.example"],
        "ethereum_rpc_urls": ["https://rpc.example"],
    }
    value.update(overrides)
    return value


def bls_snapshot(slot_counts):
    participants = []
    start = 0
    for index, slots in enumerate(slot_counts):
        participants.append(
            {
                "address": f"gonka1validator{index}",
                "slots": slots,
                "slot_start_index": start,
                "slot_end_index": start + slots - 1,
                "percentage_weight": None,
            }
        )
        start += slots
    return {
        "epoch": 342,
        "total_slots": start,
        "dkg_phase": watcher.SIGNED_PHASE,
        "participants": participants,
    }


def bls_payload(slot_counts):
    snapshot = bls_snapshot(slot_counts)
    return {
        "epoch_data": {
            "epoch_id": "342",
            "i_total_slots": snapshot["total_slots"],
            "dkg_phase": watcher.SIGNED_PHASE,
            "participants": [
                {
                    "address": item["address"],
                    "slot_start_index": item["slot_start_index"],
                    "slot_end_index": item["slot_end_index"],
                    "percentage_weight": "0",
                }
                for item in snapshot["participants"]
            ],
        }
    }


def inspected(snapshot, classifications, finalized=1000):
    result = {}
    for item, classification in zip(snapshot["participants"], classifications):
        latest = finalized if classification == "healthy" else None
        if classification == "stale":
            latest = finalized - 65
        result[item["address"]] = {
            "address": item["address"],
            "slots": item["slots"],
            "bridge_latest": latest,
            "bridge_lag": None if latest is None else finalized - latest,
            "classification": classification,
        }
    return result


class BlsParsingTests(unittest.TestCase):
    def test_parses_inclusive_ranges_and_requires_full_slot_coverage(self):
        parsed = watcher.parse_bls_epoch(bls_payload([19, 16, 14, 51]), 342)
        self.assertEqual(parsed["total_slots"], 100)
        self.assertEqual(sum(item["slots"] for item in parsed["participants"]), 100)
        self.assertEqual(parsed["participants"][0]["slots"], 19)

    def test_rejects_overlapping_ranges(self):
        payload = bls_payload([50, 50])
        payload["epoch_data"]["participants"][1]["slot_start_index"] = 49
        with self.assertRaisesRegex(ValueError, "overlapping"):
            watcher.parse_bls_epoch(payload, 342)

    def test_rejects_missing_slots(self):
        payload = bls_payload([50, 50])
        payload["epoch_data"]["participants"][1]["slot_start_index"] = 51
        payload["epoch_data"]["participants"][1]["slot_end_index"] = 99
        with self.assertRaisesRegex(ValueError, "cover 99 of 100"):
            watcher.parse_bls_epoch(payload, 342)


class ParticipantClassificationTests(unittest.TestCase):
    def test_private_and_credentialed_validator_urls_are_rejected(self):
        for value in (
            "http://127.0.0.1:8000",
            "http://169.254.169.254",
            "https://user:password@validator.example",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                watcher.normalized_public_node_url(value)

    def test_lag_tolerance_boundaries_and_ahead_are_healthy(self):
        participant = bls_snapshot([100])["participants"][0]
        cases = (
            (1000, "healthy", 0, "equal_to_finalized"),
            (936, "healthy", 64, "lag_within_tolerance"),
            (935, "stale", 65, "lag_exceeds_tolerance"),
            (1001, "healthy", -1, "ahead_of_finalized"),
        )
        for latest, classification, lag, reason in cases:
            with (
                self.subTest(latest=latest),
                patch.object(
                    watcher,
                    "fetch_participant_info",
                    return_value=("https://validator.example", "ACTIVE"),
                ),
                patch.object(
                    watcher,
                    "probe_bridge_latest",
                    return_value=(latest, "validator"),
                ),
            ):
                result = watcher.inspect_participant(config(), participant, 1000)
            self.assertEqual(result["classification"], classification)
            self.assertEqual(result["bridge_lag"], lag)
            self.assertEqual(result["reason"], reason)

    def test_http_503_is_unknown_not_stale(self):
        participant = bls_snapshot([100])["participants"][0]
        with (
            patch.object(
                watcher,
                "fetch_participant_info",
                return_value=("https://validator.example", "ACTIVE"),
            ),
            patch.object(
                watcher,
                "probe_bridge_latest",
                side_effect=watcher.SourcesUnavailable("HTTPError: 503"),
            ),
        ):
            result = watcher.inspect_participant(config(), participant, 1000)
        self.assertEqual(result["classification"], "unknown")
        self.assertIsNone(result["bridge_latest"])
        self.assertIsNone(result["bridge_lag"])
        self.assertIn("503", result["reason"])

    def test_api_availability_is_checked_without_ethereum_finalized(self):
        participant = bls_snapshot([100])["participants"][0]
        with (
            patch.object(
                watcher,
                "fetch_participant_info",
                return_value=("https://validator.example", "ACTIVE"),
            ),
            patch.object(watcher, "probe_bridge_latest", return_value=(1000, "validator")),
        ):
            result = watcher.inspect_participant(config(), participant, None)

        self.assertEqual(result["classification"], "reachable")
        self.assertEqual(result["bridge_latest"], 1000)


class StateMigrationTests(unittest.TestCase):
    def test_old_config_without_top_peer_fields_uses_safe_defaults(self):
        old_config = config()
        old_config.pop("top_peers_count")
        old_config.pop("top_peer_missing_signatures_warning_count")
        old_config.pop("top_peer_inactive_alert_after_runs")
        old_config.pop("bridge_latest_lag_tolerance_blocks")
        old_config.pop("stale_alert_after_runs")
        with (
            patch.object(watcher, "load_json", return_value=old_config),
            patch.object(watcher, "CONFIG_FILE", Path("unused.json")),
        ):
            loaded = watcher.load_config()

        self.assertEqual(loaded["top_peers_count"], 10)
        self.assertEqual(loaded["top_peer_missing_signatures_warning_count"], 2)
        self.assertEqual(loaded["top_peer_inactive_alert_after_runs"], 2)
        self.assertEqual(loaded["bridge_latest_lag_tolerance_blocks"], 64)
        self.assertEqual(loaded["stale_alert_after_runs"], 2)

    def test_existing_state_without_top_peers_is_loaded_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "bridge_stale.json"
            state_file.write_text(
                '{"signals": {}, "sources": {}, "participants": {}}',
                encoding="utf-8",
            )
            with patch.object(watcher, "STATE_FILE", state_file):
                state = watcher.load_state()

        self.assertEqual(state["top_peers"], {})
        self.assertEqual(state["top_peer_rule_version"], watcher.TOP_PEER_RULE_VERSION)

    def test_old_http_based_top_peer_alert_is_discarded_without_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "bridge_stale.json"
            state_file.write_text(
                '{"signals": {}, "sources": {}, "participants": {}, '
                '"top_peers": {"gonka1old": {"alerted": true, '
                '"failure_reason": "bridge_api_unavailable"}}}',
                encoding="utf-8",
            )
            with patch.object(watcher, "STATE_FILE", state_file):
                state = watcher.load_state()

        self.assertEqual(state["top_peers"], {})
        self.assertEqual(state["top_peer_rule_version"], watcher.TOP_PEER_RULE_VERSION)

    def test_literal_equality_stale_state_is_discarded_without_recovery(self):
        state = watcher.default_state()
        state["stale_rule_version"] = "literal_equality_v1"
        state["signals"]["stale"] = {"active": True, "value_slots": 92}

        watcher.migrate_stale_rule_state(state, config())

        self.assertNotIn("stale", state["signals"])
        self.assertEqual(
            state["stale_rule_version"],
            watcher.stale_rule_version(config()),
        )


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.now = "2026-07-29T00:00:00+00:00"

    def test_epoch_342_style_top_three_49_does_not_alert(self):
        snapshot = bls_snapshot([19, 16, 14, 8, 8, 8, 7, 7, 6, 2, 1, 1, 1, 1, 1])
        state = watcher.default_state()
        eligible = {item["address"] for item in snapshot["participants"]}
        messages = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, {}, None, self.now
        )
        self.assertEqual(messages, [])
        self.assertFalse(state["signals"]["concentration"]["active"])
        self.assertEqual(state["signals"]["concentration"]["value_slots"], 49)

    def test_top_three_51_alerts_once_then_recovers(self):
        snapshot = bls_snapshot([20, 16, 15, 14, 13, 12, 10])
        state = watcher.default_state()
        eligible = {item["address"] for item in snapshot["participants"]}
        first = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, {}, None, self.now
        )
        second = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, {}, None, self.now
        )
        self.assertEqual(len(first), 1)
        self.assertIn("Высокая концентрация", first[0])
        self.assertEqual(second, [])

        recovered = bls_snapshot([17, 16, 16, 15, 14, 12, 10])
        recovered_eligible = {item["address"] for item in recovered["participants"]}
        messages = watcher.evaluate_snapshot(
            state, config(), recovered, recovered_eligible, {}, None, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("вернулась в норму", messages[0])

    def test_missing_group_members_create_inactive_slot_alert_without_ethereum(self):
        snapshot = bls_snapshot([20, 15, 14, 13, 13, 10, 8, 7])
        eligible = {item["address"] for item in snapshot["participants"][2:]}
        state = watcher.default_state()
        messages = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, {}, None, self.now
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("не могут голосовать", messages[0])
        self.assertEqual(state["signals"]["inactive"]["value_slots"], 35)

    def test_stale_and_unknown_slots_are_aggregated_separately(self):
        snapshot = bls_snapshot([20, 15, 14, 13, 13, 10, 8, 7])
        eligible = {item["address"] for item in snapshot["participants"]}
        observations = inspected(
            snapshot,
            ["stale", "stale", "unknown", "unknown", "unknown", "healthy", "healthy", "healthy"],
        )
        state = watcher.default_state()
        first = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, observations, 1000, self.now
        )
        self.assertEqual(len(first), 1)
        self.assertIn("недоступен", first[0])
        self.assertFalse(state["signals"]["stale"]["active"])
        self.assertTrue(state["signals"]["stale"]["condition_active"])
        self.assertEqual(state["signals"]["stale"]["consecutive_active_runs"], 1)

        second = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, observations, 1000, self.now
        )
        self.assertEqual(len(second), 1)
        self.assertIn("существенно отстаёт", second[0])
        self.assertTrue(state["signals"]["stale"]["active"])
        self.assertEqual(state["signals"]["stale"]["value_slots"], 35)
        self.assertTrue(state["signals"]["unknown"]["active"])
        self.assertEqual(state["signals"]["unknown"]["value_slots"], 40)

    def test_stale_pending_resets_without_recovery_and_alert_recovers_once(self):
        snapshot = bls_snapshot([35, 34, 31])
        eligible = {item["address"] for item in snapshot["participants"]}
        stale = inspected(snapshot, ["stale", "healthy", "healthy"])
        healthy = inspected(snapshot, ["healthy", "healthy", "healthy"])
        state = watcher.default_state()

        watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, stale, 1000, self.now
        )
        cleared = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, healthy, 1000, self.now
        )
        self.assertEqual(cleared, [])
        self.assertFalse(state["signals"]["stale"]["active"])
        self.assertEqual(state["signals"]["stale"]["consecutive_active_runs"], 0)

        watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, stale, 1000, self.now
        )
        alerted = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, stale, 1000, self.now
        )
        self.assertEqual(len(alerted), 1)
        recovered = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, healthy, 1000, self.now
        )
        self.assertEqual(len(recovered), 1)
        self.assertIn("восстановилась", recovered[0])

    def test_incomplete_check_clears_pending_but_preserves_existing_alert(self):
        pending = watcher.default_state()
        pending["signals"]["stale"] = {
            "active": False,
            "condition_active": True,
            "consecutive_active_runs": 1,
            "pending_since": self.now,
        }
        watcher.reset_pending_signal(pending, "stale")
        self.assertEqual(
            pending["signals"]["stale"]["consecutive_active_runs"],
            0,
        )
        self.assertIsNone(pending["signals"]["stale"]["pending_since"])

        alerted = watcher.default_state()
        alerted["signals"]["stale"] = {
            "active": True,
            "consecutive_active_runs": 2,
        }
        watcher.reset_pending_signal(alerted, "stale")
        self.assertTrue(alerted["signals"]["stale"]["active"])
        self.assertEqual(alerted["signals"]["stale"]["consecutive_active_runs"], 2)


class TopPeerSignalTests(unittest.TestCase):
    def setUp(self):
        self.now = "2026-07-29T00:00:00+00:00"
        self.later = "2026-07-29T00:05:00+00:00"
        self.snapshot = bls_snapshot([20, 18, 16, 14, 12, 8, 5, 3, 2, 2, 1])
        self.eligible = {item["address"] for item in self.snapshot["participants"]}

    def history(self, *validator_sets):
        return [
            {
                "key": f"ethereum/{100 + index}/0",
                "origin_chain": "ethereum",
                "block_number": 100 + index,
                "receipt_index": 0,
                "status": "BRIDGE_COMPLETED",
                "epoch_index": self.snapshot["epoch"],
                "validators": list(validators),
            }
            for index, validators in enumerate(validator_sets)
        ]

    def test_no_transaction_evidence_does_not_alert(self):
        state = watcher.default_state()
        messages = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            [],
            self.now,
        )
        self.assertEqual(messages, [])
        self.assertTrue(
            all(
                item["evidence_status"] == "insufficient_transactions"
                for item in state["top_peers"].values()
            )
        )

    def test_missing_two_signatures_alerts_once_and_new_signature_recovers(self):
        address = self.snapshot["participants"][0]["address"]
        other_signers = self.eligible - {address}
        history = self.history(other_signers, other_signers)
        state = watcher.default_state()

        first = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            history,
            self.now,
        )
        second = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            history,
            self.later,
        )
        self.assertEqual(len(first), 1)
        self.assertIn("Top-10", first[0])
        self.assertIn(address, first[0])
        self.assertIn("HTTP 503", first[0])
        self.assertEqual(second, [])
        self.assertTrue(state["top_peers"][address]["alerted"])

        recovered_history = history + self.history({address})
        recovered_history[-1]["block_number"] = 200
        recovered_history[-1]["key"] = "ethereum/200/0"
        recovered = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            recovered_history,
            self.later,
        )
        self.assertEqual(len(recovered), 1)
        self.assertIn("восстановились", recovered[0])
        self.assertFalse(state["top_peers"][address]["alerted"])

    def test_inactive_top_peer_alerts_on_second_check_but_peer_below_top_ten_does_not(self):
        top_address = self.snapshot["participants"][1]["address"]
        below_top_address = self.snapshot["participants"][10]["address"]
        eligible = self.eligible - {top_address}
        state = watcher.default_state()

        watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            eligible,
            [],
            self.now,
        )
        messages = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            eligible,
            [],
            self.later,
        )

        self.assertEqual(len(messages), 1)
        self.assertIn(top_address, messages[0])
        self.assertNotIn(below_top_address, messages[0])
        self.assertIn("inactive", messages[0])
        self.assertNotIn(below_top_address, state["top_peers"])

    def test_multiple_failed_top_peers_are_aggregated_in_one_message(self):
        first = self.snapshot["participants"][0]["address"]
        second = self.snapshot["participants"][1]["address"]
        other_signers = self.eligible - {first, second}
        state = watcher.default_state()
        messages = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            self.history(other_signers, other_signers),
            self.later,
        )

        self.assertEqual(len(messages), 1)
        self.assertIn(self.snapshot["participants"][0]["address"], messages[0])
        self.assertIn(self.snapshot["participants"][1]["address"], messages[0])

    def test_alert_is_closed_when_peer_leaves_top_ten(self):
        address = self.snapshot["participants"][0]["address"]
        other_signers = self.eligible - {address}
        state = watcher.default_state()
        watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            self.history(other_signers, other_signers),
            self.now,
        )

        changed = bls_snapshot([1, 22, 20, 18, 14, 10, 5, 4, 3, 2, 2])
        changed_eligible = {item["address"] for item in changed["participants"]}
        messages = watcher.evaluate_top_peers(
            state,
            config(),
            changed,
            changed_eligible,
            [],
            self.later,
        )

        self.assertEqual(len(messages), 1)
        self.assertIn("больше не входят", messages[0])
        self.assertNotIn(self.snapshot["participants"][0]["address"], state["top_peers"])


class RunIntegrationTests(unittest.TestCase):
    def test_run_joins_sources_persists_state_and_sends_manual_summary(self):
        snapshot = bls_snapshot([19, 16, 14, 13, 13, 10, 8, 7])
        eligible = {item["address"] for item in snapshot["participants"]}
        observations = inspected(snapshot, ["healthy"] * len(snapshot["participants"]))
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "bridge_stale.json"
            with (
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "load_config", return_value=config()),
                patch.object(watcher, "fetch_current_epoch", return_value=342),
                patch.object(watcher, "fetch_bls_epoch", return_value=snapshot),
                patch.object(watcher, "fetch_group_id", return_value="7"),
                patch.object(watcher, "fetch_group_members", return_value=eligible),
                patch.object(watcher, "fetch_finalized_block", return_value=(1000, "rpc")),
                patch.object(
                    watcher,
                    "inspect_eligible_participants",
                    return_value=observations,
                ),
                patch.object(watcher, "send_telegram_message") as send,
                patch.dict(watcher.os.environ, {"SEND_BRIDGE_STALE_SUMMARY": "true"}),
            ):
                result = watcher.run()

            self.assertTrue(state_file.exists())
            self.assertEqual(result["epoch"], 342)
            self.assertEqual(result["ethereum_finalized_block"], 1000)
            self.assertEqual(len(result["participants"]), len(snapshot["participants"]))
            self.assertFalse(result["signals"]["stale"]["active"])
            send.assert_called_once()
            self.assertIn("Проверка Bridge Stale Check A", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
