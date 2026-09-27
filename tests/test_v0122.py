"""0.12.2 回合秩序行艾特标记（@提醒）回归测试。

覆盖：
1. at_display_name：只有数字 QQ 号才生成艾特标记，其余原样返回。
2. split_mention_parts / render_mentions：文本管线内的艾特标记拆解与回退。
3. format_choices：选项头部保持原「🎯 【名字的行动回合】」称呼，不夹带艾特。
4. 引擎端到端：数字 QQ 参与者的「⚔️ 【回合秩序】」行带艾特标记，
   非数字则不带；选项头部始终为纯文本称呼。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_PREPARING
from tavern.database import TavernDatabase
from tavern.engine import TavernEngine
from tavern.events import EventBroker
from tavern.lifecycle import fallback_choices, format_choices
from tavern.platform_delivery import (
    at_display_name,
    render_mentions,
    split_mention_parts,
)


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


class AtDisplayNameTests(unittest.TestCase):
    def test_numeric_qq_wraps_name_in_marker(self) -> None:
        self.assertEqual(
            at_display_name("白鸦", "123456789"),
            "<<AT:123456789:白鸦>>",
        )

    def test_non_numeric_user_id_returns_name_unchanged(self) -> None:
        self.assertEqual(at_display_name("白鸦", "user-1"), "白鸦")

    def test_empty_user_id_returns_name_unchanged(self) -> None:
        self.assertEqual(at_display_name("白鸦", ""), "白鸦")
        self.assertEqual(at_display_name("白鸦", None), "白鸦")

    def test_empty_name_falls_back_to_user_id(self) -> None:
        self.assertEqual(at_display_name("", "123456789"), "123456789")
        self.assertEqual(at_display_name("", "user-1"), "user-1")

    def test_name_with_angle_brackets_is_not_wrapped(self) -> None:
        # 名字含 < > 时不做标记，避免破坏标记格式。
        self.assertEqual(at_display_name("白<鸦>", "123456789"), "白<鸦>")


class MentionHelpersTests(unittest.TestCase):
    def test_split_mention_parts_returns_mixed_sequence(self) -> None:
        parts = split_mention_parts("⚔️ 【回合秩序】下一位：<<AT:123:白鸦>>")
        self.assertEqual(parts, ["⚔️ 【回合秩序】下一位：", ("123", "白鸦")])

    def test_split_mention_parts_without_marker_is_single_text(self) -> None:
        parts = split_mention_parts("⚔️ 【回合秩序】下一位：白鸦")
        self.assertEqual(parts, ["⚔️ 【回合秩序】下一位：白鸦"])

    def test_render_mentions_falls_back_to_at_name(self) -> None:
        rendered = render_mentions(
            "下一位：<<AT:123:白鸦>>", mention_capable=False
        )
        self.assertEqual(rendered, "下一位：@白鸦")
        self.assertNotIn("<<AT:", rendered)

    def test_render_mentions_keeps_marker_when_capable(self) -> None:
        rendered = render_mentions(
            "下一位：<<AT:123:白鸦>>", mention_capable=True
        )
        self.assertIn("<<AT:123:白鸦>>", rendered)


class FormatChoicesHeaderTests(unittest.TestCase):
    def test_header_is_plain_character_name(self) -> None:
        text = format_choices(
            "白鸦", [{"key": "A", "text": "检查", "risk": "safe"}]
        )
        self.assertIn("【白鸦的行动回合】", text)
        self.assertNotIn("<<AT:", text)


class EngineMentionThreadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq",
            "mention-group",
            "qq:mention-group",
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

    async def _make_character(self, user_id: str, name: str, code: str) -> None:
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
        await self.database.set_participant_ready(self.session["id"], user_id)

    async def _activate_two(self, user_ids: tuple[str, str]) -> None:
        await self._make_character(user_ids[0], "白鸦", "BY")
        await self._make_character(user_ids[1], "梅林", "ML")
        result = await self.database.activate_story(self.session["id"], "admin")
        self.assertTrue(result["started"])
        self.session = result["session"]

    async def _run_freeform(self, question: str) -> str:
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "白鸦完成了当前行动，剧情继续。",
                "check": None,
                "state_patch": {"scene_summary": "白鸦的行动已裁定。"},
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "", "scene_summary": "白鸦的行动已裁定。"}
                ),
                "director_note": "自由演绎裁定完成。",
            },
            ensure_ascii=False,
        )
        # 0.13.x：自由演绎先经模型裁判（should_roll=false → 免检）
        context = FakeNarratorContext(
            [
                json.dumps(
                    {"should_roll": False, "reason": "查看情况无风险"},
                    ensure_ascii=False,
                ),
                resolution,
            ]
        )
        engine = self._engine(context)
        reply = await engine.process_freeform(
            event=SimpleNamespace(unified_msg_origin="qq:mention-group"),
            session_id=self.session["id"],
            sender_id=participant["group_user_id"],
            sender_name=participant["character_name"],
            content=question,
        )
        return reply.turn_text

    async def test_numeric_actor_marks_turn_order_line(self) -> None:
        await self._activate_two(("100001", "100002"))
        turn_text = await self._run_freeform("我上前查看情况")
        next_turn = await self.database.get_turn_status(self.session["id"])
        # 艾特标记出现在回合秩序行，且指向下一位玩家。
        self.assertIn("⚔️ 【回合秩序】", turn_text)
        self.assertIn(
            f"<<AT:{next_turn['current_user_id']}:{next_turn['current_name']}>>",
            turn_text,
        )
        # 选项头部保持纯文本称呼，不夹带艾特。
        self.assertIn(
            f"【{next_turn['current_name']}的行动回合】", turn_text
        )

    async def test_party_participants_move_together_without_skipping_turns(self) -> None:
        """模型判定「这条行动由 A、B 一起完成」时，B 一起移动、但回合照常轮。

        真实诉求：全队一起赶路，不必每个人都打一遍「我们走」——但行动顺序
        不能因此被改动，轮到 B 时他照常行动。
        """
        await self._activate_two(("user-1", "user-2"))
        roster = await self.database.list_roster(self.session["id"])
        current = await self.database.get_turn_status(self.session["id"])
        actor = next(
            item for item in roster
            if str(item["group_user_id"]) == str(current["current_user_id"])
        )
        companion = next(
            item for item in roster
            if str(item["group_user_id"]) != str(actor["group_user_id"])
        )
        resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "两人一同穿过门廊，走进内院。",
                "check": None,
                "state_patch": {"scene_summary": "两人一同抵达内院。"},
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "", "scene_summary": "两人一同抵达内院。"}
                ),
                # 模型判定这条行动由两人一起完成，两人都要写位置。
                "participants": [actor["id"], companion["id"]],
                "location_ops": [
                    {"target_id": actor["id"], "location": "内院"},
                    {"target_id": companion["id"], "location": "内院"},
                ],
                "director_note": "同行行动裁定完成。",
            },
            ensure_ascii=False,
        )
        context = FakeNarratorContext(
            [
                json.dumps(
                    {"should_roll": False, "reason": "一同赶路无风险"},
                    ensure_ascii=False,
                ),
                resolution,
            ]
        )
        engine = self._engine(context)
        await engine.process_freeform(
            event=SimpleNamespace(unified_msg_origin="qq:mention-group"),
            session_id=self.session["id"],
            sender_id=actor["group_user_id"],
            sender_name=actor["character_name"],
            content="我们一起穿过门廊",
        )
        # 两人一起到了内院——B 不必再自己打一遍赶路。
        moved = {
            str(item["group_user_id"]): (
                item.get("runtime_state") or {}
            ).get("current_location")
            for item in await self.database.list_roster(self.session["id"])
        }
        self.assertEqual(moved[str(actor["group_user_id"])], "内院")
        self.assertEqual(moved[str(companion["group_user_id"])], "内院")
        # 行动顺序照常：指针前进到下一位，轮次不变。
        turn = await self.database.get_turn_status(self.session["id"])
        self.assertEqual(turn["current_user_id"], companion["group_user_id"])
        self.assertEqual(turn["round_no"], 1)

    async def test_non_numeric_actor_keeps_plain_turn_order(self) -> None:
        await self._activate_two(("user-1", "user-2"))
        turn_text = await self._run_freeform("我上前查看情况")
        self.assertIn("⚔️ 【回合秩序】", turn_text)
        self.assertNotIn("<<AT:", turn_text)
        self.assertIn("的行动回合", turn_text)


if __name__ == "__main__":
    unittest.main()
