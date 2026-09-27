"""状态优劣势自动补进检定额外来源的回归测试。

背景：模型经常漏填 check.advantage_sources / disadvantage_sources，导致
角色状态（灼伤→近战劣势、暴露→反侦察劣势等）对骰点毫无影响。
引擎兜底：_validate_choices_for_actor 按状态的 effect 关键词 + affects
匹配，把状态名补进对应选项的优劣势来源，使检定真正掷出 2d20 取高/取低。
"""

from __future__ import annotations

import unittest

from tavern.engine import TavernEngine


def _four(text_a: str, check_a: dict) -> list[dict]:
    return [
        {"key": "A", "text": text_a, "danger_id": "dangerous", "check": check_a},
        {"key": "B", "text": "向学者询问方法", "danger_id": "safe"},
        {"key": "C", "text": "原地观察环境", "danger_id": "safe"},
        {"key": "D", "text": "向队友确认计划", "danger_id": "safe"},
    ]


class StatusSourcesTests(unittest.TestCase):
    def test_status_disadvantage_applied_on_affects_match(self) -> None:
        """affects 与选项文字共享 2 字子串 → 自动补劣势来源。"""
        actor = {
            "id": "p1",
            "runtime_state": {
                "statuses": [
                    {
                        "name": "轻度灼伤",
                        "affects": ["近战攻击"],
                        "effect": "相关近战动作有轻微劣势",
                    },
                ]
            },
        }
        choices = _four(
            "挥剑攻击巨像腿部",
            {"required": True, "attribute_id": "strength", "type": "standard"},
        )
        normalized = TavernEngine._validate_choices_for_actor(
            choices, expected_actor=actor, roster=[]
        )
        a = next(c for c in normalized if c["key"] == "A")
        self.assertIn("轻度灼伤（状态劣势）", a["disadvantage_sources"])
        # safe 选项不受影响
        b = next(c for c in normalized if c["key"] == "B")
        self.assertNotIn("轻度灼伤（状态劣势）", b.get("disadvantage_sources", []))

    def test_empty_affects_status_applies_to_all_checks(self) -> None:
        """affects 为空的重状态 → 对该角色所有检定生效。"""
        actor = {
            "id": "p1",
            "runtime_state": {
                "statuses": [
                    {
                        "name": "重度虚弱",
                        "affects": [],
                        "effect": "所有行动检定劣势",
                    },
                ]
            },
        }
        choices = _four(
            "潜行绕过守卫",
            {"required": True, "attribute_id": "agility", "type": "standard"},
        )
        normalized = TavernEngine._validate_choices_for_actor(
            choices, expected_actor=actor, roster=[]
        )
        a = next(c for c in normalized if c["key"] == "A")
        self.assertIn("重度虚弱（状态劣势）", a["disadvantage_sources"])

    def test_advantage_status_applied(self) -> None:
        """effect 含优势/增益 → 补进 advantage_sources。"""
        actor = {
            "id": "p1",
            "runtime_state": {
                "statuses": [
                    {
                        "name": "地形掩护",
                        "affects": ["潜行"],
                        "effect": "潜行相关检定优势",
                    },
                ]
            },
        }
        choices = _four(
            "借废墟掩护潜行接近",
            {"required": True, "attribute_id": "agility", "type": "standard"},
        )
        normalized = TavernEngine._validate_choices_for_actor(
            choices, expected_actor=actor, roster=[]
        )
        a = next(c for c in normalized if c["key"] == "A")
        self.assertIn("地形掩护（状态增益）", a["advantage_sources"])

    def test_no_check_option_untouched(self) -> None:
        """免检选项不追加优劣势来源。"""
        actor = {
            "id": "p1",
            "runtime_state": {
                "statuses": [
                    {"name": "受伤", "affects": [], "effect": "相关检定劣势"},
                ]
            },
        }
        choices = _four("询问学者", {"required": False})
        normalized = TavernEngine._validate_choices_for_actor(
            choices, expected_actor=actor, roster=[]
        )
        a = next(c for c in normalized if c["key"] == "A")
        self.assertEqual(a.get("disadvantage_sources", []), [])


if __name__ == "__main__":
    unittest.main()
