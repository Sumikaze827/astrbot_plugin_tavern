"""v0.12.0 回归与新增能力测试。

覆盖：
1. 缺陷修复（F1）：集体表决通过后若模型调用失败，重试必须复用已提交的
   检定凭证骰面（此前表决路径丢弃 lock_check_result 返回值，导致叙事
   使用新骰面而凭证保留旧骰面，回放与展示不一致）。
2. 缺陷修复（F2）：tavern.errors 统一失败分类（transient/integrity/policy）。
3. 缺陷修复（F3）：token_quota 配置组解析 + 默认配额播种（仅缺省时播种）。
4. B1 收敛：废弃 rich 配置必须被安全忽略，文本模式保持稳定。
"""

from __future__ import annotations

import json
import logging
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
from tavern.engine import TavernEngine, TavernEngineError
from tavern.errors import (
    PolicyRejection,
    TransientError,
    report_failure,
)
from tavern.events import EventBroker

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


def _team_choice_with_check() -> dict:
    return {
        "key": "D",
        "text": "施放奥术脉冲试探共振",
        "risk": "desperate",
        "collective": True,
        "requires_check": True,
        "check": {
            "required": True,
            "attribute_id": "magic",
            "attribute_label": "魔力",
            "type": "standard",
            "difficulty": 17,
            "known_consequences": "失败可能引发封印反噬",
        },
    }


class V0120ErrorsAndConfigTests(unittest.TestCase):
    def test_report_failure_classifies_by_taxonomy(self) -> None:
        """F2：预期内策略拒绝按 info 记录；瞬时错误按 warning；未知异常记 exception。"""
        captured: list[tuple[str, str]] = []

        class _Logger:
            def info(self, message, *args):
                captured.append(("info", message % args))

            def warning(self, message, *args):
                captured.append(("warning", message % args))

            def exception(self, message, *args):
                captured.append(("exception", message % args))

        logger = _Logger()
        message = report_failure(
            logger,
            stage="command",
            operation="quota",
            exc=PolicyRejection("配额已用尽"),
        )
        self.assertEqual(captured[0][0], "info")
        self.assertIn("policy", message)
        message = report_failure(
            logger,
            stage="model_call",
            operation="llm_generate",
            exc=TransientError("网络抖动"),
        )
        self.assertEqual(captured[1][0], "warning")
        self.assertIn("transient", message)
        message = report_failure(
            logger,
            stage="db_write",
            operation="cast_vote",
            exc=ValueError("非法选项"),
            context={"session": "s1"},
        )
        self.assertEqual(captured[2][0], "exception")
        self.assertIn("unknown", message)
        self.assertIn("session=s1", message)

    def test_config_parses_quota_and_ignores_legacy_rich(self) -> None:
        """F3/B1：token_quota 正确解析，旧 rich 配置不再进入运行时。"""
        config = TavernConfig.from_mapping(
            {
                "token_quota": {
                    "enabled": True,
                    "window_seconds": 3600,
                    "token_limit": 123456,
                },
                "rich": {"notify_degradation": True},
            }
        )
        self.assertTrue(config.token_quota_enabled)
        self.assertEqual(config.token_quota_window_seconds, 3600)
        self.assertEqual(config.token_quota_token_limit, 123456)
        self.assertNotIn("rich", config.to_mapping())
        self.assertFalse(hasattr(config, "rich_notify_degradation"))
        # 未配置时回退默认值，且非法值被夹取到允许范围。
        default = TavernConfig.from_mapping({})
        self.assertFalse(default.token_quota_enabled)
        self.assertEqual(default.token_quota_window_seconds, 86400)
        self.assertEqual(default.token_quota_token_limit, 400000)
        clamped = TavernConfig.from_mapping(
            {"token_quota": {"window_seconds": 1, "token_limit": -5}}
        )
        self.assertGreaterEqual(clamped.token_quota_window_seconds, 60)
        self.assertGreaterEqual(clamped.token_quota_token_limit, 1000)


class V0120SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v0120", "qq:group-v0120", DEFAULT_WORLD_SLUG,
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

    async def _seed_choice_set(self) -> None:
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
                            _team_choice_with_check(),
                        ],
                        ensure_ascii=False,
                    ),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")

    def _engine(self, llm_generate: AsyncMock) -> TavernEngine:
        context = SimpleNamespace()
        context.llm_generate = llm_generate
        context.get_current_chat_provider_id = AsyncMock(
            return_value="provider-test"
        )
        return TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0),
            broker=EventBroker(),
        )

    async def _open_team_vote_and_pass(self) -> tuple[TavernEngine, dict]:
        await self._seed_choice_set()
        engine = self._engine(
            AsyncMock(return_value=SimpleNamespace(completion_text=RESOLVE))
        )
        await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0120"),
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
        return engine, result["vote"]

    def _committed_dice(self) -> dict:
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT result_json FROM operation_receipts
                WHERE operation_type = 'dice_check'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        return json.loads(row["result_json"]) if row else {}

    async def test_explicit_local_vote_uses_confirmed_location(self):
        from tavern.database_support import new_id, utc_now
        with self.database._connect() as connection:
            for p in connection.execute('SELECT * FROM participants WHERE session_id=?', (self.session['id'],)).fetchall():
                existing = connection.execute('SELECT id FROM character_runtime_states WHERE session_id=? AND participant_id=?', (self.session['id'], p['id'])).fetchone()
                if existing:
                    connection.execute('UPDATE character_runtime_states SET state_json=? WHERE id=?', (json.dumps({'current_location': '殿堂'}), existing['id']))
                else:
                    connection.execute('INSERT INTO character_runtime_states(id,session_id,participant_id,character_card_id,state_json,revision,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)',
                        (new_id('runtime'), self.session['id'], p['id'], p['character_card_id'], json.dumps({'current_location': '殿堂'}), utc_now(), utc_now()))
        await self.database.create_group_vote(self.session['id'], group_decision={
            'question': '是否共同检查眼前石碑', 'vote_scope': 'local',
            'options': [{'key': 'A', 'text': '同意'}, {'key': 'B', 'text': '暂缓'}],
        }, suspended_user_id='user-a', actor_id='user-a')
        vote = await self.database.active_vote(self.session['id'])
        self.assertEqual(set(vote['eligible_user_ids']), {'user-a', 'user-b'})
        self.assertTrue(vote['question'].startswith('【现场小队】'))

    async def _seed_terminal_ending(self) -> None:
        from tests.test_chapter_progress_realign import _seed_instance_world, _seed_progress
        world = dict(await self.database.get_world(DEFAULT_WORLD_SLUG))
        world["rules"] = {**world.get("rules", {}), "progress": {"chapters": [{
            "id": "terminal", "title": "终章", "min_turns": 1,
            "current_objective": "关闭裂隙并撤离",
            "milestones": [{"id": "epilogue", "label": "任务尾声", "ending_milestone": True}],
            "exits_when": {"all_milestones": ["epilogue"]}, "next_chapter_id": None,
        }]}}
        with self.database._connect() as connection:
            _seed_instance_world(connection, self.session["id"], world)
            _seed_progress(connection, self.session["id"], {
                "current_chapter_id": "terminal", "chapter_entered_at_turn": 0,
                "last_milestone_judge_at_turn": 2,
            })
            connection.execute("UPDATE sessions SET turn_no = 2 WHERE id = ?", (self.session["id"],))

    def _ending_response(self, closed=True):
        narrative = ("裂隙在最后一道封印下彻底闭合，城中的震动随之停止。"
                     "守卫确认危险解除，居民从避难所返回，受损街区开始修复。"
                     "队伍把封印记录交给负责维护的守卫，双方确认交接完成。"
                     "曾经封锁街道的警戒线被撤下，商铺重新打开门，孩子们回到广场。"
                     "众人清点装备，带着伤员乘车离开，不再需要留下等待新的命令。"
                     "夕阳落在恢复平静的城墙上，这次行动的任务已经完成，队伍踏上归途。")
        async def generate(**kwargs):
            if kwargs["request_type"] == "ending_candidate_judge":
                payload = {"closed": closed, "event_id": "ending_candidate",
                           "quote": narrative[:24], "reason": "测试收束裁判"}
            else:
                payload = {**json.loads(RESOLVE), "narrative": narrative,
                           "director_note": "story_ending_complete", "next_choices": []}
            return SimpleNamespace(completion_text=json.dumps(payload, ensure_ascii=False))
        return AsyncMock(side_effect=generate)

    async def test_vote_ending_commits_and_completes(self):
        engine, vote = await self._open_team_vote_and_pass()
        await self._seed_terminal_ending()
        engine._llm_generate_metered = self._ending_response()
        await engine.process_vote_resolution(
            vote=vote,
            event=SimpleNamespace(unified_msg_origin="qq:group-v0120"), session_id=self.session["id"],
        )
        progress = (await self.database.get_session_rule_state(self.session["id"]))["progress"]
        self.assertTrue(progress["ending_narrated"])
        self.assertTrue(progress["story_complete"])
        self.assertIsNone(await self.database.pending_vote_resolution(self.session["id"]))

    async def test_dm_ending_commits_and_completes(self):
        engine = self._engine(AsyncMock())
        await self._seed_terminal_ending()
        await self.database.enable_dm_mode(self.session["id"], "admin-1", "admin-1")
        engine._llm_generate_metered = self._ending_response()
        await engine.process_dm_beat(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0120"), session_id=self.session["id"],
            dm_user_id="admin-1", instruction="收束本次任务",
        )
        progress = (await self.database.get_session_rule_state(self.session["id"]))["progress"]
        self.assertTrue(progress["ending_narrated"])
        self.assertTrue(progress["story_complete"])

    async def test_rejected_ending_does_not_commit(self):
        engine = self._engine(AsyncMock())
        await self._seed_terminal_ending()
        await self.database.enable_dm_mode(self.session["id"], "admin-1", "admin-1")
        engine._llm_generate_metered = self._ending_response(closed=False)
        before = (await self.database.get_session(self.session["id"]))["turn_no"]
        with self.assertRaises(TavernEngineError):
            await engine.process_dm_beat(
                event=SimpleNamespace(unified_msg_origin="qq:group-v0120"), session_id=self.session["id"],
                dm_user_id="admin-1", instruction="收束本次任务",
            )
        progress = (await self.database.get_session_rule_state(self.session["id"]))["progress"]
        self.assertFalse(progress.get("ending_narrated"))
        self.assertFalse(progress.get("story_complete"))
        self.assertEqual((await self.database.get_session(self.session["id"]))["turn_no"], before)

    async def test_progress_provider_uses_current_session(self):
        engine = self._engine(AsyncMock())
        self.assertEqual(await engine._progress_providers(self.session["id"], TavernConfig()), ["provider-test"])
        engine.context.get_current_chat_provider_id.assert_awaited_once_with(umo="qq:group-v0120")

    async def test_vote_retry_reuses_committed_dice(self) -> None:
        """F1：表决通过 → 模型首次失败 → 重试必须复用已提交的骰面。"""
        await self._seed_choice_set()
        calls = {"n": 0}

        async def flaky(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模型瞬时失败")
            return SimpleNamespace(completion_text=RESOLVE)

        engine = self._engine(AsyncMock(side_effect=flaky))
        await engine.process_team_proposal(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0120"),
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
        vote = result["vote"]
        # 第一次：模型失败 → 应抛错，但检定凭证已幂等落库。
        with self.assertRaises(TavernEngineError):
            await engine.process_vote_resolution(
                event=SimpleNamespace(unified_msg_origin="qq:group-v0120"),
                session_id=self.session["id"],
                vote=vote,
            )
        committed = self._committed_dice()
        self.assertIn("total", committed)
        # 第二次：模型恢复 → 回复携带的骰面必须等于已提交凭证的骰面。
        reply = await engine.process_vote_resolution(
            event=SimpleNamespace(unified_msg_origin="qq:group-v0120"),
            session_id=self.session["id"],
            vote=vote,
        )
        self.assertIsNotNone(reply.dice)
        self.assertEqual(reply.dice.total, committed["total"])
        self.assertEqual(list(reply.dice.rolls), list(committed["rolls"]))

    async def test_default_quota_seeds_only_when_missing(self) -> None:
        """F3：无策略时播种默认策略；已有策略或未启用时不改写。"""
        session_id = self.session["id"]
        # 初始无策略 → 播种。
        summary = await self.database.ensure_default_token_quota(
            session_id,
            window_seconds=3600,
            token_limit=5000,
            enabled=True,
            actor_id="admin-1",
        )
        with self.database._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM token_quota_policies WHERE enabled = 1"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_limit"], 5000)
        self.assertEqual(summary["quotas"][0]["token_limit"], 5000)
        # 已有策略 → 不覆盖（仍 1 条，限额不变）。
        await self.database.ensure_default_token_quota(
            session_id,
            window_seconds=3600,
            token_limit=999999,
            enabled=True,
            actor_id="admin-1",
        )
        with self.database._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS c FROM token_quota_policies"
                " WHERE enabled = 1"
            ).fetchone()["c"]
        self.assertEqual(count, 1)
        # 未启用 → 对全新副本不创建任何策略。
        other = await self.database.ensure_session(
            "qq", "group-quota-none", "qq:group-quota-none",
            DEFAULT_WORLD_SLUG, "admin-1",
        )
        await self.database.ensure_default_token_quota(
            other["id"],
            window_seconds=3600,
            token_limit=5000,
            enabled=False,
            actor_id="admin-1",
        )
        with self.database._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS c FROM token_quota_policies"
                " WHERE scope_id = ?",
                (other["id"],),
            ).fetchone()["c"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
