"""自由行动自动检定回归测试。

背景：玩家不选 A/B/C/D、自由发挥时（jg 自由行动），旧逻辑完全由模型
裁定是否摇点，模型（doubao）经常不摇，有风险的自由动作静默免检。
修复：自由行动走与 A/B/C/D 相同的引擎属性推断——推得出属性 →
标记 requires_check 交给同一套 locked-check 投骰（按世界可控档 DC）；
硬性摇点（2026-08-23）：推不出 → 用世界属性表兜底（优先感知，否则
第一个属性）强制摇点，自由行动不再静默免检，绝不落到「通用」。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tavern.engine import TavernEngine

ROOT = Path(__file__).resolve().parents[1]
WORLD_FILE = ROOT / "worlds" / "ashen-sanctum-raid.json"


class FreeformAutoCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = json.loads(WORLD_FILE.read_text("utf-8"))

    def _infer(self, world: dict, text: str) -> dict | None:
        return TavernEngine._freeform_auto_check(world, text)

    def test_freeform_action_infers_attribute(self) -> None:
        """自由行动命中属性推断 → 标记 requires_check + 正确属性。"""
        cases = {
            "我想潜行绕过守卫，去拿祭坛上的钥匙": "agility",
            "向玛拉道出三百年血盟的真相": "charisma",
            "破译祭坛底座的真名铭文": "intellect",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = self._infer(self.world, text)
                self.assertIsNotNone(result)
                self.assertTrue(result["requires_check"])
                choice = result["selected_choice"]
                self.assertEqual(choice["check_stat"], expected)
                self.assertEqual(choice["difficulty"], 9)  # 可控档

    def test_freeform_rp_text_forces_fallback_roll(self) -> None:
        """硬性摇点：纯演绎文本推不出属性 → 用世界属性表兜底强制摇点。

        「我收起武器，缓缓走到玛拉面前，等着她开口」不含任何精确动作词，
        推断回退到世界属性表——该世界含 perception（感知），兜底为感知，
        按可控档 DC，绝不静默免检。
        """
        result = self._infer(
            self.world,
            "我收起武器，缓缓走到玛拉面前，等着她开口",
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["requires_check"])
        choice = result["selected_choice"]
        self.assertEqual(choice["check_stat"], "perception")
        self.assertEqual(choice["difficulty"], 9)  # 可控档
        self.assertEqual(choice["risk"], "controlled")

    def test_freeform_fallback_without_perception_uses_first(self) -> None:
        """世界属性表没有感知时，兜底取第一个属性。"""
        world = {
            "rules": {
                "character_card": {
                    "stats": {
                        "attributes": [
                            {"key": "agility", "label": "敏捷"},
                            {"key": "wits", "label": "心智"},
                        ]
                    }
                },
                "resolution": {
                    "mode": "attribute",
                    "difficulty_policy": {"controlled": 9},
                },
            }
        }
        result = self._infer(world, "我整理一下衣领")
        self.assertIsNotNone(result)
        self.assertEqual(result["selected_choice"]["check_stat"], "agility")

    def test_freeform_uses_controlled_difficulty(self) -> None:
        """自由行动无危险度标注 → 按世界可控档 DC。"""
        choice = self._infer(self.world, "猛冲过去撞开那扇门")["selected_choice"]
        self.assertEqual(choice["difficulty"], 9)
        self.assertEqual(choice["risk"], "controlled")

    def test_freeform_choice_payload_complete(self) -> None:
        """selected_choice 结构完整，能被 locked-check 流程消费。"""
        result = self._infer(self.world, "潜行绕过守卫")
        choice = result["selected_choice"]
        self.assertIn("check_stat", choice)
        self.assertIn("text", choice)
        self.assertIn("difficulty", choice)
        self.assertIn("risk", choice)
        self.assertIn("check_type", choice)
        self.assertEqual(choice["check_type"], "standard")
        self.assertEqual(choice["advantage_sources"], [])
        self.assertEqual(choice["disadvantage_sources"], [])

    def test_freeform_melee_verb_infers_strength(self) -> None:
        """自由发挥的近战动词也推回力量（与 A/B/C/D 同一推断引擎）。

        2026-08-23：词表补近战动词（突入/直取/击溃/掀），自由行动
        同样受益，不再静默免检。
        """
        for text in (
            "冲上去击溃前锋前排",
            "直接突入前锋阵中，直取断剑骑士",
        ):
            with self.subTest(text=text):
                result = self._infer(self.world, text)
                self.assertIsNotNone(result)
                self.assertTrue(result["requires_check"])
                self.assertEqual(
                    result["selected_choice"]["check_stat"], "strength"
                )

    def test_freeform_null_controlled_dc_falls_back(self) -> None:
        """世界显式把 controlled 档设为 null → 回退 12，不崩溃。"""
        world = {
            "rules": {
                "character_card": {
                    "stats": {
                        "attributes": [
                            {
                                "key": "agility",
                                "label": "敏捷",
                                "description": "远程攻击、闪避、潜行。",
                            }
                        ]
                    }
                },
                "resolution": {
                    "mode": "attribute",
                    "difficulty_policy": {"controlled": None},
                },
            }
        }
        choice = self._infer(world, "潜行绕过守卫")["selected_choice"]
        self.assertEqual(choice["difficulty"], 12)


if __name__ == "__main__":
    unittest.main()
