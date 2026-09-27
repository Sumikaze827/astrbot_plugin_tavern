"""0.12.1 /酒馆 提问 <疑问>（主持人答疑）回归测试。

覆盖：
1. 命令解析：/酒馆 提问 xxx → action=ask_dm，argument=xxx。
2. 主持人答疑端到端：模型以主持人口吻回答，不推进剧情、不消耗回合、
   不改世界状态；问答记录为 OOC 事件；提示词使用答疑专用系统提示
   （不含 required_output_schema）。
3. 边界：提问为空 / 剧情未推进（非 running、paused）时抛出 TavernEngineError。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_PREPARING
from tavern.database import TavernDatabase
from tavern.engine import TavernEngine, TavernEngineError
from tavern.events import EventBroker
from tavern.security import parse_tavern_command


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


class AskDmCommandParseTests(unittest.TestCase):
    def test_parse_tavern_command_ask_dm(self) -> None:
        parsed = parse_tavern_command("/酒馆 提问 刚才的商人为什么知道我们的名字？")
        self.assertTrue(parsed.matched)
        self.assertEqual(parsed.action, "ask_dm")
        self.assertEqual(parsed.argument, "刚才的商人为什么知道我们的名字？")

    def test_parse_tavern_command_ask_dm_empty_argument(self) -> None:
        parsed = parse_tavern_command("/酒馆 提问")
        self.assertTrue(parsed.matched)
        self.assertEqual(parsed.action, "ask_dm")
        self.assertEqual(parsed.argument, "")


class AskDmTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq",
            "askdm-group",
            "qq:askdm-group",
            DEFAULT_WORLD_SLUG,
            "admin",
        )
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
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

    def _engine(self, context: FakeNarratorContext) -> TavernEngine:
        return TavernEngine(
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

    async def _make_character(self, user_id: str, name: str, code: str) -> dict:
        reserved = await self.database.reserve_participant(
            self.session["id"], user_id, name
        )
        origin = f"qq-private:{user_id}"
        await self.database.bind_card_code(
            reserved["binding_code"], f"private-{user_id}", origin
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
                    if isinstance(item, dict)
                    else str(item)
                    for item in options
                ]
                value = next(
                    (item for item in values if item not in used_select_values),
                    values[0],
                )
                used_select_values.add(value)
            else:
                value = stat_values.get(field["key"], fixed_values.get(field["key"], "无"))
            await self.database.fill_card_draft(origin, value)
        confirmed = await self.database.confirm_card_draft(origin)
        if not confirmed["auto_approved"]:
            confirmed = await self.database.review_character_card(
                self.session["id"], confirmed["id"], True, "admin", "测试审核"
            )
        return await self.database.set_participant_ready(self.session["id"], user_id)

    async def _activate_two(self) -> dict:
        await self._make_character("user-1", "白鸦", "BY")
        await self._make_character("user-2", "梅林", "ML")
        result = await self.database.activate_story(self.session["id"], "admin")
        self.assertTrue(result["started"])
        self.session = result["session"]
        return result

    async def test_ask_dm_full_pipeline(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        session_before = await self.database.get_session(self.session["id"])
        turn_no_before = session_before["turn_no"]
        world_state_before = dict(session_before["world_state"])

        context = FakeNarratorContext(
            [
                "那个商人自报过出身，说他常年在商路上跑货；"
                "你们之前打听目的地时，他正好听过这个名字。"
            ]
        )
        engine = self._engine(context)
        reply = await engine.ask_dm(
            event=SimpleNamespace(unified_msg_origin="qq:askdm-group"),
            session_id=self.session["id"],
            sender_id=participant["group_user_id"],
            sender_name=participant["character_name"],
            question="那个商人为什么知道我们的名字？",
        )
        self.assertIn("商路上跑货", reply)
        self.assertIn("主持人答疑", reply)

        # 不推进剧情、不消耗回合、不改世界状态。
        session_after = await self.database.get_session(self.session["id"])
        self.assertEqual(session_after["turn_no"], turn_no_before)
        self.assertEqual(dict(session_after["world_state"]), world_state_before)

        # 问答记录为 OOC，供后续回合上下文延续。
        events = await self.database.recent_events(self.session["id"], 50)
        ooc_contents = [
            item["content"]
            for item in events
            if item.get("role") == "ooc"
        ]
        self.assertTrue(
            any("提问：那个商人为什么知道我们的名字？" in c for c in ooc_contents)
        )
        self.assertTrue(any("解答：那个商人自报过出身" in c for c in ooc_contents))

        # 提示词使用答疑专用系统提示，不含情节裁定 schema。
        self.assertEqual(len(context.calls), 1)
        self.assertIn("你是本次跑团", context.calls[0]["system_prompt"])
        self.assertIn("幕后解释者", context.calls[0]["system_prompt"])
        self.assertNotIn("required_output_schema", context.calls[0]["system_prompt"])
        prompt = context.calls[0]["prompt"]
        self.assertIn("<player_question", prompt)
        self.assertIn("那个商人为什么知道我们的名字？", prompt)
        self.assertIn("recent_history", prompt)

    async def test_ask_dm_empty_question_raises(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        engine = self._engine(FakeNarratorContext([]))
        with self.assertRaises(TavernEngineError) as ctx:
            await engine.ask_dm(
                event=SimpleNamespace(unified_msg_origin="qq:askdm-group"),
                session_id=self.session["id"],
                sender_id=participant["group_user_id"],
                sender_name=participant["character_name"],
                question="   ",
            )
        self.assertIn("提问内容为空", str(ctx.exception))

    async def test_ask_dm_not_running_raises(self) -> None:
        # 保持 preparing 状态：剧情尚未推进，不允许提问。
        await self._make_character("user-1", "白鸦", "BY")
        engine = self._engine(FakeNarratorContext([]))
        with self.assertRaises(TavernEngineError) as ctx:
            await engine.ask_dm(
                event=SimpleNamespace(unified_msg_origin="qq:askdm-group"),
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="白鸦",
                question="现在可以提问吗？",
            )
        self.assertIn("剧情尚未推进", str(ctx.exception))

    async def test_ask_dm_model_failure_raises(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        context = FakeNarratorContext(["   "])
        engine = self._engine(context)
        with self.assertRaises(TavernEngineError) as ctx:
            await engine.ask_dm(
                event=SimpleNamespace(unified_msg_origin="qq:askdm-group"),
                session_id=self.session["id"],
                sender_id=participant["group_user_id"],
                sender_name=participant["character_name"],
                question="为什么他认识我们？",
            )
        self.assertIn("主持人未能回应提问", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
