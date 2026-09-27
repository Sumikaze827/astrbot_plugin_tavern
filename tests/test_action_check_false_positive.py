"""行动兑现检查的误杀与失效保护（2026-09-20）。

现场：这一轮被判废，两个叙事模型都没完成。

1. `huoshan/deepseek-v4-flash-260425` 的 provider 条目 `custom_extra_body` 是空
   的，thinking 没关，每轮把 6000 输出预算烧在 reasoning 上（实测同一句
   请求：关 thinking 5 token，不关 40 token），JSON 被截断 → 「模型未返回
   有效 JSON 对象」。这是配置问题，不在本测试范围。
2. `deepseek/deepseek-v4-flash` 的草稿被行动兑现检查拒了两次。玩家选的是
   「到街口等卡尔斯腾家信使，随车走巡防驿路把预警信送到边境村接应点」，
   正文照做，检查器却回「…让角色进入边境村、与村口披毡衣人完成交付，这与
   本轮移动目的地不符，属擅自推进到后续接应场景」——自己承认目的地就是
   边境村，又因为正文到了边境村而拒绝。

检查提示词缺少「抵达被授权的目的地并完成被授权的事」这条通过条件，
所以「把准备当完成/跳场」的拒绝项把它吃掉了。本测试锁住补上的条件，
以及检查器失联时不得拿上一份草稿的结论判死本轮。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from tavern.npc_direction import check_direction

ROOT = Path(__file__).resolve().parent.parent


class _Engine:
    """最小替身：只提供 check_direction 用到的 metered 调用。"""

    def __init__(self, result: str | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.prompts: list[str] = []

    async def _llm_generate_metered(self, **kwargs):
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if self.error is not None:
            raise self.error
        return type("R", (), {"completion_text": self.result})()


class _Config:
    max_tokens = 300
    request_timeout_seconds = 12


def _direction(**extra):
    direction = {
        "turn_contract": {
            "player_input": "到街口等卡尔斯腾家信使，随车走巡防驿路把预警信送到边境村接应点",
            "authoritative_check_result": {"outcome": "success_with_cost"},
        },
        "settled_movement": [
            {"target_id": "p1", "location": "利冯斯街道巡防驿路边境村接应点"}
        ],
        "travel_change": {"mode": "split", "members": ["p1"], "reason": "我独自送信"},
    }
    direction.update(extra)
    return direction


def _run(engine, direction):
    return asyncio.run(
        check_direction(
            engine,
            direction=direction,
            narrative="凯尔希随信使车走完整段驿路，在边境村接应点把预警信交了出去。",
            session_id="s",
            provider_id="model",
            config=_Config(),
        )
    )


class ActionContractPromptTests(unittest.TestCase):
    def test_authorized_arrival_is_a_pass_condition(self) -> None:
        engine = _Engine(result='{"ok":true,"reason":""}')
        _run(engine, _direction())
        prompt = engine.prompts[0]
        for phrase in (
            "前往某处并完成某事",
            "不算跳场",
            "只有抵达行动未授权的地点",
            "沿途经过的地名",
            "不得仅因正文到了目的地就判为「推进到后续场景」",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_still_rejects_unauthorized_advancement(self) -> None:
        """补条件不能把真正的越界也放行。"""
        engine = _Engine(result='{"ok":true,"reason":""}')
        _run(engine, _direction())
        prompt = engine.prompts[0]
        self.assertIn("行动遗漏或跳场应拒绝", prompt)
        self.assertIn("替其他分队转场才算越界", prompt)


class CheckOutageTests(unittest.TestCase):
    """检查器失联不构成对当前草稿的否决。

    引擎在每份新草稿开始前清掉 review_direction 里的旧判定，因此
    check_direction 拿到的 direction 不带 last_check_ok。这里就按那个契约测。
    """

    def test_rejection_still_raises(self) -> None:
        engine = _Engine(result='{"ok":false,"reason":"跳到了未授权地点"}')
        with self.assertRaises(ValueError):
            _run(engine, _direction())

    def test_outage_on_fresh_draft_does_not_void_the_turn(self) -> None:
        direction = _direction()
        _run(_Engine(error=TimeoutError("checker timeout")), direction)
        self.assertEqual(direction["review_status"], "unavailable")
        self.assertNotIn("last_check_ok", direction)

    def test_unparseable_verdict_on_fresh_draft_does_not_void_the_turn(self) -> None:
        direction = _direction()
        _run(_Engine(result="not json at all"), direction)
        self.assertEqual(direction["review_status"], "unavailable")
        self.assertNotIn("last_check_ok", direction)

    def test_verdict_without_ok_flag_on_fresh_draft_does_not_void_the_turn(self) -> None:
        direction = _direction()
        _run(_Engine(result='{"reason":"忘了给结论"}'), direction)
        self.assertEqual(direction["review_status"], "invalid")
        self.assertNotIn("last_check_ok", direction)

    def test_passing_verdict_is_recorded(self) -> None:
        direction = _direction()
        _run(_Engine(result='{"ok":true,"reason":""}'), direction)
        self.assertTrue(direction["last_check_ok"])
        self.assertEqual(direction["review_status"], "passed")

    def test_rejected_draft_is_not_accepted_by_a_repeat_call(self) -> None:
        """保留既有不变量：同一份被拒草稿重复检查时仍须拒绝。"""
        direction = _direction()
        engine = _Engine(result='{"ok":false,"reason":"无故反转"}')
        with self.assertRaises(ValueError):
            _run(engine, direction)
        self.assertIs(direction["last_check_ok"], False)
        with self.assertRaises(ValueError):
            _run(engine, direction)


class PerDraftVerdictResetTests(unittest.TestCase):
    """旧判定必须在下一份草稿开始前清掉。

    这是上面那条『失联不得判死』契约的来源：少了这次重置，
    「上一份被拒 + 这一次检查超时」就会组合成『暂不提交』。行为难以在
    不走数据库的情况下端到端复现，所以沿用 test_config_attributes.py 的
    做法——对 engine.py 做静态断言，防止它被无声改回去。
    """

    def test_engine_clears_verdict_before_each_check(self) -> None:
        source = (ROOT / "tavern" / "engine.py").read_text(encoding="utf-8")
        index = source.index("await check_direction(")
        window = source[max(0, index - 500) : index]
        self.assertIn("pop('last_check_ok'", window)



if __name__ == "__main__":
    unittest.main()
