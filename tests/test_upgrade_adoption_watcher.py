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

    def test_distribution_list_puts_target_first_and_collapses_post(self):
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
                "3.0.14 / post2 — 11",
                "версия не указана — 16",
            ],
        )

    def test_progress_change_requires_two_matching_checks(self):
        previous = {
            "target_mlnode_version": "3.0.16",
            "mlnode_reported_signature": "10:5:1000",
        }

        first = watcher.evaluate_mlnode_notification(
            previous, "3.0.16", "11:6:1200"
        )
        self.assertEqual(first, (False, "10:5:1000", "11:6:1200", 1))

        previous.update(
            mlnode_candidate_signature=first[2],
            mlnode_candidate_runs=first[3],
        )
        second = watcher.evaluate_mlnode_notification(
            previous, "3.0.16", "11:6:1200"
        )
        self.assertEqual(second, (True, "11:6:1200", None, 0))

    def test_new_target_version_is_reported_immediately(self):
        result = watcher.evaluate_mlnode_notification(
            {"target_mlnode_version": "3.0.15"},
            "3.0.16",
            "11:8:345704",
        )
        self.assertEqual(result, (True, "11:8:345704", None, 0))


class IntegrationTests(unittest.TestCase):
    def test_main_sends_combined_api_and_mlnode_summary(self):
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
                    "ADOPTION_THRESHOLD": "50",
                },
                clear=False,
            ),
            patch.object(watcher, "fetch_active_snapshot", return_value=([entry], 365)),
            patch.object(watcher, "validate_public_url", return_value="https://host.example"),
            patch.object(watcher, "fetch_version_info", return_value=info),
            patch.object(watcher, "load_json", return_value=None),
            patch.object(watcher, "save_json_atomic", side_effect=capture_state),
            patch.object(watcher, "send_telegram_message") as send,
        ):
            watcher.main()

        message = send.call_args.args[0]
        self.assertIn("Прогресс обновлений (эпоха 365)", message)
        self.assertIn("API v0.2.15-post3", message)
        self.assertIn("Цель: 50 веса (50.0%) у хостов с этой версией API", message)
        self.assertIn("Обновлено: 100 / 100 веса (100.0%)", message)
        self.assertIn("✅ Цель по API достигнута!", message)
        self.assertIn("MLNode 3.0.16", message)
        self.assertIn("Ноды с этой версией: 1 из 1 (100.0%)", message)
        self.assertIn("3.0.16 — 1", message)
        self.assertIn("Хосты, у которых всё железо на 3.0.16: 1 из 1", message)
        self.assertIn("Частично обновлённых: 0", message)
        self.assertIn("На старых версиях: 0", message)
        self.assertIn("Нет данных: 0", message)
        self.assertNotIn("Участников без веса", message)
        self.assertEqual(saved["mlnode_fully_updated_weight"], 100)
        self.assertEqual(saved["mlnode_reported_signature"], "1:1:100")

    def test_workflow_passes_target_mlnode_version(self):
        workflow = (
            Path(watcher.ROOT) / ".github/workflows/check-upgrade-adoption.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("TARGET_MLNODE_VERSION", workflow)
        self.assertIn("'3.0.16'", workflow)


if __name__ == "__main__":
    unittest.main()
