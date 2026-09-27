"""v0.11.3 回归测试。

覆盖：
1. collective 输出校验（每轮全队行动上限 + 超限降级）。
2. 全队行动独立标识（不占用个人 A—D 字母，jg 全队 指令）。
3. 投票超时结束按实票判定通过并标记待推进。
4. 待推进表决在下次输入时自动落实叙事。
5. 本轮未提交后作废已锁骰值（同检定重试重新掷骰）。
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
from tavern.lifecycle import MAX_TEAM_CHOICES, format_choices, normalize_choices

ROOT = Path(__file__).resolve().parents[1]


class V0113CollectiveValidationTests(unittest.TestCase):
    def test_collective_count_capped_and_downgraded(self) -> None:
        """每轮全队行动不超过 MAX_TEAM_CHOICES，超出的降级为个人选项。"""
        raw = [
            {"key": "A", "text": "甲", "risk": "safe"},
            {"key": "B", "text": "乙", "risk": "controlled", "collective": True},
            {"key": "C", "text": "丙", "risk": "safe", "collective": True},
            {"key": "D", "text": "丁", "risk": "safe", "collective": True},
        ]
        norm = normalize_choices(raw)
        count = sum(1 for item in norm if item["collective"])
        self.assertLessEqual(count, MAX_TEAM_CHOICES)
        self.assertEqual(count, 2)

    def test_team_choices_have_no_personal_letters(self) -> None:
        """全队行动不占用个人 A—D 字母，用 🌐①/② 编号与 jg 全队 指令。"""
        text = format_choices(
            "屏凡",
            [
                {"key": "A", "text": "检查徽章", "risk": "safe"},
                {"key": "B", "text": "救治信使", "risk": "safe"},
                {"key": "C", "text": "请修女开锁", "risk": "safe", "collective": True},
                {"key": "D", "text": "退出礼拜堂", "risk": "safe", "collective": True},
            ],
        )
        self.assertIn("🌐① 请修女开锁", text)
        self.assertIn("🌐② 退出礼拜堂", text)
        self.assertNotIn("🅲️", text)  # C/D 不再以个人字母出现
        self.assertNotIn("🅳️", text)
        self.assertIn("jg 全队", text)


class V0113VoteTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v0113", "qq:group-v0113", DEFAULT_WORLD_SLUG,
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

    async def _create_vote_with_ballots(self, votes: list[tuple[str, str]]) -> dict:
        vote = await self.database.create_group_vote(
            self.session["id"],
            group_decision={
                "question": "是否执行全队行动：封锁大厅",
                "options": [
                    {"key": "A", "text": "同意执行（推进）"},
                    {"key": "B", "text": "暂缓，先处理当前局面"},
                ],
            },
            suspended_user_id="user-a",
            actor_id="admin-1",
        )
        # 直接写入选票（不经 cast_vote 实时结算），模拟截止前已投票、
        # 但由超时处理器统一结算的场景。
        from tavern.database_support import new_id, utc_now

        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for user_id, option in votes:
                connection.execute(
                    """
                    INSERT INTO vote_ballots(
                        id, vote_id, user_id, option_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (new_id("ballot"), vote["id"], user_id, option, now, now),
                )
            connection.execute("COMMIT")
        return vote

    def _expire_vote_timer_now(self, vote_id: str) -> None:
        from tavern.database_support import utc_now

        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE timer_instances SET status = 'active',
                    deadline_at = ?, updated_at = ?
                WHERE session_id = ? AND timer_type = 'vote'
                """,
                ("2020-01-01T00:00:00+00:00", utc_now(), self.session["id"]),
            )
            connection.execute("COMMIT")

    def test_timeout_with_majority_marks_passed_and_pending(self) -> None:
        """超时结束时按实票判定：多数已达成 → passed + pending_resolution。"""
        from tavern.database_support import json_load

        async def run() -> None:
            vote = await self._create_vote_with_ballots(
                [("user-a", "A"), ("user-b", "A")]
            )
            self._expire_vote_timer_now(vote["id"])
            due = await self.database.process_due_timers()
            self.assertTrue(due)
            with self.database._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote["id"],),
                ).fetchone()
            result = json_load(row["result_json"], {})
            self.assertEqual(row["status"], "passed")
            self.assertEqual(row["winner_key"], "A")
            self.assertTrue(result.get("pending_resolution"))
            pending = await self.database.pending_vote_resolution(
                self.session["id"]
            )
            self.assertIsNotNone(pending)

        asyncio.run(run())

    def test_timeout_without_majority_rejected(self) -> None:
        async def run() -> None:
            vote = await self._create_vote_with_ballots(
                [("user-a", "A"), ("user-b", "B")]
            )
            self._expire_vote_timer_now(vote["id"])
            await self.database.process_due_timers()
            with self.database._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote["id"],),
                ).fetchone()
            self.assertEqual(row["status"], "rejected")

        asyncio.run(run())


class V0113RevokeDiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def test_revoke_operation_receipt_is_idempotent(self) -> None:
        async def run() -> None:
            db = TavernDatabase(Path(self.temp.name))
            s = await db.ensure_session(
                "qq", "g-revoke", "qq:g-revoke", DEFAULT_WORLD_SLUG, "admin-1"
            )
            receipt = await db.lock_check_result(
                "dice:test:1",
                s["id"],
                {"stat": "魅力", "check_type": "leader"},
                {"total": 17, "rolls": [17]},
            )
            self.assertEqual(receipt["status"], "completed")
            revoked = await db.revoke_operation_receipt("dice:test:1")
            self.assertTrue(revoked)
            # 幂等：再次作废返回 False，不抛错
            again = await db.revoke_operation_receipt("dice:test:1")
            self.assertFalse(again)
            found = await db.get_operation_receipt("dice:test:1")
            self.assertIsNone(found)

        asyncio.run(run())


class V0113TeamProposalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v0113b", "qq:group-v0113b", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        await self.database.reserve_participant(
            self.session["id"], "user-a", "甲"
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
                ) VALUES (?, ?, ?, 1, 1, ?, 'active', 0, 'k', ?, ?)
                """,
                (
                    new_id("choices"),
                    self.session["id"],
                    pid,
                    json.dumps(
                        [
                            {"key": "A", "text": "检查徽章", "risk": "safe"},
                            {"key": "B", "text": "救治信使", "risk": "safe"},
                            {
                                "key": "C",
                                "text": "请修女开锁",
                                "risk": "safe",
                                "collective": True,
                            },
                            {
                                "key": "D",
                                "text": "退出礼拜堂",
                                "risk": "safe",
                                "collective": True,
                            },
                        ],
                        ensure_ascii=False,
                    ),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_team_proposal_creates_vote_without_model(self) -> None:
        context = SimpleNamespace(calls=[], outputs=[])
        context.llm_generate = AsyncMock(
            side_effect=AssertionError("jg 全队 不应调用模型")
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )
        reply = await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0113b"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            index=1,  # 选择第 2 个全队行动（退出礼拜堂）
        )
        self.assertIn("集体表决", reply.text)
        self.assertIn("退出礼拜堂", reply.text)
        vote = await self.database.active_vote(self.session["id"])
        self.assertIsNotNone(vote)
        self.assertIn("退出礼拜堂", vote["question"])


if __name__ == "__main__":
    unittest.main()
