"""0.12.1 自由情景演绎（jg 自由行动 / /酒馆 选择 <自由演绎>）回归测试。

覆盖：
1. parse_choice_input：A-D 返回 (key, flavor)；其他输入返回 (None, "")。
2. 自由演绎回合提交：作废当前活跃选项集，并为下一位玩家生成新选项集，
   不会卡死唯一的 active 占位，也不会因缺少 selected_key 而抛错。
3. 普通选择回合不受影响（selected_key 校验仍生效）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_PREPARING, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.engine import TavernEngine
from tavern.events import EventBroker
from tavern.lifecycle import fallback_choices, parse_choice_input


def _next_round_choices() -> list[dict]:
    """下一组选项的模型应答。

    刻意**不用** ``fallback_choices``：那四条里「谨慎观察…」「保持警戒…」
    会被 ``_option_is_noop`` 判成零行动，进而触发 ``_maybe_force_decisive``
    再补一次模型调用——用例就会看到 4 次调用，而它想验的是自由演绎主流程。
    这里每条都带决定性动作词，不触发那条补救路径。
    """
    return [
        {"key": "A", "text": "直接拨通宅邸电话，向接线员报出地址", "risk": "safe"},
        {"key": "B", "text": "把拍到的照片发给联络人，请对方辨认", "risk": "safe"},
        {"key": "C", "text": "离开巷口，沿来路返回车站", "risk": "controlled"},
        {"key": "D", "text": "推开大门，进入宅邸当面对质", "risk": "dangerous"},
    ]


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


class FreeformInputTests(unittest.TestCase):
    def test_parse_choice_input_a_with_flavor(self) -> None:
        self.assertEqual(parse_choice_input("A"), ("A", ""))
        self.assertEqual(parse_choice_input("b 打开门"), ("B", "打开门"))

    def test_parse_choice_input_freeform_returns_none(self) -> None:
        self.assertEqual(parse_choice_input("我掏出手机报警"), (None, ""))
        self.assertEqual(parse_choice_input("打开门走进去"), (None, ""))
        self.assertEqual(parse_choice_input(""), (None, ""))


class FreeformTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq",
            "freeform-group",
            "qq:freeform-group",
            DEFAULT_WORLD_SLUG,
            "admin",
        )
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
        )
        await self.database.set_timer_policy(self.session["id"], "all", True, "admin")
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

    async def _activate_two(self) -> tuple[dict, dict, dict]:
        first = await self._make_character("user-1", "白鸦", "BY")
        second = await self._make_character("user-2", "梅林", "ML")
        result = await self.database.activate_story(self.session["id"], "admin")
        self.assertTrue(result["started"])
        self.session = result["session"]
        return first, second, result

    async def test_freeform_turn_supersedes_active_set_and_makes_next(self) -> None:
        await self._activate_two()
        session = await self.database.get_session(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(choice)
        participant = choice["participant"]
        next_user = await self.database.get_turn_status(self.session["id"])

        state = dict(session["world_state"])
        state["scene_summary"] = "自由演绎已经得到裁定。"
        workflow = {
            "freeform": True,
            "choice_set_id": choice["id"],
            "flavor_text": "我掏出手机报警",
            "controller_user_id": participant["group_user_id"],
            "control_mode": "owner",
            "control_source": "owner",
            "next_choices": fallback_choices(state),
        }
        result = await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=participant["player_id"],
            player_user_id=participant["group_user_id"],
            player_name=participant["character_name"],
            player_input="我掏出手机报警",
            narrative="白鸦拨通了报警电话，把情况告诉了对面的接线员。",
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
        # 旧选项集必须被作废，释放 active 占位。
        stale = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(
            stale, "自由演绎后应为下一位玩家生成新的活跃选项集"
        )
        self.assertNotEqual(stale["id"], choice["id"], "不应复用被作废的旧选项集")
        self.assertNotEqual(
            stale["participant"]["group_user_id"],
            participant["group_user_id"],
            "新选项集应属于下一位行动玩家",
        )
        self.assertEqual(
            [item["key"] for item in stale["choices"]],
            ["A", "B", "C", "D"],
        )
        self.assertEqual(next_user["current_user_id"], participant["group_user_id"])

    async def test_freeform_without_existing_choice_set_still_commits(self) -> None:
        await self._activate_two()
        session = await self.database.get_session(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]

        state = dict(session["world_state"])
        workflow = {
            "freeform": True,
            "flavor_text": "我直接报警",
            # 不带 choice_set_id：自由演绎不依赖既有选项集，
            # 提交时仍应作废任何残留 active 选项集并生成下一回合选项。
            "next_choices": fallback_choices(state),
        }
        result = await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=participant["player_id"],
            player_user_id=participant["group_user_id"],
            player_name=participant["character_name"],
            player_input="我直接报警",
            narrative="白鸦报警并说明情况。",
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
        nxt = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(nxt, "自由演绎后应生成下一回合选项")

    async def test_normal_choice_still_rejects_missing_selected_key(self) -> None:
        await self._activate_two()
        session = await self.database.get_session(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        state = dict(session["world_state"])
        workflow = {
            # 故意不带 selected_key（不是 freeform）
            "choice_set_id": choice["id"],
            "flavor_text": "",
            "next_choices": fallback_choices(state),
        }
        with self.assertRaises(Exception) as ctx:
            await self.database.commit_turn(
                session_id=session["id"],
                expected_revision=session["revision"],
                player_id=participant["player_id"],
                player_user_id=participant["group_user_id"],
                player_name=participant["character_name"],
                player_input="选择 A",
                narrative="测试。",
                world_state=state,
                memories=[],
                check_payload=None,
                model_payload={"mode": "resolve"},
                director_note="测试",
                auto_snapshot_interval=5,
                store_model_payload=False,
                workflow=workflow,
            )
        self.assertIn("缺少有效的选项提交信息", str(ctx.exception))

    async def test_engine_process_freeform_full_pipeline(self) -> None:
        """端到端：process_freeform 通过引擎完整提交自由演绎回合。"""
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "白鸦拨通电话，把情况告诉了接线员。",
                "check": None,
                "state_patch": {
                    "scene_summary": "白鸦的报警电话已经接通。",
                },
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "", "scene_summary": "报警电话已经接通。"}
                ),
                "director_note": "自由演绎裁定完成。",
            },
            ensure_ascii=False,
        )
        # 0.13.x：自由演绎先经模型裁判（should_roll=false → 硬性摇点，
        # 降级为引擎自动检定必摇），再走正常叙事流程；裁判调用是第一次
        # 模型请求。
        #
        # 叙事与「下一组行动选项」是**两次独立调用**：resolve 里的
        # next_choices 只是兼容残留，选项一律由 _generate_choices 单独
        # 生成（模型在叙事中途顺带编选项，质量与行动者归属都不可控）。
        # 所以这里要备三份应答，否则第三次调用落空、走兜底选项。
        context = FakeNarratorContext(
            [
                json.dumps(
                    {
                        "should_roll": False,
                        "reason": "拨打电话无风险",
                    },
                    ensure_ascii=False,
                ),
                resolution,
                json.dumps({"choices": _next_round_choices()}, ensure_ascii=False),
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
        reply = await engine.process_freeform(
            event=SimpleNamespace(unified_msg_origin="qq:freeform-group"),
            session_id=self.session["id"],
            sender_id=participant["group_user_id"],
            sender_name=participant["character_name"],
            content="我掏出手机报警",
        )
        self.assertEqual(reply.session["turn_no"], 1)
        self.assertEqual(
            reply.session["world_state"].get("scene_summary"),
            "白鸦的报警电话已经接通。",
        )
        # 旧选项集被作废，新选项集属于下一位玩家。
        next_choice = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(next_choice)
        self.assertNotEqual(next_choice["id"], choice["id"])
        self.assertNotEqual(
            next_choice["participant"]["group_user_id"],
            participant["group_user_id"],
        )
        # 三次调用：① 自由演绎裁判 ② 按权威检定结果叙事 ③ 生成下一组选项。
        self.assertEqual(len(context.calls), 3, "叙事与选项应当是两次独立调用")
        self.assertIn("should_roll", context.calls[0]["prompt"])
        self.assertIn("<player_input", context.calls[1]["prompt"])
        self.assertIn("我掏出手机报警", context.calls[1]["prompt"])
        # 自由演绎强制检定后走的是「按引擎给的权威检定结果叙事」这条线，
        # 不是 planning 那条「已锁定的免检选项」——后者会把一次必摇检定的
        # 结果交给模型自行发挥。这条断言是原来那句 assertNotIn 的正面写法：
        # 光断言"没有免检标记"，把整段提示换成空串也能过。
        self.assertIn("权威检定结果", context.calls[1]["prompt"])
        self.assertNotIn(
            "本条行动来自插件已锁定的免检选项", context.calls[1]["prompt"]
        )
        # 第三次是独立的选项生成，且面向下一位行动者。
        self.assertIn("行动意图", context.calls[2]["prompt"])


if __name__ == "__main__":
    unittest.main()
