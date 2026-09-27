"""v0.11.2 回归测试。

覆盖：
1. 全队行动（collective）选项的呈现分区与「选中即投票」流程。
2. 检定骰值锁定键按检定类别隔离（不同检定独立掷骰、同检定重试幂等）。
3. 表决通过后的推进：旧选项作废 + 落实叙事落库 + 生成新选项。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tavern.config import TavernConfig
from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import TavernDatabase
from tavern.engine import TavernEngine
from tavern.events import EventBroker
from tavern.lifecycle import format_choices
from tavern.operations import operation_key

ROOT = Path(__file__).resolve().parents[1]


class V0112FormatTests(unittest.TestCase):
    def test_collective_choices_are_sectioned(self) -> None:
        """全队行动选项进入独立「🌐 全队行动」区块，不再与个人选项混排。"""
        text = format_choices(
            "屏凡",
            [
                {"key": "A", "text": "检查徽章", "risk": "safe"},
                {
                    "key": "B",
                    "text": "救治信使",
                    "risk": "controlled",
                    "requires_check": True,
                    "check_stat": "信仰",
                    "difficulty": 9,
                },
                {
                    "key": "C",
                    "text": "前往石桥",
                    "risk": "dangerous",
                    "collective": True,
                },
                {
                    "key": "D",
                    "text": "封锁大厅",
                    "risk": "controlled",
                    "collective": True,
                },
            ],
        )
        self.assertIn("🌐 【全队行动 · 需集体表决】", text)
        self.assertIn("发送 jg 全队 选择", text)
        self.assertIn("不消耗个人行动机会", text)
        # 个人选项在前；全队行动不再占用个人 A—D 字母（0.11.3）。
        self.assertLess(text.index("🅱️"), text.index("🌐"))
        self.assertNotIn("🅳️", text)
        self.assertIn("🌐①", text)

    def test_personal_choices_still_rendered(self) -> None:
        text = format_choices("屏凡", [{"key": "A", "text": "检查", "risk": "safe"}])
        self.assertIn("🅰️ 检查（安全）", text)
        self.assertNotIn("🌐", text)


class V0112DiceKeyTests(unittest.TestCase):
    def test_dice_key_isolates_check_category(self) -> None:
        """骰值锁定键必须区分检定类别与所选选项。"""
        base = dict(
            session_id="s1",
            operation_type="dice",
            turn_no=1,
            actor_id="u1",
            source_id="cs1",
        )
        charisma = operation_key(
            **base,
            payload={
                "selected_key": "D",
                "stat": "魅力",
                "check_type": "leader",
            },
        )
        faith = operation_key(
            **base,
            payload={
                "selected_key": "B",
                "stat": "信仰",
                "check_type": "standard",
            },
        )
        faith_retry = operation_key(
            **base,
            payload={
                "selected_key": "B",
                "stat": "信仰",
                "check_type": "standard",
            },
        )
        self.assertNotEqual(charisma, faith)
        self.assertEqual(faith, faith_retry)  # 同选项同检定重试幂等

    def test_dice_key_differs_by_stat_only(self) -> None:
        base = dict(
            session_id="s1",
            operation_type="dice",
            turn_no=1,
            actor_id="u1",
            source_id="cs1",
        )
        a = operation_key(
            **base,
            payload={"selected_key": "B", "check_type": "standard", "stat": "信仰"},
        )
        b = operation_key(
            **base,
            payload={"selected_key": "B", "check_type": "standard", "stat": "感知"},
        )
        self.assertNotEqual(a, b)


class V0112VoteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v0112", "qq:group-v0112", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        await self.database.reserve_participant(
            self.session["id"], "user-a", "甲"
        )
        await self.database.reserve_participant(
            self.session["id"], "user-b", "乙"
        )
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE participants SET card_status = 'approved',
                    participation_status = 'active'
                WHERE session_id = ?
                """,
                (self.session["id"],),
            )
            connection.execute("COMMIT")
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _seed_old_choice_set(self) -> None:
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pid = connection.execute(
                "SELECT id FROM participants WHERE session_id = ? LIMIT 1",
                (self.session["id"],),
            ).fetchone()["id"]
            from tavern.database_support import new_id, utc_now

            connection.execute(
                """
                INSERT INTO choice_sets(
                    id, session_id, participant_id, round_no,
                    session_revision, choices_json, status, reroll_count,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 1, '[]', 'active', 0, 'x', ?, ?)
                """,
                (new_id("choices"), self.session["id"], pid, utc_now(), utc_now()),
            )
            connection.execute("COMMIT")

    async def test_collective_selection_creates_vote_without_model(self) -> None:
        """engine.process_choice 对 collective 选项直接发起投票，不调用模型。"""
        context = SimpleNamespace(calls=[], outputs=[])
        context.llm_generate = AsyncMock(
            side_effect=AssertionError("collective 选项不应调用模型")
        )
        config = TavernConfig(user_cooldown_seconds=0)
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: config,
            broker=EventBroker(),
        )
        from tavern.database_support import new_id, utc_now

        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pid = connection.execute(
                "SELECT id FROM participants WHERE session_id = ? LIMIT 1",
                (self.session["id"],),
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO choice_sets(
                    id, session_id, participant_id, round_no,
                    session_revision, choices_json, status, reroll_count,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 1, '[]', 'active', 0, 'k1', ?, ?)
                """,
                (new_id("choices"), self.session["id"], pid, utc_now(), utc_now()),
            )
            connection.execute("COMMIT")
        # 注入一个 collective 选项
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT choices_json FROM choice_sets WHERE session_id = ?",
                (self.session["id"],),
            ).fetchone()
            choices = json.loads(row["choices_json"]) if row["choices_json"] else []
            choices.append(
                {
                    "key": "A",
                    "text": "封锁大厅并组织调查",
                    "risk": "controlled",
                    "collective": True,
                }
            )
            connection.execute(
                "UPDATE choice_sets SET choices_json = ? WHERE session_id = ?",
                (json.dumps(choices, ensure_ascii=False), self.session["id"]),
            )
            connection.execute("COMMIT")
        reply = await engine.process_choice(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0112"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            choice_key="A",
        )
        self.assertIn("集体表决", reply.text)
        vote = await self.database.active_vote(self.session["id"])
        self.assertIsNotNone(vote)
        self.assertEqual(vote["status"], "open")

    async def test_vote_pass_supersedes_old_choices_and_commits_resolution(self) -> None:
        """表决通过：旧选项作废，落实叙事落库，生成新选项。"""
        await self._seed_old_choice_set()
        vote = await self.database.create_group_vote(
            self.session["id"],
            group_decision={
                "question": "是否执行全队行动：封锁公会大厅",
                "options": [
                    {"key": "A", "text": "同意执行（推进）"},
                    {"key": "B", "text": "暂缓，先处理当前局面"},
                ],
            },
            suspended_user_id="user-a",
            actor_id="admin-1",
        )
        self.assertEqual(vote["status"], "open")
        await self.database.cast_vote(self.session["id"], "user-a", "A")
        result = await self.database.cast_vote(
            self.session["id"], "user-b", "A"
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["vote"]["status"], "passed")
        # 旧选项集已被 _resume_after_vote 作废
        with self.database._connect() as connection:
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM choice_sets WHERE session_id = ?",
                    (self.session["id"],),
                ).fetchall()
            ]
        self.assertIn("superseded", statuses)

        updated = await self.database.commit_vote_resolution(
            self.session["id"],
            expected_revision=(await self.database.get_session(self.session["id"]))[
                "revision"
            ],
            narrative="公会大厅被封锁，众人被引导至侧厅等候。",
            world_state={
                "location": "公会大厅",
                "scene_summary": "封锁进行中",
                "facts": ["队伍决定封锁大厅"],
            },
            memories=[
                {
                    "scope": "world",
                    "scope_id": "",
                    "kind": "fact",
                    "content": "队伍决定封锁大厅",
                    "importance": 3,
                }
            ],
            workflow={
                "vote_id": vote["id"],
                "next_choices": [
                    {"key": "A", "text": "检查徽章", "risk": "safe"},
                    {"key": "B", "text": "询问信使", "risk": "controlled"},
                ],
                "ledger_ops": [
                    {
                        "op": "create",
                        "kind": "milestone",
                        "stable_key": "m_vote_01_lock_hall",
                        "title": "封锁公会大厅",
                        "description": "集体表决已经执行",
                        "visibility": "public",
                    }
                ],
            },
            vote_id=vote["id"],
        )
        self.assertTrue(updated["next_choice_set_id"])
        events = await self.database.recent_events(self.session["id"], 10)
        self.assertTrue(
            any(item["role"] == "narrator" for item in events)
        )
        milestones = [
            item
            for item in await self.database.list_story_ledger(self.session["id"])
            if item["kind"] == "milestone"
        ]
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["stable_key"], "m_vote_01_lock_hall")
        self.assertEqual(milestones[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
