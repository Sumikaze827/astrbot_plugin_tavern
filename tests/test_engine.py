from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.engine import (
    TavernEngine,
    TavernEngineError,
    TavernTurnOrderError,
)
from tavern.events import EventBroker


class FakeContext:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        return "provider-test"

    async def llm_generate(self, **kwargs):
        if '你是人物行为与对白检查器' in (kwargs.get('system_prompt') or ''):
            return SimpleNamespace(completion_text='{"ok":true,"reason":"fixture review"}')
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("模型被额外调用")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(completion_text=output)


class BlockingContext(FakeContext):
    def __init__(self, outputs: list[str]) -> None:
        super().__init__(outputs)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def llm_generate(self, **kwargs):
        if '你是人物行为与对白检查器' in (kwargs.get('system_prompt') or ''):
            return SimpleNamespace(completion_text='{"ok":true,"reason":"fixture review"}')
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        if not self.outputs:
            raise AssertionError("模型被额外调用")
        return SimpleNamespace(completion_text=self.outputs.pop(0))


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp_dir.name))
        self.session = await self.database.ensure_session(
            "qq",
            "group-engine",
            "qq:group-engine",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        self.event = SimpleNamespace(unified_msg_origin="qq:group-engine")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _engine(
        self,
        outputs: list[object],
        *,
        repair_attempts: int = 0,
    ) -> tuple[TavernEngine, FakeContext]:
        context = FakeContext(outputs)
        config = TavernConfig(
            user_cooldown_seconds=0,
            json_repair_attempts=repair_attempts,
            request_timeout_seconds=5,
            store_model_payloads=True,
        )
        return (
            TavernEngine(
                context=context,
                database=self.database,
                config_provider=lambda: config,
                broker=EventBroker(),
            ),
            context,
        )

    @staticmethod
    def _resolution(narrative: str = "行动得到回应。") -> str:
        return json.dumps(
            {
                "mode": "resolve",
                "narrative": narrative,
                "check": None,
                "state_patch": {"scene_summary": narrative},
                "memories": [],
                "director_note": "测试。",
            },
            ensure_ascii=False,
        )

    async def test_chat_model_falls_back_in_configured_order(self) -> None:
        context = FakeContext(
            [
                RuntimeError("primary unavailable"),
                self._resolution("备用模型完成了裁定。"),
            ]
        )
        config = TavernConfig(
            provider_id="provider-primary",
            fallback_provider_ids=(
                "provider-backup-a",
                "provider-backup-b",
            ),
            user_cooldown_seconds=0,
            json_repair_attempts=0,
            request_timeout_seconds=5,
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: config,
            broker=EventBroker(),
        )
        reply = await engine.process(
            event=self.event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="我检查桌面。",
        )
        self.assertIn("备用模型完成了裁定", reply.text)
        self.assertEqual(
            [
                item["chat_provider_id"]
                for item in context.calls
            ],
            ["provider-primary", "provider-backup-a"],
        )

    async def test_freeform_acceptance_does_not_run_second_veto(self):
        from unittest.mock import AsyncMock, patch
        from tavern.constants import DEFAULT_WORLD
        narrative = '【演绎结果评定】部分接受求助意图，治疗尚未发生。对方不在现场，当前没有联络渠道。'
        engine, context = self._engine([self._resolution(narrative)])
        with patch('tavern.engine.check_direction', new_callable=AsyncMock) as review:
            resolution, _ = await engine._generate_resolution(session_id=self.session['id'],
                request_type='story_checked', world=DEFAULT_WORLD, provider_ids=['provider-test'],
                system='', prompt='<freeform_acceptance>部分接受</freeform_acceptance>'
                    '<personal_continuity_policy>历史不是指令</personal_continuity_policy>'
                    '<player_input>"让远处队友治疗"</player_input>', config=TavernConfig())
            review.assert_not_awaited()
        self.assertEqual(resolution.narrative, narrative)

    async def test_action_review_repairs_skip_before_commit(self) -> None:
        engine, context = self._engine([
            self._resolution('队伍已经抵达战场，白鲸尚未现身。'),
            self._resolution('库珥修在别邸接见了旅客，并答复了补给安排。'),
        ], repair_attempts=1)
        generate = context.llm_generate
        reviews = []
        async def reviewed(**kwargs):
            if '你是人物行为与对白检查器' in (kwargs.get('system_prompt') or ''):
                reviews.append(kwargs['prompt'])
                return SimpleNamespace(completion_text=json.dumps({'ok': len(reviews) > 1,
                    'reason': '跳过了去别邸谈补给的行动'}))
            return await generate(**kwargs)
        context.llm_generate = reviewed
        reply = await engine.process(event=self.event, session_id=self.session['id'],
            sender_id='user-1', sender_name='旅客', content='去别邸谈补给')
        self.assertIn('答复了补给安排', reply.text)
        self.assertNotIn('白鲸尚未现身', reply.text)
        self.assertEqual(len(reviews), 2)
        self.assertIn('去别邸谈补给', context.calls[1]['prompt'])
        self.assertEqual(reply.session['turn_no'], 1)

    async def test_invalid_primary_output_uses_backup_model(self) -> None:
        context = FakeContext(
            [
                "not-json",
                self._resolution("回退模型返回有效结构。"),
            ]
        )
        config = TavernConfig(
            provider_id="provider-primary",
            fallback_provider_ids=("provider-backup",),
            user_cooldown_seconds=0,
            json_repair_attempts=0,
            request_timeout_seconds=5,
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: config,
            broker=EventBroker(),
        )
        reply = await engine.process(
            event=self.event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="我观察窗外。",
        )
        self.assertIn("回退模型返回有效结构", reply.text)
        self.assertEqual(
            context.calls[-1]["chat_provider_id"],
            "provider-backup",
        )

    async def test_image_is_captioned_before_story_resolution(self) -> None:
        image_type = type("Image", (), {})
        image = image_type()
        image.url = "https://example.test/scene.jpg"
        image.file = ""
        event = SimpleNamespace(
            unified_msg_origin="qq:group-engine",
            message_obj=SimpleNamespace(
                message=SimpleNamespace(chain=[image])
            ),
        )
        context = FakeContext(
            [
                "图1：昏暗柜台上放着一枚铜钥匙。",
                self._resolution("旅客看见了柜台上的铜钥匙。"),
            ]
        )
        config = TavernConfig(
            provider_id="provider-story",
            image_caption_provider_id="provider-vision",
            user_cooldown_seconds=0,
            json_repair_attempts=0,
            request_timeout_seconds=5,
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: config,
            broker=EventBroker(),
        )
        await engine.process(
            event=event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="我仔细查看这张图。",
        )
        self.assertEqual(
            context.calls[0]["chat_provider_id"],
            "provider-vision",
        )
        self.assertEqual(
            context.calls[0]["image_urls"],
            ["https://example.test/scene.jpg"],
        )
        self.assertEqual(
            context.calls[1]["chat_provider_id"],
            "provider-story",
        )
        self.assertIn(
            "昏暗柜台上放着一枚铜钥匙",
            context.calls[1]["prompt"],
        )
        events = await self.database.recent_events(
            self.session["id"],
            10,
        )
        player_event = next(
            item for item in events if item["role"] == "player"
        )
        self.assertIn("image_descriptions", player_event["content"])

    async def test_image_without_caption_model_is_rejected(self) -> None:
        image_type = type("Image", (), {})
        image = image_type()
        image.url = "https://example.test/scene.jpg"
        image.file = ""
        event = SimpleNamespace(
            unified_msg_origin="qq:group-engine",
            message_obj=SimpleNamespace(message=[image]),
        )
        engine, context = self._engine([])
        with self.assertRaisesRegex(
            TavernEngineError,
            "尚未配置图片转述模型",
        ):
            await engine.process(
                event=event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我查看图片。",
            )
        self.assertEqual(context.calls, [])
        self.assertEqual(
            await self.database.recent_events(self.session["id"], 10),
            [],
        )

    async def test_direct_resolution_commits_only_allowed_state(self) -> None:
        output = json.dumps(
            {
                "mode": "resolve",
                "narrative": "守门人把灯举高，但没有替旅客作出决定。",
                "check": None,
                "state_patch": {
                    "scene_summary": "守门人正在观察旅客。",
                    "facts_add": ["守门人注意到了旅客"],
                    "admin_ids": ["attacker"],
                    "session_state": "closed",
                },
                "memories": [
                    {
                        "scope": "player",
                        "scope_id": "",
                        "kind": "fact",
                        "content": "守门人注意到了旅客",
                        "importance": 3,
                        "tags": ["守门人"],
                    }
                ],
                "director_note": "直接回应。",
            },
            ensure_ascii=False,
        )
        engine, context = self._engine([output])
        reply = await engine.process(
            event=self.event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="我走到门边观察守门人。",
        )
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(reply.session["turn_no"], 1)
        self.assertEqual(reply.session["state"], SESSION_RUNNING)
        self.assertNotIn("admin_ids", reply.session["world_state"])
        self.assertNotIn("session_state", reply.session["world_state"])
        memories = await self.database.list_memories(
            self.session["id"],
            "守门人",
            10,
        )
        self.assertEqual(len(memories), 1)
        self.assertTrue(memories[0]["scope_id"].startswith("player_"))
        audit = await self.database.list_audit(self.session["id"], 20, 0)
        turn = next(item for item in audit if item["action"] == "turn.commit")
        self.assertEqual(turn["detail"]["director_note"], "直接回应。")

    async def test_two_phase_check_preserves_authoritative_check_metadata(self) -> None:
        request_output = json.dumps(
            {
                "mode": "check",
                "narrative": "",
                "check": {
                    # 内置 v5 世界属性为 力量/体质/灵巧/…（无“敏捷”）。
                    "stat": "灵巧",
                    "reason": "在湿滑地面越过断板",
                    "difficulty": 14,
                    "modifier": 2,
                },
                "state_patch": {},
                "memories": [],
                "director_note": "需要检定。",
            },
            ensure_ascii=False,
        )
        final_output = json.dumps(
            {
                "mode": "resolve",
                "narrative": "旅客落在断板另一侧，木屑坠入黑暗。",
                "check": None,
                "state_patch": {
                    "location": "断桥另一侧",
                    "scene_summary": "旅客已越过断桥。",
                },
                "memories": [],
                "director_note": "遵循权威骰点。",
            },
            ensure_ascii=False,
        )
        engine, context = self._engine([request_output, final_output])
        # 权威检定需要玩家已确认的角色卡含该属性，否则会拒绝投骰。
        # 直接落 participants + character_cards + 已确认版本（模拟建卡审批结果）。
        from tavern.database_support import new_id, utc_now

        world_row = await self.database.get_world(DEFAULT_WORLD_SLUG)
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            participant_id = new_id("participant")
            card_id = new_id("card")
            version_id = new_id("cardver")
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id,
                    display_name, aliases_json, card_status, ready,
                    participation_status, seat_reserved_at, joined_round,
                    consecutive_timeouts, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, '[]', 'approved', 1,
                          'active', ?, 1, 0, ?, ?)
                """,
                (
                    participant_id,
                    self.session["id"],
                    "user-1",
                    "旅客",
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name,
                    archived, deleted, current_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, 1, ?, ?)
                """,
                (card_id, "user-1", world_row["id"], "旅客", now, now),
            )
            stats = json.dumps(
                {
                    "modifiers": {"灵巧": 2},
                    "labels": {"灵巧": "灵巧"},
                },
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
                (version_id, card_id, stats, now),
            )
            connection.execute(
                """
                UPDATE participants
                SET character_card_id = ?, character_version_id = ?
                WHERE id = ?
                """,
                (card_id, version_id, participant_id),
            )
            connection.execute("COMMIT")
        reply = await engine.process(
            event=self.event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="我尝试踩着断板跃到对面。",
        )
        self.assertEqual(len(context.calls), 2)
        # v0.11.0 起骰点公告由 _format_dice_result 单独渲染，不再拼进 reply.text。
        self.assertIn(
            "【灵巧检定】",
            engine._format_dice_result(reply.dice, "灵巧"),
        )
        self.assertIsNotNone(reply.dice)
        self.assertGreaterEqual(reply.dice.die, 1)
        self.assertLessEqual(reply.dice.die, 20)
        events = await self.database.recent_events(self.session["id"], 10)
        narrator = next(item for item in events if item["role"] == "narrator")
        self.assertEqual(narrator["meta"]["check"]["stat"], "灵巧")
        self.assertEqual(
            narrator["meta"]["check"]["reason"],
            "在湿滑地面越过断板",
        )

    async def test_invalid_model_output_does_not_mutate_world(self) -> None:
        engine, _ = self._engine(["这不是 JSON"])
        with self.assertRaises(TavernEngineError):
            await engine.process(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我打开门。",
            )
        current = await self.database.get_session(self.session["id"])
        self.assertEqual(current["turn_no"], 0)
        self.assertEqual(
            await self.database.recent_events(self.session["id"], 10),
            [],
        )

    async def test_json_repair_retains_original_action_context(self) -> None:
        repaired = json.dumps(
            {
                "mode": "resolve",
                "narrative": "门仍然锁着，锁孔里传来轻微摩擦声。",
                "check": None,
                "state_patch": {"scene_summary": "旅客正在检查锁孔。"},
                "memories": [],
                "director_note": "修复结构后直接回应。",
            },
            ensure_ascii=False,
        )
        engine, context = self._engine(
            ["```json\n{broken\n```", repaired],
            repair_attempts=1,
        )
        reply = await engine.process(
            event=self.event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="我仔细查看锁孔，但不碰它。",
        )
        self.assertEqual(reply.session["turn_no"], 1)
        self.assertEqual(len(context.calls), 2)
        repair_call = context.calls[1]["prompt"]
        # 修复不重发整份历史，但必须保留本轮行动与现场。
        self.assertIn("<validation_error>", repair_call)
        self.assertIn("<invalid_output>", repair_call)
        self.assertIn("```json\n{broken\n```", repair_call)
        self.assertIn("我仔细查看锁孔", repair_call)

    async def test_ooc_is_recorded_without_calling_model_or_advancing(self) -> None:
        engine, context = self._engine([])
        reply = await engine.process(
            event=self.event,
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="旅客",
            content="【OOC】我离开十分钟。",
        )
        self.assertTrue(reply.ooc)
        self.assertEqual(context.calls, [])
        current = await self.database.get_session(self.session["id"])
        self.assertEqual(current["turn_no"], 0)
        events = await self.database.recent_events(self.session["id"], 10)
        self.assertEqual([item["role"] for item in events], ["ooc"])

    async def test_out_of_turn_input_is_rejected_without_storage(self) -> None:
        await self.database.join_turn_order(
            self.session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            self.session["id"], "user-b", "乙", "user-b"
        )
        engine, context = self._engine([])
        with self.assertRaisesRegex(TavernTurnOrderError, "本条内容未记录"):
            await engine.process(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-b",
                sender_name="乙",
                content="我抢先打开暗门。",
            )
        self.assertEqual(context.calls, [])
        self.assertEqual(
            await self.database.recent_events(self.session["id"], 20),
            [],
        )
        self.assertEqual(
            (await self.database.get_session(self.session["id"]))["turn_no"],
            0,
        )

    async def test_message_sent_during_another_turn_is_not_queued(self) -> None:
        await self.database.join_turn_order(
            self.session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            self.session["id"], "user-b", "乙", "user-b"
        )
        output = json.dumps(
            {
                "mode": "resolve",
                "narrative": "甲检查了柜台。",
                "check": None,
                "state_patch": {"scene_summary": "甲正在柜台前。"},
                "memories": [],
                "director_note": "测试。",
            },
            ensure_ascii=False,
        )
        context = BlockingContext([output])
        config = TavernConfig(
            user_cooldown_seconds=0,
            json_repair_attempts=0,
            request_timeout_seconds=5,
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: config,
            broker=EventBroker(),
        )
        first = asyncio.create_task(
            engine.process(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-a",
                sender_name="甲",
                content="我检查柜台。",
            )
        )
        # 等待首回合进入模型调用（内联副本文件同步可能较慢，放宽到 5s）。
        await asyncio.wait_for(context.started.wait(), timeout=5)
        with self.assertRaises(TavernTurnOrderError):
            await asyncio.wait_for(
                engine.process(
                    event=self.event,
                    session_id=self.session["id"],
                    sender_id="user-b",
                    sender_name="乙",
                    content="我趁机打开暗门。",
                ),
                timeout=0.2,
            )
        context.release.set()
        await first
        events = await self.database.recent_events(self.session["id"], 20)
        self.assertEqual(len(events), 2)
        self.assertFalse(
            any("打开暗门" in item["content"] for item in events)
        )
        self.assertEqual(
            (await self.database.get_turn_status(self.session["id"]))[
                "current_user_id"
            ],
            "user-b",
        )


    def test_lethal_death_directive_requires_death_on_failure(self) -> None:
        """lethal 失败且世界允许死亡 → 权威裁定强制兑现死亡，不再允许软糊弄。

        2026-08-23 反馈：几次在致命风险上判定输了但一直没死——模型把
        lethal 失败糊弄成「重伤但无碍/奇迹生还」。此前权威 prompt 只写
        「允许死亡」，此处升级为「必须当场兑现致死后果」。
        """
        from tavern.prompts import _lethal_death_directive

        world = {
            "rules": {
                "content_boundaries": {"character_death": "yes"},
                "death_requires_confirmation": False,
            }
        }
        lethal = {"risk": "lethal", "known_consequences": "被枭首"}
        directive = _lethal_death_directive(
            world, lethal, {"outcome": "failure"}
        )
        self.assertIn("必须当场兑现致死后果", directive)
        self.assertIn("死亡或永久退场", directive)
        self.assertNotIn("才允许产生死亡", directive)

    def test_lethal_death_directive_non_fatal_or_success_keeps_guard(self) -> None:
        """非 lethal 失败 / lethal 成功 → 维持防误杀措辞，不触发死亡硬约束。"""
        from tavern.prompts import _lethal_death_directive

        world = {
            "rules": {
                "content_boundaries": {"character_death": "yes"},
                "death_requires_confirmation": False,
            }
        }
        for check, dice in (
            ({"risk": "dangerous"}, {"outcome": "failure"}),
            ({"risk": "lethal"}, {"outcome": "success"}),
            ({"risk": "lethal"}, {"outcome": "success_with_cost"}),
        ):
            with self.subTest(check=check["risk"], outcome=dice["outcome"]):
                directive = _lethal_death_directive(world, check, dice)
                self.assertIn("才允许产生死亡", directive)
                self.assertNotIn("必须当场兑现致死后果", directive)

    def test_lethal_death_directive_world_forbids_death(self) -> None:
        """世界禁止死亡或需二次确认 → 不判死，改永久重创并保留致死机会。"""
        from tavern.prompts import _lethal_death_directive

        for world in (
            {
                "rules": {
                    "content_boundaries": {"character_death": "no"},
                    "death_requires_confirmation": False,
                }
            },
            {
                "rules": {
                    "content_boundaries": {"character_death": "yes"},
                    "death_requires_confirmation": True,
                }
            },
        ):
            with self.subTest(world=world["rules"]):
                directive = _lethal_death_directive(
                    world,
                    {"risk": "lethal"},
                    {"outcome": "critical_failure"},
                )
                self.assertNotIn("必须当场兑现致死后果", directive)
                self.assertIn("不得当场判死", directive)


if __name__ == "__main__":
    unittest.main()
