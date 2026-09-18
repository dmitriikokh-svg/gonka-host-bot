import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import devshard_watcher as watcher


def healthz(*names):
    return [{"name": name, "port": 5000, "status": "running"} for name in names]


def census(**overrides):
    base = {
        "approved": ["v3", "v4"],
        "network_hosts": 31,
        "answered": 25,
        "unreachable": 6,
        "slot_hosts": {"v3": 25, "v4": 25},
        "complete_hosts": 25,
        "incomplete_hosts": 0,
        "extra_slots": {},
    }
    base.update(overrides)
    return base


class ParsingTests(unittest.TestCase):
    def test_approved_versions_use_slot_names(self):
        names = watcher.parse_approved_versions(
            {
                "params": {
                    "devshard_escrow_params": {
                        "approved_versions": [
                            {"name": "v3", "sha256": "aaa"},
                            {"name": "v4", "binary": "https://example"},
                        ]
                    }
                }
            }
        )
        self.assertEqual(names, ["v3", "v4"])

    def test_empty_or_duplicate_approved_is_rejected(self):
        with self.assertRaises(ValueError):
            watcher.parse_approved_versions({"params": {"devshard_escrow_params": {}}})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            watcher.parse_approved_versions(
                {
                    "params": {
                        "devshard_escrow_params": {
                            "approved_versions": [{"name": "v3"}, {"name": "v3"}]
                        }
                    }
                }
            )

    def test_healthz_keeps_running_slots_and_skips_dead(self):
        slots = watcher.parse_healthz(
            [
                {"name": "v4", "status": "running"},
                {"name": "v3", "status": "running"},
                {"name": "v1", "status": "exited"},
                {"name": "v4", "status": "running"},
            ]
        )
        self.assertEqual(slots, ["v4", "v3"])

    def test_healthz_rejects_object_payload(self):
        with self.assertRaises(ValueError):
            watcher.parse_healthz({"name": "v4"})


class CensusTests(unittest.TestCase):
    def test_counts_approved_extra_and_complete_set(self):
        probes = [
            {"id": "a", "status": "ok", "slots": ["v3", "v4"]},
            {"id": "b", "status": "ok", "slots": ["v3", "v4", "v1"]},
            {"id": "c", "status": "ok", "slots": ["v4"]},
            {"id": "d", "status": "unreachable", "slots": []},
        ]
        result = watcher.build_census(["v3", "v4"], probes, network_hosts=4)
        self.assertEqual(result["answered"], 3)
        self.assertEqual(result["unreachable"], 1)
        self.assertEqual(result["slot_hosts"]["v4"], 3)
        self.assertEqual(result["slot_hosts"]["v3"], 2)
        self.assertEqual(result["extra_slots"], {"v1": 1})
        self.assertEqual(result["complete_hosts"], 2)
        self.assertEqual(result["incomplete_hosts"], 1)
        self.assertEqual(result["approved_combos"], {"v3+v4": 2, "v4": 1})


class FormatTests(unittest.TestCase):
    def test_host_word_after_u_and_iz(self):
        self.assertEqual(watcher.host_word_genitive(1), "хоста")
        self.assertEqual(watcher.host_word_genitive(2), "хостов")
        self.assertEqual(watcher.host_word_genitive(11), "хостов")
        self.assertEqual(watcher.host_word_genitive(21), "хоста")
        self.assertEqual(watcher.host_word_genitive(31), "хоста")

    def test_digest_uses_sets_and_genitive(self):
        text = watcher.format_digest_message(
            374,
            census(
                answered=28,
                unreachable=3,
                slot_hosts={"v3": 28, "v4": 28, "v1": 1},
                complete_hosts=28,
                extra_slots={"v1": 1},
            ),
        )
        self.assertEqual(
            text,
            "\n".join(
                [
                    "📊 Devshard, эпоха 374",
                    "",
                    "Разрешены слоты v3, v4",
                    "Ответили: 28 из 31 хоста",
                    "",
                    "у ответивших:",
                    "• и v3, и v4 — 28",
                    "лишние слоты (поверх approved): v1 у 1 хоста",
                    "Не ответили: 3",
                ]
            ),
        )

    def test_two_hosts_use_hostov(self):
        text = watcher.format_census_body(
            census(extra_slots={"v1": 2}, slot_hosts={"v3": 25, "v4": 25, "v1": 2})
        )
        self.assertIn("v1 у 2 хостов", text)


class TickTests(unittest.TestCase):
    def test_first_successful_run_sends_digest(self):
        state, messages = watcher.apply_tick({}, census(), epoch=374, now="t0")
        self.assertEqual(len(messages), 1)
        self.assertIn("📊 Devshard, эпоха 374", messages[0])
        self.assertIn("и v3, и v4 — 25", messages[0])
        self.assertNotIn("v3 — 25 хостов", messages[0])
        self.assertEqual(state["last_digest_epoch"], 374)

    def test_same_epoch_without_shift_is_silent(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        _state, messages = watcher.apply_tick(previous, census(), epoch=374, now="t1")
        self.assertEqual(messages, [])

    def test_new_epoch_sends_digest_again(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        _state, messages = watcher.apply_tick(
            previous, census(answered=24, unreachable=7), epoch=375, now="t1"
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("эпоха 375", messages[0])

    def test_approved_change_is_immediate(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        _state, messages = watcher.apply_tick(
            previous,
            census(approved=["v4"], slot_hosts={"v4": 25}, complete_hosts=25),
            epoch=374,
            now="t1",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("approved: v3, v4 → v4", messages[0])

    def test_small_coverage_change_is_ignored(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        shifted = census(
            slot_hosts={"v3": 25, "v4": 23},
            complete_hosts=23,
            incomplete_hosts=2,
        )
        _state, messages = watcher.apply_tick(previous, shifted, epoch=374, now="t1")
        self.assertEqual(messages, [])

    def test_three_host_drop_alerts_on_second_identical_run(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        dropped = census(
            slot_hosts={"v3": 25, "v4": 22},
            complete_hosts=22,
            incomplete_hosts=3,
        )
        mid, first = watcher.apply_tick(previous, dropped, epoch=374, now="t1")
        self.assertEqual(first, [])
        self.assertEqual(mid["event_candidate_runs"], 1)
        _state, second = watcher.apply_tick(mid, dropped, epoch=374, now="t2")
        self.assertEqual(len(second), 1)
        self.assertIn("v4: 25 → 22 хостов", second[0])

    def test_extra_slot_debounces(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        extra = census(
            slot_hosts={"v3": 25, "v4": 25, "v1": 1},
            extra_slots={"v1": 1},
        )
        mid, first = watcher.apply_tick(previous, extra, epoch=374, now="t1")
        self.assertEqual(first, [])
        _state, second = watcher.apply_tick(mid, extra, epoch=374, now="t2")
        self.assertEqual(len(second), 1)
        self.assertIn("лишние слоты (поверх approved): v1 у 1 хоста", second[0])


class CommandTests(unittest.TestCase):
    def test_command_reads_snapshot_age(self):
        text = watcher.format_command_devshard_message(
            {
                **census(),
                "epoch": 374,
                "checked_at": "2026-08-27T10:19:00+00:00",
            },
            now=datetime(2026, 8, 27, 11, 19, tzinfo=timezone.utc),
        )
        self.assertIn("📊 Devshard, эпоха 374", text)
        self.assertIn("и v3, и v4 — 25", text)
        self.assertIn("1 ч назад", text)

    def test_missing_snapshot(self):
        self.assertIn("ещё нет", watcher.format_command_devshard_message({}))


class ProbeTests(unittest.TestCase):
    def test_collect_probes_maps_host_results(self):
        hosts = [
            {"id": "a", "weight": 1, "url": "https://a.example"},
            {"id": "b", "weight": 2, "url": "https://b.example"},
        ]

        def fake_probe(url, session=None):
            if "a.example" in url:
                return {"status": "ok", "slots": ["v3", "v4"]}
            return {"status": "unreachable", "slots": []}

        with patch.object(watcher, "probe_devshard", side_effect=fake_probe):
            probes = watcher.collect_probes(hosts)
        self.assertEqual(probes[0]["slots"], ["v3", "v4"])
        self.assertEqual(probes[1]["status"], "unreachable")


if __name__ == "__main__":
    unittest.main()
