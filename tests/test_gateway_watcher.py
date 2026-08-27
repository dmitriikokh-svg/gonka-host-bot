import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import _bootstrap  # noqa: F401 - installs an optional requests stub

import gateway_watcher as watcher


def census(**overrides):
    base = {
        "approved": ["v3", "v4"],
        "allowlist": ["gonka1dahl", "gonka1gw2"],
        "allowlist_total": 2,
        "allowlist_seen": 2,
        "brokers": [
            {
                "id": "gonka1dahl",
                "label": "Dahl",
                "slots": ["v3"],
                "on_allowlist": True,
            },
            {
                "id": "gonka1gw2",
                "label": "gw2",
                "slots": ["v4"],
                "on_allowlist": True,
            },
        ],
        "approved_combos": {"v3": 1, "v4": 1},
        "newest_slot": "v4",
        "on_newest": ["gw2"],
        "network_hosts": 31,
        "answered": 28,
        "unreachable": 3,
        "lookup_failed": 0,
    }
    base.update(overrides)
    return base


class ParsingTests(unittest.TestCase):
    def test_allowlist_and_shards(self):
        names = watcher.parse_allowlist(
            {
                "params": {
                    "devshard_escrow_params": {
                        "allowed_creator_addresses": ["gonka1aaa", "gonka1bbb"]
                    }
                }
            }
        )
        self.assertEqual(names, ["gonka1aaa", "gonka1bbb"])
        self.assertEqual(
            watcher.parse_active_escrows({"active_escrows": [1, "2", "2", None]}),
            ["1", "2"],
        )

    def test_escrow_creator_skips_pruned(self):
        self.assertEqual(
            watcher.parse_escrow_creator({"escrow": {"creator": "gonka1x"}, "found": True}),
            "gonka1x",
        )
        self.assertIsNone(watcher.parse_escrow_creator({"escrow": None, "found": False}))


class CensusTests(unittest.TestCase):
    def test_groups_creators_by_bind_slot(self):
        result = watcher.build_census(
            approved=["v3", "v4"],
            allowlist=["gonka1dahl", "gonka1gw2", "gonka1idle"],
            by_slot={"v3": {"10", "11"}, "v4": {"20"}},
            creators={"10": "gonka1dahl", "11": "gonka1dahl", "20": "gonka1gw2"},
            labels={"gonka1dahl": "Dahl", "gonka1gw2": "gw2"},
            network_hosts=4,
            answered=3,
            lookup_failed=1,
        )
        self.assertEqual(result["allowlist_seen"], 2)
        self.assertEqual(result["approved_combos"], {"v3": 1, "v4": 1})
        self.assertEqual(result["on_newest"], ["gw2"])
        self.assertEqual(result["unreachable"], 1)

    def test_on_newest_dedupes_shared_label(self):
        result = watcher.build_census(
            approved=["v3", "v4"],
            allowlist=["gonka1a", "gonka1b"],
            by_slot={"v3": {"1"}, "v4": {"2", "3"}},
            creators={"1": "gonka1a", "2": "gonka1b", "3": "gonka1c"},
            labels={"gonka1b": "Node4", "gonka1c": "Node4"},
            network_hosts=2,
            answered=2,
            lookup_failed=0,
        )
        self.assertEqual(result["on_newest"], ["Node4"])

    def test_on_newest_puts_names_before_short_addresses(self):
        result = watcher.build_census(
            approved=["v3", "v4"],
            allowlist=["gonka1a", "gonka1b"],
            by_slot={"v4": {"1", "2"}},
            creators={
                "1": "gonka10fynmy2npvdvew0vj2288gz8ljfvmjs35lat8n",
                "2": "gonka1w66aw6jayepglwgz66qtunetr5nyw9ls7evq5g",
            },
            labels={"gonka1w66aw6jayepglwgz66qtunetr5nyw9ls7evq5g": "Node4"},
            network_hosts=2,
            answered=2,
            lookup_failed=0,
        )
        self.assertEqual(result["on_newest"], ["Node4", "gonka10f…5lat8n"])


class FormatTests(unittest.TestCase):
    def test_digest_lists_newest_slot_without_escrow_ids(self):
        text = watcher.format_digest_message(374, census())
        self.assertIn("📊 Гейтвеи, эпоха 374", text)
        self.assertIn("Версии протокола: v3, v4", text)
        self.assertIn("Могут создавать девшарды: 2 ключей, живые сессии: 2", text)
        self.assertIn("только v3 — 1", text)
        self.assertIn("на v4:", text)
        self.assertIn("• gw2", text)
        self.assertNotIn("Хосты stats", text)
        self.assertNotIn("бинаря", text)
        self.assertNotIn("bind", text)
        self.assertNotIn("dev-log", text)
        self.assertNotIn("0.2.15", text)
        self.assertNotIn("63876", text)

    def test_heartbeat_and_leaderboard_labels(self):
        heartbeat = {
            "providers": [
                {
                    "name": "Node4",
                    "wallets": [
                        {"address": "gonka1sy7ug80wrnm6gk47creak0j5eagjpf7maqcqwk"},
                        {"address": "gonka1w66aw6jayepglwgz66qtunetr5nyw9ls7evq5g"},
                    ],
                }
            ]
        }
        board = {
            "leaderboard": [
                {
                    "name": "Gonka Labs",
                    "addresses": ["gonka1r2s0rwgskp6y4ed7qr7d25qdwjwlvpp6demv90"],
                },
                {
                    "name": "gonka10fynmy2npvdvew0vj2288gz8ljfvmjs35lat8n",
                    "addresses": ["gonka10fynmy2npvdvew0vj2288gz8ljfvmjs35lat8n"],
                },
            ]
        }
        labels = {}
        labels.update(watcher.parse_leaderboard_labels(board))
        labels.update(watcher.parse_heartbeat_labels(heartbeat))
        self.assertEqual(
            labels["gonka1w66aw6jayepglwgz66qtunetr5nyw9ls7evq5g"],
            "Node4",
        )
        self.assertEqual(
            labels["gonka1r2s0rwgskp6y4ed7qr7d25qdwjwlvpp6demv90"],
            "Gonka Labs",
        )
        self.assertNotIn("gonka10fynmy2npvdvew0vj2288gz8ljfvmjs35lat8n", labels)
        self.assertEqual(
            watcher.broker_label(
                "gonka10fynmy2npvdvew0vj2288gz8ljfvmjs35lat8n",
                labels,
            ),
            "gonka10f…5lat8n",
        )
        board_first = watcher.parse_leaderboard_labels(
            {
                "leaderboard": [
                    {
                        "name": "node4.gonka.ai",
                        "addresses": ["gonka1w66aw6jayepglwgz66qtunetr5nyw9ls7evq5g"],
                    }
                ]
            }
        )
        board_first.update(watcher.parse_heartbeat_labels(heartbeat))
        self.assertEqual(
            board_first["gonka1w66aw6jayepglwgz66qtunetr5nyw9ls7evq5g"],
            "Node4",
        )

    def test_command_reads_snapshot_age(self):
        text = watcher.format_command_gateway_message(
            {
                **census(),
                "epoch": 374,
                "checked_at": "2026-08-27T10:47:00+00:00",
            },
            now=datetime(2026, 8, 27, 11, 47, tzinfo=timezone.utc),
        )
        self.assertIn("📊 Гейтвеи, эпоха 374", text)
        self.assertIn("1 ч назад", text)


class TickTests(unittest.TestCase):
    def test_first_run_sends_digest(self):
        state, messages = watcher.apply_tick({}, census(), epoch=374, now="t0")
        self.assertEqual(len(messages), 1)
        self.assertIn("📊 Гейтвеи, эпоха 374", messages[0])
        self.assertEqual(state["last_digest_epoch"], 374)

    def test_same_epoch_without_shift_is_silent(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        _state, messages = watcher.apply_tick(previous, census(), epoch=374, now="t1")
        self.assertEqual(messages, [])

    def test_broker_version_change_alerts_on_second_run(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        moved = census(
            brokers=[
                {
                    "id": "gonka1dahl",
                    "label": "Dahl",
                    "slots": ["v3"],
                    "on_allowlist": True,
                },
                {
                    "id": "gonka1gw2",
                    "label": "gw2",
                    "slots": ["v3", "v4"],
                    "on_allowlist": True,
                },
            ],
            approved_combos={"v3": 1, "v3+v4": 1},
            on_newest=["gw2"],
        )
        mid, first = watcher.apply_tick(previous, moved, epoch=374, now="t1")
        self.assertEqual(first, [])
        _state, second = watcher.apply_tick(mid, moved, epoch=374, now="t2")
        self.assertEqual(len(second), 1)
        self.assertIn("gw2: только v4 → и v3, и v4", second[0])

    def test_approved_change_is_immediate(self):
        previous, _ = watcher.apply_tick({}, census(), epoch=374, now="t0")
        _state, messages = watcher.apply_tick(
            previous,
            census(approved=["v4"], newest_slot="v4"),
            epoch=374,
            now="t1",
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("approved: v3, v4 → v4", messages[0])


class LookupTests(unittest.TestCase):
    def test_resolve_creators_uses_cache_and_skips_pruned(self):
        session = type("S", (), {})()

        def fake_lookup(escrow_id, session=None):
            if escrow_id == "new":
                return escrow_id, "gonka1dahl", "found"
            if escrow_id == "dead":
                return escrow_id, None, "pruned"
            return escrow_id, None, "failed"

        with patch.object(watcher, "lookup_creator", side_effect=fake_lookup):
            cache, stats = watcher.resolve_creators(
                {"v3": {"cached", "new", "dead"}},
                {"cached": "gonka1old"},
            )
        self.assertEqual(cache["cached"], "gonka1old")
        self.assertEqual(cache["new"], "gonka1dahl")
        self.assertEqual(cache["dead"], "")
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["found"], 1)
        self.assertEqual(stats["pruned"], 1)


if __name__ == "__main__":
    unittest.main()
