"""v0.11.4 回归测试。

覆盖：全队行动声明检定时，投票携带检定定义，表决通过后执行检定
并依据检定结果生成落实叙事（修复「全队行动 + 检定」跳过检定的回归）。
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
from tavern.turns import embed_turn_state

ROOT = Path(__file__).resolve().parents[1]

RESOLVE = json.dumps(
    {
        "mode": "resolve",
        "narrative": "奥术脉冲触及锁扣，金属环泛起微光后归于平静。",
        "check": None,
        "state_patch": {"scene_summary": "锁扣共振响应被试探"},
        "memories": [],
        "director_note": "依据检定结果。",
    },
    ensure_ascii=False,
)


def _team_choice(check: bool, *, risk: str = "desperate") -> dict:
    """全队行动候选。

    ``risk`` 默认 ``desperate``：那是**强制检定**档（2026-08-23 用户拍板：
    危险/绝境/致命必然摇点），所以「无检定」用例必须显式降到 ``safe``——
    否则引擎会替它补一个检定出来，"没有声明检定就不摇点"根本无从验证。
    """
    choice = {
        "key": "D",
        "text": "施放奥术脉冲试探共振",
        "risk": risk,
        "collective": True,
    }
    if check:
        choice["requires_check"] = True
        choice["check"] = {
            "required": True,
            "attribute_id": "magic",
            "attribute_label": "魔力",
            "type": "standard",
            "difficulty": 17,
            "known_consequences": "失败可能引发封印反噬",
        }
    return choice


class V0114TeamCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v0114", "qq:group-v0114", DEFAULT_WORLD_SLUG,
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
        # 给 user-a 角色卡带「魔力」属性
        from tavern.database_support import new_id, utc_now

        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pid = connection.execute(
                "SELECT id FROM participants WHERE session_id = ? LIMIT 1",
                (self.session["id"],),
            ).fetchone()["id"]
            card_id, ver_id = new_id("card"), new_id("cardver")
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name,
                    archived, deleted, current_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, 1, ?, ?)
                """,
                (
                    card_id,
                    "user-a",
                    (await self.database.get_world(DEFAULT_WORLD_SLUG))["id"],
                    "甲",
                    now,
                    now,
                ),
            )
            stats = json.dumps(
                {"modifiers": {"魔力": 3}, "labels": {"魔力": "魔力"}},
                ensure_ascii=False,
            )
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES (?, ?, 1, 6, '{}', ?, 'approved', '', 'admin-1', ?)
                """,
                (ver_id, card_id, stats, now),
            )
            connection.execute(
                """
                UPDATE participants SET character_card_id = ?,
                    character_version_id = ? WHERE id = ?
                """,
                (card_id, ver_id, pid),
            )
            connection.execute("COMMIT")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _seed_choice_set(
        self, check: bool, *, risk: str = "desperate"
    ) -> None:
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
                            {"key": "A", "text": "注视表情", "risk": "safe"},
                            {"key": "B", "text": "检查地砖", "risk": "controlled"},
                            {"key": "C", "text": "推导序列", "risk": "dangerous"},
                            _team_choice(check, risk=risk),
                        ],
                        ensure_ascii=False,
                    ),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")

    async def _engine(self, outputs: list[str]) -> TavernEngine:
        context = SimpleNamespace(calls=[], outputs=[])
        context.llm_generate = AsyncMock(
            side_effect=AssertionError("collective 发起不应调用模型")
        )
        context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-test"
        )
        return TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )

    async def test_team_vote_carries_check_and_announces_dc(self) -> None:
        """全队行动带检定时：投票携带 check 定义，发起消息标注检定与 DC。"""
        await self._seed_choice_set(check=True)
        engine = await self._engine([])
        reply = await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            index=0,
        )
        self.assertIn("魔力", reply.text)
        self.assertIn("DC17", reply.text)
        vote = await self.database.active_vote(self.session["id"])
        first = (vote["options"] or [{}])[0]
        self.assertTrue(first.get("check"))
        self.assertEqual(first["check"].get("stat"), "魔力")

    async def test_vote_passed_runs_declared_check_and_commits(self) -> None:
        """表决通过后执行声明检定：回复含骰面与检定凭证，叙事按结果生成。"""
        await self._seed_choice_set(check=True)
        context = SimpleNamespace(calls=[], outputs=[RESOLVE])
        context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text=RESOLVE)
        )
        context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-test"
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )
        await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            index=0,
        )
        vote = await self.database.active_vote(self.session["id"])
        await self.database.cast_vote(self.session["id"], "user-a", "A")
        result = await self.database.cast_vote(
            self.session["id"], "user-b", "A"
        )
        reply = await engine.process_vote_resolution(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            vote=result["vote"],
        )
        self.assertIsNotNone(reply.dice)
        self.assertIn("检定", reply.story_text)
        self.assertIn("魔力", reply.story_text)
        with self.database._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS c FROM operation_receipts"
                " WHERE operation_type = 'dice_check'"
            ).fetchone()["c"]
        self.assertGreaterEqual(count, 1)

    async def test_forced_check_falls_back_when_card_lacks_the_attribute(
        self,
    ) -> None:
        """强制检定挑中的世界属性，角色卡上没有时**不能把整场表决打死**。

        回归（真实事故）：绝境档的全队行动没有声明检定，引擎按"高风险必然
        摇点"替它推断出一个属性——推断只看得见**世界属性表**，而结算查的是
        **角色卡**。本用例的卡只有「魔力」，于是推断出的「智力」查不到，
        旧实现直接抛「检定属性"智力"不属于当前世界或角色卡，表决检定无法
        执行」，全场表决作废。

        现在走玩家最高属性兜底：掷骰照常发生，属性被换成卡上真有的那个，
        公告显示【通用检定】让玩家看出这是兜底。
        """
        await self._seed_choice_set(check=False)  # desperate → 强制检定
        context = SimpleNamespace(calls=[], outputs=[RESOLVE])
        context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text=RESOLVE)
        )
        context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-test"
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )
        opening = await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            index=0,
        )
        # 发起时就必须对齐到卡上真有的属性，不能先承诺一个摇不出来的检定。
        self.assertIn("通用检定", opening.text)
        self.assertNotIn("智力检定", opening.text)

        await self.database.cast_vote(self.session["id"], "user-a", "A")
        result = await self.database.cast_vote(
            self.session["id"], "user-b", "A"
        )
        reply = await engine.process_vote_resolution(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            vote=result["vote"],
        )
        # 关键：表决活下来了，骰子照摇。
        self.assertIsNotNone(reply.dice)
        self.assertIn("通用检定", reply.story_text)

    async def test_team_vote_without_check_skips_dice(self) -> None:
        """无检定的全队行动表决通过后不产生骰值。

        必须用 ``safe`` 档：危险/绝境/致命是**强制检定**档，引擎会替它补
        一个属性出来（见 ``_team_choice`` 的说明）。这条用例证的是"没有
        声明检定就不摇点"，只有在免检档才成立。
        """
        await self._seed_choice_set(check=False, risk="safe")
        context = SimpleNamespace(calls=[], outputs=[RESOLVE])
        context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text=RESOLVE)
        )
        context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-test"
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )
        await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            index=0,
        )
        vote = await self.database.active_vote(self.session["id"])
        await self.database.cast_vote(self.session["id"], "user-a", "A")
        result = await self.database.cast_vote(
            self.session["id"], "user-b", "A"
        )
        reply = await engine.process_vote_resolution(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            vote=result["vote"],
        )
        self.assertIsNone(reply.dice)
        self.assertNotIn("检定", reply.story_text)

    async def test_vote_resolution_keeps_current_actor_choices(self) -> None:
        """表决通过后，下一组选项归属当前行动者（回合指针不前进）。

        回归：旧实现把表决后的 next_actor 计算成「轮转后的下一位」，
        但表决落库规则是「不移动玩家指针」，于是回合秩序行显示当前
        玩家、而选项头部/正文写的是下一位玩家，二者错位。
        """
        await self._seed_choice_set(check=False)
        from tavern.database_support import new_id, utc_now

        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 回合秩序以 players 表为准，先补两个有效玩家。
            for uid, name in (("user-a", "甲"), ("user-b", "乙")):
                connection.execute(
                    """
                    INSERT INTO players(
                        id, session_id, user_id, display_name,
                        character_name, profile_json, enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '{}', 1, ?, ?)
                    ON CONFLICT(session_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        character_name = excluded.character_name,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("player"),
                        self.session["id"],
                        uid,
                        name,
                        name,
                        now,
                        now,
                    ),
                )
            # 播种回合秩序：当前行动者 user-a（甲），轮转 [user-a, user-b]。
            row = connection.execute(
                "SELECT world_state_json FROM sessions WHERE id = ?",
                (self.session["id"],),
            ).fetchone()
            persisted = embed_turn_state(
                json.loads(row["world_state_json"] or "{}"),
                {
                    "version": 1,
                    "round_no": 3,
                    "order": ["user-a", "user-b"],
                    "current_user_id": "user-a",
                },
            )
            connection.execute(
                "UPDATE sessions SET world_state_json = ? WHERE id = ?",
                (
                    json.dumps(persisted, ensure_ascii=False),
                    self.session["id"],
                ),
            )
            connection.execute("COMMIT")
        choice = await self.database.active_choice_set(self.session["id"])
        actor_id = choice["participant"]["id"]
        resolve = json.dumps(
            {
                "mode": "resolve",
                "narrative": "全队行动落实：甲带队完成既定行动。",
                "check": None,
                "state_patch": {"scene_summary": "全队行动已落实。"},
                "memories": [],
                "next_choices": [
                    {
                        "key": "A",
                        "text": "甲确认行动结果",
                        "risk": "safe",
                        "actor_id": actor_id,
                        "check": None,
                    },
                    {
                        "key": "B",
                        "text": "甲追问行动细节",
                        "risk": "safe",
                        "actor_id": actor_id,
                        "check": None,
                    },
                    {
                        "key": "C",
                        "text": "甲整理现场装备",
                        "risk": "safe",
                        "actor_id": actor_id,
                        "check": None,
                    },
                    {
                        "key": "D",
                        "text": "甲联络后勤人员",
                        "risk": "safe",
                        "actor_id": actor_id,
                        "check": None,
                    },
                ],
                "director_note": "test",
            },
            ensure_ascii=False,
        )
        context = SimpleNamespace(calls=[], outputs=[resolve])
        context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text=resolve)
        )
        context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-test"
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )
        await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            sender_id="user-a",
            sender_name="甲",
            index=0,
        )
        vote = await self.database.active_vote(self.session["id"])
        self.assertIsNotNone(vote)
        await self.database.cast_vote(self.session["id"], "user-a", "A")
        result = await self.database.cast_vote(
            self.session["id"], "user-b", "A"
        )
        reply = await engine.process_vote_resolution(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0114"),
            session_id=self.session["id"],
            vote=result["vote"],
        )
        # 回合秩序行仍指向当前行动者 user-a（指针不前进）。
        self.assertIn("【回合秩序】", reply.turn_text)
        self.assertIn("当前：甲", reply.turn_text)
        # 选项头部必须是当前行动者甲，而不是轮转后的下一位乙。
        self.assertIn("【甲的行动回合】", reply.turn_text)
        self.assertNotIn("【乙的行动回合】", reply.turn_text)
        # 落库的下一组选项 actor_id 全部指向当前行动者甲。
        active = await self.database.active_choice_set(self.session["id"])
        self.assertEqual(active["participant"]["id"], actor_id)
        for option in active["choices"]:
            self.assertEqual(option.get("actor_id"), actor_id)


if __name__ == "__main__":
    unittest.main()
