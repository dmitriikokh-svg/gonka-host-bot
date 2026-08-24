import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import upgrade_adoption_watcher as watcher


def node(node_id, version):
    return {
        "node_id": node_id,
        "version": version,
        "poc_validation_inference": False,
    }


class VersionParsingTests(unittest.TestCase):
    def test_api_and_mlnode_versions_are_independent(self):
        payload = {
            "api_version": {"version": "v0.2.15-post3"},
            "node_version": {"version": "v0.2.15"},
            "mlnodes": [node("ml-1", "3.0.16"), node("ml-2", "")],
        }

        self.assertEqual(watcher.extract_api_version(payload), "v0.2.15-post3")
        self.assertEqual(
            watcher.extract_mlnodes(payload),
            [
                {
                    "node_id": "ml-1",
                    "version": "3.0.16",
                    "poc_validation_inference": False,
                },
                {
                    "node_id": "ml-2",
                    "version": None,
                    "poc_validation_inference": False,
                },
            ],
        )

    def test_empty_duplicate_or_malformed_mlnodes_are_unknown(self):
        cases = (
            {},
            {"mlnodes": []},
            {"mlnodes": [node("same", "3.0.16"), node("same", "3.0.16")]},
            {"mlnodes": ["invalid"]},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertIsNone(watcher.extract_mlnodes(payload))


class MLNodeSummaryTests(unittest.TestCase):
    def test_nodes_hosts_and_weight_are_counted_separately(self):
        participants = [
            {"id": "full", "weight": 100},
            {"id": "mixed", "weight": 200},
            {"id": "missing", "weight": 300},
            {"id": "down", "weight": 400},
            {"id": "old", "weight": 10},
        ]
        results = {
            "full": {
                "mlnodes": [node("f1", "3.0.16"), node("f2", "v3.0.16")]
            },
            "mixed": {
                "mlnodes": [node("x1", "3.0.16"), node("x2", "3.0.14")]
            },
            "missing": {
                "mlnodes": [node("m1", "3.0.16"), node("m2", None)]
            },
            "down": None,
            "old": {
                "mlnodes": [node("o1", "0.2.0")]
            },
        }

        summary = watcher.summarize_mlnode_adoption(
            participants,
            results,
            "3.0.16",
            network_total_weight=1060,
            network_host_count=6,
            initial_unknown_weight=50,
            initial_unknown_hosts=1,
        )

        self.assertEqual(summary["target_node_count"], 4)
        self.assertEqual(summary["visible_node_count"], 7)
        self.assertEqual(summary["missing_version_node_count"], 1)
        self.assertEqual(summary["fully_updated_host_count"], 1)
        self.assertEqual(summary["fully_updated_weight"], 100)
        self.assertEqual(summary["mixed_host_count"], 2)
        self.assertEqual(summary["other_host_count"], 1)
        self.assertEqual(summary["unknown_host_count"], 2)
        self.assertEqual(summary["unknown_weight"], 450)
        self.assertEqual(summary["unavailable_host_count"], 1)
        self.assertEqual(
            summary["fully_updated_host_count"]
            + summary["mixed_host_count"]
            + summary["other_host_count"]
            + summary["unknown_host_count"],
            6,
        )
        self.assertEqual(summary["version_distribution"]["3.0.16"], 3)
        self.assertEqual(summary["version_distribution"]["v3.0.16"], 1)

    def test_distribution_list_puts_target_first_without_merging_post(self):
        lines = watcher.format_mlnode_distribution_lines(
            {
                "3.0.16": 46,
                "0.2.0": 34,
                "3.0.14": 7,
                "3.0.14-post2": 4,
                "MISSING_VERSION": 16,
            },
            "3.0.16",
        )
        self.assertEqual(
            lines,
            [
                "3.0.16 — 46",
                "0.2.0 — 34",
                "3.0.14 — 7",
                "3.0.14-post2 — 4",
                "версия не указана — 16",
            ],
        )

    def test_distribution_list_keeps_post_target_as_its_own_line(self):
        lines = watcher.format_mlnode_distribution_lines(
            {
                "3.0.16": 46,
                "3.0.14": 7,
                "3.0.14-post2": 4,
            },
            "3.0.14-post2",
        )
        self.assertEqual(
            lines,
            [
                "3.0.14-post2 — 4",
                "3.0.16 — 46",
                "3.0.14 — 7",
            ],
        )

    def test_distribution_list_adds_count_deltas(self):
        lines = watcher.format_mlnode_distribution_lines(
            {
                "3.0.16": 78,
                "0.2.0": 22,
                "3.0.14-post2": 3,
                "3.0.14": 1,
                "MISSING_VERSION": 12,
            },
            "3.0.16",
            previous={
                "3.0.16": 71,
                "0.2.0": 28,
                "3.0.14-post2": 3,
                "3.0.14": 2,
                "MISSING_VERSION": 12,
            },
        )
        self.assertEqual(
            lines,
            [
                "3.0.16 — 78  (было 71, +7)",
                "0.2.0 — 22  (было 28, −6)",
                "3.0.14-post2 — 3",
                "3.0.14 — 1  (было 2, −1)",
                "версия не указана — 12",
            ],
        )

    def test_api_unknown_lines_split_unreachable_from_other_weight(self):
        self.assertEqual(
            watcher.format_api_unknown_lines(
                unreachable=8,
                unreachable_weight=149330,
                other_unknown_weight=31747,
                network_total_weight=572397,
            ),
            [
                "Не достучались до /v1/versions: 8 хостов, их вес 149330 (26.1%)",
                "Неизвестна версия API по иным причинам: 31747 (5.5%)",
            ],
        )
        self.assertEqual(
            watcher.format_api_unknown_lines(
                unreachable=0,
                unreachable_weight=0,
                other_unknown_weight=0,
                network_total_weight=572397,
            ),
            ["Не достучались до /v1/versions: 0 хостов"],
        )

    def test_digest_window_requires_inference_after_claim_money(self):
        stage = {
            "phase": "Inference",
            "is_confirmation_poc_active": False,
            "block_height": 5716216,
            "claim_money": 5706426,
            "next_poc_start": 5721416,
        }
        self.assertTrue(watcher.digest_window(stage))
        too_early = dict(stage, block_height=stage["claim_money"] + 100)
        self.assertFalse(watcher.digest_window(too_early))
        near_poc = dict(stage, block_height=stage["next_poc_start"] - 100)
        self.assertFalse(watcher.digest_window(near_poc))
        self.assertFalse(
            watcher.telegram_allowed(dict(stage, phase="PoC"))
        )
        self.assertFalse(
            watcher.telegram_allowed(
                dict(stage, is_confirmation_poc_active=True)
            )
        )

    def test_mlnode_event_requires_two_matching_host_increases(self):
        previous = {
            "last_notified_mlnode": {
                "target_version": "3.0.16",
                "fully_updated_host_count": 3,
                "target_pct": 61.2,
            }
        }
        snapshot = {
            "target_version": "3.0.16",
            "fully_updated_host_count": 4,
            "target_pct": 61.2,
        }
        first = watcher.evaluate_mlnode_event(previous, snapshot)
        self.assertEqual(first[0], False)
        previous.update(
            mlnode_candidate_signature=first[2],
            mlnode_candidate_runs=first[3],
            last_notified_mlnode=previous["last_notified_mlnode"],
        )
        second = watcher.evaluate_mlnode_event(previous, snapshot)
        self.assertEqual(second[0], True)

    def test_new_target_mlnode_version_is_reported_immediately(self):
        result = watcher.evaluate_mlnode_event(
            {
                "last_notified_mlnode": {
                    "target_version": "3.0.15",
                    "fully_updated_host_count": 3,
                    "target_pct": 50.0,
                }
            },
            {
                "target_version": "3.0.16",
                "fully_updated_host_count": 3,
                "target_pct": 50.0,
            },
        )
        self.assertEqual(result[0], True)

    def test_api_plus_five_points_uses_debounce(self):
        previous = {
            "last_notified_api": {
                "target_version": "v0.2.15-post3",
                "adopted_pct": 65.8,
                "threshold_reached": False,
                "unknown_band": "unreliable",
            }
        }
        snapshot = {
            "target_version": "v0.2.15-post3",
            "adopted_pct": 71.0,
            "threshold_reached": False,
            "unknown_band": "unreliable",
        }
        first = watcher.evaluate_api_event(previous, snapshot)
        self.assertFalse(first[0])
        previous["api_candidate_signature"] = first[2]
        previous["api_candidate_runs"] = first[3]
        second = watcher.evaluate_api_event(previous, snapshot)
        self.assertTrue(second[0])


def digest_stage(epoch=365):
    return {
        "phase": "Inference",
        "is_confirmation_poc_active": False,
        "block_height": 5720000,
        "claim_money": 5717000,
        "next_poc_start": 5723000,
        "epoch_index": epoch,
    }


class IntegrationTests(unittest.TestCase):
    def test_main_sends_digest_in_inference_window(self):
        entry = {
            "index": "gonka1host",
            "weight": "100",
            "inference_url": "https://host.example",
        }
        info = {
            "api_version": "v0.2.15-post3",
            "mlnodes": [node("ml-1", "3.0.16")],
        }
        saved = {}

        def capture_state(_path, state):
            saved.update(state)

        with (
            patch.dict(
                "os.environ",
                {
                    "TARGET_API_VERSION": "v0.2.15-post3",
                    "TARGET_MLNODE_VERSION": "3.0.16",
                },
                clear=False,
            ),
            patch.object(watcher, "fetch_active_snapshot", return_value=([entry], 365)),
            patch.object(watcher, "fetch_epoch_stage", return_value=digest_stage(365)),
            patch.object(watcher, "validate_public_url", return_value="https://host.example"),
            patch.object(watcher, "fetch_version_info", return_value=info),
            patch.object(watcher, "load_json", return_value=None),
            patch.object(watcher, "save_json_atomic", side_effect=capture_state),
            patch.object(watcher, "send_telegram_message") as send,
        ):
            watcher.main()

        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[0]
        self.assertIn("📊 Версии API и MLNode, эпоха 365", message)
        self.assertIn("API v0.2.15-post3", message)
        self.assertIn("Цель: 80% сети (80)", message)
        self.assertIn("Обновлено: 100 / 100 веса (100.0%)", message)
        self.assertIn("✅ Цель 80% достигнута", message)
        self.assertIn("Не достучались до /v1/versions: 0 хостов", message)
        self.assertNotIn("их вес", message)
        self.assertNotIn("Неизвестна версия API по иным причинам", message)
        self.assertIn("MLNode 3.0.16", message)
        self.assertIn("Ноды с этой версией: 1 из 1 (100.0%)", message)
        self.assertIn("3.0.16 — 1", message)
        self.assertIn("Хосты полностью на 3.0.16: 1 из 1", message)
        self.assertIn("Частично обновлённых: 0", message)
        self.assertIn("На старых версиях: 0", message)
        self.assertIn("Нет данных: 0", message)
        self.assertNotIn("Участников без веса", message)
        self.assertEqual(saved["mlnode_fully_updated_weight"], 100)
        self.assertEqual(saved["last_digest_epoch"], 365)
        self.assertEqual(saved["unreachable_weight"], 0)
        self.assertEqual(saved["missing_api_version_weight"], 0)
        self.assertEqual(saved["unqueryable_weight"], 0)

    def test_main_splits_api_unknown_weight_reasons(self):
        entries = [
            {
                "index": "gonka1ok",
                "weight": "100",
                "inference_url": "https://ok.example",
            },
            {
                "index": "gonka1down",
                "weight": "9857",
                "inference_url": "https://down.example",
            },
            {
                "index": "gonka1empty",
                "weight": "5000",
                "inference_url": "https://empty.example",
            },
            {
                "index": "gonka1nourl",
                "weight": "8924",
            },
        ]

        def fake_info(url, retries=2, timeout=5, delay=0):
            mapping = {
                "https://ok.example": {
                    "api_version": "v0.2.15-post3",
                    "mlnodes": [node("ml-1", "3.0.16")],
                },
                "https://down.example": None,
                "https://empty.example": {
                    "api_version": None,
                    "mlnodes": [node("ml-1", "3.0.16")],
                },
            }
            return mapping[url]

        saved = {}

        with (
            patch.dict(
                "os.environ",
                {
                    "TARGET_API_VERSION": "v0.2.15-post3",
                    "TARGET_MLNODE_VERSION": "3.0.16",
                },
                clear=False,
            ),
            patch.object(watcher, "fetch_active_snapshot", return_value=(entries, 365)),
            patch.object(watcher, "fetch_epoch_stage", return_value=digest_stage(365)),
            patch.object(watcher, "validate_public_url", side_effect=lambda url: url),
            patch.object(watcher, "fetch_version_info", side_effect=fake_info),
            patch.object(watcher, "load_json", return_value=None),
            patch.object(watcher, "save_json_atomic", side_effect=lambda _path, state: saved.update(state)),
            patch.object(watcher, "send_telegram_message") as send,
        ):
            watcher.main()

        message = send.call_args.args[0]
        self.assertIn(
            "Не достучались до /v1/versions: 1 хост, их вес 9857 (41.3%)",
            message,
        )
        self.assertIn(
            "Неизвестна версия API по иным причинам: 13924 (58.3%)",
            message,
        )
        self.assertEqual(saved["unreachable_count"], 1)
        self.assertEqual(saved["unreachable_weight"], 9857)
        self.assertEqual(saved["missing_api_version_weight"], 5000)
        self.assertEqual(saved["unqueryable_weight"], 8924)
        self.assertEqual(saved["unknown_weight"], 23781)

    def test_main_stays_quiet_during_poc(self):
        entry = {
            "index": "gonka1host",
            "weight": "100",
            "inference_url": "https://host.example",
        }
        with (
            patch.dict(
                "os.environ",
                {
                    "TARGET_API_VERSION": "v0.2.15-post3",
                    "TARGET_MLNODE_VERSION": "3.0.16",
                },
                clear=False,
            ),
            patch.object(watcher, "fetch_active_snapshot", return_value=([entry], 365)),
            patch.object(
                watcher,
                "fetch_epoch_stage",
                return_value={
                    "phase": "PoC",
                    "is_confirmation_poc_active": False,
                    "block_height": 5720000,
                    "claim_money": 5717000,
                    "next_poc_start": 5723000,
                    "epoch_index": 365,
                },
            ),
            patch.object(watcher, "validate_public_url", return_value="https://host.example"),
            patch.object(
                watcher,
                "fetch_version_info",
                return_value={
                    "api_version": "v0.2.15-post3",
                    "mlnodes": [node("ml-1", "3.0.16")],
                },
            ),
            patch.object(watcher, "load_json", return_value=None),
            patch.object(watcher, "save_json_atomic"),
            patch.object(watcher, "send_telegram_message") as send,
        ):
            watcher.main()
        send.assert_not_called()

    def test_mlnode_event_message_shows_inline_deltas(self):
        current = {
            "target_version": "3.0.16",
            "target_node_count": 78,
            "visible_node_count": 116,
            "target_pct": 67.24137931034483,
            "fully_updated_host_count": 4,
            "mixed_host_count": 5,
            "other_host_count": 6,
            "unknown_host_count": 14,
            "network_host_count": 29,
            "version_distribution": {
                "3.0.16": 78,
                "0.2.0": 22,
                "3.0.14-post2": 3,
                "3.0.14": 1,
                "MISSING_VERSION": 12,
            },
        }
        previous = {
            "target_version": "3.0.16",
            "target_node_count": 71,
            "visible_node_count": 116,
            "target_pct": 61.206896551724135,
            "fully_updated_host_count": 3,
            "mixed_host_count": 6,
            "other_host_count": 6,
            "unknown_host_count": 14,
            "network_host_count": 29,
            "version_distribution": {
                "3.0.16": 71,
                "0.2.0": 28,
                "3.0.14-post2": 3,
                "3.0.14": 2,
                "MISSING_VERSION": 12,
            },
        }
        message = watcher.format_mlnode_event_message(370, current, previous)
        self.assertIn("📊 MLNode, эпоха 370", message)
        self.assertIn("Ноды с этой версией: 78 из 116 (67.2%)  (+6.0 п.п.)", message)
        self.assertIn("3.0.16 — 78  (было 71, +7)", message)
        self.assertIn("0.2.0 — 22  (было 28, −6)", message)
        self.assertIn("3.0.14-post2 — 3", message)
        self.assertIn("3.0.14 — 1  (было 2, −1)", message)
        self.assertIn("Хосты полностью на 3.0.16: 4 из 29  (было 3, +1)", message)
        self.assertIn("Частично обновлённых: 5  (было 6, −1)", message)
        self.assertIn("На старых версиях: 6", message)
        self.assertNotIn("Изменение:", message)

    def test_workflow_passes_target_mlnode_version(self):
        workflow = (
            Path(watcher.ROOT) / ".github/workflows/check-upgrade-adoption.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("TARGET_MLNODE_VERSION", workflow)
        self.assertIn("'3.0.16'", workflow)
        self.assertIn("ADOPTION_PERCENT", workflow)


if __name__ == "__main__":
    unittest.main()
