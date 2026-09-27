"""自由演绎的「接收程度」必须真的进到提示词里，并且单独发到群里。

2026-09-21 实测：turn 289 起连续十几次自由演绎都没有评定，玩家侧的感觉是
"接受程度判定没了"。根因是三点叠加：

1. `planning_prompt` 里那段接收程度注入写在 `else` 分支内，而自由演绎恒有
   `requires_check=True`（引擎强制摇点），必定走第一个分支——那段注入对自由
   演绎从来没生效过，是死代码；
2. 唯一的真实来源 `workflow["freeform_acceptance_guidance"]` 只在裁判可用时
   才赋值，裁判返回 None 就整块静默消失；
3. 评定标题由模型写进正文的要求，与同一段提示词靠后的「禁止把判定/结果分类
   标签写进正文」直接冲突，模型于是干脆不写。

本测试锁定修复后的契约：无论走哪条分支、无论裁判是否可用，自由演绎都必须带着
接收程度；评定本身由插件成文并作为独立消息投递。
"""

from __future__ import annotations

import json
import pathlib
import unittest

from tavern.engine import _normalize_acceptance
from tavern.prompts import checked_resolution_prompt, planning_prompt

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUIDANCE = "玩家自由演绎的接收程度：部分接受。接受挥剑，不接受斩下龙头。"


def world() -> dict:
    return json.loads(
        (ROOT / "templates" / "world-package-v5-full-example.json").read_text(
            encoding="utf-8"
        )
    )


def session() -> dict:
    return {
        "progress": {},
        "world_state": {},
        "turn_status": {},
        "turn_no": 1,
        "next_actor": {},
    }


class PlanningPromptAcceptanceTests(unittest.TestCase):
    def build(self, workflow: dict) -> str:
        return planning_prompt(
            world=world(),
            session=session(),
            player={"character_name": "测试", "runtime_state": {}},
            player_input="我一剑斩下龙头",
            events=[],
            memories=[],
            allow_checks=True,
            workflow=workflow,
        )

    def test_forced_roll_freeform_still_gets_the_acceptance_block(self) -> None:
        """核心回归：自由演绎恒 requires_check=True，也不能因此丢掉接收程度。"""
        prompt = self.build(
            {
                "freeform": True,
                "requires_check": True,
                "freeform_acceptance": "partial",
                "freeform_acceptance_guidance": GUIDANCE,
            }
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn(GUIDANCE, prompt)

    def test_freeform_without_forced_roll_also_gets_it(self) -> None:
        prompt = self.build(
            {
                "freeform": True,
                "requires_check": False,
                "freeform_acceptance_guidance": GUIDANCE,
            }
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn(GUIDANCE, prompt)

    def test_body_is_no_longer_asked_to_start_with_the_header(self) -> None:
        """评定改由插件单独公告，正文不该再被要求写这个判定标签。"""
        prompt = self.build(
            {
                "freeform": True,
                "requires_check": True,
                "freeform_acceptance_guidance": GUIDANCE,
            }
        )
        self.assertNotIn("正文必须以「【演绎结果评定】」开头", prompt)
        self.assertIn("已由插件单独公告", prompt)

    def test_locked_choice_turns_get_no_acceptance_block(self) -> None:
        """选项路径（非自由演绎）不该被塞进自由演绎的接收程度。"""
        prompt = self.build(
            {
                "freeform": False,
                "requires_check": True,
                "freeform_acceptance_guidance": GUIDANCE,
            }
        )
        self.assertNotIn("<freeform_acceptance>", prompt)


class CheckedResolutionAcceptanceTests(unittest.TestCase):
    def test_post_dice_prompt_carries_scope_constraint_only(self) -> None:
        prompt = checked_resolution_prompt(
            world=world(),
            session=session(),
            player={"character_name": "测试", "runtime_state": {}},
            player_input="我一剑斩下龙头",
            events=[],
            memories=[],
            check={
                "stat": "strength",
                "difficulty": 12,
                "risk": "controlled",
                "check_type": "standard",
                "known_consequences": "",
                "advantage_sources": [],
                "disadvantage_sources": [],
            },
            dice={"outcome": "success", "total": 15, "difficulty": 12},
            death_verdict=None,
            freeform=True,
            acceptance_guidance=GUIDANCE,
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn(GUIDANCE, prompt)
        # 越界部分仍不许兑现，但不再要求正文写评定标题
        self.assertIn("不得兑现", prompt)
        self.assertNotIn("正文必须", prompt)


class NormalizeAcceptanceTests(unittest.TestCase):
    def test_known_values_pass_through(self) -> None:
        for value in ("full", "partial", "reduced"):
            self.assertEqual(_normalize_acceptance(value), value)

    def test_unknown_or_missing_falls_back_to_full(self) -> None:
        self.assertEqual(_normalize_acceptance(None), "full")
        self.assertEqual(_normalize_acceptance(""), "full")
        self.assertEqual(_normalize_acceptance("whatever"), "full")

    def test_case_and_padding_are_tolerated(self) -> None:
        self.assertEqual(_normalize_acceptance("  Partial "), "partial")


if __name__ == "__main__":
    unittest.main()
