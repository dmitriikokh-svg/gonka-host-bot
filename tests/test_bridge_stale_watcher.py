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
        "top_peers_count": 10,
        "top_peer_unavailable_alert_after_runs": 2,
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
            latest = finalized - 1
        result[item["address"]] = {
            "address": item["address"],
            "slots": item["slots"],
            "bridge_latest": latest,
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

    def test_only_literal_equality_is_healthy(self):
        participant = bls_snapshot([100])["participants"][0]
        with (
            patch.object(
                watcher,
                "fetch_participant_info",
                return_value=("https://validator.example", "ACTIVE"),
            ),
            patch.object(watcher, "probe_bridge_latest", return_value=(1000, "validator")),
        ):
            equal = watcher.inspect_participant(config(), participant, 1000)
        self.assertEqual(equal["classification"], "healthy")

        with (
            patch.object(
                watcher,
                "fetch_participant_info",
                return_value=("https://validator.example", "ACTIVE"),
            ),
            patch.object(watcher, "probe_bridge_latest", return_value=(1001, "validator")),
        ):
            ahead = watcher.inspect_participant(config(), participant, 1000)
        self.assertEqual(ahead["classification"], "stale")

    def test_unavailable_api_is_unknown_not_stale(self):
        participant = bls_snapshot([100])["participants"][0]
        with patch.object(
            watcher,
            "fetch_participant_info",
            side_effect=watcher.SourcesUnavailable("down"),
        ):
            result = watcher.inspect_participant(config(), participant, 1000)
        self.assertEqual(result["classification"], "unknown")
        self.assertIsNone(result["bridge_latest"])

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
        messages = watcher.evaluate_snapshot(
            state, config(), snapshot, eligible, observations, 1000, self.now
        )
        self.assertEqual(len(messages), 2)
        self.assertTrue(state["signals"]["stale"]["active"])
        self.assertEqual(state["signals"]["stale"]["value_slots"], 35)
        self.assertTrue(state["signals"]["unknown"]["active"])
        self.assertEqual(state["signals"]["unknown"]["value_slots"], 40)


class TopPeerSignalTests(unittest.TestCase):
    def setUp(self):
        self.now = "2026-07-29T00:00:00+00:00"
        self.later = "2026-07-29T00:05:00+00:00"
        self.snapshot = bls_snapshot([20, 18, 16, 14, 12, 8, 5, 3, 2, 2, 1])
        self.eligible = {item["address"] for item in self.snapshot["participants"]}

    def test_top_peer_unknown_alerts_on_second_run_only_and_then_recovers(self):
        observations = inspected(
            self.snapshot,
            ["unknown"] + ["healthy"] * (len(self.snapshot["participants"]) - 1),
        )
        state = watcher.default_state()

        first = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            observations,
            self.now,
        )
        second = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            observations,
            self.later,
        )
        third = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            observations,
            self.later,
        )

        address = self.snapshot["participants"][0]["address"]
        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertIn("Top-10", second[0])
        self.assertIn(address, second[0])
        self.assertEqual(third, [])
        self.assertTrue(state["top_peers"][address]["alerted"])

        healthy = inspected(self.snapshot, ["healthy"] * len(self.snapshot["participants"]))
        recovered = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            healthy,
            self.later,
        )
        self.assertEqual(len(recovered), 1)
        self.assertIn("восстановилась", recovered[0])
        self.assertFalse(state["top_peers"][address]["alerted"])

    def test_inactive_top_peer_alerts_but_unknown_peer_below_top_ten_does_not(self):
        top_address = self.snapshot["participants"][1]["address"]
        below_top_address = self.snapshot["participants"][10]["address"]
        eligible = self.eligible - {top_address}
        observations = inspected(
            self.snapshot,
            ["healthy"] * 10 + ["unknown"],
        )
        state = watcher.default_state()

        watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            eligible,
            observations,
            self.now,
        )
        messages = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            eligible,
            observations,
            self.later,
        )

        self.assertEqual(len(messages), 1)
        self.assertIn(top_address, messages[0])
        self.assertNotIn(below_top_address, messages[0])
        self.assertIn("inactive", messages[0])
        self.assertNotIn(below_top_address, state["top_peers"])

    def test_multiple_failed_top_peers_are_aggregated_in_one_message(self):
        observations = inspected(
            self.snapshot,
            ["unknown", "unknown"]
            + ["healthy"] * (len(self.snapshot["participants"]) - 2),
        )
        state = watcher.default_state()
        watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            observations,
            self.now,
        )
        messages = watcher.evaluate_top_peers(
            state,
            config(),
            self.snapshot,
            self.eligible,
            observations,
            self.later,
        )

        self.assertEqual(len(messages), 1)
        self.assertIn(self.snapshot["participants"][0]["address"], messages[0])
        self.assertIn(self.snapshot["participants"][1]["address"], messages[0])

    def test_alert_is_closed_when_peer_leaves_top_ten(self):
        observations = inspected(
            self.snapshot,
            ["unknown"] + ["healthy"] * (len(self.snapshot["participants"]) - 1),
        )
        state = watcher.default_state()
        watcher.evaluate_top_peers(
            state,
            config(top_peer_unavailable_alert_after_runs=1),
            self.snapshot,
            self.eligible,
            observations,
            self.now,
        )

        changed = bls_snapshot([1, 22, 20, 18, 14, 10, 5, 4, 3, 2, 2])
        changed_eligible = {item["address"] for item in changed["participants"]}
        changed_observations = inspected(changed, ["healthy"] * len(changed["participants"]))
        messages = watcher.evaluate_top_peers(
            state,
            config(top_peer_unavailable_alert_after_runs=1),
            changed,
            changed_eligible,
            changed_observations,
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
