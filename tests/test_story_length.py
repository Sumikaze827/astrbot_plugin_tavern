"""故事正文长度约束 + 结局收尾内容要求的回归测试。

背景：正文长度 100—300 字硬编码在 _validate_mobile_resolution 与两个叙事
prompt 里，结局收尾时模型只能写 300 字，写不下「关键人物后续 + 玩家后续 +
事件收尾」。结局点现在限制为 150—450，并只要求一次性主线收束。

2026-09-20：**上限全部取消**（story_length_bounds 返回 (150, 0)，0 = 无上限）。
玩家反馈模型写到 755 字被判「结构校验失败（必须为 150—500 字）」，两个
provider 接连失败导致整轮裁定作废。正文写长不罚，只保留下限。
"""

from __future__ import annotations

import unittest

from tavern.engine import TavernEngine
from tavern.prompts import (
    checked_resolution_prompt,
    planning_prompt,
    story_length_bounds,
)
from tavern.resolution import validate_resolution


def _resolve(narrative: str):
    return validate_resolution(
        {
            "mode": "resolve",
            "narrative": narrative,
            "state_patch": {},
            "memories": [],
        }
    )


class StoryLengthBoundsTests(unittest.TestCase):
    def test_no_upper_bound_anywhere(self) -> None:
        """2026-09-20：上限取消，任何 band / 结局点都只留下限 150。"""
        self.assertEqual(story_length_bounds(True, "compact"), (150, 0))
        self.assertEqual(story_length_bounds(False, "standard"), (150, 0))
        self.assertEqual(story_length_bounds(False, "compact"), (150, 0))
        self.assertEqual(story_length_bounds(False, "expanded"), (150, 0))
        self.assertEqual(story_length_bounds(False, "epilogue"), (150, 0))
        self.assertEqual(story_length_bounds(False, "weird"), (150, 0))
        self.assertEqual(story_length_bounds(False, ""), (150, 0))


class MobileLengthValidationTests(unittest.TestCase):
    def test_long_narrative_is_not_rejected(self) -> None:
        """755 字的正文（玩家实际遇到的数字）在默认参数下必须通过。"""
        resolution = _resolve("雨" * 755)
        validated = TavernEngine._validate_mobile_resolution(
            resolution,
            expected_actor=None,
            roster=[],
            min_length=150,
            max_length=0,
        )
        self.assertEqual(validated.mode, "resolve")

    def test_explicit_upper_bound_still_usable(self) -> None:
        """max_length > 0 时上限仍然生效——留给需要硬约束的调用方。"""
        resolution = _resolve("雨" * 500)
        with self.assertRaises(ValueError):
            TavernEngine._validate_mobile_resolution(
                resolution,
                expected_actor=None,
                roster=[],
                min_length=150,
                max_length=300,
            )

    def test_too_short_narrative_still_rejected(self) -> None:
        """下限保留：太短的正文锚不住 NPC 身份、场景与动作结果。"""
        with self.assertRaisesRegex(ValueError, "不少于 150 字"):
            TavernEngine._validate_mobile_resolution(
                _resolve("雨" * 149),
                expected_actor=None,
                roster=[],
            )

    def test_long_ending_narrative_passes(self) -> None:
        """收尾正文写长同样不再判失败，只查下限。"""
        validated = TavernEngine._validate_ending_resolution(
            validate_resolution(
                {
                    "mode": "resolve",
                    "narrative": "雨" * 900,
                    "state_patch": {},
                    "memories": [],
                    "group_decision": None,
                    "director_note": "story_ending_complete",
                }
            )
        )
        self.assertEqual(validated.director_note, "story_ending_complete")

    def test_ending_contract_rejects_continuation_ui(self) -> None:
        payload = {
            "mode": "resolve",
            "narrative": "雨" * 180,
            "state_patch": {},
            "memories": [],
            "group_decision": None,
            "director_note": "story_ending_complete",
        }
        validated = TavernEngine._validate_ending_resolution(
            validate_resolution(payload)
        )
        self.assertEqual(validated.director_note, "story_ending_complete")

        payload["director_note"] = "普通裁定"
        with self.assertRaisesRegex(ValueError, "story_ending_complete"):
            TavernEngine._validate_ending_resolution(
                validate_resolution(payload)
            )


class EndingPromptLengthTests(unittest.TestCase):
    def _session(self, **progress) -> dict:
        return {
            "world_state": {"location": "城郊老巷"},
            "progress": {
                "current_chapter_id": "ch_04_epilogue",
                "chapter": "终章",
                "narrative_length_band": "compact",
                **progress,
            },
        }

    def test_planning_prompt_ending_requires_compact_group_closure(self) -> None:
        prompt = planning_prompt(
            world={"name": "t", "slug": "t", "system_prompt": "s", "rules": {}},
            session=self._session(story_complete=True),
            player={"participant_id": "p1"},
            player_input="转身离开",
            events=[],
            memories=[],
            allow_checks=False,
        )
        self.assertIn("不少于 150 个中文可见字符；建议落在 250—700 个中文可见字符", prompt)
        self.assertIn("主线核心冲突的结果", prompt)
        self.assertIn("队伍整体离场或去向", prompt)
        self.assertIn("不得逐一安排每名玩家角色的私人结局", prompt)
        self.assertIn("<authoritative_ending_contract>", prompt)
        self.assertIn("director_note 必须精确填写story_ending_complete", prompt)
        self.assertIn("next_choices=[]", prompt)
        self.assertNotIn("是否续接下一卷』作为明确选项", prompt)

    def test_pending_ending_uses_same_contract_before_story_complete(self) -> None:
        session = self._session(
            _ending_phase=True,
            _ending_objective="把证据交给公众并让队伍离场",
        )
        prompt = checked_resolution_prompt(
            world={"name": "t", "slug": "t", "system_prompt": "s", "rules": {}},
            session=session,
            player={"participant_id": "p1"},
            player_input="全队上船离开",
            events=[],
            memories=[],
            check={"stat": "意志"},
            dice={"outcome": "success"},
        )
        self.assertIn("不少于 150 个中文可见字符；建议落在 250—700 个中文可见字符", prompt)
        self.assertIn("引擎已进入一次性收尾阶段", prompt)
        self.assertIn("把证据交给公众并让队伍离场", prompt)
        self.assertIn("收尾阶段不得生成任何 next_choices", prompt)

    def test_planning_prompt_normal_has_floor_without_cap(self) -> None:
        prompt = planning_prompt(
            world={"name": "t", "slug": "t", "system_prompt": "s", "rules": {}},
            session=self._session(),
            player={"participant_id": "p1"},
            player_input="观察周围",
            events=[],
            memories=[],
            allow_checks=False,
        )
        # 2026-08-24：compact 下限从 100 放宽到 150。
        # 2026-09-20：上限取消（不做结构拒绝），prompt 给下限 + 软篇幅建议
        # + 反流水账清单（玩家反馈「全是无意义的琐碎信息」）。
        self.assertIn("不少于 150 个中文可见字符", prompt)
        self.assertIn("建议落在 250—700 个中文可见字符", prompt)
        self.assertIn("一次行动只写一个拍子", prompt)
        self.assertIn("以下都属于注水，必须删掉", prompt)
        self.assertNotIn("故事关键人物的后续去向", prompt)


if __name__ == "__main__":
    unittest.main()
