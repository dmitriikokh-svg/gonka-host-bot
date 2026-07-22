import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import escrow_balance_watcher as watcher


def config(**overrides):
    value = {
        "owner": "Dmitrii Kokh",
        "denom": "ngonka",
        "decimals": 9,
        "low_balance_below_gnk": 100,
        "recovery_at_or_above_gnk": 100,
        "reminder_interval_hours": 24,
        "unavailable_alert_after_runs": 3,
    }
    value.update(overrides)
    return value


ACCOUNT = {"name": "gw2", "address": "gonka1test"}


class BalanceParsingTests(unittest.TestCase):
    def test_parses_ngonka_and_converts_to_gnk_exactly(self):
        amount = watcher.parse_balance(
            {
                "balances": [
                    {"denom": "other", "amount": "8"},
                    {"denom": "ngonka", "amount": "123456789012"},
                ],
                "pagination": {"next_key": None, "total": "2"},
            },
            "ngonka",
        )
        self.assertEqual(amount, 123456789012)
        self.assertEqual(watcher.to_gnk(amount, 9), Decimal("123.456789012"))

    def test_valid_empty_balances_means_zero(self):
        self.assertEqual(watcher.parse_balance({"balances": []}, "ngonka"), 0)

    def test_missing_balances_is_not_treated_as_zero(self):
        with self.assertRaises(ValueError):
            watcher.parse_balance({"pagination": {}}, "ngonka")

    def test_invalid_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            watcher.parse_balance(
                {"balances": [{"denom": "ngonka", "amount": "not-a-number"}]},
                "ngonka",
            )


class StateTransitionTests(unittest.TestCase):
    def success(self, previous, amount_gnk, now):
        return watcher.apply_success(
            previous,
            account=ACCOUNT,
            config=config(),
            amount=int(Decimal(str(amount_gnk)) * Decimal(10**9)),
            source_url="https://node3/source",
            now=now,
        )

    def test_low_balance_alerts_once_then_reminds_after_24_hours(self):
        first, messages = self.success({}, 99, "2026-07-22T00:00:00+00:00")
        self.assertEqual(first["status"], "low")
        self.assertEqual(len(messages), 1)
        self.assertIn("Низкий баланс", messages[0])

        second, messages = self.success(first, 98, "2026-07-22T23:59:59+00:00")
        self.assertEqual(messages, [])

        third, messages = self.success(second, 97, "2026-07-23T00:00:00+00:00")
        self.assertEqual(len(messages), 1)
        self.assertIn("Напоминание", messages[0])
        self.assertEqual(third["low_since"], "2026-07-22T00:00:00+00:00")

    def test_exactly_100_is_not_low_and_recovers(self):
        low, _ = self.success({}, 99, "2026-07-22T00:00:00+00:00")
        recovered, messages = self.success(low, 100, "2026-07-22T01:00:00+00:00")
        self.assertEqual(recovered["status"], "ok")
        self.assertFalse(recovered["low_alerted"])
        self.assertEqual(len(messages), 1)
        self.assertIn("восстановлен", messages[0])

    def test_unavailable_preserves_last_balance_and_alerts_on_third_run(self):
        previous, _ = self.success({}, 150, "2026-07-22T00:00:00+00:00")
        for run in range(1, 4):
            previous, messages = watcher.apply_failure(
                previous,
                account=ACCOUNT,
                config=config(),
                error=RuntimeError("API down"),
                now=f"2026-07-22T0{run}:00:00+00:00",
            )
            self.assertEqual(previous["balance_gnk"], "150")
            self.assertEqual(len(messages), 1 if run == 3 else 0)
        self.assertEqual(previous["status"], "unavailable")
        self.assertTrue(previous["unavailable_alerted"])

    def test_api_recovery_is_reported(self):
        previous = {
            "unavailable_alerted": True,
            "unavailable_runs": 3,
            "status": "unavailable",
        }
        recovered, messages = self.success(previous, 150, "2026-07-22T04:00:00+00:00")
        self.assertEqual(recovered["status"], "ok")
        self.assertFalse(recovered["unavailable_alerted"])
        self.assertEqual(len(messages), 1)
        self.assertIn("снова доступен", messages[0])


class PersistenceTests(unittest.TestCase):
    def test_missing_state_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with patch.object(watcher, "STATE_FILE", state_file):
                self.assertEqual(watcher.load_state(), {"accounts": {}})

    def test_run_persists_both_accounts_and_sends_only_low_alert(self):
        full_config = config(
            balance_url_templates=["https://node/{address}"],
            request_timeout_seconds=10,
            attempts_per_source=1,
            accounts=[
                ACCOUNT,
                {"name": "node4", "address": "gonka1node4"},
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with (
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "load_config", return_value=full_config),
                patch.object(
                    watcher,
                    "fetch_account_balance",
                    side_effect=[
                        (99 * 10**9, "https://node/gw2"),
                        (150 * 10**9, "https://node/node4"),
                    ],
                ),
                patch.object(watcher, "send_telegram_message") as send,
                patch.dict(os.environ, {}, clear=True),
            ):
                state = watcher.run()

        self.assertEqual(state["accounts"]["gw2"]["status"], "low")
        self.assertEqual(state["accounts"]["node4"]["status"], "ok")
        self.assertEqual(send.call_count, 1)
        self.assertIn("Низкий баланс", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
