from __future__ import annotations

import json
import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tavern.config import TavernConfig
from tavern.constants import (
    DATABASE_SCHEMA_VERSION,
    DEFAULT_WORLD_SLUG,
    SESSION_FINISHED,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import (
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)
from tavern.engine import TavernEngine
from tavern.events import EventBroker
from tavern.lifecycle import fallback_choices
from tavern.resolution import roll_check, validate_resolution


class FakeNarratorContext:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        return "provider-test"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("模型被额外调用")
        return SimpleNamespace(completion_text=self.outputs.pop(0))


class VNextWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq",
            "vnext-group",
            "qq:vnext-group",
            DEFAULT_WORLD_SLUG,
            "admin",
        )
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        # These workflow regressions exercise the enabled countdown path.
        # The built-in B1 world intentionally defaults countdowns to off.
        await self.database.set_timer_policy(
            self.session["id"], "all", True, "admin"
        )
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "card_completion_timeout_seconds": 600,
                "preparation_timeout_seconds": 300,
                "ready_timeout_seconds": 300,
                "turn_timeout_seconds": 180,
                "turn_reminder_seconds": 30,
                "standby_timeout_seconds": 600,
                "vote_round_one_seconds": 90,
                "vote_round_two_seconds": 60,
                "vote_reminder_seconds": 30,
                "announce_timeouts": True,
            },
            "admin",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _make_character(
        self,
        user_id: str,
        name: str,
        code: str,
    ) -> dict:
        reserved = await self.database.reserve_participant(
            self.session["id"],
            user_id,
            name,
        )
        origin = f"qq-private:{user_id}"
        await self.database.bind_card_code(
            reserved["binding_code"],
            f"private-{user_id}",
            origin,
        )
        draft = await self.database.card_draft_for_private(origin)
        self.assertIsNotNone(draft)
        stat_values = {
            "stat_body": "3",
            "stat_agility": "3",
            "stat_will": "2",
            "stat_knowledge": "2",
        }
        fixed_values = {
            "name": name,
            "code": code,
            "appearance": f"{name}穿着便于旅行的旧外套。",
            "background": f"{name}来自边境商路，了解基本旅行常识。",
            "personality": "谨慎、克制，不会替同伴作决定。",
            "goal": "调查酒馆附近的异常并保护同行者。",
            "weakness": "不擅长强行对抗，也缺乏贵族人脉。",
            "knowledge_boundary": "只知道边境常识，不知道隐秘魔法真相。",
        }
        used_select_values: set[str] = set()
        for field in draft["template"]["fields"]:
            options = field.get("options") or []
            if field.get("type") == "preset_select" and options:
                values = [
                    str(item.get("value") or item.get("label") or item)
                    if isinstance(item, dict) else str(item)
                    for item in options
                ]
                value = next(
                    (item for item in values if item not in used_select_values),
                    values[0],
                )
                used_select_values.add(value)
            else:
                value = stat_values.get(
                    field["key"],
                    fixed_values.get(field["key"], "无"),
                )
            await self.database.fill_card_draft(origin, value)
        confirmed = await self.database.confirm_card_draft(origin)
        if not confirmed["auto_approved"]:
            confirmed = await self.database.review_character_card(
                self.session["id"],
                confirmed["id"],
                True,
                "admin",
                "测试审核",
            )
        return await self.database.set_participant_ready(
            self.session["id"],
            user_id,
        )

    async def _activate_two(self) -> tuple[dict, dict, dict]:
        first = await self._make_character("user-1", "白鸦", "BY")
        second = await self._make_character("user-2", "梅林", "ML")
        result = await self.database.activate_story(
            self.session["id"],
            "admin",
        )
        self.assertTrue(result["started"])
        self.session = result["session"]
        return first, second, result

    async def _commit_current(
        self,
        *,
        group_decision: dict | None = None,
        return_progress: dict | None = None,
    ) -> dict:
        session = await self.database.get_session(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(choice)
        participant = choice["participant"]
        state = dict(session["world_state"])
        state["scene_summary"] = "测试行动已经得到裁定。"
        workflow = {
            "choice_set_id": choice["id"],
            "selected_key": "A",
            "flavor_text": "",
            "next_choices": fallback_choices(state),
            "group_decision": group_decision,
            "return_progress": return_progress,
        }
        result = await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=participant["player_id"],
            player_user_id=participant["group_user_id"],
            player_name=participant["character_name"],
            player_input="选择 A",
            narrative="测试行动已经得到裁定。",
            world_state=state,
            memories=[],
            check_payload=None,
            model_payload={"mode": "resolve"},
            director_note="测试",
            auto_snapshot_interval=5,
            store_model_payload=False,
            workflow=workflow,
        )
        self.session = result
        return result

    async def test_opening_is_two_stage_and_creates_four_choices(self) -> None:
        self.assertEqual(self.session["state"], SESSION_PREPARING)
        preflight = await self.database.opening_preflight(self.session["id"])
        self.assertFalse(preflight["ok"])
        self.assertTrue(preflight["blockers"])

        _, _, result = await self._activate_two()
        self.assertEqual(result["session"]["state"], SESSION_RUNNING)
        self.assertEqual(
            [item["key"] for item in result["choice_set"]["choices"]],
            ["A", "B", "C", "D"],
        )
        timers = await self.database.list_timers(self.session["id"])
        self.assertTrue(
            any(
                item["timer_type"] == "turn"
                and item["status"] == "active"
                for item in timers
            )
        )

    async def test_validated_ending_is_atomic_and_creates_no_next_choices(self) -> None:
        """Ending milestone, ending marker, and no-next-turn commit together."""
        await self._activate_two()
        session = await self.database.get_session(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(choice)
        participant = choice["participant"]
        state = dict(session["world_state"])
        workflow = {
            "choice_set_id": choice["id"],
            "selected_key": "A",
            "flavor_text": "",
            "next_choices": fallback_choices(state),
            "group_decision": None,
            "ledger_ops": [{
                "op": "complete",
                "kind": "milestone",
                "stable_key": "m_test_ending",
                "title": "写完一次性尾声",
            }],
            "ending_completion": {
                "complete": True,
                "chapter_id": "ch_end",
                "milestone_ids": ["m_test_ending"],
            },
        }
        result = await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=participant["player_id"],
            player_user_id=participant["group_user_id"],
            player_name=participant["character_name"],
            player_input="离开这里",
            narrative="雨停后，队伍完成交接并一起离开。" * 12,
            world_state=state,
            memories=[],
            check_payload=None,
            model_payload={"mode": "resolve"},
            director_note="story_ending_complete",
            auto_snapshot_interval=5,
            store_model_payload=False,
            workflow=workflow,
        )
        self.assertEqual(result["turn_no"], 1)
        self.assertIsNone(
            await self.database.active_choice_set(self.session["id"])
        )
        progress = (
            await self.database.get_session_rule_state(self.session["id"])
        )["progress"]
        self.assertTrue(progress["ending_narrated"])
        self.assertFalse(progress["ending_pending"])
        self.assertTrue(progress["ending_event_id"].startswith("event_"))
        ledger = await self.database.list_story_ledger(self.session["id"])
        ending = next(
            item for item in ledger
            if item["stable_key"] == "m_test_ending"
        )
        self.assertEqual(ending["status"], "completed")
        self.assertEqual(ending["source_event_id"], progress["ending_event_id"])

    async def test_required_choice_recovers_when_model_skips_check(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        required_choices = fallback_choices(
            {
                "location": "断桥前",
                "scene_summary": "队伍需要越过断桥",
            }
        )
        required_choices[2].update(
            {
                "text": "借助绳索翻越断桥",
                "risk": "dangerous",
                "danger_id": "dangerous",
                "requires_check": True,
                "check_type": "standard",
                "check_stat": "灵巧",
                "difficulty": 15,
                "known_consequences": "失败可能跌落并受伤",
                "advantage_sources": ["装备：绳索"],
            }
        )
        await self.database.replace_active_choices(
            self.session["id"],
            choice["participant_id"],
            required_choices,
            actor_id=choice["participant"]["group_user_id"],
        )
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        premature_resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "白鸦未经检定便越过了断桥。",
                "check": None,
                "state_patch": {
                    "location": "断桥另一侧",
                    "scene_summary": "白鸦已经越过断桥。",
                },
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "断桥另一侧"}
                ),
                "director_note": "错误地跳过了检定。",
            },
            ensure_ascii=False,
        )
        checked_resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "白鸦依据检定结果完成了这次尝试。",
                "check": None,
                "state_patch": {
                    "scene_summary": "断桥尝试已经得到裁定。",
                },
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "断桥前"}
                ),
                "director_note": "遵循插件锁定的权威骰点。",
            },
            ensure_ascii=False,
        )
        # 检定参数已由选项提前锁定，B1 会直接投骰，不再先让模型重复申请检定。
        # 但要两次调用：①「按权威检定结果叙事」② 生成下一组选项——
        # 叙事与选项是分开的两次请求（见 _generate_choices）。
        context = FakeNarratorContext(
            [
                checked_resolution,
                json.dumps(
                    {
                        "choices": [
                            {"key": "A", "text": "直接过桥，抵达对岸", "risk": "safe"},
                            {"key": "B", "text": "沿河岸向下游绕行", "risk": "controlled"},
                            {"key": "C", "text": "拆掉断桥残段搭出通路", "risk": "dangerous"},
                            {"key": "D", "text": "离开桥头，返回营地", "risk": "safe"},
                        ]
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                store_model_payloads=True,
            ),
            broker=EventBroker(),
        )
        reply = await engine.process_choice(
            event=SimpleNamespace(
                unified_msg_origin="qq:vnext-group"
            ),
            session_id=self.session["id"],
            sender_id=participant["group_user_id"],
            sender_name=participant["character_name"],
            choice_key="C",
        )

        self.assertIsNotNone(reply.dice)
        self.assertEqual(reply.dice.difficulty, 13)
        self.assertEqual(reply.session["turn_no"], 1)
        self.assertEqual(
            reply.session["world_state"].get("scene_summary"),
            "断桥尝试已经得到裁定。",
        )
        self.assertEqual(
            len(context.calls), 2, "锁定选项后应是「叙事 + 选项」两次调用"
        )
        self.assertIn(
            "<authoritative_check>",
            context.calls[0]["prompt"],
        )
        self.assertIn(
            '"difficulty":13',
            context.calls[0]["prompt"],
        )
        self.assertNotIn("未经检定便越过", reply.text)

    async def test_private_card_draft_survives_database_reload(self) -> None:
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "draft-user",
            "草稿玩家",
        )
        origin = "qq-private:draft-user"
        await self.database.bind_card_code(
            reserved["binding_code"],
            "private-draft-user",
            origin,
        )
        await self.database.fill_card_draft(origin, "草稿角色")

        reloaded = TavernDatabase(self.data_dir)
        draft = await reloaded.card_draft_for_private(origin)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["fields"]["name"], "草稿角色")
        self.assertEqual(draft["current_step"], 1)

    async def test_seat_limit_rejects_fifth_player(self) -> None:
        for index in range(4):
            await self.database.reserve_participant(
                self.session["id"],
                f"seat-{index}",
                f"席位{index}",
            )
        with self.assertRaisesRegex(ValueError, "已满"):
            await self.database.reserve_participant(
                self.session["id"],
                "seat-5",
                "第五人",
            )

    async def test_authoritative_stat_modifier_comes_from_card(self) -> None:
        await self._make_character("user-1", "白鸦", "BY")
        modifier = await self.database.authoritative_modifier(
            self.session["id"],
            "user-1",
            "力量",
        )
        self.assertTrue(modifier["matched"])
        self.assertEqual(modifier["modifier"], 5)
        unknown = await self.database.authoritative_modifier(
            self.session["id"],
            "user-1",
            "凭空创造",
        )
        self.assertFalse(unknown["matched"])
        self.assertEqual(unknown["modifier"], 0)

    async def test_group_vote_requires_majority_and_preserves_turn(self) -> None:
        await self._activate_two()
        before = await self.database.get_turn_status(self.session["id"])
        await self._commit_current(
            group_decision={
                "question": "是否前往北方废墟？",
                "options": [
                    {"key": "A", "text": "前往北方废墟"},
                    {"key": "B", "text": "留在酒馆调查"},
                    {"key": "C", "text": "先休整"},
                    {"key": "D", "text": "放弃路线"},
                ],
            }
        )
        vote = await self.database.active_vote(self.session["id"])
        self.assertIsNotNone(vote)
        suspended = vote["suspended_user_id"]
        self.assertEqual(suspended, before["current_user_id"])

        first = await self.database.cast_vote(
            self.session["id"],
            "user-1",
            "A",
        )
        self.assertFalse(first["resolved"])
        second = await self.database.cast_vote(
            self.session["id"],
            "user-2",
            "A",
        )
        self.assertTrue(second["resolved"])
        self.assertEqual(second["vote"]["winner_key"], "A")
        # 表决通过后由上层生成“集体决定”的续写与新选项；数据库层
        # 只保留行动指针，不能让旧个人选项继续可用。
        self.assertIsNone(
            await self.database.active_choice_set(self.session["id"])
        )
        session = await self.database.get_session(self.session["id"])
        turn = await self.database.get_turn_status(self.session["id"])
        self.assertEqual(turn["current_user_id"], suspended)
        self.assertIn(
            "队伍多数决定：前往北方废墟",
            session["world_state"]["facts"],
        )

    async def test_snapshot_restores_inflight_choice_and_pauses_timer(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        await self.database.create_snapshot(
            self.session["id"],
            "保留中的选项",
            "admin",
        )
        await self.database.replace_active_choices(
            self.session["id"],
            choice["participant_id"],
            fallback_choices({"location": "改变后的地点"}),
            actor_id="user-1",
        )
        restored = await self.database.restore_snapshot(
            self.session["id"],
            "保留中的选项",
            "admin",
        )
        self.assertEqual(restored["state"], SESSION_PAUSED)
        restored_choice = await self.database.active_choice_set(
            self.session["id"]
        )
        self.assertEqual(restored_choice["id"], choice["id"])
        timers = await self.database.list_timers(self.session["id"])
        self.assertTrue(
            any(
                item["timer_type"] == "turn"
                and item["status"] == "paused"
                for item in timers
            )
        )

    async def test_turn_timeout_can_hold_original_action_right(self) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "turn_timeout_seconds": 60,
                "turn_timeout_action": "hold",
                "all_idle_pause_seconds": None,
            },
            "admin",
        )
        await self._activate_two()
        before = await self.database.get_turn_status(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "turn" and item["status"] == "active"
        )
        await self.database.control_timer(
            timer["id"],
            "expire",
            "admin",
        )
        after = await self.database.get_turn_status(self.session["id"])
        active_choice = await self.database.active_choice_set(self.session["id"])
        self.assertEqual(
            after["current_user_id"],
            before["current_user_id"],
        )
        self.assertEqual(active_choice["id"], choice["id"])

    async def test_countdown_repeats_every_thirty_seconds_and_targets_actor(
        self,
    ) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "turn_timeout_seconds": 120,
                "turn_reminder_seconds": 30,
                "all_idle_pause_seconds": None,
            },
            "admin",
        )
        await self._activate_two()
        turn = await self.database.get_turn_status(self.session["id"])
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "turn" and item["status"] == "active"
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances
                SET reminder_at = ?, deadline_at = ?, reminder_sent = 0
                WHERE id = ?
                """,
                (
                    (now - timedelta(seconds=1)).isoformat(),
                    (now + timedelta(seconds=95)).isoformat(),
                    timer["id"],
                ),
            )

        notices = await self.database.process_due_timers()
        reminder = next(
            item
            for item in notices
            if item.get("timer_id") == timer["id"]
            and item["kind"] == "reminder"
        )
        self.assertEqual(
            [item["user_id"] for item in reminder["targets"]],
            [turn["current_user_id"]],
        )
        self.assertGreaterEqual(reminder["remaining_seconds"], 94)
        self.assertLessEqual(reminder["remaining_seconds"], 95)

        updated = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["id"] == timer["id"]
        )
        next_reminder = datetime.fromisoformat(updated["reminder_at"])
        self.assertGreaterEqual(
            (next_reminder - now).total_seconds(),
            29,
        )
        self.assertLessEqual(
            (next_reminder - now).total_seconds(),
            30,
        )
        immediate = await self.database.process_due_timers()
        self.assertFalse(
            any(
                item.get("timer_id") == timer["id"]
                and item["kind"] == "reminder"
                for item in immediate
            )
        )

        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances SET reminder_at = ?
                WHERE id = ?
                """,
                (
                    (
                        datetime.now(timezone.utc)
                        - timedelta(seconds=1)
                    ).isoformat(timespec="seconds"),
                    timer["id"],
                ),
            )
        repeated = await self.database.process_due_timers()
        self.assertTrue(
            any(
                item.get("timer_id") == timer["id"]
                and item["kind"] == "reminder"
                for item in repeated
            )
        )

        await self.database.pause_session_timers(
            self.session["id"],
            "admin",
        )
        paused = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["id"] == timer["id"]
        )
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["reminder_at"], "")
        self.assertFalse(
            any(
                item.get("timer_id") == timer["id"]
                for item in await self.database.process_due_timers()
            )
        )

    async def test_card_countdown_uses_private_two_minute_toggleable_notice(
        self,
    ) -> None:
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "card-timer-user",
            "建卡玩家",
        )
        private_origin = "qq:FriendMessage:card-timer-user"
        await self.database.bind_card_code(
            reserved["binding_code"],
            "private-card-timer-user",
            private_origin,
        )
        await self.database.set_card_completion_reminder(
            private_origin,
            True,
        )
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "card_completion"
            and item["status"] == "active"
        )
        self.assertTrue(timer["action"]["reminder_enabled"])
        self.assertEqual(
            timer["action"]["reminder_interval_seconds"],
            120,
        )

        # Simulate a timer created by the previous 30-second release. The
        # first poll migrates it without emitting the stale reminder.
        legacy_action = dict(timer["action"])
        legacy_action.pop("reminder_enabled")
        legacy_action.pop("reminder_interval_seconds")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances
                SET action_json = ?, reminder_at = ?, deadline_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(legacy_action, ensure_ascii=False),
                    (now - timedelta(seconds=1)).isoformat(),
                    (now + timedelta(seconds=300)).isoformat(),
                    timer["id"],
                ),
            )
        migrated_notices = await self.database.process_due_timers()
        self.assertFalse(
            any(
                item.get("timer_id") == timer["id"]
                and item["kind"] == "reminder"
                for item in migrated_notices
            )
        )
        migrated = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["id"] == timer["id"]
        )
        self.assertEqual(
            migrated["action"]["reminder_interval_seconds"],
            120,
        )
        migrated_at = datetime.fromisoformat(migrated["reminder_at"])
        self.assertGreaterEqual(
            (migrated_at - now).total_seconds(),
            119,
        )
        self.assertLessEqual(
            (migrated_at - now).total_seconds(),
            120,
        )

        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances SET reminder_at = ?
                WHERE id = ?
                """,
                (
                    (
                        datetime.now(timezone.utc)
                        - timedelta(seconds=1)
                    ).isoformat(timespec="seconds"),
                    timer["id"],
                ),
            )
        notices = await self.database.process_due_timers()
        reminder = next(
            item
            for item in notices
            if item.get("timer_id") == timer["id"]
            and item["kind"] == "reminder"
        )
        self.assertEqual(reminder["reminder_interval_seconds"], 120)
        self.assertEqual(
            [item["private_origin"] for item in reminder["targets"]],
            [private_origin],
        )

        disabled = await self.database.set_card_completion_reminder(
            private_origin,
            False,
        )
        self.assertFalse(disabled["enabled"])
        self.assertEqual(
            disabled["next_reminder_at"],
            next(
                item
                for item in await self.database.list_timers(
                    self.session["id"]
                )
                if item["id"] == timer["id"]
            )["deadline_at"],
        )
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances SET reminder_at = ?
                WHERE id = ?
                """,
                (
                    (
                        datetime.now(timezone.utc)
                        - timedelta(seconds=1)
                    ).isoformat(timespec="seconds"),
                    timer["id"],
                ),
            )
        muted_notices = await self.database.process_due_timers()
        self.assertFalse(
            any(
                item.get("timer_id") == timer["id"]
                and item["kind"] == "reminder"
                for item in muted_notices
            )
        )

        enabled = await self.database.set_card_completion_reminder(
            private_origin,
            True,
        )
        self.assertTrue(enabled["enabled"])
        next_notice = datetime.fromisoformat(enabled["next_reminder_at"])
        seconds_until_notice = (
            next_notice
            - datetime.now(timezone.utc).replace(microsecond=0)
        ).total_seconds()
        self.assertGreaterEqual(seconds_until_notice, 119)
        self.assertLessEqual(seconds_until_notice, 120)

    async def test_vote_reminder_targets_only_players_who_have_not_voted(
        self,
    ) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "all_idle_pause_seconds": None,
            },
            "admin",
        )
        await self._activate_two()
        await self._commit_current(
            group_decision={
                "question": "是否继续调查？",
                "options": [
                    {"key": "A", "text": "继续调查"},
                    {"key": "B", "text": "暂时撤退"},
                    {"key": "C", "text": "原地观察"},
                ],
            }
        )
        await self.database.cast_vote(
            self.session["id"],
            "user-1",
            "A",
        )
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "vote" and item["status"] == "active"
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances
                SET reminder_at = ?, deadline_at = ?, reminder_sent = 0
                WHERE id = ?
                """,
                (
                    (now - timedelta(seconds=1)).isoformat(),
                    (now + timedelta(seconds=65)).isoformat(),
                    timer["id"],
                ),
            )
        notices = await self.database.process_due_timers()
        reminder = next(
            item
            for item in notices
            if item.get("timer_id") == timer["id"]
            and item["kind"] == "reminder"
        )
        self.assertEqual(
            [item["user_id"] for item in reminder["targets"]],
            ["user-2"],
        )

        resolved = await self.database.cast_vote(
            self.session["id"],
            "user-2",
            "A",
        )
        self.assertTrue(resolved["resolved"])
        self.assertFalse(
            any(
                item.get("timer_id") == timer["id"]
                for item in await self.database.process_due_timers()
            )
        )

    async def test_preparation_reminder_targets_only_unready_players(
        self,
    ) -> None:
        await self._make_character("user-1", "白鸦", "BY")
        await self._make_character("user-2", "梅林", "ML")
        await self.database.set_participant_ready(
            self.session["id"],
            "user-2",
            False,
        )
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "preparation"
            and item["status"] == "active"
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE timer_instances
                SET reminder_at = ?, deadline_at = ?, reminder_sent = 0
                WHERE id = ?
                """,
                (
                    (now - timedelta(seconds=1)).isoformat(),
                    (now + timedelta(seconds=65)).isoformat(),
                    timer["id"],
                ),
            )
        notices = await self.database.process_due_timers()
        reminder = next(
            item
            for item in notices
            if item.get("timer_id") == timer["id"]
            and item["kind"] == "reminder"
        )
        self.assertEqual(
            [item["user_id"] for item in reminder["targets"]],
            ["user-2"],
        )

    async def test_all_idle_timeout_pauses_and_freezes_timers(self) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "turn_timeout_seconds": None,
                "all_idle_pause_seconds": 1,
            },
            "admin",
        )
        await self._activate_two()
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT phase_meta_json FROM instance_configs
                WHERE session_id = ?
                """,
                (self.session["id"],),
            ).fetchone()
            phase_meta = json.loads(row["phase_meta_json"])
            phase_meta["started_at"] = "2000-01-01T00:00:00+00:00"
            connection.execute(
                """
                UPDATE instance_configs SET phase_meta_json = ?
                WHERE session_id = ?
                """,
                (
                    json.dumps(phase_meta, ensure_ascii=False),
                    self.session["id"],
                ),
            )
        notices = await self.database.process_due_timers()
        session = await self.database.get_session(self.session["id"])
        self.assertEqual(session["state"], SESSION_PAUSED)
        self.assertTrue(any(item["kind"] == "idle_pause" for item in notices))
        timers = await self.database.list_timers(self.session["id"])
        self.assertFalse(any(item["status"] == "active" for item in timers))

    async def test_delegation_survives_the_owner_acting(self) -> None:
        """本人行动**不**解除代控（默认 auto_restore=0）。

        2026-09-21：旧实现无条件在本人行动时把该角色的有效代管全部 revoked，
        而且不写审计。轮转局里本人每轮必然行动一次，于是代管每轮被销毁一次——
        玩家侧的表现就是"代管只能代理一轮"，必须反复重新授权。
        """
        _, _, result = await self._activate_two()
        owner = result["current_participant"]
        await self.database.grant_delegation(
            self.session["id"],
            owner["group_user_id"],
            "delegate-user",
            owner["group_user_id"],
            duration_seconds=3600,
            expiry_kind="datetime",
        )
        delegated = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            "delegate-user",
            "choose",
        )
        self.assertTrue(delegated["authorized"])
        returned = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            owner["group_user_id"],
            "choose",
        )
        self.assertEqual(returned["mode"], "owner")
        # 本人行动一次之后，受托人**仍然**有权
        still_delegated = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            "delegate-user",
            "choose",
        )
        self.assertTrue(still_delegated["authorized"])
        self.assertEqual(still_delegated["mode"], "delegate")

    async def test_delegation_with_auto_restore_ends_when_owner_acts(self) -> None:
        """auto_restore=1 时才保留「本人一行动就收回」的旧语义。"""
        _, _, result = await self._activate_two()
        owner = result["current_participant"]
        await self.database.grant_delegation(
            self.session["id"],
            owner["group_user_id"],
            "delegate-user",
            owner["group_user_id"],
            expiry_kind="none",
            auto_restore=True,
        )
        self.assertTrue(
            (
                await self.database.authorize_participant_control(
                    self.session["id"], owner["id"], "delegate-user", "choose"
                )
            )["authorized"]
        )
        await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            owner["group_user_id"],
            "choose",
        )
        revoked = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            "delegate-user",
            "choose",
        )
        self.assertFalse(revoked["authorized"])

    async def test_delegation_without_duration_has_no_expiry(self) -> None:
        """不填时长 = 不限时，expires_at 必须为空，否则会被到期清理误杀。"""
        _, _, result = await self._activate_two()
        owner = result["current_participant"]
        grant = await self.database.grant_delegation(
            self.session["id"],
            owner["group_user_id"],
            "delegate-user",
            owner["group_user_id"],
        )
        self.assertEqual(grant["expires_at"], "")
        self.assertEqual(grant["expiry_kind"], "none")

    async def test_delegation_with_duration_gets_a_deadline(self) -> None:
        """给了时长就必须真的有到期时间。

        2026-09-21：`/酒馆 授权代控 <id> 2小时` 只传了 duration_seconds、
        expiry_kind 仍是 none，expires_at 恒为空——玩家填的时长被静默忽略，
        回执还骗他说「不限时」。
        """
        _, _, result = await self._activate_two()
        owner = result["current_participant"]
        grant = await self.database.grant_delegation(
            self.session["id"],
            owner["group_user_id"],
            "delegate-user",
            owner["group_user_id"],
            duration_seconds=7200,
            expiry_kind="datetime",
        )
        self.assertEqual(grant["expiry_kind"], "datetime")
        self.assertTrue(grant["expires_at"])

    async def test_ban_atomically_retires_and_keeps_history(self) -> None:
        await self._activate_two()
        result = await self.database.create_ban(
            self.session["id"],
            "BY",
            "admin",
            duration_seconds=3600,
            reason="测试封禁",
        )
        self.assertIn("白鸦", result["narrative"])
        participant = await self.database.get_participant(
            self.session["id"],
            participant_ref="BY",
        )
        self.assertEqual(participant["participation_status"], "retired")
        bans = await self.database.list_bans(self.session["id"])
        self.assertEqual(len(bans), 1)
        events = await self.database.recent_events(self.session["id"], 20)
        self.assertTrue(
            any(item["meta"].get("kind") == "safe_exit" for item in events)
        )

    async def test_return_requires_vote_then_story_progress(self) -> None:
        await self._activate_two()
        await self.database.retire_participant(
            self.session["id"],
            "BY",
            "admin",
            forced=True,
            reason="测试退场",
        )
        request = await self.database.request_return(
            self.session["id"],
            "user-1",
        )
        vote = await self.database.active_vote(self.session["id"])
        self.assertEqual(vote["id"], request["vote_id"])
        result = await self.database.cast_vote(
            self.session["id"],
            "user-2",
            "A",
        )
        self.assertTrue(result["resolved"])
        requests = await self.database.list_return_requests(
            self.session["id"]
        )
        self.assertEqual(requests[0]["status"], "quest_active")

        await self.database.designate_turn(
            self.session["id"],
            "user-2",
            "admin",
        )
        await self._commit_current(
            return_progress={
                "request_id": request["request_id"],
                "evidence": "队伍找到离场者留下的信物并完成会合条件。",
                "completed": True,
            }
        )
        requests = await self.database.list_return_requests(
            self.session["id"]
        )
        self.assertEqual(requests[0]["status"], "completed")
        participant = await self.database.get_participant(
            self.session["id"],
            participant_ref="BY",
        )
        self.assertEqual(participant["participation_status"], "active")

    async def test_schema_upgrade_creates_consistent_backup(self) -> None:
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE tavern_meta SET value = '2'
                WHERE key = 'schema_version'
                """
            )
        upgraded = TavernDatabase(self.data_dir)
        self.assertIsNotNone(upgraded.migration_backup_path)
        self.assertTrue(upgraded.migration_backup_path.exists())
        with closing(sqlite3.connect(upgraded.migration_backup_path)) as connection:
            version = connection.execute(
                """
                SELECT value FROM tavern_meta
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
        self.assertEqual(version, "2")

    async def test_v053_schema_contains_timer_and_token_tables(self) -> None:
        with self.database._connect() as connection:
            version = int(
                connection.execute(
                    """
                    SELECT value FROM tavern_meta
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
            )
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()
            }
        self.assertEqual(version, DATABASE_SCHEMA_VERSION)
        # A15：schema 已演进到 10，保留“至少 6”的历史语义（计时/配额表自 6 起）。
        self.assertGreaterEqual(version, 6)
        self.assertTrue(
            {
                "timer_policies",
                "token_usage",
                "token_quota_policies",
            }.issubset(tables)
        )

    async def test_existing_story_requires_continue_and_keeps_choice(
        self,
    ) -> None:
        await self._activate_two()
        await self._commit_current()
        before = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(before)
        await self.database.pause_session_timers(
            self.session["id"],
            "admin",
        )
        await self.database.transition_session(
            self.session["id"],
            SESSION_PAUSED,
            "admin",
        )
        await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        with self.assertRaises(InvalidTransitionError):
            await self.database.activate_story(
                self.session["id"],
                "admin",
                resume=False,
            )
        await self.database.force_all_ready(
            self.session["id"],
            "admin",
        )
        resumed = await self.database.activate_story(
            self.session["id"],
            "admin",
            resume=True,
        )
        self.assertEqual(resumed["opening"], "")
        self.assertEqual(resumed["choice_set"]["id"], before["id"])
        self.assertEqual(resumed["session"]["turn_no"], 1)

    async def test_mobile_story_is_split_bounded_and_actor_owned(self) -> None:
        first, second, _ = await self._activate_two()
        # 2026-08-24：compact band 下限从 100 抬到 150，凑到 76+76。
        narrative = "雨" * 76 + "\n" + "风" * 76
        choices = [
            {
                "key": key,
                "actor_id": second["id"],
                "text": text,
                "risk": "safe" if key == "A" else "controlled",
                "requires_check": False,
                "collective": False,
            }
            for key, text in zip(
                ("A", "B", "C", "D"),
                (
                    "观察门缝里的微光",
                    "检查自己携带的工具",
                    "询问眼前守门人的公开消息",
                    "退到墙边保持警戒",
                ),
                strict=True,
            )
        ]
        output = json.dumps(
            {
                "mode": "resolve",
                "narrative": narrative,
                "check": None,
                "state_patch": {"scene_summary": "风雨中的门廊"},
                "memories": [],
                "next_choices": choices,
                "director_note": "移动端短篇输出测试",
            },
            ensure_ascii=False,
        )
        context = FakeNarratorContext([output])
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                enforce_mobile_output=True,
            ),
            broker=EventBroker(),
        )
        reply = await engine.process_choice(
            event=SimpleNamespace(
                unified_msg_origin="qq:vnext-group",
                message_obj=SimpleNamespace(
                    message=SimpleNamespace(chain=[])
                ),
            ),
            session_id=self.session["id"],
            sender_id=first["group_user_id"],
            sender_name=first["character_name"],
            choice_key="A",
        )
        self.assertIn("-----------", reply.story_text)
        self.assertTrue(reply.turn_text.startswith("⚔️ 【回合秩序】"))
        self.assertNotIn("【回合秩序】", reply.story_text)
        active = await self.database.active_choice_set(self.session["id"])
        self.assertTrue(
            all(
                item["actor_id"] == second["id"]
                and len(item["text"]) <= 50
                for item in active["choices"]
            )
        )
        usage = await self.database.token_usage_summary(self.session["id"])
        self.assertGreater(usage["session"]["all"], 0)

    async def test_invalid_next_choices_repair_without_rewriting_story(
        self,
    ) -> None:
        first, second, _ = await self._activate_two()
        # 2026-08-24：compact band 下限从 100 抬到 150，句子 25 字 × 7 = 175。
        narrative = "雨声压过檐下的低语，白鸦确认了门后的脚步并未靠近。" * 7
        invalid_choices = [
            {"key": "A", "text": "观察门边", "risk": "safe"},
            {"key": "B", "text": "检查行囊"},
            {"key": "C", "text": "询问掌柜"},
        ]
        repaired_choices = [
            {
                "key": key,
                "actor_id": second["id"],
                "text": text,
                "risk": "safe" if key == "A" else "controlled",
                "requires_check": False,
                "collective": False,
            }
            for key, text in zip(
                ("A", "B", "C", "D"),
                ("观察门边", "检查行囊", "询问掌柜", "原地警戒"),
                strict=True,
            )
        ]
        context = FakeNarratorContext(
            [
                json.dumps(
                    {
                        "mode": "resolve",
                        "narrative": narrative,
                        "check": None,
                        "state_patch": {"scene_summary": "门后传来脚步"},
                        "memories": [],
                        "next_choices": invalid_choices,
                        "director_note": "剧情核心有效，选项缺失。",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"choices": repaired_choices},
                    ensure_ascii=False,
                ),
            ]
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                enforce_mobile_output=True,
                store_model_payloads=True,
            ),
            broker=EventBroker(),
        )
        reply = await engine.process_choice(
            event=SimpleNamespace(unified_msg_origin="qq:vnext-group"),
            session_id=self.session["id"],
            sender_id=first["group_user_id"],
            sender_name=first["character_name"],
            choice_key="A",
        )
        self.assertEqual(len(context.calls), 2)
        self.assertIn("只生成当前角色", context.calls[1]["prompt"])
        self.assertIn(narrative, reply.story_text)
        active = await self.database.active_choice_set(self.session["id"])
        self.assertEqual(
            [item["key"] for item in active["choices"]],
            ["A", "B", "C", "D"],
        )
        events = await self.database.recent_events(self.session["id"], 20)
        narrator = [item for item in events if item["role"] == "narrator"]
        self.assertEqual(len(narrator), 1)
        audits = await self.database.list_audit(
            self.session["id"],
            50,
            0,
        )
        self.assertEqual(
            sum(item["action"] == "turn.commit" for item in audits),
            1,
        )

    async def test_choice_repair_failure_uses_safe_fallback(self) -> None:
        first, second, _ = await self._activate_two()
        # 2026-08-24：compact band 下限从 100 抬到 150，句子 24 字 × 7 = 168。
        narrative = "炉火映亮墙上的水痕，新的动静从门廊深处逐渐靠近。" * 7
        context = FakeNarratorContext(
            [
                json.dumps(
                    {
                        "mode": "resolve",
                        "narrative": narrative,
                        "check": None,
                        "state_patch": {
                            "location": "极长地点" * 30,
                            "scene_summary": "极长现场摘要" * 30,
                        },
                        "memories": [],
                        "next_choices": [
                            {
                                "key": "A",
                                "text": "只有一个选项",
                                "risk": "safe",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                "仍然不是合法 JSON",
            ]
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                enforce_mobile_output=True,
            ),
            broker=EventBroker(),
        )
        reply = await engine.process_choice(
            event=SimpleNamespace(unified_msg_origin="qq:vnext-group"),
            session_id=self.session["id"],
            sender_id=first["group_user_id"],
            sender_name=first["character_name"],
            choice_key="A",
        )
        self.assertEqual(reply.session["turn_no"], 1)
        self.assertEqual(len(context.calls), 2)
        active = await self.database.active_choice_set(self.session["id"])
        self.assertEqual(
            [item["key"] for item in active["choices"]],
            ["A", "B", "C", "D"],
        )
        self.assertTrue(
            all(
                item["actor_id"] == second["id"]
                and len(item["text"]) <= 50
                for item in active["choices"]
            )
        )

    async def test_choice_repair_after_check_does_not_reroll_dice(self) -> None:
        first, second, _ = await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        checked_choices = fallback_choices({"location": "断桥"})
        checked_choices[0].update(
            {
                "risk": "dangerous",
                "danger_id": "dangerous",
                "requires_check": True,
                "check_type": "standard",
                "check_stat": "灵巧",
                "difficulty": 13,
                "known_consequences": "失败会滑倒并失去位置优势",
            }
        )
        await self.database.replace_active_choices(
            self.session["id"],
            choice["participant_id"],
            checked_choices,
            actor_id=first["group_user_id"],
        )
        planning = json.dumps(
            {
                "mode": "check",
                "narrative": "",
                "check": {
                    "stat": "灵巧",
                    "reason": "越过湿滑断桥",
                    "difficulty": 13,
                    "modifier": 0,
                "risk": "dangerous",
                "danger_id": "dangerous",
                },
            },
            ensure_ascii=False,
        )
        narrative = "白鸦踩住断裂木板边缘，根据已经锁定的检定结果稳住身形。" * 5
        checked = json.dumps(
            {
                "mode": "resolve",
                "narrative": narrative,
                "check": None,
                "state_patch": {"scene_summary": "断桥行动已完成裁定"},
                "memories": [],
                "next_choices": [
                    {"key": "A", "text": "仍然只有一项", "risk": "safe"}
                ],
            },
            ensure_ascii=False,
        )
        context = FakeNarratorContext([checked, "坏选项"])
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                enforce_mobile_output=False,
            ),
            broker=EventBroker(),
        )
        with patch(
            "tavern.engine.roll_check",
            side_effect=roll_check,
        ) as mocked_roll:
            reply = await engine.process_choice(
                event=SimpleNamespace(unified_msg_origin="qq:vnext-group"),
                session_id=self.session["id"],
                sender_id=first["group_user_id"],
                sender_name=first["character_name"],
                choice_key="A",
            )
        self.assertIsNotNone(reply.dice)
        self.assertEqual(mocked_roll.call_count, 1)
        self.assertEqual(len(context.calls), 2)
        active = await self.database.active_choice_set(self.session["id"])
        self.assertTrue(
            all(item["actor_id"] == second["id"] for item in active["choices"])
        )

    async def test_group_vote_ignores_invalid_personal_choices(self) -> None:
        first, _, _ = await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        collective = fallback_choices({"location": "门廊"})
        collective[0]["collective"] = True
        await self.database.replace_active_choices(
            self.session["id"],
            choice["participant_id"],
            collective,
            actor_id=first["group_user_id"],
        )
        narrative = "众人面前出现两条互斥路线，必须先共同决定下一步方向。" * 6
        context = FakeNarratorContext(
            [
                json.dumps(
                    {
                        "mode": "resolve",
                        "narrative": narrative,
                        "check": None,
                        "state_patch": {},
                        "memories": [],
                        "next_choices": [{"key": "A", "text": ""}],
                        "group_decision": {
                            "question": "队伍选择哪条路线？",
                            "options": [
                                {"key": "A", "text": "前往旧塔"},
                                {"key": "B", "text": "留在酒馆"},
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                enforce_mobile_output=True,
            ),
            broker=EventBroker(),
        )
        await engine.process_choice(
            event=SimpleNamespace(unified_msg_origin="qq:vnext-group"),
            session_id=self.session["id"],
            sender_id=first["group_user_id"],
            sender_name=first["character_name"],
            choice_key="A",
        )
        self.assertEqual(len(context.calls), 0)
        self.assertIsNotNone(
            await self.database.active_vote(self.session["id"])
        )

    async def test_resume_preserves_legacy_choices_without_regeneration(
        self,
    ) -> None:
        await self._activate_two()
        await self._commit_current()
        before = await self.database.active_choice_set(self.session["id"])
        legacy_choices = [dict(item) for item in before["choices"]]
        legacy_choices[0]["text"] = "旧版本允许保存的超长行动选项" * 6
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE choice_sets SET choices_json = ? WHERE id = ?",
                (
                    json.dumps(legacy_choices, ensure_ascii=False),
                    before["id"],
                ),
            )
        await self.database.pause_session_timers(
            self.session["id"],
            "admin",
        )
        await self.database.transition_session(
            self.session["id"],
            SESSION_PAUSED,
            "admin",
        )
        await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        await self.database.force_all_ready(
            self.session["id"],
            "admin",
        )
        with patch(
            "tavern.database.fallback_choices",
            side_effect=AssertionError("恢复旧选项时不得生成新选项"),
        ):
            resumed = await self.database.activate_story(
                self.session["id"],
                "admin",
                resume=True,
            )
        self.assertEqual(resumed["choice_set"]["id"], before["id"])
        self.assertEqual(
            resumed["choice_set"]["choices"][0]["text"],
            legacy_choices[0]["text"],
        )
        await self.database.resume_session_timers(
            self.session["id"],
            "admin",
        )
        timers = await self.database.list_timers(self.session["id"])
        self.assertFalse(
            any(
                item["timer_type"] == "preparation"
                and item["status"] in {"active", "paused"}
                for item in timers
            )
        )
        self.assertTrue(
            any(
                item["timer_type"] == "turn"
                and item["status"] == "active"
                for item in timers
            )
        )

    async def test_resume_vote_restores_matching_deadline(self) -> None:
        await self._activate_two()
        await self._commit_current(
            group_decision={
                "question": "是否进入旧塔？",
                "options": [
                    {"key": "A", "text": "进入旧塔"},
                    {"key": "B", "text": "留在门外"},
                ],
            }
        )
        self.assertIsNotNone(
            (await self.database.active_vote(self.session["id"]))[
                "deadline_at"
            ]
        )
        await self.database.pause_session_timers(
            self.session["id"],
            "admin",
        )
        paused_vote = await self.database.active_vote(self.session["id"])
        self.assertEqual(paused_vote["deadline_at"], "")
        await self.database.transition_session(
            self.session["id"],
            SESSION_PAUSED,
            "admin",
        )
        await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        await self.database.force_all_ready(
            self.session["id"],
            "admin",
        )
        resumed = await self.database.activate_story(
            self.session["id"],
            "admin",
            resume=True,
        )
        self.assertIsNotNone(resumed["vote"])
        await self.database.resume_session_timers(
            self.session["id"],
            "admin",
        )
        vote = await self.database.active_vote(self.session["id"])
        vote_timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "vote"
            and item["status"] == "active"
        )
        self.assertTrue(vote["deadline_at"])
        self.assertEqual(vote["deadline_at"], vote_timer["deadline_at"])

    async def test_timer_policy_freezes_and_resumes_category(self) -> None:
        await self.database.set_timer_policy(
            self.session["id"],
            "all",
            True,
            "admin",
        )
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "timer-user",
            "计时角色",
        )
        timers = await self.database.list_timers(self.session["id"])
        timer = next(
            item
            for item in timers
            if item["participant_id"] == reserved["id"]
            and item["timer_type"] == "card_completion"
        )
        self.assertEqual(timer["status"], "active")
        policy = await self.database.set_timer_policy(
            self.session["id"],
            "card_completion",
            False,
            "admin",
        )
        self.assertFalse(policy["effective"]["card_completion"])
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["id"] == timer["id"]
        )
        self.assertEqual(timer["status"], "paused")
        self.assertTrue(timer["action"]["paused_by_policy"])
        await self.database.set_timer_policy(
            self.session["id"],
            "card_completion",
            True,
            "admin",
        )
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["id"] == timer["id"]
        )
        self.assertEqual(timer["status"], "active")
        self.assertNotIn("paused_by_policy", timer["action"])

    async def test_force_ready_only_handles_approved_active_roles(self) -> None:
        first = await self._make_character("force-1", "甲", "A1")
        second = await self._make_character("force-2", "乙", "B2")
        await self.database.set_participant_ready(
            self.session["id"],
            first["group_user_id"],
            False,
        )
        await self.database.set_participant_ready(
            self.session["id"],
            second["group_user_id"],
            False,
        )
        await self.database.reserve_participant(
            self.session["id"],
            "force-pending",
            "待建卡",
        )
        result = await self.database.force_all_ready(
            self.session["id"],
            "admin",
        )
        self.assertEqual(result["ready_count"], 2)
        self.assertTrue(
            any(item["name"] == "待建卡" for item in result["skipped"])
        )
        roster = await self.database.list_roster(self.session["id"])
        ready = {
            item["group_user_id"]: item["ready"]
            for item in roster
        }
        self.assertTrue(ready["force-1"])
        self.assertTrue(ready["force-2"])
        self.assertFalse(ready["force-pending"])

    async def test_token_quota_reserves_before_model_call(self) -> None:
        await self.database.set_token_quota(
            self.session["id"],
            "session",
            window_seconds=3600,
            token_limit=100,
            enabled=True,
            actor_id="admin",
        )
        reservation = await self.database.reserve_token_usage(
            self.session["id"],
            "story_plan",
            "provider-a",
            60,
        )
        with self.assertRaisesRegex(ValueError, "Token 限额不足"):
            await self.database.reserve_token_usage(
                self.session["id"],
                "story_plan",
                "provider-b",
                50,
            )
        await self.database.settle_token_usage(
            reservation["id"],
            input_tokens=20,
            cached_input_tokens=5,
            output_tokens=15,
            usage_source="provider",
        )
        second = await self.database.reserve_token_usage(
            self.session["id"],
            "story_checked",
            "provider-b",
            50,
        )
        await self.database.fail_token_usage(second["id"])
        summary = await self.database.token_usage_summary(
            self.session["id"]
        )
        self.assertEqual(summary["session"]["all"], 35)
        self.assertEqual(summary["by_type"][0]["request_type"], "story_plan")

    async def test_group_token_quota_is_managed_by_group_identity(self) -> None:
        updated = await self.database.set_group_token_quota(
            self.session["platform_id"],
            self.session["group_id"],
            window_seconds=86_400,
            token_limit=432_100,
            enabled=True,
            actor_id="admin",
        )
        self.assertEqual(updated["platform_id"], self.session["platform_id"])
        self.assertEqual(updated["group_id"], self.session["group_id"])
        self.assertEqual(updated["quota"]["token_limit"], 432_100)
        self.assertTrue(updated["quota"]["enabled"])

        summary = await self.database.group_token_usage_summary(
            self.session["platform_id"],
            self.session["group_id"],
        )
        self.assertEqual(summary["session_id"], self.session["id"])
        self.assertEqual(summary["quota"]["window_seconds"], 86_400)

    async def test_v053_backup_preserves_timer_policy_and_token_ledger(
        self,
    ) -> None:
        await self.database.set_timer_policy(
            self.session["id"],
            "vote",
            False,
            "admin",
        )
        await self.database.set_token_quota(
            self.session["id"],
            "session",
            window_seconds=7200,
            token_limit=5000,
            enabled=True,
            actor_id="admin",
        )
        reservation = await self.database.reserve_token_usage(
            self.session["id"],
            "choice_reroll",
            "provider-test",
            100,
        )
        await self.database.settle_token_usage(
            reservation["id"],
            input_tokens=40,
            cached_input_tokens=10,
            output_tokens=20,
            usage_source="provider",
        )
        bundle = await self.database.export_bundle()
        restored_dir = tempfile.TemporaryDirectory()
        try:
            restored = TavernDatabase(Path(restored_dir.name))
            await restored.import_bundle(bundle, "replace", "web:tester")
            policy = await restored.get_timer_policy(self.session["id"])
            usage = await restored.token_usage_summary(self.session["id"])
            self.assertFalse(policy["switches"]["vote"])
            self.assertEqual(usage["session"]["all"], 60)
            quota = next(
                item
                for item in usage["quotas"]
                if item["scope_type"] == "session"
            )
            self.assertEqual(quota["window_seconds"], 7200)
            self.assertEqual(quota["token_limit"], 5000)
        finally:
            restored_dir.cleanup()

    async def test_finished_archive_is_readable_and_deletable(self) -> None:
        await self._activate_two()
        await self._commit_current()
        session_id = self.session["id"]
        storage = await self.database.get_storage_info(session_id)
        story_dir = self.data_dir / storage["relative_path"]
        self.assertTrue(story_dir.exists())
        archived = await self.database.finalize_session(
            session_id,
            "admin",
            termination_type="completed",
            reason="测试完结",
        )
        self.assertEqual(archived["state"], SESSION_FINISHED)
        events = await self.database.recent_events(session_id, 100)
        self.assertTrue(any(item["role"] == "narrator" for item in events))
        await self.database.list_memories(
            session_id,
            "",
            100,
            include_invalidated=True,
        )
        result = await self.database.delete_session(
            session_id,
            "admin",
            archived["instance_name"],
        )
        self.assertTrue(result["deleted"])
        self.assertFalse(story_dir.exists())
        self.assertTrue(Path(result["trash_path"]).exists())
        with self.assertRaises(DatabaseNotFoundError):
            await self.database.get_session(session_id)

    async def test_same_name_save_requires_replace_and_files_can_trash(
        self,
    ) -> None:
        await self._activate_two()
        first = await self.database.create_snapshot(
            self.session["id"],
            "门前",
            "admin",
        )
        with self.assertRaises(ValueError):
            await self.database.create_snapshot(
                self.session["id"],
                "门前",
                "admin",
            )
        replaced = await self.database.create_snapshot(
            self.session["id"],
            "门前",
            "admin",
            replace=True,
        )
        self.assertNotEqual(replaced["id"], first["id"])
        self.assertEqual(
            sum(
                item["name"] == "门前"
                for item in await self.database.list_snapshots(
                    self.session["id"]
                )
            ),
            1,
        )
        storage = await self.database.get_storage_info(self.session["id"])
        self.assertTrue(storage["save_files"])
        filename = storage["save_files"][0]["filename"]
        trashed = self.database.storage.trash_archive(
            self.session["id"],
            kind="save",
            filename=filename,
        )
        self.assertEqual(trashed["filename"], filename)
        self.assertTrue(Path(trashed["trash_path"]).exists())
        storage = await self.database.get_storage_info(self.session["id"])
        self.assertNotIn(
            filename,
            {item["filename"] for item in storage["save_files"]},
        )

    def test_mobile_validator_rejects_cross_character_control(self) -> None:
        payload = {
            "mode": "resolve",
            # 200 字：越过默认下限 150，让本用例只考选项归属校验（2026-09-20
            # 默认 min_length 由 100 提到 150）。
            "narrative": "灯" * 200,
            "check": None,
            "state_patch": {},
            "memories": [],
            "next_choices": [
                {
                    "key": key,
                    "actor_id": "participant-next",
                    "text": (
                        "让白鸦替自己打开铁门"
                        if key == "B"
                        else f"检查自己的线索{key}"
                    ),
                    "risk": "safe" if key == "A" else "controlled",
                    "requires_check": False,
                    "collective": False,
                }
                for key in ("A", "B", "C", "D")
            ],
        }
        resolution = validate_resolution(payload)
        with self.assertRaisesRegex(ValueError, "越权操控"):
            TavernEngine._validate_mobile_resolution(
                resolution,
                expected_actor={
                    "id": "participant-next",
                    "character_name": "梅林",
                },
                roster=[
                    {"id": "participant-old", "character_name": "白鸦"},
                    {"id": "participant-next", "character_name": "梅林"},
                ],
            )
