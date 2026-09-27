from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tavern.config import TavernConfig
from tavern.constants import (
    DATABASE_SCHEMA_VERSION,
    DEFAULT_WORLD_SLUG,
    SESSION_FINISHED,
    SESSION_PREPARING,
)
from tavern.database import InvalidTransitionError, TavernDatabase
from tavern.lifecycle import normalize_time_rules
from tavern.resolution import CheckRequest, roll_check


class V05RulesTests(unittest.TestCase):
    def test_advantage_disadvantage_and_five_outcomes(self) -> None:
        with patch("tavern.resolution.secrets.randbelow", side_effect=[3, 14]):
            result = roll_check(
                CheckRequest(
                    stat="敏捷",
                    reason="穿过警戒线",
                    difficulty=12,
                    modifier=1,
                    advantage_sources=("提前侦察",),
                )
            )
        self.assertEqual(result.rolls, (4, 15))
        self.assertEqual(result.kept, 15)
        self.assertEqual(result.dice_mode, "advantage")
        self.assertEqual(result.outcome, "success")

        with patch("tavern.resolution.secrets.randbelow", return_value=8):
            cancelled = roll_check(
                CheckRequest(
                    stat="敏捷",
                    reason="雨夜攀爬",
                    difficulty=12,
                    modifier=0,
                    advantage_sources=("合适工具",),
                    disadvantage_sources=("暴雨",),
                )
            )
        self.assertEqual(cancelled.rolls, (9,))
        self.assertTrue(cancelled.advantages_cancelled)
        self.assertEqual(cancelled.dice_mode, "standard")
        self.assertEqual(cancelled.outcome, "failure")

    def test_native_provider_slots_and_unlimited_time(self) -> None:
        config = TavernConfig.from_mapping(
            {
                "model": {
                    "provider_id": "primary",
                    "fallback_provider_1_id": "backup-a",
                    "fallback_provider_2_id": "backup-b",
                    "fallback_provider_ids": ["backup-b", "backup-c"],
                },
                "runtime": {
                    "time_rules": {
                        "turn_timeout_seconds": -1,
                        "max_consecutive_timeouts": -1,
                    }
                },
            }
        )
        self.assertEqual(
            config.fallback_provider_ids,
            ("backup-a", "backup-b", "backup-c"),
        )
        mapped = config.to_mapping()["model"]
        self.assertEqual(mapped["fallback_provider_1_id"], "backup-a")
        self.assertEqual(mapped["fallback_provider_3_id"], "backup-c")
        self.assertIsNone(config.time_rules["turn_timeout_seconds"])
        self.assertEqual(config.time_rules["max_consecutive_timeouts"], -1)
        with self.assertRaisesRegex(ValueError, "0"):
            normalize_time_rules({"turn_timeout_seconds": 0})


class V05PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp_dir.name))
        self.session = await self.database.ensure_session(
            "qq",
            "v05-group",
            "qq:v05-group",
            DEFAULT_WORLD_SLUG,
            "admin",
            "v05-main",
            "0.5 测试副本",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_schema_four_contains_multiplayer_immersion_tables(self) -> None:
        with closing(sqlite3.connect(self.database.path)) as connection:
            version = int(
                connection.execute(
                    "SELECT value FROM tavern_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(version, DATABASE_SCHEMA_VERSION)
        self.assertTrue(
            {
                "session_archives",
                "session_characters",
                "session_rule_states",
                "story_ledger",
                "scene_clocks",
                "memory_governance",
                "assist_tokens",
                "provider_health",
                "operation_receipts",
            }.issubset(tables)
        )

    async def test_npc_memory_progress_and_check_receipt_persist(self) -> None:
        npc = await self.database.save_session_character(
            {
                "session_id": self.session["id"],
                "name": "灰斗篷商人",
                "aliases": ["灰商"],
                "role_type": "商人",
                "public_profile": {"appearance": "总戴着灰色兜帽"},
                "known_facts": ["知道北门守卫的换班时间"],
                "misconceptions": ["误以为旅客来自王都"],
                "state": {"location": "壁炉旁", "faction": "中立"},
                "review_status": "pending",
            },
            "admin",
        )
        self.assertEqual(npc["source"], "admin")
        self.assertEqual(npc["state"]["location"], "壁炉旁")

        memory = await self.database.save_memory(
            {
                "session_id": self.session["id"],
                "scope": "world",
                "kind": "fact",
                "content": "北门守卫在午夜换班。",
                "importance": 5,
                "tags": ["线索"],
                "visibility": "host",
                "locked": True,
                "pinned": True,
            },
            "admin",
        )
        self.assertTrue(memory["locked"])
        self.assertEqual(memory["visibility"], "host")

        rules = await self.database.get_session_rule_state(self.session["id"])
        rules["progress"] = {
            "chapter": "第一章",
            "current_objective": "调查北门",
            "completed_milestones": 2,
            "total_milestones": 8,
        }
        saved = await self.database.save_session_rule_state(
            self.session["id"],
            rules,
            "admin",
        )
        self.assertEqual(saved["progress"]["completed_milestones"], 2)

        first = await self.database.lock_check_result(
            "dice:test:v05",
            self.session["id"],
            {"difficulty": 15, "inspiration_mode": ""},
            {"rolls": [7, 16], "kept": 16},
        )
        second = await self.database.lock_check_result(
            "dice:test:v05",
            self.session["id"],
            {"difficulty": 5, "inspiration_mode": ""},
            {"rolls": [1], "kept": 1},
        )
        self.assertEqual(first["result"], second["result"])
        self.assertEqual(second["result"]["rolls"], [7, 16])

    async def test_finish_is_permanent_readonly_and_clone_continues(self) -> None:
        await self.database.save_memory(
            {
                "session_id": self.session["id"],
                "scope": "world",
                "kind": "fact",
                "content": "旅队已经取得铜钥匙。",
                "importance": 5,
                "tags": ["结局"],
                "locked": True,
            },
            "admin",
        )
        await self.database.grant_permission(
            self.session["id"],
            "host-user",
            "host",
            "admin",
        )
        finished = await self.database.finalize_session(
            self.session["id"],
            "admin",
            termination_type="completed",
            reason="故事正常结束",
        )
        self.assertEqual(finished["state"], SESSION_FINISHED)
        archive = await self.database.get_session_archive(self.session["id"])
        self.assertTrue(archive["readonly"])
        self.assertTrue(archive["final_snapshot_id"])
        self.assertEqual(
            await self.database.list_permission_grants(self.session["id"]),
            [],
        )

        with self.assertRaises(InvalidTransitionError):
            await self.database.transition_session(
                self.session["id"],
                SESSION_PREPARING,
                "admin",
            )
        with self.assertRaises(InvalidTransitionError):
            await self.database.save_manual_state(
                self.session["id"],
                {"location": "被篡改"},
                finished["revision"],
                "admin",
            )
        with self.assertRaises(InvalidTransitionError):
            await self.database.save_memory(
                {
                    "session_id": self.session["id"],
                    "scope": "world",
                    "kind": "fact",
                    "content": "归档后写入",
                },
                "admin",
            )
        with self.assertRaises(InvalidTransitionError):
            await self.database.restore_snapshot(
                self.session["id"],
                archive["final_snapshot_id"],
                "admin",
            )

        clone = await self.database.clone_session(
            self.session["id"],
            "admin",
            instance_slug="v05-sequel",
            instance_name="0.5 测试副本·续作",
        )
        self.assertEqual(clone["state"], "closed")
        self.assertNotEqual(clone["id"], self.session["id"])
        cloned_memories = await self.database.list_memories(
            clone["id"],
            "",
            20,
        )
        self.assertEqual(cloned_memories[0]["content"], "旅队已经取得铜钥匙。")
        snapshots = await self.database.list_snapshots(clone["id"])
        self.assertTrue(any(item["name"] == "branch-origin" for item in snapshots))
