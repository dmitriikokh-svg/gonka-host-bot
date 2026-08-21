import tempfile
import unittest
from json import loads
from pathlib import Path
from unittest.mock import Mock, patch

import _bootstrap  # noqa: F401 - installs an optional requests stub
import requests

import model_coefficients_watcher as watcher


def params_payload(models=None):
    if models is None:
        models = [
            {
                "model_id": "Kimi-K2.6",
                "weight_scale_factor": {"value": "3024", "exponent": "-4"},
            },
            {
                "model_id": "MiniMax-M2.7",
                "weight_scale_factor": {"value": "945", "exponent": "-3"},
            },
        ]
    return {"params": {"poc_params": {"models": models}}}


class ParsingTests(unittest.TestCase):
    def test_real_scale_factor_shape_is_converted_exactly(self):
        models = watcher.parse_params_payload(params_payload())
        self.assertEqual(models["Kimi-K2.6"], "0.3024")
        self.assertEqual(models["MiniMax-M2.7"], "0.945")

    def test_numerically_equivalent_values_are_canonical(self):
        self.assertEqual(watcher.canonical_decimal("0.78"), "0.78")
        self.assertEqual(watcher.canonical_decimal("0.780"), "0.78")
        self.assertEqual(watcher.canonical_decimal(0.78), "0.78")
        self.assertEqual(watcher.model_changes({"M": "0.780"}, {"M": "0.78"}), [])

    def test_malformed_payload_is_rejected(self):
        for payload in (
            {},
            {"params": {"poc_params": {"models": []}}},
            params_payload([{"model_id": "M"}]),
            params_payload(
                [
                    {
                        "model_id": "M",
                        "weight_scale_factor": {"value": "NaN", "exponent": 0},
                    }
                ]
            ),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    watcher.parse_params_payload(payload)

    def test_duplicate_model_is_rejected(self):
        model = {
            "model_id": "M",
            "weight_scale_factor": {"value": 78, "exponent": -2},
        }
        with self.assertRaisesRegex(ValueError, "duplicate"):
            watcher.parse_params_payload(params_payload([model, model]))

    def test_params_fetch_falls_back_to_second_source(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = params_payload()
        config = {
            "params_urls": ["https://first.example", "https://second.example"],
            "request_timeout_seconds": 3,
            "attempts_per_source": 1,
        }
        with patch(
            "bot_common.requests.get",
            side_effect=[requests.Timeout("timed out"), response],
        ) as get:
            models, source = watcher.fetch_models(config)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(source, "https://second.example")
        self.assertEqual(models["Kimi-K2.6"], "0.3024")

    def test_malformed_first_source_also_uses_fallback(self):
        malformed = Mock()
        malformed.raise_for_status.return_value = None
        malformed.json.return_value = {"params": {}}
        valid = Mock()
        valid.raise_for_status.return_value = None
        valid.json.return_value = params_payload()
        config = {
            "params_urls": ["https://first.example", "https://second.example"],
            "request_timeout_seconds": 3,
            "attempts_per_source": 1,
        }
        with patch("bot_common.requests.get", side_effect=[malformed, valid]):
            _, source = watcher.fetch_models(config)
        self.assertEqual(source, "https://second.example")


class ChangeDetectionTests(unittest.TestCase):
    def test_detects_change_addition_and_removal(self):
        changes = watcher.model_changes(
            {"Changed": "0.72", "Removed": "0.1", "Same": "0.780"},
            {"Changed": "0.78", "Added": "0.31", "Same": "0.78"},
        )
        self.assertEqual(
            [(item["model_id"], item["type"]) for item in changes],
            [
                ("Added", "added"),
                ("Changed", "changed"),
                ("Removed", "removed"),
            ],
        )

    def test_first_success_is_silent_baseline(self):
        state, messages = watcher.apply_success(
            {},
            {"M": "0.78"},
            epoch=359,
            source="https://params",
            epoch_source="https://group",
            now="2026-08-13T00:00:00+00:00",
        )
        self.assertEqual(messages, [])
        self.assertEqual(state["models"], {"M": "0.78"})

    def test_change_alert_is_sent_only_once_after_state_update(self):
        previous = {"models": {"M": "0.72"}}
        state, messages = watcher.apply_success(
            previous,
            {"M": "0.78"},
            epoch=359,
            source="https://params",
            epoch_source="https://group",
            now="2026-08-13T00:00:00+00:00",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("0.72", messages[0])
        self.assertIn("0.78", messages[0])
        self.assertNotIn("Без изменений", messages[0])

        _, repeated = watcher.apply_success(
            state,
            {"M": "0.780"},
            epoch=359,
            source="https://params",
            epoch_source="https://group",
            now="2026-08-13T01:00:00+00:00",
        )
        self.assertEqual(repeated, [])

    def test_change_alert_lists_unchanged_coefficients(self):
        previous = {"models": {"Kimi": "0.90", "GLM": "2.5935"}}
        _, messages = watcher.apply_success(
            previous,
            {"Kimi": "0.945", "GLM": "2.5935"},
            epoch=367,
            source="https://params",
            epoch_source="https://group",
            now="2026-08-13T00:00:00+00:00",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("0.9", messages[0])
        self.assertIn("0.945", messages[0])
        self.assertIn("Без изменений:", messages[0])
        self.assertIn("GLM — 2.5935", messages[0])

    def test_unavailable_alerts_once_and_preserves_models(self):
        previous = {"models": {"M": "0.78"}}
        first, messages = watcher.apply_unavailable(
            previous,
            requests.Timeout("timed out with full details"),
            alert_after_runs=2,
            now="one",
        )
        self.assertEqual(messages, [])
        second, messages = watcher.apply_unavailable(
            first,
            requests.Timeout("timed out with full details"),
            alert_after_runs=2,
            now="two",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("Не удалось прочитать коэффициенты PoC", messages[0])
        self.assertIn("Проверок подряд", messages[0])
        self.assertIn("timeout", messages[0])
        self.assertNotIn("full details", messages[0])
        self.assertEqual(second["models"], {"M": "0.78"})

        third, messages = watcher.apply_unavailable(
            second,
            requests.Timeout("timed out"),
            alert_after_runs=2,
            now="three",
        )
        self.assertEqual(messages, [])
        self.assertEqual(third["unavailable_runs"], 3)

    def test_success_after_alert_emits_recovery(self):
        previous = {
            "models": {"M": "0.78"},
            "unavailable_alerted": True,
            "unavailable_runs": 3,
        }
        state, messages = watcher.apply_success(
            previous,
            {"M": "0.78"},
            epoch=360,
            source="https://params",
            epoch_source="https://group",
            now="now",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("снова доступны", messages[0])
        self.assertIn("Эпоха: 360", messages[0])
        self.assertIn("M — 0.78", messages[0])
        self.assertEqual(state["unavailable_runs"], 0)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            watcher.save_state({"models": {"M": "0.78"}}, path)
            self.assertEqual(watcher.load_state(path), {"models": {"M": "0.78"}})


class RepositoryWiringTests(unittest.TestCase):
    def test_config_uses_real_params_and_epoch_paths(self):
        root = Path(__file__).resolve().parents[1]
        config = loads(
            (root / "config" / "model_coefficients.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(config["params_urls"]), 3)
        self.assertTrue(all(url.endswith("/params") for url in config["params_urls"]))
        self.assertTrue(
            all(
                url.endswith("/current_epoch_group_data")
                for url in config["epoch_group_data_urls"]
            )
        )

    def test_hourly_workflow_commits_state_and_has_telegram_routing(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (
            root / ".github" / "workflows" / "check-model-coefficients.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "43 * * * *"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn("python model_coefficients_watcher.py", workflow)
        self.assertIn("state/model_coefficients.json", workflow)
        self.assertIn("TELEGRAM_MESSAGE_THREAD_ID", workflow)
        self.assertIn("TELEGRAM_SECONDARY_CHAT_ID", workflow)


if __name__ == "__main__":
    unittest.main()
